"""Writing the app's details into the library's own files (Phase 10a).

An exception to "never change files in the library" (owner's choice,
2026-11-09): only when asked, after a preview page listing every file and
each tag that will change. Only the tags change; the sound is never
touched.

The tags are the same ones exports write into their copies
(``app/tagging.py``): the usual ones every player shows, and the Airwave
tags another Airwave Archive server reads back.

Each file is done safely: the tags are written into a hidden copy beside
it, the copy is checked (it opens, and is as long as the original), and
only then does it take the original's place. If anything goes wrong, the
original is left exactly as it was. The app's record of the file is
updated in the same step, so the next scan doesn't see a changed file
(and can't undo any edits).
"""

from __future__ import annotations

import os
import secrets
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import mutagen

from app.db import AudioFile, Episode
from app.scanner import LIBRARY_LOCK, _apply, read_audio
from app.tagging import PREFIX, airwave_values, common_values, read_airwave_tags, write_tags

# Hidden copies being written are named like this (scans skip hidden files).
TEMPORARY = ".tagging-"

# The usual tags, as the preview names them.
COMMON_LABELS = {"title": "Title", "artist": "Artist", "album": "Album", "date": "Date",
                 "comment": "Comment", "track": "Track"}
# The Airwave tags, as the preview names them.
AIRWAVE_LABELS = {
    "ID": "Permanent ID", "SERIES": "Series", "EPISODE_NUMBER": "Episode number",
    "TITLE": "Title", "AIR_DATE": "Air date", "AIR_TIME": "Air time",
    "TIME_ZONE": "Time zone", "NETWORK": "Station / network", "HOST": "Host",
    "CAST": "Cast", "NOTES": "Notes", "PART": "Part",
}
NEW_ID = "(a new permanent ID)"


class TagError(Exception):
    """Writing one file's tags didn't work: the file is left as it was."""


@dataclass
class Change:
    label: str
    now: str | None          # None: not there yet
    will_be: str | None      # None: taken out


@dataclass
class FilePlan:
    file: AudioFile
    episode: Episode
    changes: list[Change] = field(default_factory=list)          # the usual tags
    airwave: list[Change] = field(default_factory=list)          # Airwave tags
    problem: str | None = None
    no_details: bool = False       # the episode has no details to write yet

    @property
    def up_to_date(self) -> bool:
        return self.problem is None and (self.no_details or (not self.changes and not self.airwave))


def path_of(f: AudioFile, library: Path) -> Path:
    return library / f.folder / f.name


def part_of(f: AudioFile, episode: Episode) -> tuple[int | None, int]:
    parts = episode.parts
    return f.part_number, len(parts)


def plan_file(f: AudioFile, episode: Episode, library: Path) -> FilePlan:
    """What writing the episode's details would change in this file."""
    plan = FilePlan(file=f, episode=episode)
    path = path_of(f, library)
    if f.missing or not path.is_file():
        plan.problem = "The file isn't in the library any more."
        return plan
    try:
        audio = mutagen.File(path)
    except Exception:
        audio = None
    if audio is None:
        plan.problem = "This kind of file can't hold tags."
        return plan
    part, parts = part_of(f, episode)
    common = common_values(episode, part, parts)
    wanted = {key[len(PREFIX):]: value for key, value in airwave_values(episode, part, parts).items()}
    if not common and not set(wanted) - {"ID"}:
        plan.no_details = True       # (just an ID would be no use to anyone)
        return plan
    current = read_audio(path).tags
    for key, value in common.items():
        if (current.get(key) or "") != value:
            plan.changes.append(Change(COMMON_LABELS[key], current.get(key) or None, value))
    found = read_airwave_tags(path)
    if not episode.uid:
        wanted["ID"] = NEW_ID
    for key in [*AIRWAVE_LABELS, *sorted(set(found) - set(AIRWAVE_LABELS))]:
        now, will_be = found.get(key) or None, wanted.get(key)
        if now != will_be:
            plan.airwave.append(Change(AIRWAVE_LABELS.get(key, key.title()), now, will_be))
    return plan


def plan_episodes(episodes: list[Episode], library: Path) -> list[FilePlan]:
    return [plan_file(f, episode, library) for episode in episodes for f in episode.parts]


def write_file(f: AudioFile, episode: Episode, library: Path) -> None:
    """Write the episode's details into one library file, safely (see the
    top of this file), and bring the app's record of it up to date.
    Raises TagError, leaving the file as it was, if that can't be done."""
    path = path_of(f, library)
    part, parts = part_of(f, episode)
    with LIBRARY_LOCK:
        try:
            before = path.stat()
        except OSError:
            raise TagError("The file isn't in the library any more.") from None
        length = read_audio(path).duration_seconds
        temporary = path.with_name(f"{TEMPORARY}{secrets.token_hex(4)}{path.suffix}")
        try:
            try:
                shutil.copy2(path, temporary)
                written = write_tags(temporary, episode, part, parts)
            except OSError as error:
                raise TagError(f"The tags couldn't be written ({error.strerror or error}).") from None
            except Exception:
                raise TagError("The tags couldn't be written: the file may be damaged.") from None
            if not written:
                raise TagError("This kind of file can't hold tags.")
            after = read_audio(temporary).duration_seconds
            if length and (after is None or abs(after - length) > 1.0):
                raise TagError("The file didn't read back properly after writing, so it was left as it was.")
            now = path.stat()
            if (now.st_size, now.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
                raise TagError("The file changed while its tags were being written, so it was left as it was.")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        _apply(f, read_audio(path), path.stat())


def clear_leftovers(library: Path) -> None:
    """After a restart: remove hidden copies a stopped write left behind."""
    if not library.is_dir():
        return
    for folder, _dirs, files in os.walk(library):
        for name in files:
            if name.startswith(TEMPORARY):
                try:
                    (Path(folder) / name).unlink()
                except OSError:
                    pass
