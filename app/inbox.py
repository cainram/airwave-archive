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

Files can bring more details with them: an export zip from Airwave
Archive (see app/zipimport.py) lists every detail, and its copies carry
Airwave tags. Those details (time, station, cast, notes, the episode's
permanent ID, which part it is) wait with the file and are saved on its
episode when it's added. The permanent ID shows when you already have an
episode; parts of one episode are put back together.

Files that look like parts of one broadcast ("Hour 1", "Hour 2"...) are
suggested as a group (see app/grouping.py): if the owner leaves the
suggestion ticked, they go into the library together, as one episode with
the first part's details.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import convert, grouping
from app.db import MANUAL, AudioFile, Episode, InboxItem, SeriesArt, utcnow
from app.episodes import (
    PART_PATTERN, TIME_PATTERN, TIME_ZONES, _clean, apply_confirmed, date_from_tag, read_date_text,
)
from app.filenames import date_text, parse_filename
from app.parts import set_parts
from app.scanner import AUDIO_EXTENSIONS, add_one_file, read_audio
from app.series import one_series, set_uploaded_picture
from app.tagging import read_airwave_tags

UNSAFE = re.compile(r'[\x00-\x1f/\\:*?"<>|]')
MAX_NAME = 180
NO_SERIES_FOLDER = "Unsorted"
MAX_TEXT = 20_000                      # longest detail kept from an import
UID_PATTERN = re.compile(r"^[0-9A-Za-z-]{8,64}$")


def upload_accept() -> str:
    """The file filter for the upload window: every extension the inbox
    takes, plus the type names some systems go by instead (a Mac, for
    example, greys out files that match neither)."""
    names = sorted(AUDIO_EXTENSIONS | convert.CONVERT_EXTENSIONS)
    return ",".join(["audio/*", *names, *convert.MEDIA_TYPES, ".zip", "application/zip"])


def is_audio(name: str) -> bool:
    """A recording the inbox accepts: one the app plays, or one it can
    convert (video files, RealAudio, WMA, Ogg, Opus; see app/convert.py)."""
    suffix = Path(name).suffix.lower()
    return suffix in AUDIO_EXTENSIONS or suffix in convert.CONVERT_EXTENSIONS


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


def _text(value, limit: int = MAX_TEXT) -> str | None:
    return _clean(str(value)[:limit]) if value is not None else None


def airwave_details(values: dict) -> tuple[dict, dict]:
    """Read Airwave details (named as in Airwave tags: SERIES, AIR_DATE,
    PART "1/2"...) into (main details, extra details). Anything that
    doesn't make sense is left out, never guessed."""
    values = {str(key).upper(): value for key, value in values.items()}
    date, _error = read_date_text(_text(values.get("AIR_DATE"), 20))
    main = {"series": _text(values.get("SERIES"), 500),
            "episode_number": _text(values.get("EPISODE_NUMBER"), 100),
            "title": _text(values.get("TITLE"), 1000)}
    if date:
        main.update(air_year=date[0], air_month=date[1], air_day=date[2])
    extra = {name.lower(): _text(values.get(name))
             for name in ("NETWORK", "HOST", "CAST", "NOTES")}
    uid = _text(values.get("ID"), 100)
    if uid and UID_PATTERN.match(uid):
        extra["uid"] = uid
    time = TIME_PATTERN.match(_text(values.get("AIR_TIME"), 10) or "")
    if time and date and None not in date:
        extra["air_date"] = date_text(*date)
        extra["air_time"] = f"{int(time[1]):02d}:{time[2]}"
        if values.get("TIME_ZONE") in TIME_ZONES:
            extra["time_zone"] = values["TIME_ZONE"]
    part = re.match(r"^\s*(\d{1,3})\s*/\s*(\d{1,3})\s*$", str(values.get("PART") or ""))
    if part and 1 <= int(part[1]) <= int(part[2]) and int(part[2]) > 1:
        extra["part"], extra["parts"] = int(part[1]), int(part[2])
    return main, {key: value for key, value in extra.items() if value}


