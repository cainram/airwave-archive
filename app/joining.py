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

import logging
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import mutagen
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, selectinload

from app.convert import (Stopped, available, ffmpeg_path, needs_converting, probe, run_command,
                         sound_length)
from app.db import MANUAL, STANDIN, AudioFile, Episode
from app.episodes import PART_PATTERN, _clean
from app.series import sort_key

log = logging.getLogger(__name__)

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
    new_title: str | None = None      # the title without its leftover "- Hour 1"

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


# A part 1 marker left at the end of an episode's title: "- Hour 1",
# "Hr. 1", "Part 1", "(part 1 of 4)".
LEFTOVER_MARKER = re.compile(
    r"[\s_,(-]*\b(?:hour|hr|part|pt)\.?[\s_]*0*1(?:\s*of\s*\d+)?\)?[\s_.-]*$", re.I)


def tidy_title(title: str | None) -> str | None:
    """The episode's title without a leftover part 1 marker at its end
    ("Coast to Coast AM - Hour 1" becomes "Coast to Coast AM"), or None if
    there's none to take off. Real words ("Part 2 of the Mystery") stay."""
    if not title:
        return None
    tidied = LEFTOVER_MARKER.sub("", title).strip(" -_,")
    return tidied if tidied and tidied != title.strip() else None


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
    its own ("Hour 3 - Open Lines") and the parts' titles aren't all the
    same; else "Hour 2" from the file names, or
    from the titles, when each part's is different; else "Part 2"."""
    by_name = [_marker(PurePosixPath(f.name).stem) for f in files]
    by_title = [_marker(f.tag_title) for f in files]
    markers = next((found for found in (by_name, by_title)
                    if all(found) and len(set(found)) == len(found)), None)
    # The same title on every part tells them apart no better than nothing.
    same = len({(_clean(f.tag_title) or "").lower() for f in files}) == 1
    names = []
    for number, f in enumerate(files, 1):
        if not same and _extra_words(f.tag_title, episode_title):
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
    if episode.sources.get("title") != STANDIN:
        plan.new_title = tidy_title(episode.title)
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


# ------------------------------------------------------------- joining

# Joining happens in hidden files beside the parts (hidden files aren't
# listed, so a half-made one never shows); a restart clears them away.
TEMPORARY = ".joining-"
CHECK_SECONDS = 2.0         # the joined file may be this much off the parts' total
CHECK_SHARE = 0.01          # ... or 1% of it, whichever is more


class JoinError(Exception):
    """One episode couldn't be joined: its parts are left as they were."""


@dataclass
class Job:
    """Joining many episodes, in the background, one at a time. Kept in
    memory: after a restart it's simply gone (every episode it reached is
    either fully joined or untouched)."""
    episode_ids: list[int]
    done: int = 0                     # episodes tried
    joined: int = 0
    deleted: int = 0                  # parts deleted
    current: str = ""                 # the episode being joined
    fraction: float = 0.0             # how far into it
    stop: bool = False
    finished: bool = False
    problems: list[tuple[str, str]] = field(default_factory=list)     # episode, what

    @property
    def total(self) -> int:
        return len(self.episode_ids)


def _escape(text: str) -> str:
    """Text for an ffmpeg metadata file (chapter names)."""
    return re.sub(r"([=;#\\\n])", r"\\\1", text)


def chapters_text(names: list[str], seconds: list[float]) -> str:
    """An ffmpeg metadata file with a chapter at each part."""
    lines, start = [";FFMETADATA1"], 0
    for name, length in zip(names, seconds):
        end = start + round(length * 1000)
        lines += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={start}", f"END={end}",
                  f"title={_escape(name)}"]
        start = end
    return "\n".join(lines) + "\n"


def _concat_line(path: Path) -> str:
    return "file '" + str(path).replace("'", "'\\''") + "'"


def reencode_command(part: Path, target: Path, sound: Sound, kbps: int) -> list[str]:
    """ffmpeg: re-encode one odd part to the joined file's format, at the
    encoder's best quality (only the sound)."""
    cmd = [ffmpeg_path() or "ffmpeg", "-nostdin", "-hide_banner", "-v", "error", "-y",
           "-i", str(part), "-map", "0:a:0", "-vn", "-sn", "-dn", "-map_metadata", "-1",
           "-progress", "pipe:1", "-nostats",
           "-ar", str(sound.sample_rate), "-ac", str(sound.channels), "-b:a", f"{kbps}k"]
    if sound.codec == "mp3":
        cmd += ["-c:a", "libmp3lame", "-compression_level", "0", "-f", "mp3"]
    else:
        cmd += ["-c:a", "aac", "-f", "mp4"]
    return [*cmd, str(target)]


