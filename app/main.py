"""Airwave Archive web app — Phase 6c: the library, episode details, series, artwork, bulk editing, search,
and importing episode logs.

Run locally with:   uvicorn app.main:app --reload
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from urllib.parse import quote
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.db import (
    DB_FILENAME, FIRST_PARTS, MANUAL, WITH_PARTS, AudioFile, Episode, ScanRun, make_engine, utcnow,
)
from app.episodes import (
    LONG_FIELDS, MONTHS, TEXT_FIELDS, TIME_ZONES, apply_confirmed, apply_edits,
    format_air_date, read_date_text, read_form,
)
from app.filenames import date_text, parse_filename
from app.parts import nearby_files, renumber, set_parts
from app import csvimport
from app.search import search, words_of
from app.bulk import BULK_FIELDS, apply_bulk, read_bulk_form, rename_series
from app.artwork import HASH_PATTERN, MAX_PICTURE_BYTES, art_path, save_picture
from app.series import (
    all_series, episode_art, follow_rename, one_series, series_files, set_uploaded_picture,
)
from app.scanner import format_bitrate, format_duration, format_size, forget_files, sync_library

APP_DIR = Path(__file__).parent

# Where the recordings live. Inside Docker this is /library (see compose.yaml).
LIBRARY_DIR = Path(os.environ.get("AIRWAVE_LIBRARY", "/library"))
# Where the app keeps its database. Inside Docker this is /data.
DATA_DIR = Path(os.environ.get("AIRWAVE_DATA", "data"))

PAGE_SIZE = 200  # files shown per page
REVIEW_LIMIT = 100  # file-name guesses shown at a time

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


def _version() -> str:
    """A fingerprint of the files a browser keeps while you click around
    (stylesheet, scripts, and the page frame). It changes with every update
    that changes them."""
    digest = hashlib.sha1()
    for path in sorted([*(APP_DIR / "static").rglob("*"), APP_DIR / "templates" / "base.html"]):
        if path.is_file():
            digest.update(path.name.encode() + path.read_bytes())
    return digest.hexdigest()[:10]


VERSION = _version()
templates.env.globals["version"] = VERSION


def static_url(path: str) -> str:
    """The address of a stylesheet or script, with the version added, so a
    browser never uses a copy saved before an update."""
    return f"/static/{path}?v={VERSION}"


templates.env.globals["static_url"] = static_url
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


def series_url(name: str | None) -> str:
    """The page of a series (no name: episodes without a series)."""
    return "/series/episodes" + (f"?name={quote(name, safe='')}" if name else "")


templates.env.filters["series_url"] = series_url


def initials(name: str | None) -> str:
    """'The Campbell Playhouse' -> 'CP' (for a series without a picture)."""
    words = [w for w in (name or "?").split() if w.lower() not in ("the", "a", "an")]
    return "".join(w[0] for w in words[:2] if w[0].isalnum()).upper() or "?"


templates.env.filters["initials"] = initials
# A colour (hue on the colour wheel) that's always the same for a name.
templates.env.filters["hue"] = lambda name: sum(map(ord, name or "")) * 47 % 360
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
    if from_htmx and request.headers.get("X-Airwave-Version", VERSION) != VERSION:
        # The tab was opened before the app was updated: it still has the
        # old stylesheet and scripts. Ask it to load the whole page it was
        # going to afresh (or, after sending a form, the page it's on).
        if request.method == "GET":
            target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
            return Response(headers={"HX-Redirect": target, "Vary": "HX-Request"})
        return Response(headers={"HX-Refresh": "true", "Vary": "HX-Request"})
    context["layout"] = "_partial.html" if from_htmx else "base.html"
    response = templates.TemplateResponse(request, template, context, status_code=status_code)
    # Tell browsers the full and partial versions are different pages.
    response.headers["Vary"] = "HX-Request, HX-History-Restore-Request"
    return response


def get_file(file_id: int) -> AudioFile:
    with Session(get_engine()) as session:
        row = session.get(AudioFile, file_id, options=[WITH_PARTS])
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


def art_dir() -> Path:
    """Where the app keeps its copies of pictures: next to the database."""
    return Path(get_engine().url.database).parent / "artwork"


def upload_dir() -> Path:
    """Where pictures uploaded by the owner are kept. Unlike ``artwork``,
    a scan can't make these again, so they belong in backups."""
    return Path(get_engine().url.database).parent / "uploaded-artwork"