def guess_details(path: Path, found: dict | None = None) -> dict:
    """What the file's tags and name say: series, episode number, title and
    air date. Tags come first; the name fills in what they lack, and gives
    the date if it says more (tags often have only the year). Airwave tags
    (from an export) beat both, and bring extra details (``extra``).
    ``found`` is what ffprobe saw in a file that needs converting: its
    tags and length are used where the usual reader finds none."""
    info = read_audio(path)
    tags = info.tags
    if found is not None:
        tags = {**convert.tags_from(found), **{k: v for k, v in tags.items() if v}}
        if info.duration_seconds is None:
            try:
                info.duration_seconds = float(found["format"]["duration"]) or None
            except (KeyError, TypeError, ValueError):
                pass
    guess = parse_filename(path.name)
    from_tags = (date_from_tag(tags.get("date")))
    from_name = (guess.year, guess.month, guess.day) if guess else (None, None, None)
    date = from_name if _known_parts(from_name) > _known_parts(from_tags) else from_tags
    details = {
        "series": _clean(tags.get("artist")) or _clean(tags.get("album")) or (guess and guess.series),
        "episode_number": guess.episode_number if guess else None,
        "title": _clean(tags.get("title")) or (guess and guess.title),
        "air_year": date[0], "air_month": date[1], "air_day": date[2],
        "duration_seconds": info.duration_seconds,
    }
    found = read_airwave_tags(path)
    if found:
        return with_airwave(details, found)
    return details


def with_airwave(details: dict, values: dict) -> dict:
    """``details`` updated with Airwave details (which win where known)."""
    main, extra = airwave_details(values)
    details = {**details, **{key: value for key, value in main.items() if value is not None}}
    if "air_year" in main:
        details.update(air_year=main["air_year"], air_month=main.get("air_month"),
                       air_day=main.get("air_day"))
    return {**details, "extra": extra}


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
        row = rows.get(name)
        if row is None:
            found = convert.probe(path) if convert.needs_converting(name) else None
            details = guess_details(path, found)
            row = InboxItem(name=name, size_bytes=path.stat().st_size, added_at=utcnow(), **details)
            if convert.needs_converting(name) and convert.available():
                row.convert_json = convert.plan_for(name, found).to_json()
            session.add(row)
        elif convert.needs_converting(name) and row.convert_json is None:
            # Waiting since before converting was possible, or ffmpeg was missing.
            if convert.available():
                row.convert_json = convert.plan_for(name, convert.probe(path)).to_json()
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


class AlreadyHave(Exception):
    """The library already has this episode (same permanent ID)."""


class NeedsConverting(Exception):
    """The file must be converted before it can go into the library."""


def same_episode(session: Session, item: InboxItem) -> Episode | None:
    """The library's episode with this file's permanent ID, if any."""
    uid = item.extra.get("uid")
    return session.scalar(select(Episode).where(Episode.uid == uid)) if uid else None


def part_to_come(episode: Episode, extra: dict) -> bool:
    """Is this file a part the episode is still waiting for?"""
    part = extra.get("part")
    return bool(part) and all(
        f.part_number is not None and f.part_number != part for f in episode.files)


def already_have(session: Session, item: InboxItem) -> AudioFile | None:
    """The library's copy of this file's episode, if adding it would make
    a second copy. (A missing part of an episode isn't a second copy: it
    joins the rest.)"""
    episode = same_episode(session, item)
    if episode is None or not episode.files or part_to_come(episode, item.extra):
        return None
    return episode.parts[0]