def join_command(pieces: Path, chapters: Path, first: Path, target: Path, codec: str) -> list[str]:
    """ffmpeg: lay the pieces end to end, as they are (nothing decoded),
    with the chapters, and part 1's cover picture if it has one."""
    cmd = [ffmpeg_path() or "ffmpeg", "-nostdin", "-hide_banner", "-v", "error", "-y",
           "-f", "concat", "-safe", "0", "-i", str(pieces), "-i", str(chapters), "-i", str(first),
           "-map", "0:a", "-map", "2:v:0?", "-c", "copy", "-disposition:v", "attached_pic",
           "-map_metadata", "1", "-map_chapters", "1", "-progress", "pipe:1", "-nostats"]
    if codec == "mp3":
        cmd += ["-f", "mp3", "-id3v2_version", "3", "-write_id3v1", "0"]
    else:
        cmd += ["-f", "ipod", "-movflags", "+faststart"]
    return [*cmd, str(target)]


def _unchanged(f: AudioFile, path: Path) -> bool:
    try:
        now = path.stat()
    except OSError:
        return False
    return (now.st_size, now.st_mtime_ns) == (f.size_bytes, f.mtime_ns)


def join_episode(session: Session, episode: Episode, library: Path, art_dir: Path | None = None,
                 progress=None, stop=None) -> int:
    """Join one episode's parts into one file, safely: the joined file is
    made under a hidden name and checked (it opens and is as long as the
    parts together) before anything else happens. Then it takes its name,
    the episode's record is brought up to date (part 1's record becomes the
    joined file, so feeds, groups and listening carry on; the place in a
    later part moves to the same moment of the joined file), and only then
    are the parts deleted. Returns how many parts were deleted. Raises
    JoinError (the parts left as they were) or Stopped."""
    from app.deleting import file_path
    from app.genres import of_episode
    from app.scanner import LIBRARY_LOCK, _apply, forget_files, read_audio
    from app.series import about_of_episode
    from app.tagging import write_tags

    if not available():
        raise JoinError("ffmpeg isn't installed, so nothing can be joined.")
    taken = {episode.parts[0].folder: {name.lower() for name in session.scalars(
        select(AudioFile.name).where(AudioFile.folder == episode.parts[0].folder))}}
    plan = plan_episode(episode, library, taken)
    if plan.problem:
        raise JoinError(plan.problem)
    paths = [file_path(library, p.file) for p in plan.parts]
    final = file_path(library, AudioFile(folder=plan.folder, name=plan.name))
    if None in paths or final is None:
        raise JoinError("A part isn't inside the library, so nothing was touched.")
    folder = final.parent
    made: list[Path] = []

    def hidden(suffix: str) -> Path:
        path = folder / f"{TEMPORARY}{secrets.token_hex(6)}{suffix}"
        made.append(path)
        return path

    total = plan.seconds or 1
    try:
        # 1. Re-encode the odd parts (counted as half of the work).
        pieces, done = [], 0.0
        for part, path in zip(plan.parts, paths):
            if part.reencode:
                piece = hidden(EXTENSIONS[plan.sound.codec])
                share, before = part.seconds / total / 2, done
                run_command(reencode_command(path, piece, plan.sound, part.target_kbps),
                            part.seconds, lambda f: progress and progress(before + f * share), stop)
                pieces.append(piece)
            else:
                pieces.append(path)
            done += part.seconds / total / 2
        lengths = [sound_length(probe(piece)) or part.seconds
                   for piece, part in zip(pieces, plan.parts)]
        # 2. Join them, with the chapters.
        listing, chapters = hidden(".txt"), hidden(".txt")
        listing.write_text("ffconcat version 1.0\n" + "\n".join(_concat_line(p) for p in pieces) + "\n",
                           encoding="utf-8")
        chapters.write_text(chapters_text([p.chapter for p in plan.parts], lengths), encoding="utf-8")
        joined = hidden(EXTENSIONS[plan.sound.codec])
        start = 0.0 if plan.quick else 0.5
        run_command(join_command(listing, chapters, paths[0], joined, plan.sound.codec), sum(lengths),
                    lambda f: progress and progress(start + f * (1 - start)), stop)
        # 3. Check it.
        length = sound_length(probe(joined))
        expected = sum(p.seconds for p in plan.parts) or sum(lengths)
        if length is None:
            raise JoinError("The joined file couldn't be opened, so the parts were left as they were.")
        if abs(length - expected) > max(CHECK_SECONDS, expected * CHECK_SHARE):
            raise JoinError(f"The joined file is {length:.0f} seconds long, but the parts add up to "
                            f"{expected:.0f}, so the parts were left as they were.")
        # 4. The episode's details (with the tidied title and added notes) into its tags.
        if plan.new_title:
            episode.title = plan.new_title
        if plan.notes_added:
            episode.notes = "\n\n".join(filter(None, [episode.notes, *plan.notes_added]))
        if plan.new_title or plan.notes_added:
            sources = episode.sources
            for name, changed in (("title", plan.new_title), ("notes", plan.notes_added)):
                if changed:
                    sources[name] = MANUAL
            episode.sources = sources
        about, about_source = about_of_episode(episode)
        try:
            write_tags(joined, episode, None, 1, of_episode(episode), standin=False,
                       about=about, about_source=about_source)
        except Exception:
            raise JoinError("The joined file's tags couldn't be written, so the parts were left as they were.") from None
        # 5. Swap it in.
        with LIBRARY_LOCK:
            if not all(_unchanged(p.file, path) for p, path in zip(plan.parts, paths)):
                raise JoinError("A part changed while it was being joined, so nothing was replaced.")
            ours = {path.name.lower() for path in paths}
            if final.exists() and final.name.lower() not in ours:
                raise JoinError(f"A file called {final.name} appeared meanwhile, so nothing was replaced.")
            os.replace(joined, final)
            first, others = plan.parts[0].file, [p.file for p in plan.parts[1:]]
            offsets = {p.file.id: sum(lengths[:number]) for number, p in enumerate(plan.parts)}
            row = episode.listening
            if row is not None and row.file_id in offsets:
                row.position = (row.position or 0) + offsets[row.file_id]
                row.file_id = first.id
            first.name, first.part_number = final.name, None
            _apply(first, read_audio(final), final.stat(), art_dir)
            session.flush()
            forget_files(session, [f.id for f in others])
            session.commit()
            deleted = 0
            for path in paths:
                if path == final:
                    continue
                try:
                    path.unlink()
                    deleted += 1
                except FileNotFoundError:
                    pass
                except OSError as error:
                    log.warning("Couldn't delete %s after joining: %s", path, error)
        log.info("Joined %d parts into %s", len(paths), final)
        return deleted
    except (RuntimeError, OSError) as error:
        session.rollback()
        raise JoinError(f"It couldn't be joined ({error}); the parts were left as they were.") from None
    except BaseException:
        session.rollback()
        raise
    finally:
        for path in made:
            path.unlink(missing_ok=True)


