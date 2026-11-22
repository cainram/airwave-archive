"""Joining an episode's parts ("Hour 1", "Hour 2"...) into one file
(owner's request, 2026-11-22; an Advanced page tool).

This module works out the plan: for each episode in several parts, what
the joined file will be called, which parts can simply be laid end to end
(no change to the sound) and which must be re-encoded first, the chapter
names, and any notes to add. Nothing here changes a file.

The least damage to the sound:
- Parts in the same format (MP3 or AAC, with the same sample rate and
  number of channels) are joined as they are, frame by frame: nothing is
  decoded or compressed again.
- Where they differ, the format most of the episode's TIME is in wins,
  and only the other parts are re-encoded, once, to match it, at the
  encoder's best quality setting and a bitrate a little above their own.
- Files that still need converting (WMA, FLAC...) are converted first
  (Phase 13), so they're left out here until they have been.

Details: the joined file gets the episode's details, as the app shows
them. Each part's own information isn't lost: chapter names come from the
parts' own titles (else "Part 2"), and a part's comment missing from the
episode's notes is added to them, labelled "Part 2: ...".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import mutagen
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.convert import needs_converting
from app.db import AudioFile, Episode
from app.episodes import PART_PATTERN, _clean
from app.series import sort_key

# Standard bitrates (kbps) for re-encoding a part.
MP3_RATES = [32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320]
HEADROOM = 1.25        # re-encode a little above the part's own bitrate
MARKER = re.compile(r"\b(?:hour|hr|part|pt)\.?[\s_]*\d+\b", re.I)
EXTENSIONS = {"mp3": ".mp3", "aac": ".m4a"}
CODEC_WORDS = {"mp3": "MP3", "aac": "AAC"}


@dataclass(frozen=True)
class Sound:
    """What's inside a part, as far as joining cares."""
    codec: str              # "mp3" or "aac"
    sample_rate: int
    channels: int

    def words(self) -> str:
        mono = {1: "mono", 2: "stereo"}.get(self.channels, f"{self.channels} channels")
        return f"{CODEC_WORDS[self.codec]}, {self.sample_rate / 1000:g} kHz, {mono}"


def read_sound(path: Path) -> tuple[Sound | None, int | None]:
    """A part's format and bitrate (kbps), read from the start of the file.
    (None, None) if it can't be read or isn't MP3 or AAC."""
    try:
        audio = mutagen.File(path)
    except Exception:
        return None, None
    info = getattr(audio, "info", None)
    if info is None:
        return None, None
    kind = type(info).__name__
    if kind == "MPEGInfo" and getattr(info, "layer", 3) == 3:
        codec = "mp3"
    elif kind == "MP4Info" and str(getattr(info, "codec", "")).startswith("mp4a"):
        codec = "aac"
    else:
        return None, None
    rate, channels = getattr(info, "sample_rate", 0), getattr(info, "channels", 0)
    if not rate or not channels:
        return None, None
    bitrate = getattr(info, "bitrate", 0)
    return Sound(codec, int(rate), int(channels)), (round(bitrate / 1000) or None)


def reencode_kbps(own: int | None, channels: int) -> int:
    """The bitrate for re-encoding a part: the smallest standard one a
    little above its own (so as little as possible is lost)."""
    if own is None:
        return 64 if channels == 1 else 128
    wanted = own * HEADROOM
    return next((rate for rate in MP3_RATES if rate >= wanted), MP3_RATES[-1])


@dataclass
class Part:
    file: AudioFile
    sound: Sound | None
    kbps: int | None
    seconds: float
    reencode: bool = False
    target_kbps: int | None = None
    chapter: str = ""


@dataclass
class Plan:
    """What joining one episode's parts will do."""
    episode: Episode
    parts: list[Part]
    problem: str | None = None        # why it can't be joined (yet)
    sound: Sound | None = None        # the joined file's format
    folder: str = ""
    name: str = ""                    # the joined file's name
    notes_added: list[str] = field(default_factory=list)

    @property
    def quick(self) -> bool:
        return not any(p.reencode for p in self.parts)

    @property
    def seconds(self) -> float:
        return sum(p.seconds for p in self.parts)

    @property
    def path(self) -> str:
        return str(PurePosixPath(self.folder, self.name)) if self.folder else self.name


# "Hour 1", "Part 2", "Hr 3", "(part 2 of 4)": anywhere in a name.
MARKER_ANYWHERE = re.compile(
    r"[\s_,(-]*\b(?:hour|hr|part|pt)\.?[\s_]*\d+(?:\s*of\s*\d+)?\)?", re.I)


def _marker(text: str | None) -> str | None:
    """The first "Hour 2" (or "Part 2"...) in a name, tidied: "Hour 2"."""
    found = MARKER.search(text or "")
    if not found:
        return None
    word, number = re.match(r"([a-z]+)\.?[\s_]*(\d+)", found.group(0), re.I).groups()
    word = {"hr": "Hour", "pt": "Part"}.get(word.lower(), word.title())
    return f"{word} {int(number)}"


