"""Airwave Archive web app — Phase 7a: the library, episode details, series, artwork, bulk editing, search,
importing episode logs, and the upload inbox.

Run locally with:   uvicorn app.main:app --reload
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from urllib.parse import quote, urlencode
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.db import (
    DB_FILENAME, InboxItem, FIRST_PARTS, MANUAL, WITH_PARTS, AudioFile, Episode, Feed, ScanRun,
    make_engine, utcnow,
)
from app.episodes import (
    LONG_FIELDS, _clean, MONTHS, TEXT_FIELDS, TIME_ZONES, apply_confirmed, apply_edits,
    day_heading, format_air_date, format_air_span, format_air_time, format_time, read_date_text,
    read_form,
)
from app.filenames import date_text, parse_filename
from app.parts import nearby_files, renumber, set_parts
from app import airdates, csvimport, deleting, export, feeds, inbox, listening, zipimport
from app.search import search, words_of
from app.bulk import BULK_FIELDS, apply_bulk, read_bulk_form, rename_series
from app.artwork import HASH_PATTERN, MAX_PICTURE_BYTES, art_path, save_picture
from app.series import (
    all_series, episode_art, follow_rename, most_played, one_series, recently_added,
    series_files, set_uploaded_picture,
)
from app.scanner import format_bitrate, format_duration, format_size, forget_files, sync_library

APP_DIR = Path(__file__).parent

# Where the recordings live. Inside Docker this is /library (see compose.yaml).
LIBRARY_DIR = Path(os.environ.get("AIRWAVE_LIBRARY", "/library"))
# Where the app keeps its database. Inside Docker this is /data.
DATA_DIR = Path(os.environ.get("AIRWAVE_DATA", "data"))
# Where uploads wait before being added to the library. Inside Docker: /inbox.
INBOX_DIR = Path(os.environ.get("AIRWAVE_INBOX", "inbox"))

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
templates.env.filters["progress"] = listening.progress
templates.env.filters["resume_point"] = listening.resume_point
templates.env.filters["listened_at"] = listening.listened_at
templates.env.filters["time_left"] = listening.time_left
templates.env.filters["air_time"] = format_air_time
templates.env.filters["air_span"] = format_air_span
templates.env.filters["clock"] = format_time
templates.env.globals["day_heading"] = day_heading


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
def home(request: Request):
    """The home page: Continue listening, then rows of series (recently
    added, most played). A row with nothing in it isn't shown."""
    last_scan_or_scan()
    with Session(get_engine()) as session:
        unfinished = [(f, done, episode_art(session, f))
                      for f, done in listening.continue_listening(session)]
        recent = recently_added(session)
        played = most_played(session)
        anything = recent or session.scalar(select(Episode.id).limit(1)) is not None
        session.expunge_all()
    return render(request, "home.html", {"unfinished": unfinished, "recent": recent,
                                         "played": played, "anything": anything})


@app.get("/series", response_class=HTMLResponse)
def series_list(request: Request, view: str = "tiles"):
    """All series, as picture tiles or (view=list) as a table."""
    last_scan_or_scan()
    with Session(get_engine()) as session:
        found = all_series(session)
        session.expunge_all()
    return render(request, "series.html",
                  {"series": found, "view": "list" if view == "list" else "tiles"})


@app.get("/calendar", response_class=HTMLResponse)
def calendar_month(request: Request, year: int | None = None, month: int | None = None):
    """A month of broadcasts: a calendar grid, and the episodes day by day.
    Without a month: the most recent month anything aired in."""
    with Session(get_engine()) as session:
        if year is None or month is None or not (1 <= month <= 12 and 1 <= year <= 9999):
            year, month = airdates.latest_month(session) or (date.today().year, date.today().month)
        view = airdates.month_view(session, year, month)
        earlier = airdates.nearest_month(session, year, month, later=False)
        later = airdates.nearest_month(session, year, month, later=True)
        years = airdates.years_with_episodes(session)
        session.expunge_all()
    return render(request, "calendar_month.html", {
        "view": view, "earlier": earlier, "later": later, "years": years,
        "before": airdates.next_month(year, month, -1), "after": airdates.next_month(year, month, 1),
        "day_names": airdates.DAY_NAMES, "month_names": MONTHS,
    })


