"""Airwave Archive web app — Phase 2: remember the library in a database.

Run locally with:   uvicorn app.main:app --reload
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.db import DB_FILENAME, AudioFile, ScanRun, make_engine, utcnow
from app.scanner import format_bitrate, format_duration, format_size, sync_library

APP_DIR = Path(__file__).parent

# Where the recordings live. Inside Docker this is /library (see compose.yaml).
LIBRARY_DIR = Path(os.environ.get("AIRWAVE_LIBRARY", "/library"))
# Where the app keeps its database. Inside Docker this is /data.
DATA_DIR = Path(os.environ.get("AIRWAVE_DATA", "data"))

PAGE_SIZE = 200  # files shown per page

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


templates = Jinja2Templates(directory=APP_DIR / "templates")
templates.env.filters["size"] = format_size
templates.env.filters["duration"] = format_duration
templates.env.filters["bitrate"] = format_bitrate
templates.env.filters["ago"] = time_ago
templates.env.filters["fromjson"] = lambda text: json.loads(text) if text else []

_engine: Engine | None = None
_scan_lock = threading.Lock()  # only one scan at a time


def get_engine() -> Engine:
    """Open the database the first time it's needed."""
    global _engine
    if _engine is None:
        _engine = make_engine(DATA_DIR / DB_FILENAME)
    return _engine


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
        count, total_bytes, total_seconds, unreadable = session.execute(select(
            func.count(AudioFile.id),
            func.sum(AudioFile.size_bytes),
            func.sum(AudioFile.duration_seconds),
            func.count(AudioFile.id).filter(AudioFile.duration_seconds.is_(None)),
        )).one()
        pages = max(1, -(-count // PAGE_SIZE))   # round up
        page = min(max(1, page), pages)
        files = session.scalars(
            select(AudioFile)
            .order_by(AudioFile.folder, AudioFile.name)
            .offset((page - 1) * PAGE_SIZE)
            .limit(PAGE_SIZE)
        ).all()

    return templates.TemplateResponse(request, "library.html", {
        "files": files,
        "count": count,
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
def rescan():
    run_scan()
    # Send the browser back to the list ("303" means "go look at this page now").
    return RedirectResponse("/", status_code=303)


@app.get("/health")
def health():
    """A tiny page Docker uses to check that the app is running."""
    return {"status": "ok"}
