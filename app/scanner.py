"""Find audio files in the library folder, read facts and tags from them,
and keep the database in step with what's on disk.

This module only *reads* library files. It never writes, moves, or renames
anything in the library.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

import mutagen
from mutagen.id3 import ID3, Frame
from sqlalchemy import Engine, delete, exists, select
from sqlalchemy.orm import Session

from app.db import AudioFile, Episode, ScanRun, utcnow
from app.episodes import fill_from_tags

# File endings we treat as audio.
AUDIO_EXTENSIONS = {
    ".mp3", ".m4a", ".m4b", ".aac", ".flac", ".ogg", ".oga", ".opus",
    ".wav", ".wma", ".aiff", ".aif",
}

# Save to the database every this many new or changed files, so a long
# first scan isn't lost if it's interrupted.
BATCH_SIZE = 500

# Very long tag values (rare) are cut to this many characters.
MAX_TAG_LENGTH = 10_000

COMMON_TAGS = ("title", "artist", "album", "date", "genre", "track", "comment")

# Where each common tag lives in each tag format.
ID3_FRAMES = {"TIT2": "title", "TPE1": "artist", "TALB": "album",
              "TDRC": "date", "TCON": "genre", "TRCK": "track"}
OTHER_KEYS = {
    # MP4 / M4A
    "\xa9nam": "title", "\xa9ART": "artist", "\xa9alb": "album", "\xa9day": "date",
    "\xa9gen": "genre", "trkn": "track", "\xa9cmt": "comment",
    # FLAC, Ogg, Opus (Vorbis comments)
    "title": "title", "artist": "artist", "album": "album", "date": "date",
    "genre": "genre", "tracknumber": "track", "comment": "comment",
    "description": "comment",
    # WMA
    "Title": "title", "Author": "artist", "WM/AlbumTitle": "album",
    "WM/Year": "date", "WM/Genre": "genre", "WM/TrackNumber": "track",
    "Description": "comment",
}
# Tags that hold pictures or other non-text data; not shown.
SKIP_TAGS = {"APIC", "PRIV", "GEOB", "MCDI", "covr", "metadata_block_picture",
             "WM/Picture"}


@dataclass
class AudioInfo:
    """What we could read from inside one audio file."""

    duration_seconds: float | None = None
    bitrate: int | None = None
    tags: dict[str, str] = field(default_factory=dict)        # the common tags
    all_tags: list[tuple[str, str]] = field(default_factory=list)


def _text(value) -> str:
    """Turn any tag value into plain text."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (bytes, bytearray)):
        return ""
    if isinstance(value, tuple) and len(value) == 2 and all(isinstance(v, int) for v in value):
        number, total = value          # MP4 track number: (3, 10) -> "3/10"
        return f"{number}/{total}" if total else str(number)
    if isinstance(value, list):
        return "; ".join(t for t in (_text(v) for v in value) if t)
    if isinstance(value, Frame) and hasattr(value, "text"):   # ID3 text frames
        return _text(value.text)   # a list, or one piece of text (lyrics)
    if isinstance(value, Frame) and hasattr(value, "url"):    # ID3 link frames
        return str(value.url)
    if hasattr(value, "value"):         # WMA attributes
        return _text(value.value)
    return str(value).strip()


def _clip(text: str) -> str:
    return text[:MAX_TAG_LENGTH]


def _read_id3(tags: ID3, info: AudioInfo) -> None:
    for frame_id, name in ID3_FRAMES.items():
        text = _text(tags.getall(frame_id))
        if text:
            info.tags[name] = _clip(text)
    # A file can have several comments. Prefer the plain one (no description),
    # then any other except the short "ID3v1" copy.
    comments = [c for c in tags.getall("COMM") if _text(c)]
    best = (next((c for c in comments if c.desc == ""), None)
            or next((c for c in comments if c.desc != "ID3v1 Comment"), None)
            or (comments[0] if comments else None))
    if best is not None:
        info.tags["comment"] = _clip(_text(best))
    for frame in tags.values():
        if frame.FrameID in SKIP_TAGS:
            continue
        text = _text(frame)
        if text:
            info.all_tags.append((frame.HashKey[:60], _clip(text)))


