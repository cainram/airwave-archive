"""Renaming and organizing library files from their details (Phase 10b).

Like the "organize my music folder" feature of classic MP3 software: each
recording is named from its episode's details, in a folder named after its
series (owner's choice, 2026-11-10):

    Gunsmoke/Gunsmoke - 1952-09-13 - Home Surgery.mp3
    Amos and Andy/Amos and Andy - 1928-12-27.mp3          (no title)
    Suspense/Suspense - 1943 - Donovan's Brain.mp3        (only the year known)
    Coast To Coast AM/Coast To Coast AM - 2009-03-14 - Part 1.mp3

The date is year-month-day, so files list in air-date order anywhere, and
only the parts that are known are written (never invented). Episodes with
no series go in "Unsorted". A file needs at least a date or a title to be
renamed; otherwise it's left alone.

An exception to "never change files in the library" (owner's choice,
2026-11-10): only when asked, after a preview listing every file's old and
new name. The file itself isn't changed, only its name and folder. An
existing file is never replaced: a name that's taken gets a number.

Every move is written down first (``Renamed``), so a batch can be put back,
and so a restart halfway through can be tidied up. The app's record of each
file moves with it, so details, listening, artwork and feeds carry on.
Folder pictures (cover.jpg...) move along when every recording left their
folder for the same new one, and folders left empty are removed.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app import convert
from app.artwork import folder_image_rank
from app.db import AudioFile, Episode, LibraryConversion, RenameBatch, Renamed, utcnow
from app.inbox import MAX_NAME, NO_SERIES_FOLDER
from app.scanner import AUDIO_EXTENSIONS, LIBRARY_LOCK

log = logging.getLogger(__name__)

SEPARATOR = " - "
# The most files the preview lists; the rest are counted.
SHOWN = 300

# Characters some computers can't have in names, and what they become.
_REPLACE = [(re.compile(r"[\x00-\x1f]"), ""), (re.compile(r"\s*:\s*"), " - "),
            (re.compile(r"[/\\|]"), "-"), (re.compile(r'[?*"<>]'), ""),
            (re.compile(r"\s+"), " ")]


def clean(text: str | None) -> str:
    """Text made safe for a file or folder name on Windows, Mac and Linux."""
    text = text or ""
    for pattern, replacement in _REPLACE:
        text = pattern.sub(replacement, text)
    return text.strip().strip(".").strip()


def date_text(episode: Episode) -> str:
    """The air date as far as it's known: 1943-05-13, 1943-05 or 1943."""
    if not episode.air_year:
        return ""
    text = f"{episode.air_year:04d}"
    if episode.air_month:
        text += f"-{episode.air_month:02d}"
        if episode.air_day:
            text += f"-{episode.air_day:02d}"
    return text


def folder_for(episode: Episode) -> str:
    return clean(episode.series)[:MAX_NAME].strip() or NO_SERIES_FOLDER


def name_for(f: AudioFile, episode: Episode) -> str | None:
    """The file's new name, or None if the episode hasn't enough details
    (a date or a title) to name it by."""
    date, title = date_text(episode), clean(episode.title)
    if not date and not title:
        return None
    suffix = Path(f.name).suffix.lower()
    before = [clean(episode.series), date]
    after = [f"Part {f.part_number}"] if f.part_number and len(episode.files) > 1 else []
    others = [p for p in [*before, *after] if p]
    room = MAX_NAME - len(suffix) - len(SEPARATOR.join(others)) - (len(SEPARATOR) if others else 0)
    if title and room < len(title):
        title = title[:max(room, 10)].rstrip(" .-")
    return SEPARATOR.join(p for p in [*before, title, *after] if p) + suffix


