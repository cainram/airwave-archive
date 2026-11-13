"""The inbox's converting queue (Phase 13b).

Files that need converting (see app/convert.py) join a queue as soon as
they're in the inbox. Helpers inside the app (background *threads*: lines
of work running alongside the web pages) convert them, several at once on
a machine with several cores: one helper for each core but one, so a
core stays free and the pages stay quick (see ``helpers``). Making an MP3
uses one core per file, so this is what makes a big batch faster. The queue is kept in the database
(``inbox_items.convert_status``), so it carries on after a restart.

For each file:

1. ffmpeg writes the new file under a hidden name in the inbox (hidden
   files aren't listed, so a half-made file never shows).
2. The new file is checked: it opens, holds sound, and is as long as the
   original.
   A "video" that's really one picture (cover art, or the same picture
   all the way through, as many uploads of radio shows are) gives that
   picture to the new file as its cover art, so the episode shows it.
3. It takes its proper name ("Show.webm" becomes "Show.mp3"; a number is
   added if that's taken), the uploaded original is deleted (owner's
   choice), and the waiting file's details, including anything typed in
   the inbox, move to the new file.

If anything goes wrong, the original stays, with the reason; the owner
can try again or reject it. Cancelling stops at once and keeps the
original.

**Library files** (Phase 13d, an advanced technique: files added straight
to the library folder and found by a scan) go through the same steps,
once the owner has confirmed them on the "Convert library files" page.
The new file is made beside the original, in the same folder; once it's
checked it takes its place and the original is deleted. The app's record
of the file is updated to the new file, so its episode, listening place,
group and feeds stay as they were. Their queue is the
``library_conversions`` table. Inbox files go first.
"""

from __future__ import annotations

import logging
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Callable

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app import convert
from app.db import AudioFile, InboxItem, LibraryConversion, utcnow

log = logging.getLogger(__name__)

WAITING, CONVERTING, FAILED, CANCELLED = "waiting", "converting", "failed", "cancelled"
BUSY = (WAITING, CONVERTING)
TEMPORARY = ".converting-"       # start of a half-made file's name
IDLE_SECONDS = 3                 # how often an idle helper looks for work
LOOK_IN_FOLDER_SECONDS = 30      # and for files copied straight into the inbox folder
SAVE_PROGRESS_SECONDS = 2

# Taking the next waiting file and marking it "converting" happen together
# under this lock, so two helpers never take the same file.
_CLAIM = threading.Lock()


def helpers(cores: int | None = None) -> int:
    """How many files to convert at once: the number of cores this app may
    use, minus one (at least one)."""
    if cores is None:
        cores = getattr(os, "process_cpu_count", os.cpu_count)() or 1
    return max(1, cores - 1)


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


def recover(engine: Engine, inbox_dir: Path, library_dir: Path | None = None) -> None:
    """After a restart: a file that was halfway through starts again, and
    half-made files are cleared away."""
    folders = set()
    with Session(engine) as session:
        for item in session.scalars(select(InboxItem).where(InboxItem.convert_status == CONVERTING)):
            item.convert_status, item.convert_progress = WAITING, None
        for job in session.scalars(select(LibraryConversion).where(LibraryConversion.status == CONVERTING)):
            job.status, job.progress = WAITING, None
            f = session.get(AudioFile, job.file_id)
            if f is not None and library_dir is not None:
                folders.add(library_dir / f.folder)
        session.commit()
    for folder in [inbox_dir, *folders]:
        if folder.is_dir():
            for leftover in folder.glob(TEMPORARY + "*"):
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

    with _CLAIM, Session(engine) as session:
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
    try:
        _make(plan, source, made,
              save=lambda fraction: _update(engine, item_id, convert_progress=fraction),
              still_going=lambda: _status(engine, item_id) == CONVERTING)   # not cancelled or rejected
    except convert.Stopped:
        return True
    except (RuntimeError, OSError) as error:
        log.warning("Converting %s failed: %s", source.name, error)
        _update(engine, item_id, convert_status=FAILED, convert_progress=None,
                convert_error=str(error)[:500])
        return True

    with LOCK:
        _finish(engine, inbox_dir, item_id, source, made, plan)
    return True