def _read_other(tags, info: AudioInfo) -> None:
    for key, value in tags.items():
        if key in SKIP_TAGS or (isinstance(key, str) and key.lower() in SKIP_TAGS):
            continue
        text = _text(value)
        if not text:
            continue
        info.all_tags.append((str(key)[:60], _clip(text)))
        name = OTHER_KEYS.get(key) or OTHER_KEYS.get(str(key).lower())
        if name and name not in info.tags:
            info.tags[name] = _clip(text)


def read_audio(path: Path) -> AudioInfo:
    """Read length, bitrate, and tags from one file. Never fails: anything
    that can't be read is simply left empty."""
    info = AudioInfo()
    try:
        audio = mutagen.File(path)
    except Exception:
        return info
    if audio is None:
        return info
    if audio.info is not None:
        length = getattr(audio.info, "length", None)
        info.duration_seconds = float(length) if length else None
        bitrate = getattr(audio.info, "bitrate", None)
        info.bitrate = int(bitrate) if bitrate else None
    try:
        if isinstance(audio.tags, ID3):
            _read_id3(audio.tags, info)
        elif audio.tags is not None:
            _read_other(audio.tags, info)
    except Exception:
        pass  # odd or damaged tags shouldn't stop the scan
    return info


def find_audio_files(root: Path) -> Iterator[tuple[Path, str, os.stat_result]]:
    """Walk every folder under ``root``, yielding (path, folder, file info)
    for each audio file. ``folder`` is relative to ``root``."""
    for dirpath, dirnames, filenames in os.walk(root):
        # Skip hidden folders such as ".Trash" or ".@__thumb".
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        folder = Path(dirpath).relative_to(root).as_posix()
        folder = "" if folder == "." else folder
        for filename in sorted(filenames):
            if filename.startswith("."):
                continue
            path = Path(dirpath) / filename
            if path.suffix.lower() not in AUDIO_EXTENSIONS:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            yield path, folder, stat


def _apply(row: AudioFile, info: AudioInfo, stat: os.stat_result) -> None:
    """Copy freshly read facts onto a database row."""
    row.size_bytes = stat.st_size
    row.mtime_ns = stat.st_mtime_ns
    row.duration_seconds = info.duration_seconds
    row.bitrate = info.bitrate
    for name in COMMON_TAGS:
        setattr(row, f"tag_{name}", info.tags.get(name))
    row.tags_json = json.dumps(info.all_tags) if info.all_tags else None
    row.last_read = utcnow()


def _new_episode() -> Episode:
    now = utcnow()
    return Episode(created_at=now, updated_at=now)