@dataclass
class Move:
    file: AudioFile
    episode: Episode
    new_folder: str = ""
    new_name: str = ""
    numbered: bool = False          # a number was added: the name was taken
    problem: str | None = None      # it will be left alone, because...
    no_details: bool = False        # not enough details to name it by

    @property
    def old_path(self) -> str:
        return path_text(self.file.folder, self.file.name)

    @property
    def new_path(self) -> str:
        return path_text(self.new_folder, self.new_name)

    @property
    def up_to_date(self) -> bool:
        return self.problem is None and not self.no_details and self.old_path == self.new_path

    @property
    def to_do(self) -> bool:
        return self.problem is None and not self.no_details and not self.up_to_date


def path_text(folder: str, name: str) -> str:
    return f"{folder}/{name}" if folder else name


def plan(session: Session, episodes: list[Episode], library: Path) -> list[Move]:
    """Where each of these episodes' files would go. Nothing is moved."""
    moves = [Move(file=f, episode=e) for e in episodes for f in e.parts]
    converting = set(session.scalars(select(LibraryConversion.file_id).where(
        LibraryConversion.status.in_(["waiting", "converting"]))))
    for move in moves:
        f = move.file
        if f.missing or not (library / f.folder / f.name).is_file():
            move.problem = "The file isn't in the library any more."
        elif f.id in converting:
            move.problem = "It's waiting to be converted. Rename it once that's done."
        elif (name := name_for(f, move.episode)) is None:
            move.no_details = True
        else:
            move.new_folder, move.new_name = folder_for(move.episode), name

    # Names that are taken: by files on the disk, by other recordings the
    # app knows (even missing ones), or by another file in this batch.
    wanted = [m for m in moves if m.new_name]
    known: set = set()
    folders = sorted({m.new_folder for m in wanted})
    for start_at in range(0, len(folders), 500):              # (in chunks: a big library)
        known.update(session.execute(select(AudioFile.folder, AudioFile.name).where(
            AudioFile.folder.in_(folders[start_at:start_at + 500]))).all())
    claimed: set[str] = set()
    for move in wanted:
        if move.old_path == move.new_path:
            claimed.add(move.new_path.lower())
    for move in wanted:
        if move.old_path == move.new_path:
            continue
        stem, suffix = Path(move.new_name).stem, Path(move.new_name).suffix
        name, number = move.new_name, 1
        while _taken(library, move, name, known, claimed):
            number += 1
            name = f"{stem} ({number}){suffix}"
        move.new_name, move.numbered = name, number > 1
        claimed.add(move.new_path.lower())
    return moves


def _taken(library: Path, move: Move, name: str, known: set, claimed: set) -> bool:
    if path_text(move.new_folder, name).lower() in claimed:
        return True
    target = library / move.new_folder / name
    if path_text(move.new_folder, name) == move.old_path:
        return False
    if os.path.lexists(target):
        source = library / move.file.folder / move.file.name
        # (Only the capitals change, on a disk that ignores them: it's itself.)
        return not (source.exists() and os.path.samefile(source, target))
    return (move.new_folder, name) in known


# ------------------------------------------------------------- doing it

@dataclass
class Result:
    batch_id: int | None = None
    total: int = 0                  # files to rename
    checked: int = 0                # of those, how many have been tried
    stop: bool = False              # asked to stop (a background run)
    finished: bool = False
    moved: int = 0
    pictures: int = 0
    folders_removed: int = 0
    problems: list[tuple[str, str]] = field(default_factory=list)     # path, what


class RenameError(Exception):
    """One file couldn't be moved: it's left where it was."""


def rename(session: Session, moves: list[Move], library: Path, label: str,
           result: Result | None = None) -> Result:
    """Move the files, writing each move down first so the batch can be
    put back. Then move folder pictures along and remove emptied folders.
    ``result`` (for a background run) shows the progress as it goes, and
    stops it between files when its ``stop`` is set."""
    result = result or Result()
    to_do = [m for m in moves if m.to_do]
    result.total = len(to_do)
    for move in moves:
        if move.problem:
            result.problems.append((move.old_path, move.problem))
    if not to_do:
        return result
    batch = RenameBatch(label=label[:200], made_at=utcnow())
    session.add(batch)
    session.commit()
    result.batch_id = batch.id
    for move in to_do:
        if result.stop:
            break
        f = move.file
        result.checked += 1
        try:
            _move(session, batch, library, f.folder, f.name, move.new_folder, move.new_name, f)
            result.moved += 1
        except RenameError as error:
            result.problems.append((move.old_path, str(error)))
    left: dict[str, set[str]] = {}            # old folder -> the new folders its files went to
    for record in batch.moves:
        if record.done and record.old_folder != record.new_folder:
            left.setdefault(record.old_folder, set()).add(record.new_folder)
    with LIBRARY_LOCK:
        result.pictures = _move_pictures(session, batch, library, left)
        result.folders_removed = remove_empty(library, left)
    return result


