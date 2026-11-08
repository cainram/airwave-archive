"""Converting recordings the app can't play well (Phase 13).

Some recordings come in formats that browsers, iPhones and podcast apps
can't play: video files (MKV, WebM, MP4), RealAudio, WMA, Ogg, Opus. The
free program **ffmpeg** takes the sound out of them and, where needed,
converts it:

- sound that's already MP3 or AAC is **copied unchanged** (fast, no loss
  of quality) into an ``.mp3`` or ``.m4a`` file;
- anything else is converted to **MP3**, mono if the original is mono, at
  a bitrate at or a little above the original's (capped at 128 kbps).

Lossless formats (FLAC, WAV, AIFF) play almost everywhere and are left
alone (owner's choice, 2026-11-08).

This module looks inside a file (with ffprobe, ffmpeg's companion) and
works out the plan. Where ffmpeg is found comes from the AIRWAVE_FFMPEG
setting (the program's full path), or else the usual places; without it,
the app still runs and says converting isn't available.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

# Files that are accepted, but need converting before they can be used.
CONVERT_EXTENSIONS = {
    ".mkv", ".mka", ".webm",          # Matroska and WebM (video or sound)
    ".mp4", ".m4v",                   # MP4 video
    ".ra", ".rm",                     # RealAudio (older old-time radio sites)
    ".wma",                           # Windows Media Audio
    ".ogg", ".oga", ".opus",          # Ogg Vorbis and Opus
}

# The type names browsers and computers use for them (for the upload
# window's filter, together with the extensions).
MEDIA_TYPES = [
    "video/x-matroska", "audio/x-matroska", "video/webm", "audio/webm",
    "video/mp4", "audio/mp4", "video/x-m4v",
    "audio/x-pn-realaudio", "audio/vnd.rn-realaudio", "application/vnd.rn-realmedia",
    "audio/x-ms-wma", "audio/ogg", "application/ogg", "audio/opus",
]

# Names people know these by.
KINDS = {
    ".mkv": "MKV", ".mka": "MKA (Matroska) audio", ".webm": "WebM",
    ".mp4": "MP4", ".m4v": "MP4", ".ra": "RealAudio", ".rm": "RealMedia",
    ".wma": "WMA (Windows Media Audio)", ".ogg": "Ogg", ".oga": "Ogg", ".opus": "Opus",
}
CODEC_NAMES = {
    "mp3": "MP3", "aac": "AAC", "opus": "Opus", "vorbis": "Vorbis", "flac": "FLAC",
    "wmav1": "WMA", "wmav2": "WMA", "wmapro": "WMA Pro", "wmavoice": "WMA Voice",
    "cook": "RealAudio (Cook)", "ra_144": "RealAudio 1", "ra_288": "RealAudio 2",
    "sipr": "RealAudio (Sipro)", "atrac3": "ATRAC3", "ac3": "Dolby Digital (AC-3)",
    "eac3": "Dolby Digital Plus", "dts": "DTS", "alac": "Apple Lossless",
}
# Sound that can go into a playable file unchanged: codec -> new extension.
COPYABLE = {"mp3": ".mp3", "aac": ".m4a"}
MP3_RATES = [32, 40, 48, 56, 64, 80, 96, 112, 128]   # kbps
MOST_KBPS = 128
# Re-encoding loses a little each time; give MP3 a bit more room than the
# original had (newer formats such as Opus also do more with fewer bits).
HEADROOM = 1.25
PROBE_SECONDS = 30


def needs_converting(name: str) -> bool:
    return Path(name).suffix.lower() in CONVERT_EXTENSIONS


def ffmpeg_path() -> str | None:
    """Where ffmpeg is: the AIRWAVE_FFMPEG setting, or the usual places."""
    setting = os.environ.get("AIRWAVE_FFMPEG")
    if setting:
        return setting if Path(setting).is_file() else None
    return shutil.which("ffmpeg")


def ffprobe_path() -> str | None:
    """ffprobe sits next to ffmpeg."""
    ffmpeg = ffmpeg_path()
    if ffmpeg is None:
        return None
    beside = Path(ffmpeg).with_name("ffprobe" + Path(ffmpeg).suffix)   # ".exe" on Windows
    return str(beside) if beside.is_file() else shutil.which("ffprobe")


def available() -> bool:
    return ffmpeg_path() is not None and ffprobe_path() is not None


def probe(path: Path) -> dict | None:
    """What ffprobe says is inside a file (streams, length, tags), or None
    if it can't be read (or ffprobe isn't there)."""
    ffprobe = ffprobe_path()
    if ffprobe is None:
        return None
    try:
        done = subprocess.run(
            [ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
             str(path)],
            capture_output=True, timeout=PROBE_SECONDS, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if done.returncode != 0:
        return None
    try:
        return json.loads(done.stdout or b"{}")
    except ValueError:
        return None


@dataclass
class Plan:
    """What converting a file will do."""
    ok: bool                       # False: it can't be converted (see ``problem``)
    kind: str = ""                 # "WebM", "RealAudio"...
    codec: str = ""                # the sound inside: "Opus", "AAC"...
    kbps: int | None = None        # the original sound's bitrate
    mono: bool | None = None
    copy: bool = False             # the sound is copied unchanged
    extension: str = ""            # the new file's: ".mp3" or ".m4a"
    target_kbps: int | None = None # MP3 bitrate, when converting
    still_picture: bool = False    # a "video" that's just one picture
    video: bool = False            # moving pictures (they're left out)
    duration: float | None = None
    problem: str = ""
    stream: int | None = None      # which track holds the sound (ffprobe's number)

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, text: str | None) -> Plan | None:
        if not text:
            return None
        try:
            return cls(**json.loads(text))
        except (ValueError, TypeError):
            return None

    @property
    def description(self) -> str:
        """In plain words, for the inbox."""
        if not self.ok:
            return self.problem
        sound = self.codec
        details = [d for d in (f"{self.kbps} kbps" if self.kbps else "",
                               {True: "mono", False: "stereo"}.get(self.mono, "")) if d]
        if details:
            sound += ", " + ", ".join(details)
        what = (f"{self.kind} video file" if self.video else f"{self.kind} file"
                + (" (a still picture with sound)" if self.still_picture else ""))
        if self.copy:
            then = f"It will be copied into an {self.extension} file unchanged, with no loss of quality."
        else:
            channels = "mono" if self.mono else "stereo"
            then = f"It will be converted to MP3 ({channels}, {self.target_kbps} kbps)."
        return f"{what}. Its sound is {sound}. {then}"


def _kbps(value) -> int | None:
    try:
        bits = int(value)
    except (TypeError, ValueError):
        return None
    return round(bits / 1000) if bits > 0 else None


def mp3_rate(kbps: int | None, mono: bool) -> int:
    """The MP3 bitrate for converting sound of ``kbps``: the smallest
    standard rate with a little headroom over the original."""
    if kbps is None:
        return 64 if mono else 128
    wanted = kbps * HEADROOM
    for rate in MP3_RATES:
        if rate >= wanted:
            return min(rate, MOST_KBPS)
    return MOST_KBPS


def _is_still(stream: dict) -> bool:
    """Is this "video" just one picture (cover art, or a still image)?"""
    if (stream.get("disposition") or {}).get("attached_pic"):
        return True
    if str(stream.get("nb_frames")) == "1" or stream.get("codec_name") in ("mjpeg", "png", "bmp"):
        return True
    # Matroska and WebM note each track's length: a picture's is zero.
    length = str((stream.get("tags") or {}).get("DURATION") or "")
    return bool(length) and length.replace("0", "").replace(":", "").replace(".", "") == ""


def plan_for(name: str, found: dict | None) -> Plan:
    """Work out what converting a file will do, from what ffprobe found."""
    kind = KINDS.get(Path(name).suffix.lower(), Path(name).suffix.lstrip(".").upper())
    if found is None:
        return Plan(ok=False, kind=kind, problem=(
            f"This {kind} file can't be read: it may be damaged, or not really a "
            f"{kind} file."))
    streams = found.get("streams") or []
    sounds = [s for s in streams if s.get("codec_type") == "audio"]
    if not sounds:
        return Plan(ok=False, kind=kind, problem=f"This {kind} file has no sound in it.")
    sound = next((s for s in sounds if (s.get("disposition") or {}).get("default")), sounds[0])
    pictures = [s for s in streams if s.get("codec_type") == "video"]
    still = bool(pictures) and all(_is_still(s) for s in pictures)
    codec = sound.get("codec_name") or ""
    channels = sound.get("channels")
    mono = None if channels is None else int(channels) == 1
    kbps = _kbps(sound.get("bit_rate"))
    if kbps is None and len(sounds) == 1 and (still or not pictures):
        # Some files only give the whole file's bitrate: near enough, when
        # the sound is all there is (a picture adds next to nothing).
        kbps = _kbps((found.get("format") or {}).get("bit_rate"))
    try:
        duration = float((found.get("format") or {}).get("duration")) or None
    except (TypeError, ValueError):
        duration = None
    plan = Plan(ok=True, kind=kind, codec=CODEC_NAMES.get(codec, codec.upper() or "unknown"),
                kbps=kbps, mono=mono, still_picture=still, video=bool(pictures) and not still,
                duration=duration, stream=sound.get("index"))
    if codec in COPYABLE:
        plan.copy, plan.extension = True, COPYABLE[codec]
    else:
        plan.extension, plan.target_kbps = ".mp3", mp3_rate(kbps, bool(mono))
    return plan


def tags_from(found: dict | None) -> dict[str, str]:
    """The file's tags as ffprobe reports them (title, artist, date...),
    with lower-case names. Used when the usual tag reader can't open it."""
    if not found:
        return {}
    tags: dict[str, str] = {}
    for source in [found.get("format") or {}, *(found.get("streams") or [])]:
        for key, value in (source.get("tags") or {}).items():
            key = key.lower()
            if isinstance(value, str) and value.strip() and key not in tags:
                tags[key] = value.strip()
    return tags