def _make(plan: convert.Plan, source: Path, made: Path, save: Callable[[float], None],
          still_going: Callable[[], bool]) -> None:
    """Make the new file (``made``, a hidden name) from ``source`` and check
    it, keeping a "video"'s still picture as its cover art. ``save`` is
    given the progress now and then; ``still_going`` is asked about once
    a second whether to carry on. Raises convert.Stopped if not (the
    half-made file is removed), or RuntimeError/OSError if it fails."""
    saved_at, asked_at = [0.0], [0.0]

    def progress(fraction: float) -> None:
        if time.monotonic() - saved_at[0] >= SAVE_PROGRESS_SECONDS:
            saved_at[0] = time.monotonic()
            save(fraction)

    def stop() -> bool:
        if time.monotonic() - asked_at[0] < 1:
            return False
        asked_at[0] = time.monotonic()
        return not still_going()

    cover = None
    if plan.picture_stream is not None and (plan.still_picture or convert.looks_still(source, plan)):
        cover = made.with_name(f"{TEMPORARY}{secrets.token_hex(6)}.jpg")
        if not convert.grab_picture(source, plan, cover):
            cover.unlink(missing_ok=True)
            cover = None
    try:
        convert.run(plan, source, made, progress, stop, cover=cover)
        problem = convert.check(plan, made)
        if problem:
            raise RuntimeError(problem)
    except BaseException:
        made.unlink(missing_ok=True)
        raise
    finally:
        if cover is not None:
            cover.unlink(missing_ok=True)


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


# ------------------------------------------------------------ library files

def odd_library_files(session: Session) -> list[AudioFile]:
    """Library files in formats that need converting, in folder order. A
    file whose plan isn't known yet (scanned before converting existed)
    is looked inside now, and the plan kept."""
    from sqlalchemy import func, or_
    files = session.scalars(
        select(AudioFile).where(AudioFile.missing.is_(False), or_(*[
            func.lower(AudioFile.name).like(f"%{ext}") for ext in sorted(convert.CONVERT_EXTENSIONS)]))
        .order_by(AudioFile.folder, AudioFile.name)).all()
    return files


def plan_library_file(f: AudioFile, library_dir: Path) -> convert.Plan | None:
    """The plan for converting a library file (worked out now if it's not
    known yet, and kept)."""
    plan = convert.Plan.from_json(f.convert_json)
    if plan is None and convert.available():
        path = library_dir / f.folder / f.name
        plan = convert.plan_for(f.name, convert.probe(path))
        f.convert_json = plan.to_json()
    return plan


def queue_library(session: Session, files: list[AudioFile]) -> int:
    """Put library files the owner confirmed in the queue (or back in it,
    after a failure or cancel). Returns how many were queued."""
    queued = 0
    for f in files:
        plan = convert.Plan.from_json(f.convert_json)
        if f.missing or plan is None or not plan.ok or not convert.needs_converting(f.name):
            continue
        job = session.get(LibraryConversion, f.id)
        if job is None:
            session.add(LibraryConversion(file_id=f.id, status=WAITING, queued_at=utcnow()))
        elif job.status in (FAILED, CANCELLED):
            job.status, job.progress, job.error, job.queued_at = WAITING, None, None, utcnow()
        else:
            continue
        queued += 1
    return queued


def cancel_library(session: Session) -> int:
    """Stop converting library files: the one in progress and the rest."""
    jobs = session.scalars(select(LibraryConversion).where(LibraryConversion.status.in_(BUSY))).all()
    for job in jobs:
        job.status, job.progress = CANCELLED, None
    return len(jobs)


def _library_update(engine: Engine, file_id: int, **values) -> None:
    with Session(engine) as session:
        job = session.get(LibraryConversion, file_id)
        if job is not None:
            for key, value in values.items():
                setattr(job, key, value)
            session.commit()


def _library_status(engine: Engine, file_id: int) -> str | None:
    with Session(engine) as session:
        job = session.get(LibraryConversion, file_id)
        return None if job is None else job.status


