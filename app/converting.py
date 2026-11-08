"""The inbox's converting queue (Phase 13b).

Files that need converting (see app/convert.py) join a queue as soon as
they're in the inbox. A helper inside the app (a background *thread*: a
second line of work running alongside the web pages) converts them one at
a time, so the pages stay quick. The queue is kept in the database
(``inbox_items.convert_status``), so it carries on after a restart.

For each file:

1. ffmpeg writes the new file under a hidden name in the inbox (hidden
   files aren't listed, so a half-made file never shows).
2. The new file is checked: it opens, holds sound, and is as long as the
   original.
3. It takes its proper name ("Show.webm" becomes "Show.mp3"; a number is
   added if that's taken), the uploaded original is deleted (owner's
   choice), and the waiting file's details, including anything typed in
   the inbox, move to the new file.

If anything goes wrong, the original stays, with the reason; the owner
can try again or reject it. Cancelling stops at once and keeps the
original.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from pathlib import Path
from typing import Callable

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app import convert
from app.db import InboxItem

log = logging.getLogger(__name__)

WAITING, CONVERTING, FAILED, CANCELLED = "waiting", "converting", "failed", "cancelled"
BUSY = (WAITING, CONVERTING)
TEMPORARY = ".converting-"       # start of a half-made file's name
IDLE_SECONDS = 3                 # how often an idle helper looks for work
LOOK_IN_FOLDER_SECONDS = 30      # and for files copied straight into the inbox folder
SAVE_PROGRESS_SECONDS = 2


def queue(item: InboxItem) -> None:
    """Put a file that needs converting in the queue, if it can be done."""
    plan = convert.Plan.from_json(item.convert_json)
    if plan is not None and plan.ok and item.convert_status is None:
        item.convert_status, item.convert_progress, item.convert_error = WAITING, None, None


def cancel(item: InboxItem) -> None:
    """Stop converting this file (the helper notices within a second)."""
    if item.convert_status in BUSY:
        item.convert_status, item.convert_progress = CANCELLED, None


def retry(item: InboxItem) -> None:
    if item.convert_status in (FAILED, CANCELLED):
        item.convert_status, item.convert_progress, item.convert_error = WAITING, None, None


def recover(engine: Engine, inbox_dir: Path) -> None:
    """After a restart: a file that was halfway through starts again, and
    half-made files are cleared away."""
    with Session(engine) as session:
        for item in session.scalars(select(InboxItem).where(InboxItem.convert_status == CONVERTING)):
            item.convert_status, item.convert_progress = WAITING, None
        session.commit()
    if inbox_dir.is_dir():
        for leftover in inbox_dir.glob(TEMPORARY + "*"):
            leftover.unlink(missing_ok=True)


def _status(engine: Engine, item_id: int) -> str | None:
    with Session(engine) as session:
        item = session.get(InboxItem, item_id)
        return None if item is None else item.convert_status


def _update(engine: Engine, item_id: int, **values) -> None:
    with Session(engine) as session:
        item = session.get(InboxItem, item_id)
        if item is not None:
            for key, value in values.items():
                setattr(item, key, value)
            session.commit()


def convert_next(engine: Engine, inbox_dir: Path) -> bool:
    """Convert the next waiting file, if there is one. Returns whether
    there was one."""
    from app.inbox import LOCK, item_path   # here, to avoid a circular import

    with Session(engine) as session:
        item = session.scalars(
            select(InboxItem).where(InboxItem.convert_status == WAITING)
            .order_by(InboxItem.added_at, InboxItem.id).limit(1)).first()
        if item is None:
            return False
        plan = convert.Plan.from_json(item.convert_json)
        source = item_path(inbox_dir, item)
        if source is None:                       # gone from the inbox meanwhile
            session.delete(item)
            session.commit()
            return True
        if plan is None or not plan.ok:
            item.convert_status = FAILED
            item.convert_error = plan.problem if plan else "It hasn't been looked inside yet."
            session.commit()
            return True
        item.convert_status, item.convert_progress, item.convert_error = CONVERTING, 0.0, None
        session.commit()
        item_id = item.id

    made = inbox_dir / f"{TEMPORARY}{secrets.token_hex(6)}{plan.extension}"
    saved_at, asked_at = [0.0], [0.0]

    def progress(fraction: float) -> None:
        if time.monotonic() - saved_at[0] >= SAVE_PROGRESS_SECONDS:
            saved_at[0] = time.monotonic()
            _update(engine, item_id, convert_progress=fraction)

    def stop() -> bool:
        if time.monotonic() - asked_at[0] < 1:
            return False
        asked_at[0] = time.monotonic()
        return _status(engine, item_id) != CONVERTING   # cancelled, or rejected

    try:
        convert.run(plan, source, made, progress, stop)
        problem = convert.check(plan, made)
        if problem:
            raise RuntimeError(problem)
    except convert.Stopped:
        made.unlink(missing_ok=True)
        return True
    except (RuntimeError, OSError) as error:
        made.unlink(missing_ok=True)
        log.warning("Converting %s failed: %s", source.name, error)
        _update(engine, item_id, convert_status=FAILED, convert_progress=None,
                convert_error=str(error)[:500])
        return True

    with LOCK:
        _finish(engine, inbox_dir, item_id, source, made, plan)
    return True


def _finish(engine: Engine, inbox_dir: Path, item_id: int, source: Path, made: Path,
            plan: convert.Plan) -> None:
    """Swap the original for the new file, keeping the waiting file's
    details."""
    from app.inbox import free_name
    from app.scanner import read_audio

    with Session(engine) as session:
        item = session.get(InboxItem, item_id)
        if item is None or item.convert_status != CONVERTING:   # rejected or cancelled at the end
            made.unlink(missing_ok=True)
            return
        final = inbox_dir / free_name(inbox_dir, Path(item.name).stem + plan.extension)
        made.rename(final)
        try:
            source.unlink(missing_ok=True)
        except OSError as error:
            final.unlink(missing_ok=True)       # keep things as they were
            item.convert_status, item.convert_progress = FAILED, None
            item.convert_error = f"The original couldn't be deleted ({error})."
            session.commit()
            return
        info = read_audio(final)
        new = InboxItem(
            name=final.name, size_bytes=final.stat().st_size,
            duration_seconds=info.duration_seconds or plan.duration, added_at=item.added_at,
            series=item.series, episode_number=item.episode_number, title=item.title,
            air_year=item.air_year, air_month=item.air_month, air_day=item.air_day,
            extra_json=item.extra_json)
        session.delete(item)
        session.flush()
        session.add(new)
        session.commit()
        log.info("Converted %s into %s", source.name, final.name)


_started = False


def start(get_engine: Callable[[], Engine], get_inbox: Callable[[], Path],
          sync: Callable[[Engine, Path], None] | None = None) -> None:
    """Start the helper that works through the queue (once). ``sync``
    brings the inbox list in step with its folder now and then, so files
    copied straight into the folder are converted too."""
    global _started
    if _started:
        return
    _started = True

    def work() -> None:
        try:
            recover(get_engine(), get_inbox())
        except Exception:
            log.exception("Couldn't tidy up the converting queue")
        looked = 0.0
        while True:
            try:
                if sync is not None and time.monotonic() - looked > LOOK_IN_FOLDER_SECONDS:
                    looked = time.monotonic()
                    sync(get_engine(), get_inbox())
                busy = convert_next(get_engine(), get_inbox())
            except Exception:
                log.exception("The converting helper ran into a problem")
                busy = False
            if not busy:
                time.sleep(IDLE_SECONDS)

    threading.Thread(target=work, name="converting", daemon=True).start()