def clear_leftovers(library: Path) -> None:
    """After a restart: remove half-made joined files."""
    if not library.is_dir():
        return
    for folder, _dirs, files in os.walk(library):
        for name in files:
            if name.startswith(TEMPORARY):
                try:
                    (Path(folder) / name).unlink()
                except OSError:
                    pass


# ---------------------------------------------------- in the background

_job: Job | None = None
_job_lock = threading.Lock()


def current_job() -> Job | None:
    return _job


def busy() -> bool:
    return _job is not None and not _job.finished


def start(engine: Engine, library: Path, episode_ids: list[int], art_dir: Path | None = None) -> bool:
    """Join these episodes' parts in the background, one at a time. False
    if a run is still going."""
    global _job
    with _job_lock:
        if busy():
            return False
        _job = Job(list(episode_ids))
        threading.Thread(target=_run, args=(_job, engine, library, art_dir),
                         daemon=True, name="airwave-joining").start()
        return True


def stop() -> None:
    if _job is not None:
        _job.stop = True


def _run(job: Job, engine: Engine, library: Path, art_dir: Path | None) -> None:
    asked = [0.0]

    def stopping() -> bool:            # asked often by ffmpeg's runner: once a second is plenty
        if time.monotonic() - asked[0] < 1:
            return False
        asked[0] = time.monotonic()
        return job.stop

    def progress(fraction: float) -> None:
        job.fraction = fraction

    try:
        for episode_id in job.episode_ids:
            if job.stop:
                break
            job.fraction = 0.0
            with Session(engine) as session:
                episode = session.get(Episode, episode_id)
                if episode is None or len(episode.files) < 2:
                    job.done += 1
                    continue
                job.current = episode.title or "Untitled"
                try:
                    job.deleted += join_episode(session, episode, library, art_dir, progress, stopping)
                    job.joined += 1
                except Stopped:
                    break
                except JoinError as error:
                    job.problems.append((job.current, str(error)))
                job.done += 1
    except Exception as error:                   # (never leave the run "going")
        log.exception("Joining stopped")
        job.problems.append(("", f"Joining stopped because of an error: {error}"))
    finally:
        job.current, job.finished = "", True
