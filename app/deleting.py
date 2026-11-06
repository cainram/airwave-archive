"""Deleting recordings from the library, for good.

The library is normally only read (and added to from the inbox). Deleting
is the one exception, the owner's choice (2026-11-06; they keep backups
and snapshots): always an explicit action, after a page that lists exactly
which files will go, and only for files inside the library.

An episode is deleted whole: all its parts. Its files are removed from the
disk, then the episode and everything about it (details, where listening
stopped) is removed from the database. Folders are left as they are, even
if they end up empty.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session

from app.db import AudioFile, Episode
from app.scanner import forget_files


@dataclass
class Plan:
    """What deleting some episodes would remove."""
    episodes: list[Episode]
    files: list[AudioFile] = field(default_factory=list)     # on the disk now
    missing: list[AudioFile] = field(default_factory=list)   # already gone

    @property
    def size(self) -> int:
        return sum(f.size_bytes or 0 for f in self.files)


@dataclass
class Result:
    deleted: int = 0          # files removed from the disk
    freed: int = 0            # bytes
    forgotten: int = 0        # episodes removed from the app
    problems: list[str] = field(default_factory=list)


def file_path(library: Path, f: AudioFile) -> Path | None:
    """Where a file of the library is on the disk, or None if that would
    be outside the library (so it's never touched)."""
    root = library.resolve()
    folder = (library / f.folder).resolve()
    if "/" in f.name or "\\" in f.name or f.name in ("", ".", ".."):
        return None
    if folder != root and root not in folder.parents:
        return None
    return folder / f.name


def make_plan(episodes: list[Episode], library: Path) -> Plan:
    plan = Plan(episodes)
    for episode in episodes:
        for f in episode.parts:
            path = file_path(library, f)
            if path is not None and (path.is_file() or path.is_symlink()):
                plan.files.append(f)
            else:
                plan.missing.append(f)
    return plan


def delete_episodes(session: Session, episodes: list[Episode], library: Path) -> Result:
    """Delete these episodes' files from the disk, and the episodes from
    the app. A file that can't be deleted is left (with its episode), and
    the reason is given."""
    result = Result()
    forget: list[int] = []
    for episode in episodes:
        all_gone = True
        for f in episode.parts:
            path = file_path(library, f)
            if path is None:
                result.problems.append(f"{f.name}: it isn't inside the library, so it was left alone.")
                all_gone = False
                continue
            try:
                if path.is_file() or path.is_symlink():
                    path.unlink()
                    result.deleted += 1
                    result.freed += f.size_bytes or 0
            except OSError as error:
                result.problems.append(f"{f.name}: {error.strerror or error}.")
                all_gone = False
                continue
            forget.append(f.id)
        if all_gone:
            result.forgotten += 1
    forget_files(session, forget)
    return result