def typed_day(session: Session, text: str) -> tuple[date, str | None]:
    """The day typed in a calendar's "Go to date" box (any common way; a
    year and month means the 1st), or the most recent broadcast if none
    was typed. Returns (day, what's wrong with what was typed)."""
    error = None
    found, problem = csvimport.read_date(text) if text.strip() else (None, None)
    if problem or (found and found[1] is None):
        error = problem or "Type a date with at least a month, like 1938-10-30."
        found = None
    try:
        day = date(found[0], found[1], found[2] or 1) if found else None
    except ValueError:
        day, error = None, "That day doesn't exist."
    return day or airdates.latest_day(session) or date.today(), error


@app.get("/calendar/week", response_class=HTMLResponse)
def calendar_week(request: Request, date_text: str = Query("", alias="date")):
    """A week of broadcasts, Sunday to Saturday, holding the given date
    (typed any common way). Without one: the week of the most recent
    broadcast. A date with only a year and month goes to that month's
    first week."""
    with Session(get_engine()) as session:
        day, error = typed_day(session, date_text)
        week = airdates.week_view(session, day)
        earlier = airdates.nearest_week(session, week.start, later=False)
        later = airdates.nearest_week(session, week.start, later=True)
        session.expunge_all()
    middle = week.start + timedelta(days=3)
    return render(request, "calendar_week.html", {
        "week": week, "earlier": earlier, "later": later, "error": error, "typed": date_text,
        "before": week.start - timedelta(days=7), "after": week.start + timedelta(days=7),
        "month": (middle.year, middle.month), "month_names": MONTHS,
    }, status_code=400 if error else 200)


@app.get("/calendar/day", response_class=HTMLResponse)
def calendar_day(request: Request, date_text: str = Query("", alias="date")):
    """One day's broadcasts, hour by hour. Without a date: the most recent
    day with a broadcast."""
    with Session(get_engine()) as session:
        day, error = typed_day(session, date_text)
        view = airdates.day_view(session, day)
        earlier = airdates.nearest_day(session, day, later=False)
        later = airdates.nearest_day(session, day, later=True)
        session.expunge_all()
    return render(request, "calendar_day.html", {
        "view": view, "earlier": earlier, "later": later, "error": error, "typed": date_text,
        "before": day - timedelta(days=1), "after": day + timedelta(days=1),
        "hour_label": airdates.hour_label,
    }, status_code=400 if error else 200)


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


# ------------------------------------------------------------------ inbox

INBOX_FIELDS = {"series": "Series", "episode_number": "Ep.", "date": "Aired", "title": "Title"}


def render_inbox(request: Request, typed=None, errors=None, problems=(), **notices):
    with Session(get_engine()) as session:
        items = inbox.sync_inbox(session, INBOX_DIR)
        rows = []
        for item in items:
            values = {"series": item.series or "", "episode_number": item.episode_number or "",
                      "title": item.title or "",
                      "date": date_text(item.air_year, item.air_month, item.air_day)}
            values.update((typed or {}).get(item.id, {}))
            have = inbox.already_have(session, item)
            rows.append({"item": item, "vals": values, "extra": item.extra,
                         "have": have and {"id": have.id, "title": display_title(have)}})
        session.expunge_all()
    return render(request, "inbox.html", {
        "rows": rows, "fields": INBOX_FIELDS, "errors": errors or {},
        "duplicates": sum(1 for row in rows if row["have"]),
        "zips": zipimport.inbox_zips(INBOX_DIR), "problems": list(problems),
        "zone_names": TIME_ZONES, "inbox_dir": INBOX_DIR, **notices,
    }, status_code=400 if errors or problems else 200)


@app.get("/inbox", response_class=HTMLResponse)
def inbox_page(request: Request, uploaded: int | None = None, skipped: int | None = None,
               saved: int | None = None, rejected: int | None = None,
               added: int | None = None, added_all: int | None = None,
               skipped_have: int | None = None, unpacked: int | None = None,
               with_details: int | None = None, folder: list[str] = Query(default=[])):
    added_file = None
    if added is not None:
        with Session(get_engine()) as session:
            added_file = session.get(AudioFile, added)
            session.expunge_all()
    return render_inbox(request, uploaded=uploaded, skipped=skipped, saved=saved,
                        rejected=rejected, added=added_file, added_all=added_all,
                        skipped_have=skipped_have, unpacked=unpacked,
                        with_details=with_details, folders=folder)


