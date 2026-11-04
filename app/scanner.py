"""Find audio files in the library folder and read basic facts about them.

This module only *reads* files. It never writes, moves, or renames anything.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import mutagen

# File endings we treat as audio.
AUDIO_EXTENSIONS = {
    ".mp3", ".m4a", ".m4b", ".aac", ".flac", ".ogg", ".oga", ".opus",
    ".wav", ".wma", ".aiff", ".aif",
}


@dataclass
class AudioFile:
    """One audio file found during a scan."""

    name: str              # file name, e.g. "Suspense 1943-05-13 - Donovan's Brain.mp3"
    folder: str            # folder relative to the library root ("" = top level)
    size_bytes: int
    duration_seconds: float | None  # None if the length couldn't be read


@dataclass
class ScanResult:
    files: list[AudioFile]
    unreadable: int        # audio files whose length couldn't be read

    @property
    def total_bytes(self) -> int:
        return sum(f.size_bytes for f in self.files)

    @property
    def total_seconds(self) -> float:
        return sum(f.duration_seconds or 0 for f in self.files)


def read_duration(path: Path) -> float | None:
    """Return the length of an audio file in seconds, or None if unknown."""
    try:
        audio = mutagen.File(path)
    except Exception:
        return None
    if audio is None or audio.info is None:
        return None
    length = getattr(audio.info, "length", None)
    return float(length) if length else None


def scan_library(root: Path) -> ScanResult:
    """Walk every folder under ``root`` and collect the audio files."""
    files: list[AudioFile] = []
    unreadable = 0
    if not root.is_dir():
        return ScanResult(files=[], unreadable=0)

    for dirpath, dirnames, filenames in os.walk(root):
        # Skip hidden folders such as ".Trash" or ".@__thumb".
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for filename in sorted(filenames):
            if filename.startswith("."):
                continue
            path = Path(dirpath) / filename
            if path.suffix.lower() not in AUDIO_EXTENSIONS:
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            duration = read_duration(path)
            if duration is None:
                unreadable += 1
            folder = str(Path(dirpath).relative_to(root))
            files.append(AudioFile(
                name=filename,
                folder="" if folder == "." else folder,
                size_bytes=size,
                duration_seconds=duration,
            ))
    return ScanResult(files=files, unreadable=unreadable)


def format_size(num_bytes: int) -> str:
    """1536 -> '1.5 KB'."""
    size = float(num_bytes)
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