def _move(session: Session, batch: RenameBatch, library: Path, old_folder: str, old_name: str,
          new_folder: str, new_name: str, f: AudioFile | None) -> None:
    """Move one file (a recording ``f``, or a picture), safely."""
    source, target = library / old_folder / old_name, library / new_folder / new_name
    with LIBRARY_LOCK:
        if not source.is_file():
            raise RenameError("The file isn't in the library any more.")
        if os.path.lexists(target) and not os.path.samefile(source, target):
            raise RenameError(f"There's already a file called {new_name} there.")
        record = Renamed(batch=batch, file_id=f.id if f else None, old_folder=old_folder,
                         old_name=old_name, new_folder=new_folder, new_name=new_name)
        session.add(record)
        session.commit()                       # written down before anything moves
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            os.rename(source, target)
        except OSError as error:
            session.delete(record)
            session.commit()
            raise RenameError(f"It couldn't be moved ({error.strerror or error}).") from None
        if f is not None:
            f.folder, f.name = new_folder, new_name
        record.done = True
        session.commit()


def _move_pictures(session: Session, batch: RenameBatch, library: Path,
                   left: dict[str, set[str]]) -> int:
    """Folder pictures go with the recordings: when a folder has no
    recordings left and they all went to the same new folder."""
    moved = 0
    for folder, destinations in left.items():
        path = library / folder
        if len(destinations) != 1 or not path.is_dir() or _has_recordings(path):
            continue
        destination = next(iter(destinations))
        for entry in sorted(path.iterdir()):
            if entry.is_file() and folder_image_rank(entry.name) is not None \
                    and not (library / destination / entry.name).exists():
                try:
                    _move(session, batch, library, folder, entry.name, destination, entry.name, None)
                    moved += 1
                except RenameError:
                    pass
    return moved


def _has_recordings(path: Path) -> bool:
    for entry in path.iterdir():
        if entry.is_dir():
            return True                        # (a folder inside: leave it all be)
        suffix = entry.suffix.lower()
        if suffix in AUDIO_EXTENSIONS or suffix in convert.CONVERT_EXTENSIONS:
            return True
    return False


def remove_empty(library: Path, folders) -> int:
    """Remove these folders (and the folders above them) if they're now
    completely empty. Never the library itself."""
    removed = 0
    for folder in sorted(folders, key=lambda f: -f.count("/")):
        while folder:
            path = library / folder
            try:
                if not path.is_dir() or any(path.iterdir()):
                    break
                path.rmdir()
                removed += 1
            except OSError:
                break
            folder = folder.rpartition("/")[0]
    return removed


# ------------------------------------------------------- the whole library

_job: Result | None = None
_job_lock = threading.Lock()


def current_job() -> Result | None:
    return _job


def busy() -> bool:
    return _job is not None and not _job.finished


def start(engine: Engine, library: Path, episode_ids: list[int], label: str,
          afterwards: Callable[[], object] | None = None) -> bool:
    """Rename these episodes' files in the background (the Advanced page's
    whole-library run). False if a run is still going. ``afterwards`` is
    called when it's done (a scan, to pick up moved folder pictures)."""
    global _job
    with _job_lock:
        if busy():
            return False
        _job = Result()
        threading.Thread(target=_run, args=(_job, engine, library, episode_ids, label, afterwards),
                         daemon=True, name="airwave-renaming").start()
        return True


def stop() -> None:
    if _job is not None:
        _job.stop = True


