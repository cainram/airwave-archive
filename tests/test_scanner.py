"""Tests for the library scanner. Run with:  pytest"""

import wave
from pathlib import Path

from fastapi.testclient import TestClient

from app.scanner import format_duration, format_size, scan_library


def make_wav(path: Path, seconds: float) -> None:
    """Write a short silent WAV file to use as a test recording."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rate = 8000
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(1)
        w.setframerate(rate)
        w.writeframes(b"\x80" * int(rate * seconds))


def test_scan_finds_audio_and_skips_other_files(tmp_path):
    make_wav(tmp_path / "Suspense 1943-05-13 - Donovan's Brain.wav", 2)
    make_wav(tmp_path / "The Shadow" / "Shadow 1937-09-26.wav", 1)
    (tmp_path / "notes.txt").write_text("not audio")
    make_wav(tmp_path / ".hidden" / "skip-me.wav", 1)

    result = scan_library(tmp_path)

    names = sorted(f.name for f in result.files)
    assert names == ["Shadow 1937-09-26.wav", "Suspense 1943-05-13 - Donovan's Brain.wav"]
    shadow = next(f for f in result.files if f.name.startswith("Shadow"))
    assert shadow.folder == "The Shadow"
    assert round(shadow.duration_seconds) == 1
    assert round(result.total_seconds) == 3


def test_unreadable_audio_is_counted_not_fatal(tmp_path):
    (tmp_path / "broken.mp3").write_bytes(b"this is not really an mp3")
    result = scan_library(tmp_path)
    assert len(result.files) == 1
    assert result.files[0].duration_seconds is None
    assert result.unreadable == 1


def test_missing_folder_gives_empty_result(tmp_path):
    assert scan_library(tmp_path / "nope").files == []


def test_formatting():
    assert format_size(500) == "500 bytes"
    assert format_size(1536) == "1.5 KB"
    assert format_duration(125) == "2:05"
    assert format_duration(3725) == "1:02:05"
    assert format_duration(147 * 86400 + 7 * 3600 + 61) == "147d 7h 1m"
    assert format_duration(None) == "unknown"


def test_library_page_lists_files(tmp_path, monkeypatch):
    make_wav(tmp_path / "Suspense 1943-05-13 - Donovan's Brain.wav", 1)
    import app.main as main
    monkeypatch.setattr(main, "LIBRARY_DIR", tmp_path)
    monkeypatch.setattr(main, "_last_scan", None)
    client = TestClient(main.app)

    page = client.get("/")
    assert page.status_code == 200
    assert "Donovan&#39;s Brain" in page.text
    assert "1 audio file" in page.text

    assert client.post("/rescan", follow_redirects=False).status_code == 303
