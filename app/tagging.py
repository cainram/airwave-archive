"""Writing an episode's details into the tags of an audio file.

Only ever used on COPIES (for exports): the library's own files are never
changed.

Two kinds of tags are written:

- the usual ones every player understands: title, artist and album (the
  series), date, comment (the notes), track (the part number);
- "Airwave tags": every radio detail under a fixed name (AIRWAVE_SERIES,
  AIRWAVE_AIR_DATE...), so another Airwave Archive server can read them
  back exactly. Each format stores these its own way (MP3: ID3 "TXXX"
  frames; FLAC/Ogg/Opus: Vorbis comments; M4A: freeform atoms; WMA:
  attributes), but the names are the same everywhere.
"""

from __future__ import annotations

from pathlib import Path

import mutagen
from mutagen.asf import ASF
from mutagen.id3 import COMM, ID3, TALB, TDRC, TIT2, TPE1, TRCK, TXXX, ID3NoHeaderError
from mutagen.mp4 import MP4, MP4FreeForm

from app.db import Episode
from app.filenames import date_text

PREFIX = "AIRWAVE_"
MP4_FREEFORM = "----:com.airwavearchive:"
ASF_PREFIX = "Airwave/"


def airwave_values(episode: Episode, part: int | None, parts: int) -> dict[str, str]:
    """The Airwave tags for one file of an episode (empty ones left out)."""
    values = {
        "ID": episode.uid,
        "SERIES": episode.series,
        "EPISODE_NUMBER": episode.episode_number,
        "TITLE": episode.title,
        "AIR_DATE": date_text(episode.air_year, episode.air_month, episode.air_day) or None,
        "AIR_TIME": episode.air_time,
        "TIME_ZONE": episode.air_timezone,
        "NETWORK": episode.network,
        "HOST": episode.host,
        "CAST": episode.cast,
        "NOTES": episode.notes,
        "PART": f"{part}/{parts}" if parts > 1 and part else None,
    }
    return {PREFIX + key: value for key, value in values.items() if value}


def common_values(episode: Episode, part: int | None, parts: int) -> dict[str, str]:
    """The usual tags, in plain words (title, artist, album, date, comment, track)."""
    title = episode.title
    if title and parts > 1 and part:
        title = f"{title} (part {part} of {parts})"
    values = {
        "title": title,
        "artist": episode.series,
        "album": episode.series,
        "date": date_text(episode.air_year, episode.air_month, episode.air_day) or None,
        "comment": episode.notes,
        "track": f"{part}/{parts}" if parts > 1 and part else None,
    }
    return {key: value for key, value in values.items() if value}


def write_tags(path: Path, episode: Episode, part: int | None = None, parts: int = 1) -> bool:
    """Write the episode's details into the file at ``path`` (a copy!).
    Details the app doesn't know are left as the file had them. Returns
    False if this kind of file can't hold tags."""
    common = common_values(episode, part, parts)
    airwave = airwave_values(episode, part, parts)
    audio = mutagen.File(path)
    if audio is None:
        return False
    if isinstance(audio, MP4):
        _write_mp4(audio, common, airwave)
    elif isinstance(audio, ASF):
        _write_asf(audio, common, airwave)
    elif hasattr(audio, "tags") and _uses_id3(audio):
        _write_id3(audio, common, airwave)
    else:
        _write_vorbis(audio, common, airwave)
    audio.save()
    return True


def _uses_id3(audio) -> bool:
    """MP3, WAV and AIFF files keep ID3 tags."""
    if isinstance(audio.tags, ID3):
        return True
    if audio.tags is None:
        name = type(audio).__name__
        return name in {"MP3", "WAVE", "AIFF", "EasyMP3"}
    return False


def _write_id3(audio, common: dict, airwave: dict) -> None:
    if audio.tags is None:
        try:
            audio.add_tags()
        except ID3NoHeaderError:
            pass
    tags = audio.tags
    frames = {"title": TIT2, "artist": TPE1, "album": TALB, "date": TDRC, "track": TRCK}
    for key, frame in frames.items():
        if key in common:
            tags.setall(frame.__name__, [frame(encoding=3, text=common[key])])
    if "comment" in common:
        tags.setall("COMM", [COMM(encoding=3, lang="eng", desc="", text=common["comment"])])
    for frame in list(tags.getall("TXXX")):
        if frame.desc.startswith(PREFIX):
            tags.delall(frame.HashKey)
    for key, value in airwave.items():
        tags.add(TXXX(encoding=3, desc=key, text=value))


def _write_mp4(audio, common: dict, airwave: dict) -> None:
    if audio.tags is None:
        audio.add_tags()
    keys = {"title": "\xa9nam", "artist": "\xa9ART", "album": "\xa9alb",
            "date": "\xa9day", "comment": "\xa9cmt"}
    for key, atom in keys.items():
        if key in common:
            audio.tags[atom] = [common[key]]
    if "track" in common:
        number, total = (int(n) for n in common["track"].split("/"))
        audio.tags["trkn"] = [(number, total)]
    for atom in [k for k in audio.tags.keys() if k.startswith(MP4_FREEFORM + PREFIX)]:
        del audio.tags[atom]
    for key, value in airwave.items():
        audio.tags[MP4_FREEFORM + key] = [MP4FreeForm(value.encode("utf-8"))]


def _write_asf(audio, common: dict, airwave: dict) -> None:
    keys = {"title": "Title", "artist": "Author", "album": "WM/AlbumTitle",
            "date": "WM/Year", "comment": "Description", "track": "WM/PartOfSet"}
    for key, name in keys.items():
        if key in common:
            audio.tags[name] = [common[key]]
    for name in [k for k in audio.tags.keys() if k.startswith(ASF_PREFIX)]:
        del audio.tags[name]
    for key, value in airwave.items():
        audio.tags[ASF_PREFIX + key] = [value]


def _write_vorbis(audio, common: dict, airwave: dict) -> None:
    """FLAC, Ogg Vorbis, Opus: plain NAME=value comments."""
    if audio.tags is None:
        audio.add_tags()
    keys = {"title": "TITLE", "artist": "ARTIST", "album": "ALBUM", "date": "DATE",
            "comment": "COMMENT", "track": "TRACKNUMBER"}
    for key, name in keys.items():
        if key in common:
            audio.tags[name] = [common[key]]
    for name in [k for k in audio.tags.keys() if k.upper().startswith(PREFIX)]:
        del audio.tags[name]
    for key, value in airwave.items():
        audio.tags[key] = [value]


def read_airwave_tags(path: Path) -> dict[str, str]:
    """The Airwave tags in a file, without the AIRWAVE_ prefix (for import)."""
    try:
        audio = mutagen.File(path)
    except Exception:
        return {}
    if audio is None or audio.tags is None:
        return {}
    found: dict[str, str] = {}
    tags = audio.tags
    if isinstance(tags, ID3):
        for frame in tags.getall("TXXX"):
            if frame.desc.startswith(PREFIX):
                found[frame.desc[len(PREFIX):]] = str(frame.text[0]) if frame.text else ""
        return found
    for key in tags.keys():
        name = str(key)
        for prefix in (MP4_FREEFORM + PREFIX, ASF_PREFIX + PREFIX, PREFIX):
            if name.upper().startswith(prefix.upper()):
                value = tags[key]
                value = value[0] if isinstance(value, list) and value else value
                if isinstance(value, bytes | MP4FreeForm):
                    value = bytes(value).decode("utf-8", "replace")
                found[name[len(prefix):].upper()] = str(value)
                break
    return found