@app.post("/inbox/upload", response_class=HTMLResponse)
async def inbox_upload(request: Request):
    """Save uploaded audio files into the inbox (never into the library).
    Zips are unpacked into the inbox straight away."""
    form = await request.form()
    uploaded = skipped = unpacked = with_details = 0
    problems = []
    for upload in form.getlist("files"):
        filename = getattr(upload, "filename", "") or ""
        if zip_name := zipimport.safe_zip_name(filename):
            saved = INBOX_DIR / inbox.save_upload(upload.file, INBOX_DIR, "." + zip_name)
            try:
                result = unpack_zip(saved)
            except zipimport.ZipProblem as problem:
                problems.append(str(problem).replace(saved.name, zip_name))
                continue
            finally:
                saved.unlink(missing_ok=True)
            uploaded += result.files
            with_details += result.with_details
            unpacked += 1
            continue
        name = inbox.safe_name(filename)
        if name is None:
            skipped += 1
            continue
        inbox.save_upload(upload.file, INBOX_DIR, name)
        uploaded += 1
    if problems:
        return render_inbox(request, problems=problems, uploaded=uploaded, skipped=skipped,
                            unpacked=unpacked, with_details=with_details)
    return go_to(request, with_query("/inbox", uploaded=str(uploaded),
                                     skipped=str(skipped) if skipped else "",
                                     unpacked=str(unpacked) if unpacked else "",
                                     with_details=str(with_details) if unpacked else ""))


def unpack_zip(path: Path) -> zipimport.Unpacked:
    with Session(get_engine()) as session:
        return zipimport.unpack(session, path, INBOX_DIR, upload_dir())


@app.post("/inbox/unpack", response_class=HTMLResponse)
async def inbox_unpack(request: Request):
    """Unpack a zip that was copied into the inbox folder."""
    name = str((await request.form()).get("name", ""))
    path = next((p for p in zipimport.inbox_zips(INBOX_DIR) if p.name == name), None)
    if path is None:
        raise HTTPException(status_code=404, detail="That zip isn't in the inbox any more")
    try:
        result = unpack_zip(path)
    except zipimport.ZipProblem as problem:
        return render_inbox(request, problems=[str(problem)])
    return go_to(request, with_query("/inbox", uploaded=str(result.files), unpacked="1",
                                     with_details=str(result.with_details)))


def save_inbox_details(form) -> tuple[dict, dict, int]:
    """Save the details typed for the waiting files. Returns (typed,
    errors, how many changed); if there are errors nothing is saved."""
    typed, errors, values = {}, {}, {}
    with Session(get_engine()) as session:
        items = session.scalars(select(InboxItem)).all()
        for item in items:
            if f"title-{item.id}" not in form:
                continue
            row = {key: str(form.get(f"{key}-{item.id}", "")) for key in INBOX_FIELDS}
            typed[item.id] = row
            date, error = read_date_text(row["date"])
            if error:
                errors[item.id] = error
            values[item.id] = (row, date)
        if errors:
            return typed, errors, 0
        saved = 0
        for item in items:
            if item.id not in values:
                continue
            row, date = values[item.id]
            new = {"series": _clean(row["series"]), "episode_number": _clean(row["episode_number"]),
                   "title": _clean(row["title"])}
            new.update(zip(("air_year", "air_month", "air_day"), date or (None, None, None)))
            if any(getattr(item, key) != value for key, value in new.items()):
                for key, value in new.items():
                    setattr(item, key, value)
                saved += 1
        session.commit()
    return typed, errors, saved


@app.post("/inbox", response_class=HTMLResponse)
async def inbox_save(request: Request):
    """Save the details typed for the waiting files."""
    typed, errors, saved = save_inbox_details(await request.form())
    if errors:
        return render_inbox(request, typed, errors)
    return go_to(request, f"/inbox?saved={saved}")