def _extra_words(title: str | None, episode_title: str | None) -> bool:
    """Whether a part's title says more than the episode's title and its
    "Hour 2" / "(part 2 of 4)" markers."""
    rest = _clean(MARKER_ANYWHERE.sub(" ", title or "").strip(" -_,"))
    if not rest:
        return False
    plain = (_clean(MARKER_ANYWHERE.sub(" ", episode_title or "").strip(" -_,")) or "").lower()
    rest = " ".join(rest.lower().split())
    plain = " ".join(plain.split())
    if not plain or rest in plain:
        return False
    # What's left once the episode's title is taken out: any letters?
    return bool(re.search(r"[a-z]", rest.replace(plain, "")))


def chapter_names(files: list[AudioFile], episode_title: str | None) -> list[str]:
    """Each part's chapter name: its own title, when that has real words of
    its own ("Hour 3 - Open Lines"); else "Hour 2" from the file names, or
    from the titles, when each part's is different; else "Part 2"."""
    by_name = [_marker(PurePosixPath(f.name).stem) for f in files]
    by_title = [_marker(f.tag_title) for f in files]
    markers = next((found for found in (by_name, by_title)
                    if all(found) and len(set(found)) == len(found)), None)
    names = []
    for number, f in enumerate(files, 1):
        if _extra_words(f.tag_title, episode_title):
            names.append(_clean(f.tag_title))
        else:
            names.append(markers[number - 1] if markers else f"Part {number}")
    return names


def _joined_name(first: AudioFile, extension: str, taken: set[str], ours: set[str]) -> str:
    """Part 1's name without its "Hour 1", with the joined file's extension.
    If another file already has that name, " (joined)" is added."""
    stem = PurePosixPath(first.name).stem
    stem = PART_PATTERN.sub("", stem).strip(" -_,") or stem
    name = stem + extension
    if name.lower() in taken and name.lower() not in ours:
        name = f"{stem} (joined){extension}"
    return name


def plan_episode(episode: Episode, library: Path, taken: dict[str, set[str]]) -> Plan:
    """Work out joining one episode. `taken`: the names already used in
    each folder (lower case), to avoid them."""
    files = episode.parts
    parts = []
    for f in files:
        sound, kbps = (None, None) if f.missing else read_sound(library / f.folder / f.name)
        parts.append(Part(f, sound, kbps, f.duration_seconds or 0))
    plan = Plan(episode, parts)
    if any(p.file.missing for p in parts):
        plan.problem = "A part is missing from the library."
        return plan
    if any(needs_converting(p.file.name) for p in parts):
        plan.problem = "A part is in a format to convert first (Convert odd formats)."
        return plan
    if any(p.sound is None for p in parts):
        plan.problem = "A part can't be read as MP3 or AAC."
        return plan
    if len({p.file.folder for p in parts}) > 1:
        plan.problem = "The parts are in different folders."
        return plan
    # The format most of the time is in (a tie: part 1's).
    time: dict[Sound, float] = {}
    for p in parts:
        time[p.sound] = time.get(p.sound, 0) + max(p.seconds, 1)
    plan.sound = max(time, key=lambda s: (time[s], s == parts[0].sound))
    for p in parts:
        if p.sound != plan.sound:
            p.reencode = True
            p.target_kbps = reencode_kbps(p.kbps, plan.sound.channels)
    first = parts[0].file
    plan.folder = first.folder
    plan.name = _joined_name(first, EXTENSIONS[plan.sound.codec],
                             taken.get(first.folder, set()),
                             {p.file.name.lower() for p in parts})
    for p, name in zip(parts, chapter_names([p.file for p in parts], episode.title)):
        p.chapter = name
    notes = (episode.notes or "").lower()
    for number, p in enumerate(parts, 1):
        comment = _clean(p.file.tag_comment)
        if comment and comment.lower() not in notes:
            plan.notes_added.append(f"Part {number}: {comment}")
            notes += "\n" + comment.lower()
    return plan


def _with_parts(session: Session, series: list[str] | None = None):
    """Episodes with more than one file (of the given series)."""
    several = (select(AudioFile.episode_id).where(AudioFile.episode_id.is_not(None))
               .group_by(AudioFile.episode_id).having(func.count() > 1))
    query = select(Episode).where(Episode.id.in_(several))
    if series is not None:
        query = query.where(Episode.series.in_(series))
    return query


def series_with_parts(session: Session) -> list[dict]:
    """Each series with episodes in several parts: its name and how many."""
    rows = session.execute(
        select(Episode.series, func.count()).where(Episode.id.in_(
            _with_parts(session).with_only_columns(Episode.id)))
        .where(Episode.series.is_not(None)).group_by(Episode.series)).all()
    return sorted(({"name": name, "episodes": count} for name, count in rows),
                  key=lambda s: sort_key(s["name"]))


def plans_for(session: Session, series: list[str], library: Path) -> list[Plan]:
    """The plans for every episode in several parts in these series, by
    series and air date."""
    episodes = list(session.scalars(
        _with_parts(session, series).options(selectinload(Episode.files))
        .order_by(Episode.series, Episode.air_year, Episode.air_month, Episode.air_day,
                  Episode.title)))
    folders = {f.folder for e in episodes for f in e.files}
    taken: dict[str, set[str]] = {}
    for folder, name in session.execute(
            select(AudioFile.folder, AudioFile.name).where(AudioFile.folder.in_(folders))):
        taken.setdefault(folder, set()).add(name.lower())
    plans = [plan_episode(e, library, taken) for e in episodes]
    for plan in plans:      # two joined files mustn't get the same name either
        if plan.name:
            taken.setdefault(plan.folder, set()).add(plan.name.lower())
    return plans