def add_to_library(session: Session, inbox_folder: Path, library: Path,
                   item: InboxItem, art_dir: Path | None = None,
                   into: str | None = None) -> AudioFile | None:
    """Move an inbox file into the library and add it, with the details
    confirmed in the inbox and any extra ones it brought. None if the file
    isn't in the inbox any more; AlreadyHave if the library has its
    episode already. ``into`` picks the library folder (relative to the
    library) instead of the series' usual one."""
    source = item_path(inbox_folder, item)
    if source is None:
        return None
    if convert.needs_converting(item.name):
        raise NeedsConverting(item.name)
    if already_have(session, item) is not None:
        raise AlreadyHave(item.name)
    extra = item.extra
    joining = same_episode(session, item)           # a part of it is there
    if joining is not None and joining.files:
        folder = library / joining.parts[0].folder
    elif into is not None:
        folder = library / into
    else:
        folder = library / target_folder(session, item.series)
    if library.resolve() not in (folder.resolve(), *folder.resolve().parents):
        raise ValueError("That folder isn't inside the library")
    path = move_into(source, folder)
    row = add_one_file(session, library, path, art_dir)
    if joining is not None:
        session.flush()
        empty, row.episode = row.episode, joining
        session.delete(empty)
        episode = joining
    else:
        episode = row.episode
        date = (item.air_year, item.air_month, item.air_day)
        apply_confirmed(episode, {
            "series": item.series, "episode_number": item.episode_number, "title": item.title,
            "date": date if item.air_year is not None else None,
        })
        _apply_extra(episode, date, extra)
    if extra.get("part"):
        row.part_number = extra["part"]
        sources = episode.sources
        sources["parts"] = MANUAL
        episode.sources = sources
        episode.updated_at = utcnow()
    _use_picture(session, episode.series, extra.get("picture"))
    session.delete(item)
    return row


def suggested_groups(session: Session, items: list[InboxItem]) -> list[grouping.Suggestion]:
    """Waiting files that look like parts of one broadcast (not ones that
    came from an Airwave export, which knows its own parts)."""
    entries = []
    for item in items:
        if "part" in item.extra or "uid" in item.extra or convert.needs_converting(item.name):
            continue
        date = date_text(item.air_year, item.air_month, item.air_day) if item.air_year else None
        entries.append((item.id, grouping.INBOX, item.name, date))
    return grouping.find_sets(entries, grouping.dismissed_signatures(session))


def add_group(session: Session, inbox_folder: Path, library: Path, items: list[InboxItem],
              art_dir: Path | None = None) -> list[AudioFile]:
    """Add files that the owner approved as parts of one broadcast: each
    goes into the library (all in the first one's folder), then they're
    grouped as one episode with the first part's details. Returns the
    files added, first part first."""
    rows: list[AudioFile] = []
    for item in items:
        row = add_to_library(session, inbox_folder, library, item, art_dir,
                             into=rows[0].folder if rows else None)
        if row is None:            # gone from the inbox meanwhile
            session.delete(item)
            continue
        rows.append(row)
    session.flush()
    if len(rows) > 1:
        episode = rows[0].episode
        set_parts(session, episode, rows)
        # "Show - Mar 01 2010 - Hour 1" names part 1, not the whole broadcast.
        if episode.title:
            episode.title = _clean(PART_PATTERN.sub("", episode.title).strip(" -_,")) or episode.title
    return rows


def _apply_extra(episode: Episode, date: tuple, extra: dict) -> None:
    """Save the extra details a file brought, as if typed by hand."""
    sources = episode.sources
    for name in ("network", "host", "cast", "notes"):
        if extra.get(name):
            setattr(episode, name, extra[name])
            sources[name] = MANUAL
    # The time belongs to the date it came with: if that was changed in the
    # inbox, the time is dropped.
    if extra.get("air_time") and None not in date and date_text(*date) == extra.get("air_date"):
        episode.air_time = extra["air_time"]
        episode.air_timezone = extra.get("time_zone")
    if extra.get("uid"):
        episode.uid = extra["uid"]
    episode.sources = sources
    episode.updated_at = utcnow()


def _use_picture(session: Session, series: str | None, art_hash: str | None) -> None:
    """Use a picture that came with the file for its series, but only if
    the series has none of its own yet."""
    if not series or not art_hash or session.get(SeriesArt, series) is not None:
        return
    session.flush()
    found = one_series(session, series)
    if found and found.get("art"):
        return
    set_uploaded_picture(session, series, art_hash)
