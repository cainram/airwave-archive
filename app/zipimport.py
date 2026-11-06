"""Importing a zip of recordings into the inbox, usually an export zip from
another Airwave Archive (see app/export.py).

The zip is unpacked into the inbox: each recording becomes a file waiting
there, like an upload, and the zip is deleted afterwards. Nothing goes
into the library until it's added from the inbox as usual.

An Airwave export zip has a manifest (manifest.json) listing every
episode's details, so its files arrive filled in: series, number, title
and date in the inbox's boxes, and the rest (time, station, cast, notes,
the episode's permanent ID, which part it is) kept with the file until
it's added. Series pictures in the zip are offered for series that have
no picture yet. Any other zip of recordings works too: its files are
treated like ordinary uploads (their tags and names are read).

Zips come from other people, so nothing in them is trusted: folder names
in the zip are ignored, file names are made safe, sizes are checked
against the free disk space first, and details that don't make sense are
left out.
"""

from __future__ import annotations

import json
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.artwork import MAX_PICTURE_BYTES, save_picture
from app.db import InboxItem, utcnow
from app.export import FORMAT
from app.inbox import (
    MAX_NAME, UNSAFE, guess_details, is_audio, safe_name, save_upload, with_airwave,
)
from app.scanner import format_size

MANIFEST = "manifest.json"
MAX_MANIFEST_BYTES = 50_000_000
SPARE_SPACE = 200_000_000      # left free on the disk after unpacking


class ZipProblem(Exception):
    """A zip that can't be unpacked; the message says why, in plain words."""


@dataclass
class Unpacked:
    files: int            # recordings put into the inbox
    with_details: int     # of those, how many came with Airwave details


def is_zip(name: str) -> bool:
    return PurePosixPath(name).suffix.lower() == ".zip"


def safe_zip_name(name: str) -> str | None:
    """A safe name to save an uploaded zip under, or None if it isn't one."""
    name = UNSAFE.sub("_", PurePosixPath(name.replace("\\", "/")).name).strip().lstrip(".")
    if not is_zip(name) or len(name) <= 4:
        return None
    return name[:-4][:MAX_NAME - 4].strip() + ".zip"


def inbox_zips(folder: Path) -> list[Path]:
    """Zips waiting in the inbox folder to be unpacked (copied there by
    hand: uploaded ones are unpacked straight away)."""
    if not folder.is_dir():
        return []
    return sorted((p for p in folder.iterdir()
                   if p.is_file() and not p.name.startswith(".") and is_zip(p.name)),
                  key=lambda p: p.name.lower())


def _recordings(zf: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    found = []
    for member in zf.infolist():
        path = PurePosixPath(member.filename)
        if member.is_dir() or not is_audio(path.name):
            continue
        # Leftovers some Macs add to zips ("__MACOSX/", "._name.mp3").
        if path.name.startswith(".") or "__MACOSX" in path.parts:
            continue
        found.append(member)
    return found


def read_manifest(zf: zipfile.ZipFile) -> tuple[dict[str, dict], dict[str, str]]:
    """What an Airwave export's manifest says: details by file path in the
    zip (named like Airwave tags), and picture paths by series. Empty if
    there's no manifest, or it isn't one."""
    try:
        info = zf.getinfo(MANIFEST)
        if info.file_size > MAX_MANIFEST_BYTES:
            return {}, {}
        data = json.loads(zf.read(info))
    except (KeyError, ValueError, zipfile.BadZipFile, OSError):
        return {}, {}
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        return {}, {}
    details: dict[str, dict] = {}
    for episode in data.get("episodes") or []:
        if not isinstance(episode, dict):
            continue
        values = {key.upper(): value for key, value in episode.items()
                  if key != "files" and isinstance(value, str | int)}
        files = [f for f in episode.get("files") or [] if isinstance(f, dict)]
        for f in files:
            part = f.get("part")
            these = dict(values)
            if isinstance(part, int) and len(files) > 1:
                these["PART"] = f"{part}/{len(files)}"
            if isinstance(f.get("path"), str):
                details[f["path"]] = these
    pictures = {s["name"]: s["picture"] for s in data.get("series") or []
                if isinstance(s, dict) and isinstance(s.get("name"), str)
                and isinstance(s.get("picture"), str)}
    return details, pictures


def _save_pictures(zf: zipfile.ZipFile, pictures: dict[str, str],
                   picture_dir: Path | None) -> dict[str, str]:
    """Keep the zip's series pictures (as uploaded pictures would be).
    Returns picture fingerprints by series."""
    saved = {}
    if picture_dir is None:
        return saved
    for series, path in pictures.items():
        try:
            info = zf.getinfo(path)
            if info.file_size > MAX_PICTURE_BYTES:
                continue
            art_hash = save_picture(zf.read(info), picture_dir)
        except (KeyError, zipfile.BadZipFile, OSError):
            continue
        if art_hash:
            saved[series] = art_hash
    return saved


def unpack(session: Session, zip_path: Path, inbox_folder: Path,
           picture_dir: Path | None = None) -> Unpacked:
    """Unpack the recordings in a zip into the inbox, with their details,
    then delete the zip. Raises ZipProblem (and leaves everything as it
    was) if it can't be done."""
    try:
        zf = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError):
        raise ZipProblem(f"“{zip_path.name}” isn't a zip file the app can open.") from None
    with zf:
        members = _recordings(zf)
        if not members:
            raise ZipProblem(f"There are no recordings in “{zip_path.name}”.")
        needed = sum(m.file_size for m in members)
        free = shutil.disk_usage(inbox_folder).free
        if needed + SPARE_SPACE > free:
            raise ZipProblem(
                f"There isn't enough free space to unpack “{zip_path.name}”: its recordings "
                f"need {format_size(needed)}, and the disk has {format_size(free)} free.")
        details, pictures = read_manifest(zf)
        picture_hashes = _save_pictures(zf, pictures, picture_dir)
        done = with_details = 0
        for member in members:
            name = safe_name(member.filename)
            if name is None:
                continue
            try:
                with zf.open(member) as source:
                    name = save_upload(source, inbox_folder, name)
            except (zipfile.BadZipFile, OSError) as error:
                raise ZipProblem(
                    f"“{zip_path.name}” is damaged: {PurePosixPath(member.filename).name} "
                    f"couldn't be read. The recordings before it are in the inbox.") from error
            path = inbox_folder / name
            found = guess_details(path)
            values = details.get(member.filename)
            if values:
                found = with_airwave(found, values)
                series = values.get("SERIES")
                if series in picture_hashes:
                    found["extra"]["picture"] = picture_hashes[series]
            if found.get("extra"):
                with_details += 1
            # (The inbox page may have noticed the file already.)
            row = session.scalar(select(InboxItem).where(InboxItem.name == name))
            if row is None:
                row = InboxItem(name=name, added_at=utcnow())
                session.add(row)
            row.size_bytes = path.stat().st_size
            row.extra = found.pop("extra", {})
            for key, value in found.items():
                setattr(row, key, value)
            session.commit()
            done += 1
    zip_path.unlink(missing_ok=True)
    return Unpacked(done, with_details)