def run_scan() -> ScanRun:
    with _scan_lock:
        return sync_library(get_engine(), LIBRARY_DIR, art_dir())


def last_scan_or_scan() -> ScanRun:
    """The latest scan. On the very first visit there's none yet: scan
    straight away, so the first page you open isn't empty."""
    with Session(get_engine()) as session:
        last_scan = session.scalars(select(ScanRun).order_by(ScanRun.id.desc()).limit(1)).first()
    return last_scan or run_scan()


@app.get("/library", response_class=HTMLResponse)
def library_page(request: Request, page: int = 1):
    """Every file, in folder order."""
    engine = get_engine()
    last_scan = last_scan_or_scan()

    with Session(engine) as session:
        present = AudioFile.missing.is_(False)
        count, total_bytes, total_seconds, unreadable, missing = session.execute(select(
            func.count(AudioFile.id),
            func.sum(AudioFile.size_bytes).filter(present),
            func.sum(AudioFile.duration_seconds).filter(present),
            func.count(AudioFile.id).filter(present, AudioFile.duration_seconds.is_(None)),
            func.count(AudioFile.id).filter(AudioFile.missing.is_(True)),
        )).one()
        # Episodes made of several files take one row.
        rows = session.scalar(select(func.count(AudioFile.id)).where(FIRST_PARTS))
        pages = max(1, -(-rows // PAGE_SIZE))   # round up
        page = min(max(1, page), pages)
        files = session.scalars(
            select(AudioFile)
            .where(FIRST_PARTS)
            .options(WITH_PARTS)
            .order_by(AudioFile.folder, AudioFile.name)
            .offset((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE)
        ).all()

    return render(request, "library.html", {
        "files": files,
        "count": count,
        "rows": rows,
        "pager_url": "/library?page=",
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
    return go_to(request, "/library")


@app.get("/files/{file_id}", response_class=HTMLResponse)
def file_page(request: Request, file_id: int):
    f = get_file(file_id)
    with Session(get_engine()) as session:
        art = episode_art(session, f)
    return render(request, "file.html", {"f": f, "art": art})


def render_edit_form(request: Request, f: AudioFile, values: dict, errors: dict | None = None):
    return render(request, "edit.html", {
        "f": f, "v": values, "errors": errors or {},
        "sources": f.episode.sources if f.episode else {},
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
        old_series = row.episode.series
        apply_edits(row.episode, values)
        follow_rename(session, old_series, row.episode.series)
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
        episode_id = session.get(AudioFile, file_id).episode_id
        forget_files(session, [file_id])
        session.expire_all()
        if episode_id is not None and (episode := session.get(Episode, episode_id)):
            renumber(episode)   # the other parts, if it was one of several
        session.commit()
    return go_to(request, "/library")


# ------------------------------------------------------------------ series


@app.get("/", response_class=HTMLResponse)
def series_list(request: Request, view: str = "tiles"):
    """The front page: all series, as picture tiles or (view=list) as a
    table."""
    last_scan_or_scan()
    with Session(get_engine()) as session:
        found = all_series(session)
    return render(request, "series.html",
                  {"series": found, "view": "list" if view == "list" else "tiles"})


@app.get("/series")
def old_series_address(request: Request):
    """The series list used to be here; it's the front page now."""
    return RedirectResponse("/" + (f"?{request.url.query}" if request.url.query else ""), 303)


@app.get("/series/episodes", response_class=HTMLResponse)
def series_page(request: Request, name: str | None = None, page: int = 1):
    """One series' episodes in air-date order. Without a name: the
    episodes that have no series yet."""
    name = name or None
    with Session(get_engine()) as session:
        totals = one_series(session, name)
        if totals is None:
            raise HTTPException(status_code=404, detail="No episodes in that series")
        pages = max(1, -(-totals["episodes"] // PAGE_SIZE))   # round up
        page = min(max(1, page), pages)
        files, rows = series_files(session, name, page, PAGE_SIZE)
    return render(request, "series_page.html", {
        "name": name, "totals": totals, "files": files, "rows": rows,
        "page": page, "pages": pages, "first_index": (page - 1) * PAGE_SIZE + 1,
        "pager_url": series_url(name) + ("&" if name else "?") + "page=",
    })


# ----------------------------------------------------------- CSV import


def import_dir() -> Path:
    """Where uploaded spreadsheets wait for the preview (tidied after a day)."""
    return Path(get_engine().url.database).parent / "imports"


@app.get("/import", response_class=HTMLResponse)
def import_page(request: Request, saved: int | None = None, error: str | None = None):
    return render(request, "import.html", {"saved": saved, "error": error},
                  status_code=400 if error else 200)


@app.post("/import", response_class=HTMLResponse)
async def import_upload(request: Request):
    form = await request.form()
    upload = form.get("spreadsheet")
    data = await upload.read(csvimport.MAX_BYTES + 1) if hasattr(upload, "read") else b""
    if not data:
        return import_page(request, error="Please choose a CSV file first.")
    if len(data) > csvimport.MAX_BYTES:
        return import_page(request, error="That file is too big (the limit is 10 MB).")
    headers, rows = csvimport.read_table(data)
    if not headers or not rows:
        return import_page(request, error="That file doesn't look like a spreadsheet with a "
                                          "row of column names and at least one row under it.")
    token = csvimport.save_upload(data, import_dir())
    series = " ".join(str(form.get("series", "")).split())
    return go_to(request, with_query(f"/import/{token}", series=quote(series)))


def load_import(token: str) -> bytes:
    path = csvimport.upload_path(import_dir(), token)
    if path is None:
        raise HTTPException(status_code=404, detail="That import has expired. Please upload the file again.")
    return path.read_bytes()


@app.get("/import/{token}", response_class=HTMLResponse)
def import_preview(request: Request, token: str, series: str = "",
                   col: list[str] = Query(default=[])):
    """Show what the spreadsheet would change. Nothing is saved yet."""
    data = load_import(token)
    columns = [c if c in csvimport.FIELDS else "" for c in col] or None
    with Session(get_engine()) as session:
        headers, columns, rows = csvimport.prepare(session, data, columns, series or None)
        titles = {}   # how to name each matched episode: its title, else its file name
        for r in rows:
            if r.episode_id:
                ep = session.get(Episode, r.episode_id)
                titles[r.episode_id] = ep.title or (ep.parts[0].name if ep.files else "")
    _h, table = csvimport.read_table(data)
    examples = [next((r[i].strip() for r in table if i < len(r) and r[i].strip()), "")
                for i in range(len(headers))]
    return render(request, "import_preview.html", {
        "token": token, "series": series, "headers": headers, "columns": columns,
        "examples": examples, "fields": csvimport.FIELDS, "titles": titles,
        "to_change": [r for r in rows if r.episode_id and (r.changes or r.errors)],
        "same": [r for r in rows if r.episode_id and not r.changes and not r.errors],
        "unmatched": [r for r in rows if not r.episode_id],
        "row_count": len(rows), "max_rows": csvimport.MAX_ROWS,
    })


@app.post("/import/{token}", response_class=HTMLResponse)
async def import_save(request: Request, token: str):
    form = await request.form()
    data = load_import(token)
    columns = [c if c in csvimport.FIELDS else "" for c in form.getlist("col")]
    ticked = {int(n) for n in form.getlist("row") if str(n).isdigit()}
    series = str(form.get("series", "")) or None
    saved = 0
    with Session(get_engine()) as session:
        _headers, _columns, rows = csvimport.prepare(session, data, columns, series)
        for row in rows:
            if row.number in ticked and row.episode_id and not row.errors:
                episode = session.get(Episode, row.episode_id)
                old_series = episode.series
                if csvimport.apply_row(episode, row.values):
                    saved += 1
                follow_rename(session, old_series, episode.series)
        session.commit()
    csvimport.upload_path(import_dir(), token).unlink(missing_ok=True)
    return go_to(request, f"/import?saved={saved}")


# ------------------------------------------------------------------ search

SEARCH_PAGE_SIZE = 50


@app.get("/search", response_class=HTMLResponse)
def search_page(request: Request, q: str = "", page: int = 1):
    """Series and episodes matching the words typed in the search box."""
    q = q.strip()[:200]
    series, files, snippets, total = [], [], {}, 0
    page = max(1, page)
    if q:
        with Session(get_engine()) as session:
            words = words_of(q)
            series = [s for s in all_series(session)
                      if s["name"] and all(w in s["name"].lower() for w in words)][:12]
            found, total = search(session, q, SEARCH_PAGE_SIZE, (page - 1) * SEARCH_PAGE_SIZE)
            snippets = dict(found)
            # Each episode is shown by its first file (with all parts).
            rows = session.scalars(select(AudioFile).where(
                AudioFile.episode_id.in_(snippets), FIRST_PARTS).options(WITH_PARTS)).unique().all()
            by_episode = {f.episode_id: f for f in rows}
            files = [by_episode[e] for e in snippets if e in by_episode]
    pages = max(1, -(-total // SEARCH_PAGE_SIZE))
    return render(request, "search.html", {
        "q": q, "series": series, "files": files, "snippets": snippets, "total": total,
        "page": page, "pages": pages, "rows": total,
        "first_index": (page - 1) * SEARCH_PAGE_SIZE + 1,
        "pager_url": with_query("/search", q=quote(q)) + "&page=",
    })


# ------------------------------------------------------------ bulk editing


def safe_back(back: str | None) -> str:
    """Where to return to after a form: only addresses inside the app."""
    return back if back and back.startswith("/") and not back.startswith("//") else "/"


def with_query(path: str, **values) -> str:
    """'/library' + changed=3 -> '/library?changed=3' (or '&changed=3')."""
    extra = "&".join(f"{k}={v}" for k, v in values.items() if v)
    return path + (("&" if "?" in path else "?") + extra if extra else "")


def chosen_episodes(session: Session, ids: list[int], series: str) -> list[Episode]:
    """The episodes picked for bulk editing: ticked ones, or a whole series."""
    if series:
        query = select(Episode).where(Episode.series == series)
    else:
        query = select(Episode).where(Episode.id.in_(ids[:5000]))
    return list(session.scalars(query.order_by(Episode.air_year, Episode.air_month,
                                               Episode.air_day, Episode.title)))


def render_bulk(request: Request, episodes, ids, series, back, typed=None, errors=None):
    return render(request, "bulk_edit.html", {
        "episodes": episodes, "ids": [e.id for e in episodes] if not series else [],
        "series": series, "back": safe_back(back), "fields": BULK_FIELDS,
        "time_zones": TIME_ZONES, "typed": typed or {}, "errors": errors or {},
    }, status_code=400 if errors else 200)


@app.get("/episodes/edit", response_class=HTMLResponse)
def bulk_edit_page(request: Request, ids: list[int] = Query(default=[]),
                   whole_series: str = "", back: str = "/"):
    with Session(get_engine()) as session:
        episodes = chosen_episodes(session, ids, whole_series)
    return render_bulk(request, episodes, ids, whole_series, back)


@app.post("/episodes/edit", response_class=HTMLResponse)
async def bulk_edit_save(request: Request):
    form = await request.form()
    ids = [int(v) for v in form.getlist("ids") if str(v).isdigit()]
    series, back = form.get("whole_series", ""), safe_back(form.get("back"))
    changes, time, errors = read_bulk_form(form)
    with Session(get_engine()) as session:
        episodes = chosen_episodes(session, ids, series)
        if errors or not episodes:
            return render_bulk(request, episodes, ids, series, back, dict(form),
                               errors or {"none": "No episodes were ticked."})
        old_names = {e.series for e in episodes}
        changed = skipped = 0
        for episode in episodes:
            did, missed = apply_bulk(episode, changes, time)
            changed += did
            skipped += missed
        if changes.get("series"):
            for old in old_names:
                follow_rename(session, old, changes["series"])
            back = series_url(changes["series"])   # the series' new page
        elif "series" in changes:
            back = series_url(None)
        session.commit()
    return go_to(request, with_query(back, changed=str(changed), skipped=str(skipped)))


@app.get("/series/rename", response_class=HTMLResponse)
def rename_page(request: Request, name: str = "", error: str | None = None, typed: str = ""):
    with Session(get_engine()) as session:
        totals = series_or_404(session, name)
        others = [s["name"] for s in all_series(session) if s["name"] and s["name"] != name]
    return render(request, "rename.html", {"name": name, "totals": totals, "error": error,
                                           "typed": typed or name, "others": others},
                  status_code=400 if error else 200)


@app.post("/series/rename", response_class=HTMLResponse)
async def rename_save(request: Request, name: str = ""):
    new = " ".join(str((await request.form()).get("new_name", "")).split())
    if not new:
        return rename_page(request, name, "Please type the new name.", new)
    with Session(get_engine()) as session:
        series_or_404(session, name)
        if new != name:
            count = rename_series(session, name, new)
            follow_rename(session, name, new)
            session.commit()
        else:
            count = 0
    return go_to(request, with_query(series_url(new), renamed=str(count)))


def series_or_404(session: Session, name: str | None) -> dict:
    totals = one_series(session, name) if name else None
    if totals is None:
        raise HTTPException(status_code=404, detail="No episodes in that series")
    return totals


@app.get("/series/picture", response_class=HTMLResponse)
def series_picture_page(request: Request, name: str = "", error: str | None = None):
    """Upload a picture for a series, or remove one."""
    with Session(get_engine()) as session:
        totals = series_or_404(session, name)
    return render(request, "series_picture.html",
                  {"name": name, "totals": totals, "error": error},
                  status_code=400 if error else 200)


@app.post("/series/picture", response_class=HTMLResponse)
async def upload_series_picture(request: Request, name: str = ""):
    with Session(get_engine()) as session:
        series_or_404(session, name)
    upload = (await request.form()).get("picture")
    data = await upload.read(MAX_PICTURE_BYTES + 1) if hasattr(upload, "read") else b""
    if not data:
        error = "Please choose a picture first."
    elif len(data) > MAX_PICTURE_BYTES:
        error = f"That picture is too big (the limit is {MAX_PICTURE_BYTES // 1_000_000} MB)."
    elif (art_hash := save_picture(data, upload_dir())) is None:
        error = "That file isn't a picture the app can read. Try a JPEG, PNG or WebP picture."
    else:
        with Session(get_engine()) as session:
            set_uploaded_picture(session, name, art_hash)
            session.commit()
        return go_to(request, series_url(name))
    return series_picture_page(request, name, error)


@app.post("/series/picture/remove")
def remove_series_picture(request: Request, name: str = ""):
    with Session(get_engine()) as session:
        series_or_404(session, name)
        set_uploaded_picture(session, name, None)
        session.commit()
    return go_to(request, series_url(name))


# ------------------------------------------------------- parts (several files)


@app.get("/files/{file_id}/parts", response_class=HTMLResponse)
def parts_page(request: Request, file_id: int, error: str | None = None):
    """Choose which files are parts of the same broadcast as this one."""
    f = get_file(file_id)
    with Session(get_engine()) as session:
        candidates = nearby_files(session, session.get(AudioFile, file_id, options=[WITH_PARTS]))
        for row in candidates:   # load each one's episode while the database is open
            if row.episode is not None:
                row.episode.files
    return render(request, "parts.html", {"f": f, "candidates": candidates, "error": error})


@app.post("/files/{file_id}/parts")
async def save_parts(request: Request, file_id: int):
    f = get_file(file_id)
    ids = [int(value) for value in (await request.form()).getlist("part") if value.isdigit()]
    if not ids:
        return go_to(request, f"/files/{file_id}/parts?error=none")
    with Session(get_engine()) as session:
        episode = session.get(AudioFile, file_id).episode
        chosen = session.scalars(select(AudioFile).where(AudioFile.id.in_(ids))).all()
        if episode is None or not chosen:
            raise HTTPException(status_code=400, detail="Nothing to group")
        set_parts(session, episode, list(chosen))
        session.commit()
        first = episode.parts[0].id
    return go_to(request, f"/files/{first}")


# ------------------------------------------------------- file-name review

REVIEW_FIELDS = {"series": "Series", "episode_number": "Ep.", "date": "Aired", "title": "Title"}


def review_rows(session: Session) -> tuple[list[dict], int]:
    """Files whose names suggest something new. Returns (first rows, total)."""
    # (This reads every file name each time. Fine for tens of thousands of
    # files; if it ever gets slow, remember which files were reviewed.)
    rows: list[dict] = []
    total = 0
    query = (
        select(AudioFile.id, AudioFile.folder, AudioFile.name, Episode.series,
               Episode.episode_number, Episode.title, Episode.air_year, Episode.air_month,
               Episode.air_day, Episode.sources_json)
        .join(Episode, AudioFile.episode_id == Episode.id)
        .where(AudioFile.missing.is_(False), FIRST_PARTS)
        .order_by(AudioFile.folder, AudioFile.name)
    )
    for (file_id, folder, name, series, number, title,
         year, month, day, sources_json) in session.execute(query):
        guess = parse_filename(name)
        if guess is None:
            continue
        sources = json.loads(sources_json) if sources_json else {}
        current = {"series": series, "episode_number": number, "title": title,
                   "date": date_text(year, month, day)}
        proposed = {"series": guess.series, "episode_number": guess.episode_number,
                    "title": guess.title, "date": guess.date_text}
        fields = []
        for key in REVIEW_FIELDS:
            source = sources.get("air_date" if key == "date" else key)
            differs = bool(proposed[key]) and proposed[key] != current[key]
            fields.append({
                "key": key, "proposed": proposed[key] or "", "current": current[key] or "",
                "differs": differs,
                # Would replace something the owner typed or confirmed.
                "conflict": differs and bool(current[key]) and source == MANUAL,
            })
        # Only files offering something for a field the owner hasn't set
        # themselves. (Once a card is saved, it doesn't come back.)
        if not any(field["differs"] and not field["conflict"] for field in fields):
            continue
        total += 1
        if len(rows) < REVIEW_LIMIT:
            rows.append({"id": file_id, "folder": folder, "name": name, "fields": fields,
                         "checked": not any(field["conflict"] for field in fields)})
    return rows, total


def render_review(request: Request, rows, total, saved=None, typed=None, errors=None):
    typed = typed or {}
    for row in rows:   # show what was typed, if the form came back with a problem
        if row["id"] in typed:
            row["checked"] = typed[row["id"]]["accept"]
            for field in row["fields"]:
                field["proposed"] = typed[row["id"]][field["key"]]
    return render(request, "review.html", {
        "rows": rows, "total": total, "saved": saved, "errors": errors or {},
        "labels": REVIEW_FIELDS, "limit": REVIEW_LIMIT,
    })


@app.get("/review/filenames", response_class=HTMLResponse)
def review_page(request: Request, saved: int | None = None):
    with Session(get_engine()) as session:
        rows, total = review_rows(session)
    return render_review(request, rows, total, saved=saved)


@app.post("/review/filenames", response_class=HTMLResponse)
async def save_review(request: Request):
    form = await request.form()
    ids = [int(i) for i in str(form.get("ids", "")).split(",") if i.strip().isdigit()]
    typed: dict[int, dict] = {}
    errors: dict[int, str] = {}
    accepted: dict[int, dict] = {}
    for file_id in ids:
        entry = {key: str(form.get(f"{key}-{file_id}", "")).strip() for key in REVIEW_FIELDS}
        entry["accept"] = form.get(f"accept-{file_id}") is not None
        typed[file_id] = entry
        if not entry["accept"]:
            continue
        date, problem = read_date_text(entry["date"])
        if problem:
            errors[file_id] = problem
            continue
        accepted[file_id] = {"series": entry["series"], "episode_number": entry["episode_number"],
                             "title": entry["title"], "date": date}

    with Session(get_engine()) as session:
        if errors:
            # Save nothing; show the page again with what was typed.
            rows, total = review_rows(session)
            return render_review(request, rows, total, typed=typed, errors=errors)
        saved = 0
        for file_id, values in accepted.items():
            row = session.get(AudioFile, file_id)
            if row is None or row.episode is None:
                continue
            old_series = row.episode.series
            if apply_confirmed(row.episode, values):
                follow_rename(session, old_series, row.episode.series)
                saved += 1
        session.commit()
    return go_to(request, f"/review/filenames?saved={saved}")


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


@app.get("/art/{art_hash}.jpg")
def artwork(art_hash: str):
    """A picture, from the app's own copies. Pictures never change (a new
    picture gets a new name), so browsers may keep them for a long time."""
    if not HASH_PATTERN.match(art_hash):
        raise HTTPException(status_code=404, detail="No such picture")
    path = next((p for p in (art_path(art_dir(), art_hash), art_path(upload_dir(), art_hash))
                 if p.is_file()), None)
    if path is None:
        raise HTTPException(status_code=404, detail="No such picture")
    return FileResponse(path, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.get("/files/{file_id}/art")
def file_art(file_id: int):
    """The picture for a file's episode (for the player bar and a phone's
    lock screen), or "not found" if it has none."""
    f = get_file(file_id)
    with Session(get_engine()) as session:
        art = episode_art(session, f)
    if art is None:
        raise HTTPException(status_code=404, detail="No picture")
    return RedirectResponse(f"/art/{art}.jpg", status_code=307)


@app.get("/health")
def health():
    """A tiny page Docker uses to check that the app is running."""
    return {"status": "ok"}