def sync_library(engine: Engine, root: Path) -> ScanRun:
    """Bring the database up to date with the library folder.

    - New files are added, each with its own episode.
    - Changed files are re-read. Unchanged files aren't opened at all.
    - Files that are gone are forgotten, unless their episode has manual
      edits: then they're kept and marked "missing".
    - A missing file that comes back, at the same place or in another folder
      with the same name and size, is reconnected to its episode.
    """
    started = time.monotonic()
    run = ScanRun(started_at=utcnow(), seconds=0.0, added=0, changed=0,
                  removed=0, unchanged=0, library_missing=False)

    with Session(engine, expire_on_commit=False) as session:
        if not root.is_dir():
            # Possibly a disk that isn't mounted: change nothing.
            run.library_missing = True
        else:
            # What we knew before: path -> (id, size, modified time, missing).
            known = {
                (folder, name): (row_id, size, mtime, missing)
                for row_id, folder, name, size, mtime, missing in session.execute(
                    select(AudioFile.id, AudioFile.folder, AudioFile.name,
                           AudioFile.size_bytes, AudioFile.mtime_ns, AudioFile.missing))
            }
            found = list(find_audio_files(root))
            seen = {(folder, path.name) for path, folder, _ in found}
            gone = {key: known[key] for key in known.keys() - seen}
            # Files that are gone, by name and size, in case they turn up
            # somewhere else in the library.
            moved_from = {}
            for (_folder, name), (row_id, size, _mtime, _missing) in gone.items():
                moved_from.setdefault((name, size), row_id)
            reconnected: set[int] = set()

            pending = 0
            for path, folder, stat in found:
                before = known.get((folder, path.name))
                if before:
                    row_id, size, mtime, was_missing = before
                    if size == stat.st_size and mtime == stat.st_mtime_ns and not was_missing:
                        run.unchanged += 1
                        continue
                    row = session.get(AudioFile, row_id)
                    run.changed += 1
                elif (row_id := moved_from.pop((path.name, stat.st_size), None)) is not None:
                    row = session.get(AudioFile, row_id)   # moved to another folder
                    row.folder = folder
                    reconnected.add(row_id)
                    run.changed += 1
                else:
                    row = AudioFile(folder=folder, name=path.name, first_seen=utcnow(),
                                    episode=_new_episode())
                    session.add(row)
                    run.added += 1
                _apply(row, read_audio(path), stat)
                if row.episode is not None:
                    fill_from_tags(row.episode, row)
                row.missing = False
                row.missing_since = None
                pending += 1
                if pending >= BATCH_SIZE:
                    session.commit()
                    pending = 0

            run.removed = _handle_gone(session, [
                row_id for row_id, _size, _mtime, was_missing in gone.values()
                if row_id not in reconnected and not was_missing
            ])
            _give_files_episodes(session)

        run.seconds = time.monotonic() - started
        session.add(run)
        session.commit()
    return run


def _handle_gone(session: Session, ids: list[int]) -> int:
    """Files no longer in the library: keep the ones with edits as
    "missing", forget the rest. Returns how many there were."""
    forget: list[int] = []
    for i in range(0, len(ids), BATCH_SIZE):
        for row in session.scalars(select(AudioFile).where(AudioFile.id.in_(ids[i:i + BATCH_SIZE]))):
            if row.episode is not None and row.episode.has_manual_edits:
                row.missing = True
                row.missing_since = utcnow()
            else:
                forget.append(row.id)
    forget_files(session, forget)
    return len(ids)


def forget_files(session: Session, ids: list[int]) -> None:
    """Remove files from the database (never from the disk), along with any
    episode left with no files."""
    for i in range(0, len(ids), BATCH_SIZE):
        chunk = ids[i:i + BATCH_SIZE]
        episode_ids = list(session.scalars(
            select(AudioFile.episode_id).where(AudioFile.id.in_(chunk),
                                               AudioFile.episode_id.is_not(None))))
        session.execute(delete(AudioFile).where(AudioFile.id.in_(chunk)))
        session.execute(delete(Episode).where(
            Episode.id.in_(episode_ids),
            ~exists().where(AudioFile.episode_id == Episode.id)))


def _give_files_episodes(session: Session) -> None:
    """Every file should belong to an episode. (Files saved by an older
    version of the app don't yet.)"""
    while True:
        rows = session.scalars(
            select(AudioFile).where(AudioFile.episode_id.is_(None)).limit(BATCH_SIZE)).all()
        if not rows:
            return
        for row in rows:
            row.episode = _new_episode()
            fill_from_tags(row.episode, row)
        session.commit()


def format_size(num_bytes: int | None) -> str:
    """1536 -> '1.5 KB'."""
    size = float(num_bytes or 0)
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def format_duration(seconds: float | None) -> str:
    """3725 -> '1:02:05'; 125 -> '2:05'; long totals -> '3d 4h 5m'."""
    if seconds is None:
        return "unknown"
    total = int(round(seconds))
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_bitrate(bits_per_second: int | None) -> str:
    """64000 -> '64 kbps'."""
    return f"{round(bits_per_second / 1000)} kbps" if bits_per_second else "unknown"