@app.post("/inbox/add-all", response_class=HTMLResponse)
async def inbox_add_all(request: Request):
    """Save what was typed, then add every waiting file to the library."""
    typed, errors, _saved = save_inbox_details(await request.form())
    if errors:
        return render_inbox(request, typed, errors)
    added, have, folders = 0, 0, []
    with Session(get_engine()) as session:
        for item in session.scalars(select(InboxItem).order_by(InboxItem.name)).all():
            try:
                row = inbox.add_to_library(session, INBOX_DIR, LIBRARY_DIR, item, art_dir())
            except inbox.AlreadyHave:   # left in the inbox, marked
                have += 1
                continue
            if row is None:            # gone from the inbox meanwhile
                session.delete(item)
            else:
                added += 1
                if row.folder not in folders:
                    folders.append(row.folder)
            session.commit()            # one at a time: each one done stays done
    return go_to(request, "/inbox?" + urlencode(
        [("added_all", added), *([("skipped_have", have)] if have else []),
         *(("folder", folder) for folder in folders)]))


@app.post("/inbox/{item_id}/add", response_class=HTMLResponse)
async def inbox_add(request: Request, item_id: int):
    """Save what was typed, then move this file into the library and add
    it, with its details, as an episode."""
    typed, errors, _saved = save_inbox_details(await request.form())
    if errors:
        return render_inbox(request, typed, errors)
    with Session(get_engine()) as session:
        item = inbox_item_or_404(session, item_id)
        try:
            row = inbox.add_to_library(session, INBOX_DIR, LIBRARY_DIR, item, art_dir())
        except inbox.AlreadyHave:
            return render_inbox(request, problems=[
                f"“{item.name}” wasn't added: you already have that episode."])
        if row is None:
            session.delete(item)
            session.commit()
            raise HTTPException(status_code=404, detail="That file isn't in the inbox any more")
        session.commit()
        added = row.id
    return go_to(request, f"/inbox?added={added}")


def inbox_item_or_404(session: Session, item_id: int) -> InboxItem:
    item = session.get(InboxItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="That file isn't in the inbox any more")
    return item


@app.post("/inbox/{item_id}/reject")
def inbox_reject(request: Request, item_id: int):
    """Delete a file from the inbox. The library is never touched."""
    with Session(get_engine()) as session:
        inbox.reject(session, INBOX_DIR, inbox_item_or_404(session, item_id))
        session.commit()
    return go_to(request, "/inbox?rejected=1")


@app.post("/inbox/reject-duplicates")
def inbox_reject_duplicates(request: Request):
    """Delete every inbox file whose episode is already in the library."""
    rejected = 0
    with Session(get_engine()) as session:
        for item in session.scalars(select(InboxItem)).all():
            if inbox.already_have(session, item) is not None:
                inbox.reject(session, INBOX_DIR, item)
                rejected += 1
        session.commit()
    return go_to(request, f"/inbox?rejected={rejected}")


@app.get("/inbox/{item_id}/audio")
def inbox_audio(item_id: int):
    """Listen to a waiting file before adding it."""
    with Session(get_engine()) as session:
        item = inbox_item_or_404(session, item_id)
        path = inbox.item_path(INBOX_DIR, item)
    if path is None:
        raise HTTPException(status_code=404, detail="That file isn't in the inbox any more")
    media_type = AUDIO_TYPES.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(path, media_type=media_type)


# ----------------------------------------------------------- CSV import


def import_dir() -> Path:
    """Where uploaded spreadsheets wait for the preview (tidied after a day)."""
    return Path(get_engine().url.database).parent / "imports"


@app.get("/import", response_class=HTMLResponse)
def import_page(request: Request, saved: int | None = None, error: str | None = None):
    return render(request, "import.html", {"saved": saved, "error": error},
                  status_code=400 if error else 200)


