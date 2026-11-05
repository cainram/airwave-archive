"""The inbox: a holding folder for new recordings, separate from the
library.

Files arrive by uploading them in the app (or by copying them into the
inbox folder). The inbox page shows what the app worked out for each one
(series, episode number, title, air date) so the owner can fix it, then
add the file to the library or reject it (which deletes it from the
inbox). Nothing in the library is touched until a file is added.

Adding a file moves it into the library: next to the series' other
recordings if there are any, otherwise into a new folder named after the
series. Its details are saved on its episode as if typed by hand.

Nothing is ever overwritten: if a name is taken, a number is added.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import AudioFile, Episode, InboxItem, utcnow
from app.episodes import _clean, apply_confirmed, date_from_tag
from app.filenames import parse_filename
from app.scanner import AUDIO_EXTENSIONS, add_one_file, read_audio

UNSAFE = re.compile(r'[\x00-\x1f/\\:*?"<>|]')
MAX_NAME = 180
NO_SERIES_FOLDER = "Unsorted"


def is_audio(name: str) -> bool:
    return Path(name).suffix.lower() in AUDIO_EXTENSIONS


def safe_name(name: str) -> str | None:
    """A file name that's safe to save: no folders, no characters that
    some computers can't use, not hidden. None if it isn't an audio file."""
    name = UNSAFE.sub("_", Path(name.replace("\\", "/")).name).strip().lstrip(".")
    if not name or not is_audio(name):
        return None
    stem, suffix = Path(name).stem, Path(name).suffix
    return stem[:MAX_NAME - len(suffix)].strip() + suffix.lower()


def free_name(folder: Path, name: str) -> str:
    """``name``, or "name (2).mp3", "name (3).mp3"... if it's taken."""
    stem, suffix = Path(name).stem, Path(name).suffix
    candidate, number = name, 1
    while (folder / candidate).exists():
        number += 1
        candidate = f"{stem} ({number}){suffix}"
    return candidate


def save_upload(source, folder: Path, name: str) -> str:
    """Copy an uploaded file (a readable file object) into ``folder`` under
    a free name. It's written to a hidden temporary name first, so a
    half-finished upload never looks like a real file. Returns the name."""
    folder.mkdir(parents=True, exist_ok=True)
    temporary = folder / f".upload-{secrets.token_hex(6)}"
    with open(temporary, "wb") as out:
        shutil.copyfileobj(source, out, 1024 * 1024)
    final = free_name(folder, name)
    temporary.rename(folder / final)
    return final


def _known_parts(date: tuple) -> int:
    return sum(part is not None for part in date)


def guess_details(path: Path) -> dict:
    """What the file's tags and name say: series, episode number, title and
    air date. Tags come first; the name fills in what they lack, and gives
    the date if it says more (tags often have only the year)."""
    info = read_audio(path)
    tags = info.tags
    guess = parse_filename(path.name)
    from_tags = (date_from_tag(tags.get("date")))
    from_name = (guess.year, guess.month, guess.day) if guess else (None, None, None)
    date = from_name if _known_parts(from_name) > _known_parts(from_tags) else from_tags
    return {
        "series": _clean(tags.get("artist")) or _clean(tags.get("album")) or (guess and guess.series),
        "episode_number": guess.episode_number if guess else None,
        "title": _clean(tags.get("title")) or (guess and guess.title),
        "air_year": date[0], "air_month": date[1], "air_day": date[2],
        "duration_seconds": info.duration_seconds,
    }


def inbox_files(folder: Path) -> dict[str, Path]:
    """The audio files waiting in the inbox folder (not hidden ones, which
    includes uploads still arriving)."""
    if not folder.is_dir():
        return {}
    return {p.name: p for p in folder.iterdir()
            if p.is_file() and not p.name.startswith(".") and is_audio(p.name)}


def sync_inbox(session: Session, folder: Path) -> list[InboxItem]:
    """Bring the inbox list in step with the folder: files that appeared
    are read and get a row; rows whose file has gone are dropped. Returns
    the items, in name order."""
    files = inbox_files(folder)
    rows = {row.name: row for row in session.scalars(select(InboxItem))}
    for name, row in rows.items():
        if name not in files:
            session.delete(row)
    for name, path in files.items():
        if name not in rows:
            details = guess_details(path)
            session.add(InboxItem(name=name, size_bytes=path.stat().st_size,
                                  added_at=utcnow(), **details))
    session.commit()
    return sorted(session.scalars(select(InboxItem)), key=lambda r: r.name.lower())


def item_path(folder: Path, item: InboxItem) -> Path | None:
    """The file of an inbox item, if it's (still) there, and only inside
    the inbox folder."""
    path = folder / item.name
    if path.parent.resolve() != folder.resolve() or not path.is_file():
        return None
    return path


def reject(session: Session, folder: Path, item: InboxItem) -> None:
    """Delete a file from the inbox (never from the library)."""
    path = item_path(folder, item)
    if path is not None:
        path.unlink()
    session.delete(item)


# ------------------------------------------------------- adding to the library

def folder_name(series: str | None) -> str:
    """A folder name made from a series name ("Unsorted" if there's none)."""
    name = UNSAFE.sub("_", series or "").strip().strip(".").strip()
    return name[:MAX_NAME] or NO_SERIES_FOLDER


def target_folder(session: Session, series: str | None) -> str:
    """Where in the library a file of this series goes (relative to the
    library): the folder holding most of the series' recordings, or else a
    folder named after the series."""
    if series:
        folder = session.scalar(
            select(AudioFile.folder).join(Episode)
            .where(Episode.series == series, AudioFile.missing.is_(False))
            .group_by(AudioFile.folder).order_by(func.count().desc(), AudioFile.folder)
            .limit(1))
        if folder is not None:
            return folder
    return folder_name(series)


def move_into(source: Path, folder: Path) -> Path:
    """Move a file into ``folder`` under a free name (never replacing
    anything). Across disks it's copied under a hidden name first, so a
    half-copied file never looks like a real recording."""
    folder.mkdir(parents=True, exist_ok=True)
    final = folder / free_name(folder, source.name)
    try:
        os.rename(source, final)                    # same disk: instant
        return final
    except OSError:
        pass
    temporary = folder / f".adding-{secrets.token_hex(6)}"
    try:
        shutil.copyfile(source, temporary)
        final = folder / free_name(folder, source.name)
        temporary.rename(final)
    finally:
        temporary.unlink(missing_ok=True)
    source.unlink()
    return final


def add_to_library(session: Session, inbox_folder: Path, library: Path,
                   item: InboxItem, art_dir: Path | None = None) -> AudioFile | None:
    """Move an inbox file into the library and add it, with the details
    confirmed in the inbox. None if the file isn't in the inbox any more."""
    source = item_path(inbox_folder, item)
    if source is None:
        return None
    folder = library / target_folder(session, item.series)
    if library.resolve() not in (folder.resolve(), *folder.resolve().parents):
        raise ValueError("That folder isn't inside the library")
    path = move_into(source, folder)
    row = add_one_file(session, library, path, art_dir)
    date = (item.air_year, item.air_month, item.air_day)
    apply_confirmed(row.episode, {
        "series": item.series, "episode_number": item.episode_number, "title": item.title,
        "date": date if item.air_year is not None else None,
    })
    session.delete(item)
    return row
