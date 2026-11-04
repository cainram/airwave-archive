"""Airwave Archive web app — Phase 1: list the audio files in the library.

Run locally with:   uvicorn app.main:app --reload
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.scanner import ScanResult, format_duration, format_size, scan_library

APP_DIR = Path(__file__).parent

# Where the recordings live. Inside Docker this is /library (see compose.yaml).
LIBRARY_DIR = Path(os.environ.get("AIRWAVE_LIBRARY", "/library"))

app = FastAPI(title="Airwave Archive")
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")

templates = Jinja2Templates(directory=APP_DIR / "templates")
templates.env.filters["size"] = format_size
templates.env.filters["duration"] = format_duration

# Phase 1 keeps the scan results in memory. Phase 2 moves them into a database.
_last_scan: ScanResult | None = None
_last_scan_seconds: float = 0.0


def run_scan() -> ScanResult:
    global _last_scan, _last_scan_seconds
    started = time.monotonic()
    _last_scan = scan_library(LIBRARY_DIR)
    _last_scan_seconds = time.monotonic() - started
    return _last_scan


@app.get("/", response_class=HTMLResponse)
def library_page(request: Request):
    scan = _last_scan or run_scan()
    return templates.TemplateResponse(request, "library.html", {
        "scan": scan,
        "scan_seconds": _last_scan_seconds,
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