@app.get("/import-example.csv")
def import_example():
    """An example spreadsheet to start from."""
    return Response(csvimport.example_csv(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="airwave-example.csv"'})


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


@app.post("/episodes/played")
async def mark_played(request: Request):
    """Mark the ticked episodes (or one) as played, or as not played
    (which also forgets where listening stopped)."""
    form = await request.form()
    ids = [int(v) for v in form.getlist("ids") if str(v).isdigit()]
    played, back = form.get("played") == "1", safe_back(form.get("back"))
    with Session(get_engine()) as session:
        episodes = chosen_episodes(session, ids, "")
        listening.mark(session, episodes, played)
        session.commit()
    return go_to(request, with_query(back, marked=str(len(episodes)),
                                     played="1" if played else "0"))


@app.post("/listening/{episode_id}/dismiss")
def dismiss_listening(request: Request, episode_id: int):
    """Take an episode off the home page's "Continue listening" row
    (its place is kept, so Resume still works)."""
    with Session(get_engine()) as session:
        episode = session.get(Episode, episode_id)
        if episode is None:
            raise HTTPException(status_code=404, detail="No such episode")
        listening.dismiss(session, episode)
        session.commit()
    return go_to(request, "/")


@app.post("/listening/{episode_id}", status_code=204)
async def save_listening_place(request: Request, episode_id: int):
    """The player bar reporting where it is (or that it reached the end)."""
    form = await request.form()
    try:
        file_id, seconds = int(form.get("file", "")), float(form.get("at", 0))
    except ValueError:
        raise HTTPException(status_code=400, detail="Which part, and where?")
    with Session(get_engine()) as session:
        episode = session.get(Episode, episode_id)
        if episode is None:
            raise HTTPException(status_code=404, detail="No such episode")
        listening.save_place(session, episode, file_id, seconds, form.get("finished") == "1")
        session.commit()
    return Response(status_code=204)


def picture_file(art_hash: str) -> Path | None:
    """The file of one of the app's pictures (from a scan, or uploaded)."""
    if not HASH_PATTERN.match(art_hash or ""):
        return None
    return next((p for p in (art_path(art_dir(), art_hash), art_path(upload_dir(), art_hash))
                 if p.is_file()), None)


def export_plan(session: Session, ids: list[int], whole_series: str):
    episodes = chosen_episodes(session, ids, whole_series)
    export.give_ids(episodes)
    session.commit()
    return export.make_plan(session, episodes, LIBRARY_DIR, picture_file)


@app.get("/export", response_class=HTMLResponse)
def export_page(request: Request, ids: list[int] = Query(default=[]),
                whole_series: str = "", back: str = "/"):
    """What an export will hold, with the button to download it."""
    with Session(get_engine(), expire_on_commit=False) as session:
        plan = export_plan(session, ids, whole_series)
    query = urlencode([("whole_series", whole_series)] if whole_series
                      else [("ids", e.id) for e in plan.episodes])
    return render(request, "export.html", {
        "plan": plan, "series": whole_series, "back": safe_back(back),
        "download": f"/export/download?{query}",
        "file_name": export.zip_name(whole_series or None, len(plan.episodes)),
    })


@app.get("/export/download")
def export_download(ids: list[int] = Query(default=[]), whole_series: str = ""):
    """The zip itself, made while it downloads."""
    with Session(get_engine(), expire_on_commit=False) as session:
        plan = export_plan(session, ids, whole_series)
        for episode in plan.episodes:       # everything the zip needs, loaded now
            episode.parts
        session.expunge_all()
    if not plan.items:
        raise HTTPException(status_code=404, detail="Nothing to export")
    name = export.zip_name(whole_series or None, len(plan.episodes))
    work_dir = Path(get_engine().url.database).parent / "export-temp"
    return StreamingResponse(export.stream_zip(plan, work_dir), media_type="application/zip",
                             headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})


@app.get("/delete", response_class=HTMLResponse)
def delete_page(request: Request, ids: list[int] = Query(default=[]), back: str = "/library"):
    """Which files deleting these episodes would remove, and the button to
    do it. Nothing is deleted until that button is pressed."""
    with Session(get_engine(), expire_on_commit=False) as session:
        episodes = chosen_episodes(session, ids, "")
        plan = deleting.make_plan(episodes, LIBRARY_DIR)
        firsts = {e.id: e.parts[0] for e in episodes if e.files}
        for f in plan.files + plan.missing:
            f.episode                     # (loaded now, for the page)
        session.expunge_all()
    return render(request, "delete.html", {"plan": plan, "firsts": firsts,
                                           "back": safe_back(back)})


@app.post("/delete", response_class=HTMLResponse)
async def delete_episodes(request: Request):
    """Delete the episodes' files from the disk, and the episodes from the
    app. For good: only after the confirmation page."""
    form = await request.form()
    ids = [int(v) for v in form.getlist("ids") if str(v).isdigit()]
    back = safe_back(form.get("back"))
    if form.get("confirm") != "yes":
        raise HTTPException(status_code=400, detail="Please confirm on the delete page first")
    with Session(get_engine()) as session:
        episodes = chosen_episodes(session, ids, "")
        series = next((e.series for e in episodes if e.series), None)
        result = deleting.delete_episodes(session, episodes, LIBRARY_DIR)
        session.commit()
        if back.startswith("/files/") or back.startswith("/series/episodes"):
            # That page may be gone now: the series' page if it's still
            # there, else the library.
            still = series and one_series(session, series)
            if back.startswith("/files/") or not still:
                back = series_url(series) if still else "/library"
    if result.problems:
        return render(request, "deleted.html", {"result": result, "back": back})
    return go_to(request, with_query(back, deleted=str(result.deleted),
                                     freed=str(result.freed)))


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


# ------------------------------------------------------------------ feeds
# Podcast feeds (RSS) for podcast apps; see app/feeds.py.

MAX_FEED_TITLE, MAX_FEED_DESCRIPTION = 200, 4000
FEED_PAGE_ITEMS = 25    # recordings listed on a feed's page (the first ones)


def get_feed(session: Session, feed_id: int) -> Feed:
    feed = session.get(Feed, feed_id)
    if feed is None:
        raise HTTPException(status_code=404, detail="No such feed")
    return feed


def server_address(request: Request) -> str:
    """This server's address as the browser (or podcast app) reached it."""
    return str(request.base_url).rstrip("/")


def read_feed_form(form) -> tuple[dict, dict]:
    values = {"title": str(form.get("title", "")).strip(),
              "description": str(form.get("description", "")).strip()}
    errors = {}
    if not values["title"]:
        errors["title"] = "Give the podcast a name."
    elif len(values["title"]) > MAX_FEED_TITLE:
        errors["title"] = f"Keep the name under {MAX_FEED_TITLE} characters."
    if len(values["description"]) > MAX_FEED_DESCRIPTION:
        errors["description"] = f"Keep the description under {MAX_FEED_DESCRIPTION} characters."
    return values, errors


@app.get("/feeds", response_class=HTMLResponse)
def feeds_page(request: Request):
    """All the podcast feeds, newest first."""
    with Session(get_engine()) as session:
        rows = []
        for feed in session.scalars(select(Feed).order_by(Feed.created_at.desc())):
            found = feeds.feed_items(session, feed)
            rows.append({"feed": feed, "recordings": len(found),
                         "episodes": len({i.episode.id for i in found}),
                         "art": feeds.feed_picture(session, feed, found)})
        session.expunge_all()
    return render(request, "feeds.html", {"rows": rows})


def picked_episodes(session: Session, ids: list[int]) -> list[Episode]:
    """Ticked episodes (the ones that still exist), in air-date order."""
    found = []
    ids = list(dict.fromkeys(ids))[:5000]
    for i in range(0, len(ids), 500):
        found += session.scalars(select(Episode).where(Episode.id.in_(ids[i:i + 500]))).all()
    return sorted(found, key=feeds.air_key)


def render_new_feed(request: Request, series: str, totals, picked, back: str,
                    values: dict, errors: dict):
    return render(request, "feed_form.html", {
        "feed": None, "series": series, "totals": totals, "picked": picked,
        "back": safe_back(back) if back else (series_url(series) if series else "/feeds"),
        "values": values, "errors": errors}, status_code=400 if errors else 200)


@app.get("/feeds/new", response_class=HTMLResponse)
def new_feed(request: Request, series: str = "", ids: list[int] = Query(default=[]),
             back: str = ""):
    """Make a feed: of a whole series, or of ticked episodes. Its name and
    description first."""
    with Session(get_engine()) as session:
        totals = one_series(session, series) if series else None
        picked = [] if series else picked_episodes(session, ids)
        session.expunge_all()
    if series and totals is None:
        raise HTTPException(status_code=404, detail="No such series")
    names = {e.series for e in picked}
    title = series or (names.pop() if len(names) == 1 and None not in names else "")
    return render_new_feed(request, series, totals, picked, back,
                           {"title": title or "", "description": ""}, {})


@app.post("/feeds", response_class=HTMLResponse)
async def create_feed(request: Request):
    form = await request.form()
    series = str(form.get("series", ""))
    ids = [int(i) for i in form.getlist("ids") if str(i).isdigit()]
    back = str(form.get("back", ""))
    values, errors = read_feed_form(form)
    with Session(get_engine()) as session:
        totals = one_series(session, series) if series else None
        picked = [] if series else picked_episodes(session, ids)
        if series and totals is None:
            raise HTTPException(status_code=404, detail="No such series")
        if not series and not picked:
            errors["picked"] = "No episodes were ticked."
        if errors:
            session.expunge_all()
            return render_new_feed(request, series, totals, picked, back, values, errors)
        if series:
            feed = feeds.make_series_feed(session, series, values["title"],
                                          values["description"] or None)
        else:
            feed = feeds.make_picked_feed(session, picked, values["title"],
                                          values["description"] or None)
        session.commit()
        feed_id = feed.id
    return go_to(request, f"/feeds/{feed_id}?made=1")


@app.get("/feeds/add", response_class=HTMLResponse)
def add_to_feed_page(request: Request, ids: list[int] = Query(default=[]), back: str = ""):
    """Ticked episodes: choose a hand-picked feed to add them to."""
    with Session(get_engine()) as session:
        picked = picked_episodes(session, ids)
        choices = list(session.scalars(
            select(Feed).where(Feed.series.is_(None)).order_by(Feed.title)))
        session.expunge_all()
    return render(request, "feed_add.html", {
        "picked": picked, "choices": choices, "back": safe_back(back) if back else "/feeds"})


@app.post("/feeds/add")
async def add_to_feed(request: Request):
    """Add ticked episodes to the hand-picked feed chosen."""
    form = await request.form()
    ids = [int(i) for i in form.getlist("ids") if str(i).isdigit()]
    feed_id = int(form.get("feed", "0")) if str(form.get("feed", "")).isdigit() else 0
    with Session(get_engine()) as session:
        feed = get_feed(session, feed_id)
        if feed.series is not None:
            raise HTTPException(status_code=400, detail="A series feed holds its whole series")
        picked = picked_episodes(session, ids)
        added = feeds.add_episodes(session, feed, picked)
        session.commit()
    return go_to(request, f"/feeds/{feed_id}?added={added}&already={len(picked) - added}")


@app.post("/feeds/{feed_id}/remove")
async def remove_from_feed(request: Request, feed_id: int):
    """Take ticked episodes out of a hand-picked feed (never out of the library)."""
    form = await request.form()
    ids = {int(i) for i in form.getlist("ids") if str(i).isdigit()}
    with Session(get_engine()) as session:
        feed = get_feed(session, feed_id)
        removed = feeds.remove_episodes(feed, ids)
        session.commit()
    return go_to(request, f"/feeds/{feed_id}?removed={removed}")


@app.get("/feeds/{feed_id}", response_class=HTMLResponse)
def feed_page(request: Request, feed_id: int, made: bool = False, published: str = ""):
    """One feed: its details, whether it's published, its address and QR
    code, how to add it on an iPhone, and the episodes in it (all of a
    hand-picked feed's, with ticks to remove some)."""
    with Session(get_engine()) as session:
        feed = get_feed(session, feed_id)
        found = feeds.feed_items(session, feed)
        art = feeds.feed_picture(session, feed, found)
        first = found if feed.series is None else found[:FEED_PAGE_ITEMS]
        session.expunge_all()
    address = f"{server_address(request)}/feeds/{feed.token}/feed.xml"
    podcast_address = "podcast://" + address.split("://", 1)[1]
    return render(request, "feed.html", {
        "feed": feed, "art": art,
        "recordings": len(found), "episodes": len({i.episode.id for i in found}),
        "first": first, "odd": feeds.odd_formats(found),
        "address": address, "podcast_address": podcast_address,
        "qr": feeds.qr_svg(podcast_address) if feed.published else None,
        "made": made, "published_now": published,
    })


@app.get("/feeds/{feed_id}/edit", response_class=HTMLResponse)
def edit_feed_form(request: Request, feed_id: int):
    with Session(get_engine()) as session:
        feed = get_feed(session, feed_id)
        session.expunge_all()
    return render(request, "feed_form.html", {
        "feed": feed, "series": feed.series, "totals": None,
        "values": {"title": feed.title, "description": feed.description or ""}, "errors": {}})


@app.post("/feeds/{feed_id}/edit", response_class=HTMLResponse)
async def edit_feed(request: Request, feed_id: int):
    values, errors = read_feed_form(await request.form())
    with Session(get_engine()) as session:
        feed = get_feed(session, feed_id)
        if errors:
            session.expunge_all()
            return render(request, "feed_form.html", {
                "feed": feed, "series": feed.series, "totals": None,
                "values": values, "errors": errors}, status_code=400)
        feed.title, feed.description = values["title"], values["description"] or None
        feed.updated_at = utcnow()
        session.commit()
    return go_to(request, f"/feeds/{feed_id}")


@app.post("/feeds/{feed_id}/publish")
async def publish_feed(request: Request, feed_id: int):
    """Publish a feed (its address starts working) or take it back to a
    draft (its address stops working; podcast apps keep what they have)."""
    publish = str((await request.form()).get("published", "")) == "1"
    with Session(get_engine()) as session:
        feed = get_feed(session, feed_id)
        if publish and not feed.published:
            feed.published_at = utcnow()
        feed.published = publish
        feed.updated_at = utcnow()
        session.commit()
    return go_to(request, f"/feeds/{feed_id}?published={'yes' if publish else 'no'}")


@app.get("/feeds/{feed_id}/delete", response_class=HTMLResponse)
def delete_feed_page(request: Request, feed_id: int):
    with Session(get_engine()) as session:
        feed = get_feed(session, feed_id)
        session.expunge_all()
    return render(request, "feed_delete.html", {"feed": feed})


@app.post("/feeds/{feed_id}/delete")
async def delete_feed(request: Request, feed_id: int):
    """Delete a feed (never its recordings). Its address stops working."""
    if str((await request.form()).get("confirm", "")) != "yes":
        raise HTTPException(status_code=400, detail="Not confirmed")
    with Session(get_engine()) as session:
        session.delete(get_feed(session, feed_id))
        session.commit()
    return go_to(request, "/feeds?deleted=1")


def published_feed(session: Session, token: str) -> Feed:
    """The published feed with this private token (else "not found", the
    same for a draft as for a wrong token)."""
    feed = session.scalar(select(Feed).where(Feed.token == token))
    if feed is None or not feed.published:
        raise HTTPException(status_code=404, detail="No such feed")
    return feed


@app.get("/feeds/{token}/feed.xml")
def feed_xml(request: Request, token: str):
    """The feed itself, for podcast apps."""
    with Session(get_engine()) as session:
        feed = published_feed(session, token)
        body = feeds.rss(session, feed, server_address(request))
    return Response(body, media_type="application/rss+xml; charset=utf-8",
                    headers={"Cache-Control": "no-cache"})


@app.get("/feeds/{token}/audio/{name}")
def feed_audio(token: str, name: str):
    """A recording of a published feed, for podcast apps (like /audio,
    which can send any part of the file)."""
    stem = name.split(".", 1)[0]
    if not stem.isdigit():
        raise HTTPException(status_code=404, detail="No such recording")
    with Session(get_engine()) as session:
        feed = published_feed(session, token)
        f = session.get(AudioFile, int(stem))
        if f is None or not feeds.in_feed(session, feed, f):
            raise HTTPException(status_code=404, detail="No such recording")
    return audio(int(stem))


@app.get("/health")
def health():
    """A tiny page Docker uses to check that the app is running."""
    return {"status": "ok"}