def convert_next_library(engine: Engine, library_dir: Path, art_dir: Path | None = None) -> bool:
    """Convert the next library file waiting in the queue, if there is one.
    Returns whether there was one."""
    with _CLAIM, Session(engine) as session:
        job = session.scalars(
            select(LibraryConversion).where(LibraryConversion.status == WAITING)
            .order_by(LibraryConversion.queued_at, LibraryConversion.file_id).limit(1)).first()
        if job is None:
            return False
        f = session.get(AudioFile, job.file_id)
        plan = convert.Plan.from_json(f.convert_json) if f is not None else None
        source = (library_dir / f.folder / f.name) if f is not None else None
        if source is None or f.missing or not source.is_file() \
                or library_dir.resolve() not in source.resolve().parents:
            job.status, job.error = FAILED, "The file isn't in the library any more."
            session.commit()
            return True
        if plan is None or not plan.ok:
            job.status, job.error = FAILED, plan.problem if plan else "It hasn't been looked inside yet."
            session.commit()
            return True
        job.status, job.progress, job.error = CONVERTING, 0.0, None
        session.commit()
        file_id, size = f.id, f.size_bytes

    made = source.with_name(f"{TEMPORARY}{secrets.token_hex(6)}{plan.extension}")
    try:
        _make(plan, source, made,
              save=lambda fraction: _library_update(engine, file_id, progress=fraction),
              still_going=lambda: _library_status(engine, file_id) == CONVERTING)
    except convert.Stopped:
        return True
    except (RuntimeError, OSError) as error:
        log.warning("Converting %s failed: %s", source.name, error)
        _library_update(engine, file_id, status=FAILED, progress=None, error=str(error)[:500])
        return True

    from app.scanner import LIBRARY_LOCK
    with LIBRARY_LOCK:
        _finish_library(engine, library_dir, file_id, size, source, made, plan, art_dir)
    return True


def _finish_library(engine: Engine, library_dir: Path, file_id: int, size: int, source: Path,
                    made: Path, plan: convert.Plan, art_dir: Path | None) -> None:
    """Swap the original library file for the new one: the new file takes
    its place (and name, with the new extension), the original is deleted,
    and the app's record of the file now describes the new one."""
    from app.episodes import fill_from_tags
    from app.inbox import free_name
    from app.scanner import _apply, read_audio

    with Session(engine) as session:
        f = session.get(AudioFile, file_id)
        job = session.get(LibraryConversion, file_id)
        # Changed meanwhile (cancelled, moved, edited outside the app): leave it.
        if f is None or job is None or job.status != CONVERTING or f.missing \
                or not source.is_file() or source.stat().st_size != size:
            made.unlink(missing_ok=True)
            if job is not None and job.status == CONVERTING:
                job.status, job.progress = FAILED, None
                job.error = "The original changed while it was converting; nothing was replaced."
                session.commit()
            return
        final = source.with_name(free_name(source.parent, source.stem + plan.extension))
        made.rename(final)
        try:
            source.unlink()
        except OSError as error:
            final.unlink(missing_ok=True)       # keep things as they were
            job.status, job.progress = FAILED, None
            job.error = f"The original couldn't be deleted ({error})."
            session.commit()
            return
        f.name = final.name
        f.convert_json = None
        _apply(f, read_audio(final), final.stat(), art_dir)
        if f.episode is not None:
            fill_from_tags(f.episode, f)
        session.delete(job)
        session.commit()
        log.info("Converted %s into %s (library)", source, final.name)


_started = False


def start(get_engine: Callable[[], Engine], get_inbox: Callable[[], Path],
          sync: Callable[[Engine, Path], None] | None = None,
          get_library: Callable[[], Path] | None = None,
          get_art_dir: Callable[[], Path | None] | None = None) -> None:
    """Start the helpers that work through the queue (once): see
    ``helpers`` for how many. ``sync`` brings the inbox list in step with
    its folder every 30 seconds (on a helper of its own, so it isn't held
    up while every helper is busy), so files copied straight into the
    folder are converted too."""
    global _started
    if _started:
        return
    _started = True

    def begin() -> None:
        try:
            recover(get_engine(), get_inbox(), get_library() if get_library else None)
        except Exception:
            log.exception("Couldn't tidy up the converting queue")
        count = helpers()
        log.info("Converting up to %d file%s at once", count, "" if count == 1 else "s")
        for number in range(count):
            threading.Thread(target=work, name=f"converting-{number + 1}", daemon=True).start()
        while sync is not None:      # this one keeps an eye on the inbox folder
            try:
                sync(get_engine(), get_inbox())
            except Exception:
                log.exception("Couldn't look in the inbox folder")
            time.sleep(LOOK_IN_FOLDER_SECONDS)

    def work() -> None:
        while True:
            try:
                busy = convert_next(get_engine(), get_inbox())
                if not busy and get_library is not None:
                    busy = convert_next_library(get_engine(), get_library(),
                                                get_art_dir() if get_art_dir else None)
            except Exception:
                log.exception("The converting helper ran into a problem")
                busy = False
            if not busy:
                time.sleep(IDLE_SECONDS)

    threading.Thread(target=begin, name="converting", daemon=True).start()
