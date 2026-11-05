"""Airwave Archive web app — Phase 4a: episode details and editing.

Run locally with:   uvicorn app.main:app --reload
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.db import DB_FILENAME, AudioFile, Episode, ScanRun, make_engine, utcnow
from app.episodes import (
    LONG_FIELDS, MONTHS, TEXT_FIELDS, TIME_ZONES, apply_edits, format_air_date, read_form,
)
from app.scanner import format_bitrate, format_duration, format_size, forget_files, sync_library

APP_DIR = Path(__file__).parent

# Where the recordings live. Inside Docker this is /library (see compose.yaml).
LIBRARY_DIR = Path(os.environ.get("AIRWAVE_LIBRARY", "/library"))
# Where the app keeps its database. Inside Docker this is /data.
DATA_DIR = Path(os.environ.get("AIRWAVE_DATA", "data"))

PAGE_SIZE = 200  # files shown per page

# What kind of audio each file ending holds, so the browser knows how to play it.
AUDIO_TYPES = {
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".m4b": "audio/mp4",
    ".aac": "audio/aac", ".flac": "audio/flac", ".ogg": "audio/ogg",
    ".oga": "audio/ogg", ".opus": "audio/ogg", ".wav": "audio/wav",
    ".wma": "audio/x-ms-wma", ".aiff": "audio/aiff", ".aif": "audio/aiff",
}

app = FastAPI(title="Airwave Archive")
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")


def time_ago(when: datetime | None) -> str:
    """A UTC time -> 'just now', '5 minutes ago', '2 days ago'."""
    if when is None:
        return "never"
    seconds = int((utcnow() - when).total_seconds())
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds >= size:
            n = seconds // size
            return f"{n} {unit}{'' if n == 1 else 's'} ago"
    return "just now"


# Plain names for tag codes. MP3 tags use short codes ("TIT2" = title);
# FLAC, Ogg, M4A, and WMA use their own names.
TAG_NAMES = {
    "TIT2": "Title", "TPE1": "Artist", "TPE2": "Album artist", "TALB": "Album",
    "TDRC": "Date", "TYER": "Year", "TCON": "Genre", "TRCK": "Track",
    "TPOS": "Disc", "TCOM": "Composer", "TENC": "Encoded by", "TSSE": "Encoder",
    "TCOP": "Copyright", "TPUB": "Publisher", "TLEN": "Length", "TIT1": "Grouping",
    "TIT3": "Subtitle", "COMM": "Comment", "USLT": "Lyrics", "TXXX": "Custom",
    "WXXX": "Web link", "WOAF": "Web link", "WOAR": "Web link", "WCOM": "Web link",
    "\xa9nam": "Title", "\xa9ART": "Artist", "aART": "Album artist",
    "\xa9alb": "Album", "\xa9day": "Date", "\xa9gen": "Genre", "trkn": "Track",
    "\xa9cmt": "Comment", "\xa9too": "Encoder", "desc": "Description",
    "title": "Title", "artist": "Artist", "album": "Album", "date": "Date",
    "genre": "Genre", "tracknumber": "Track", "comment": "Comment",
    "description": "Description", "encoder": "Encoder", "albumartist": "Album artist",
    "Author": "Artist", "WM/AlbumTitle": "Album", "WM/Year": "Date",
    "WM/Genre": "Genre", "WM/TrackNumber": "Track",
}


def tag_label(key: str) -> tuple[str, str]:
    """'TIT2' -> ('Title', 'TIT2'); 'COMM:ID3v1 Comment:eng' -> ('Comment', 'ID3v1 Comment')."""
    code, _, extra = key.partition(":")
    name = TAG_NAMES.get(code) or TAG_NAMES.get(code.lower())
    if name is None:
        return key, ""
    extra = extra.rsplit(":", 1)[0] if code == "COMM" else extra   # drop the language
    return name, extra or (code if code != name else "")


templates = Jinja2Templates(directory=APP_DIR / "templates")
templates.env.filters["size"] = format_size
templates.env.filters["duration"] = format_duration
templates.env.filters["bitrate"] = format_bitrate
templates.env.filters["ago"] = time_ago
templates.env.filters["tag_label"] = tag_label
templates.env.filters["air_date"] = format_air_date


def display_title(f: AudioFile) -> str:
    """The best title we have: the episode's, then the file's tag, then its name."""
    return (f.episode and f.episode.title) or f.tag_title or f.name


templates.env.filters["display_title"] = display_title
templates.env.filters["fromjson"] = lambda text: json.loads(text) if text else []

_engine: Engine | None = None
_scan_lock = threading.Lock()  # only one scan at a time


def get_engine() -> Engine:
    """Open the database the first time it's needed."""
    global _engine
    if _engine is None:
        _engine = make_engine(DATA_DIR / DB_FILENAME)
    return _engine


def render(request: Request, template: str, context: dict, status_code: int = 200):
    """Send a page. When htmx asks (a click inside the app), send only the
    title and the middle of the page, so the player bar isn't replaced."""
    from_htmx = ("HX-Request" in request.headers
                 or "HX-History-Restore-Request" in request.headers)
    context["layout"] = "_partial.html" if from_htmx else "base.html"
    response = templates.TemplateResponse(request, template, context, status_code=status_code)
    # Tell browsers the full and partial versions are different pages.
    response.headers["Vary"] = "HX-Request, HX-History-Restore-Request"
    return response