def _run(job: Result, engine: Engine, library: Path, episode_ids: list[int], label: str,
         afterwards) -> None:
    try:
        with Session(engine) as session:
            episodes = []
            for start_at in range(0, len(episode_ids), 500):
                episodes += session.scalars(select(Episode).where(
                    Episode.id.in_(episode_ids[start_at:start_at + 500]))).all()
            moves = plan(session, episodes, library)
            rename(session, moves, library, label, job)
        if afterwards is not None and (job.pictures or job.folders_removed):
            afterwards()
    except Exception as error:                       # (never leave the run "going")
        log.exception("Renaming stopped")
        job.problems.append(("", f"Renaming stopped because of an error: {error}"))
    finally:
        job.finished = True


# ------------------------------------------------------------ past renames

# The past renames page lists this many, newest first.
HISTORY = 100


@dataclass
class Summary:
    batch: RenameBatch
    files: int              # recordings that moved
    pictures: int
    put_back: int           # recordings and pictures already put back
    moves: list[Renamed]

    @property
    def can_put_back(self) -> bool:
        return any(not m.undone for m in self.moves)


def summary(batch: RenameBatch) -> Summary:
    moves = [m for m in batch.moves if m.done]
    return Summary(batch=batch, moves=moves,
                   files=sum(1 for m in moves if m.file_id is not None or not _is_picture(m)),
                   pictures=sum(1 for m in moves if m.file_id is None and _is_picture(m)),
                   put_back=sum(1 for m in moves if m.undone))


def _is_picture(move: Renamed) -> bool:
    return folder_image_rank(move.old_name) is not None


# ------------------------------------------------------------ putting back

def put_back(session: Session, batch: RenameBatch, library: Path) -> Result:
    """Move a batch's files back where they were (newest first). A file
    that has moved again since, or whose old name is taken now, stays."""
    result = Result(batch_id=batch.id)
    emptied: set[str] = set()
    for record in reversed(batch.moves):
        if not record.done or record.undone:
            continue
        source = library / record.new_folder / record.new_name
        target = library / record.old_folder / record.old_name
        shown = path_text(record.new_folder, record.new_name)
        with LIBRARY_LOCK:
            f = session.get(AudioFile, record.file_id) if record.file_id else None
            if not source.is_file() or (f is not None and (f.folder, f.name) != (
                    record.new_folder, record.new_name)):
                result.problems.append((shown, "It has been moved or renamed again since."))
                continue
            if os.path.lexists(target) and not os.path.samefile(source, target):
                result.problems.append((shown, f"There's a file called {record.old_name} "
                                               "where it was, so it was left as it is."))
                continue
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                os.rename(source, target)
            except OSError as error:
                result.problems.append((shown, f"It couldn't be moved ({error.strerror or error})."))
                continue
            if f is not None:
                f.folder, f.name = record.old_folder, record.old_name
                result.moved += 1
            else:
                result.pictures += 1
            record.undone = True
            session.commit()
            emptied.add(record.new_folder)
    if not any(r.done and not r.undone for r in batch.moves):
        batch.undone_at = utcnow()
        session.commit()
    with LIBRARY_LOCK:
        result.folders_removed = remove_empty(library, emptied)
    return result


def finish_interrupted(session: Session, library: Path) -> int:
    """After a restart: a move that was written down but not marked done
    either happened (the file is at its new place: bring the app's record
    up to date) or didn't (forget it). Returns how many were tidied."""
    tidied = 0
    for record in session.scalars(select(Renamed).where(Renamed.done.is_(False))):
        new = library / record.new_folder / record.new_name
        old = library / record.old_folder / record.old_name
        if new.is_file() and not old.exists():
            f = session.get(AudioFile, record.file_id) if record.file_id else None
            if f is not None and (f.folder, f.name) == (record.old_folder, record.old_name):
                f.folder, f.name = record.new_folder, record.new_name
            record.done = True
        else:
            session.delete(record)
        tidied += 1
    session.commit()
    return tidied