# ------------------------------------------------------------- converting

class Stopped(Exception):
    """Converting was cancelled."""


def command(plan: Plan, source: Path, target: Path) -> list[str]:
    """The ffmpeg command that makes ``target`` from ``source``: just the
    sound (no pictures or subtitles), tags carried over."""
    sound = f"0:{plan.stream}" if plan.stream is not None else "0:a:0"
    cmd = [ffmpeg_path() or "ffmpeg", "-nostdin", "-hide_banner", "-v", "error", "-y",
           "-i", str(source), "-map", sound, "-vn", "-sn", "-dn", "-map_metadata", "0",
           "-progress", "pipe:1", "-nostats"]
    if plan.copy:
        cmd += ["-c:a", "copy"]
    else:
        cmd += ["-c:a", "libmp3lame", "-b:a", f"{plan.target_kbps}k", "-ac", "1" if plan.mono else "2"]
    if plan.extension == ".mp3":
        cmd += ["-f", "mp3", "-id3v2_version", "3", "-write_id3v1", "0"]
    else:
        cmd += ["-f", "ipod", "-movflags", "+faststart"]   # .m4a
    return [*cmd, str(target)]


def run(plan: Plan, source: Path, target: Path, progress=None, stop=None) -> None:
    """Convert ``source`` into ``target``. ``progress(fraction)`` is told
    how far it's got (0 to 1, when the length is known); ``stop()`` is
    asked now and then whether to give up (raises Stopped). Raises
    RuntimeError with ffmpeg's complaint if it fails."""
    process = subprocess.Popen(command(plan, source, target), stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
    try:
        for line in process.stdout:
            if stop is not None and stop():
                process.kill()
                raise Stopped()
            if line.startswith("out_time_us=") and progress is not None and plan.duration:
                try:
                    done = int(line.split("=", 1)[1]) / 1_000_000
                except ValueError:
                    continue
                progress(max(0.0, min(1.0, done / plan.duration)))
        complaint = process.stderr.read()
        if process.wait() != 0:
            lines = [line for line in complaint.strip().splitlines() if line.strip()]
            raise RuntimeError(lines[-1] if lines else "ffmpeg stopped with an error")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def check(plan: Plan, made: Path) -> str | None:
    """Is the new file all right? It must open, hold sound, and be as long
    as the original (within a second or two). Returns what's wrong, or
    None."""
    found = probe(made)
    if not found or not any(s.get("codec_type") == "audio" for s in found.get("streams") or []):
        return "The new file couldn't be opened."
    try:
        length = float((found.get("format") or {}).get("duration"))
    except (TypeError, ValueError):
        return "The new file's length couldn't be read."
    if plan.duration and abs(length - plan.duration) > max(2.0, plan.duration * 0.01):
        return (f"The new file is {length:.0f} seconds long, but the original is "
                f"{plan.duration:.0f}: something went wrong.")
    return None
