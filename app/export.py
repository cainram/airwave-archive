"""Exporting episodes as a zip file, to share with someone else who runs
Airwave Archive (or to use in another player).

The zip holds COPIES of the recordings, with the episode details written
into their tags (see app/tagging.py), a folder per series, a manifest
(manifest.json) listing every episode and its details, the series
pictures and genres, and a short read-me. The library's own files are never changed.

The zip is made while it downloads, one recording at a time: each is
copied to a temporary file, tagged, sent, and deleted. So even a very big
export needs only one recording's worth of spare disk space. Audio is
already compressed, so files are stored in the zip as they are.
"""

from __future__ import annotations

import io
import json
import shutil
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from sqlalchemy.orm import Session

from app.db import Episode
from app.filenames import date_text
from app.genres import genres_of
from app.inbox import folder_name
from app.series import one_series
from app.tagging import write_tags

FORMAT = "airwave-archive-export"
VERSION = 1
CHUNK = 1024 * 1024
README = """\
This zip was made by Airwave Archive, a self-hosted app for collections of
radio programs.

- Each folder holds one series' recordings. They are copies, with the
  episode details (and the series' genres) written into their tags, so
  most players show them.
- manifest.json lists every episode and all its details (air date, time,
  station, cast, notes...), for another Airwave Archive server to import.
- pictures/ holds the series pictures.

To add it to another Airwave Archive, upload the whole zip in its Inbox.
"""


@dataclass
class Item:
    """One recording to put in the zip."""
    source: Path          # the library file (only read, never changed)
    path: str             # where it goes in the zip
    episode: Episode      # (detached from the database)
    part: int | None
    parts: int
    size: int


@dataclass
class Plan:
    items: list[Item]
    episodes: list[Episode]
    pictures: dict[str, Path]      # series name -> picture file
    missing: int                   # files that couldn't be included (missing)
    genres: dict[str, list[str]] = field(default_factory=dict)   # series name -> its genres

    @property
    def size(self) -> int:
        return sum(item.size for item in self.items) + sum(
            p.stat().st_size for p in self.pictures.values() if p.is_file())


def give_ids(episodes: list[Episode]) -> None:
    """Make sure each episode has its permanent ID."""
    for episode in episodes:
        if not episode.uid:
            episode.uid = uuid.uuid4().hex


def make_plan(session: Session, episodes: list[Episode], library: Path,
              picture_path) -> Plan:
    """Work out what goes where in the zip. ``picture_path`` turns a
    picture's fingerprint into its file. Call ``give_ids`` (and save)
    first."""
    items: list[Item] = []
    used: set[str] = set()
    missing = 0
    pictures: dict[str, Path] = {}
    for episode in episodes:
        parts = episode.parts
        folder = folder_name(episode.series)
        for f in parts:
            source = library / f.folder / f.name
            if f.missing or not source.is_file():
                missing += 1
                continue
            path = _free_path(used, f"{folder}/{f.name}")
            items.append(Item(source, path, episode, f.part_number, len(parts), f.size_bytes))
        if episode.series and episode.series not in pictures:
            found = one_series(session, episode.series)
            art = picture_path(found["art"]) if found and found.get("art") else None
            if art is not None:
                pictures[episode.series] = art
    names = sorted({e.series for e in episodes if e.series})
    return Plan(items, episodes, pictures, missing, genres_of(session, names))


def _free_path(used: set[str], path: str) -> str:
    stem, dot, suffix = path.rpartition(".")
    candidate, number = path, 1
    while candidate.lower() in used:
        number += 1
        candidate = f"{stem} ({number}).{suffix}" if dot else f"{path} ({number})"
    used.add(candidate.lower())
    return candidate


def manifest(plan: Plan, picture_names: dict[str, str]) -> dict:
    """The list of every episode and its details, for the zip."""
    episodes = []
    by_episode: dict[int, list[Item]] = {}
    for item in plan.items:
        by_episode.setdefault(item.episode.id, []).append(item)
    for episode in plan.episodes:
        files = by_episode.get(episode.id)
        if not files:
            continue
        episodes.append({
            "id": episode.uid,
            "series": episode.series,
            "episode_number": episode.episode_number,
            "title": episode.title,
            "air_date": date_text(episode.air_year, episode.air_month, episode.air_day) or None,
            "air_time": episode.air_time,
            "time_zone": episode.air_timezone,
            "network": episode.network,
            "host": episode.host,
            "cast": episode.cast,
            "notes": episode.notes,
            "files": [{"path": item.path, "part": item.part, "size": item.size,
                       "original_name": item.source.name} for item in files],
        })
    return {
        "format": FORMAT,
        "version": VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "episodes": episodes,
        "series": [{"name": name, "picture": picture_names.get(name),
                    "genres": plan.genres.get(name, [])}
                   for name in sorted(set(picture_names) | set(plan.genres))],
    }


class _Pipe(io.RawIOBase):
    """Somewhere for the zip to write to, emptied as it's sent."""

    def __init__(self):
        self.chunks: list[bytes] = []

    def writable(self) -> bool:
        return True

    def write(self, data) -> int:
        self.chunks.append(bytes(data))
        return len(data)

    def take(self) -> Iterator[bytes]:
        chunks, self.chunks = self.chunks, []
        yield from chunks


def stream_zip(plan: Plan, work_dir: Path) -> Iterator[bytes]:
    """The zip file, piece by piece, made while it's sent. ``work_dir``
    holds the one temporary copy being tagged."""
    work_dir.mkdir(parents=True, exist_ok=True)
    pipe = _Pipe()
    picture_names = {name: f"pictures/{folder_name(name)}.jpg" for name in plan.pictures}
    temporary = work_dir / f"export-{uuid.uuid4().hex}"
    try:
        with zipfile.ZipFile(pipe, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
            zf.writestr("README.txt", README)
            yield from pipe.take()
            for item in plan.items:
                copy = temporary.with_suffix(item.source.suffix.lower())
                shutil.copyfile(item.source, copy)
                try:
                    write_tags(copy, item.episode, item.part, item.parts,
                               plan.genres.get(item.episode.series))
                except Exception:
                    shutil.copyfile(item.source, copy)   # untagged: the manifest has it all
                info = zipfile.ZipInfo.from_file(copy, item.path)
                info.compress_type = zipfile.ZIP_STORED
                with open(copy, "rb") as source, zf.open(info, "w") as target:
                    while chunk := source.read(CHUNK):
                        target.write(chunk)
                        yield from pipe.take()
                copy.unlink()
                yield from pipe.take()
            for name, picture in plan.pictures.items():
                zf.write(picture, picture_names[name])
                yield from pipe.take()
            zf.writestr("manifest.json", json.dumps(manifest(plan, picture_names),
                                                    indent=2, ensure_ascii=False))
        yield from pipe.take()
    finally:
        for leftover in work_dir.glob(temporary.name + "*"):
            leftover.unlink(missing_ok=True)


def zip_name(series: str | None, count: int) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    what = folder_name(series) if series else f"{count} episode{'' if count == 1 else 's'}"
    return f"Airwave - {what} - {today}.zip"