def get_file(file_id: int) -> AudioFile:
    with Session(get_engine()) as session:
        row = session.get(AudioFile, file_id)
    if row is None:
        raise HTTPException(status_code=404, detail="No such file in the library")
    return row


def go_to(request: Request, path: str) -> Response:
    """Send the browser to another page. For htmx, load just the middle of
    that page so the player bar keeps playing."""
    if "HX-Request" in request.headers:
        return Response(headers={"HX-Location": f'{{"path": "{path}", "target": "#content"}}'})
    # "303" means "go look at this page now".
    return RedirectResponse(path, status_code=303)


def run_scan() -> ScanRun:
    with _scan_lock:
        return sync_library(get_engine(), LIBRARY_DIR)


@app.get("/", response_class=HTMLResponse)
def library_page(request: Request, page: int = 1):
    engine = get_engine()
    with Session(engine) as session:
        last_scan = session.scalars(select(ScanRun).order_by(ScanRun.id.desc()).limit(1)).first()
    if last_scan is None:
        last_scan = run_scan()  # very first visit: scan straight away

    with Session(engine) as session:
        present = AudioFile.missing.is_(False)
        count, total_bytes, total_seconds, unreadable, missing = session.execute(select(
            func.count(AudioFile.id),
            func.sum(AudioFile.size_bytes).filter(present),
            func.sum(AudioFile.duration_seconds).filter(present),
            func.count(AudioFile.id).filter(present, AudioFile.duration_seconds.is_(None)),
            func.count(AudioFile.id).filter(AudioFile.missing.is_(True)),
        )).one()
        pages = max(1, -(-count // PAGE_SIZE))   # round up
        page = min(max(1, page), pages)
        files = session.scalars(
            select(AudioFile)
            .order_by(AudioFile.folder, AudioFile.name)
            .offset((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE)
        ).all()

    return render(request, "library.html", {
        "files": files,
        "count": count,
        "missing": missing,
        "total_bytes": total_bytes or 0,
        "total_seconds": total_seconds or 0,
        "unreadable": unreadable,
        "page": page,
        "pages": pages,
        "first_index": (page - 1) * PAGE_SIZE + 1,
        "scan": last_scan,
        "library_dir": LIBRARY_DIR,
        "library_exists": LIBRARY_DIR.is_dir(),
    })


@app.post("/rescan")
def rescan(request: Request):
    run_scan()
    return go_to(request, "/")


@app.get("/files/{file_id}", response_class=HTMLResponse)
def file_page(request: Request, file_id: int):
    return render(request, "file.html", {"f": get_file(file_id)})


def render_edit_form(request: Request, f: AudioFile, values: dict, errors: dict | None = None):
    return render(request, "edit.html", {
        "f": f, "v": values, "errors": errors or {},
        "text_fields": TEXT_FIELDS, "long_fields": LONG_FIELDS,
        "months": MONTHS, "time_zones": TIME_ZONES,
    })


@app.get("/files/{file_id}/edit", response_class=HTMLResponse)
def edit_page(request: Request, file_id: int):
    f = get_file(file_id)
    ep = f.episode
    values = {name: getattr(ep, name) if ep else None
              for name in [*TEXT_FIELDS, "air_year", "air_month", "air_day",
                           "air_time", "air_timezone"]}
    return render_edit_form(request, f, values)


@app.post("/files/{file_id}/edit", response_class=HTMLResponse)
async def save_edit(request: Request, file_id: int):
    f = get_file(file_id)
    values, errors = read_form(await request.form())
    if errors:
        # Show the form again with what was typed, and what needs fixing.
        form = await request.form()
        typed = {name: form.get(name, "") for name in values}
        return render_edit_form(request, f, typed, errors)
    with Session(get_engine()) as session:
        row = session.get(AudioFile, file_id)
        if row.episode is None:
            now = utcnow()
            row.episode = Episode(created_at=now, updated_at=now)
        apply_edits(row.episode, values)
        session.commit()
    return go_to(request, f"/files/{file_id}")


@app.post("/files/{file_id}/forget")
def forget(request: Request, file_id: int):
    """Remove a *missing* file and its details from the database. Files that
    are still in the library can't be forgotten (they'd come back next scan)."""
    f = get_file(file_id)
    if not f.missing:
        raise HTTPException(status_code=400, detail="Only missing files can be forgotten")
    with Session(get_engine()) as session:
        forget_files(session, [file_id])
        session.commit()
    return go_to(request, "/")


@app.get("/audio/{file_id}")
def audio(file_id: int):
    """Stream one recording. The browser can ask for any part of the file
    (a "range"), which is how jumping ahead works without downloading it all.
    Only files the scanner recorded can be played, so nothing outside the
    library is ever sent."""
    row = get_file(file_id)
    path = LIBRARY_DIR / row.folder / row.name
    if row.missing or not path.is_file():
        raise HTTPException(status_code=404, detail="File is missing; try a rescan")
    media_type = AUDIO_TYPES.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(path, media_type=media_type)


@app.get("/health")
def health():
    """A tiny page Docker uses to check that the app is running."""
    return {"status": "ok"}
