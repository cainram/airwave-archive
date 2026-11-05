"""Tests for the library scanner and the Library page. Run with:  pytest"""

import os
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mutagen.id3 import COMM, TDRC, TIT2, TPE1, USLT
from mutagen.wave import WAVE
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import AudioFile, make_engine
from app.scanner import format_bitrate, format_duration, format_size, sync_library


def make_wav(path: Path, seconds: float) -> None:
    """Write a short silent WAV file to use as a test recording."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rate = 8000
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(1)
        w.setframerate(rate)
        w.writeframes(b"\x80" * int(rate * seconds))


def add_tags(path: Path, **frames) -> None:
    """Give a test WAV file ID3 tags, like most MP3s have."""
    audio = WAVE(path)
    audio.add_tags()
    for frame in frames.values():
        audio.tags.add(frame)
    audio.save()


@pytest.fixture
def engine(tmp_path):
    return make_engine(tmp_path / "data" / "airwave.db")


def all_files(engine):
    with Session(engine) as session:
        return {f.name: f for f in session.scalars(select(AudioFile))}


def test_scan_finds_audio_and_skips_other_files(tmp_path, engine):
    lib = tmp_path / "library"
    make_wav(lib / "Suspense 1943-05-13 - Donovan's Brain.wav", 2)
    make_wav(lib / "The Shadow" / "Shadow 1937-09-26.wav", 1)
    (lib / "notes.txt").write_text("not audio")
    make_wav(lib / ".hidden" / "skip-me.wav", 1)

    run = sync_library(engine, lib)

    assert run.added == 2
    files = all_files(engine)
    assert sorted(files) == ["Shadow 1937-09-26.wav", "Suspense 1943-05-13 - Donovan's Brain.wav"]
    shadow = files["Shadow 1937-09-26.wav"]
    assert shadow.folder == "The Shadow"
    assert round(shadow.duration_seconds) == 1


def test_tags_are_read(tmp_path, engine):
    lib = tmp_path / "library"
    path = lib / "CampbellPlayhouse39-04-14ThePatriot.wav"
    make_wav(path, 1)
    add_tags(path,
             title=TIT2(encoding=3, text="The Patriot"),
             artist=TPE1(encoding=3, text="The Campbell Playhouse"),
             date=TDRC(encoding=3, text="1939"),
             short=COMM(encoding=3, lang="eng", desc="ID3v1 Comment", text="www.example"),
             main=COMM(encoding=3, lang="eng", desc="", text="Originally aired April 14, 1939"),
             # Lyrics hold one piece of text, not a list (Coast to Coast files use it).
             lyrics=USLT(encoding=3, lang="eng", desc="", text="Orson Welles stars"))

    sync_library(engine, lib)

    f = all_files(engine)[path.name]
    assert f.tag_title == "The Patriot"
    assert f.tag_artist == "The Campbell Playhouse"
    assert f.tag_date == "1939"
    assert f.tag_comment == "Originally aired April 14, 1939"
    assert "www.example" in f.tags_json   # every tag is kept for the details view
    assert "Orson Welles stars" in f.tags_json   # not "O; r; s; o; n..."


def test_rescan_picks_up_new_changed_and_removed_files(tmp_path, engine):
    lib = tmp_path / "library"
    make_wav(lib / "a.wav", 1)
    make_wav(lib / "b.wav", 1)
    make_wav(lib / "c.wav", 1)
    sync_library(engine, lib)
    first_id = all_files(engine)["a.wav"].id

    # Nothing changed: nothing is re-read.
    run = sync_library(engine, lib)
    assert (run.added, run.changed, run.removed, run.unchanged) == (0, 0, 0, 3)

    make_wav(lib / "d.wav", 1)          # new
    make_wav(lib / "b.wav", 3)          # changed (longer)
    os.utime(lib / "b.wav", ns=(1, 1))  # make sure its modified time differs
    (lib / "c.wav").unlink()            # removed

    run = sync_library(engine, lib)
    assert (run.added, run.changed, run.removed, run.unchanged) == (1, 1, 1, 1)
    files = all_files(engine)
    assert sorted(files) == ["a.wav", "b.wav", "d.wav"]
    assert round(files["b.wav"].duration_seconds) == 3
    assert files["a.wav"].id == first_id   # unchanged files keep their place


def test_missing_library_folder_removes_nothing(tmp_path, engine):
    lib = tmp_path / "library"
    make_wav(lib / "a.wav", 1)
    sync_library(engine, lib)

    run = sync_library(engine, tmp_path / "not-mounted")

    assert run.library_missing
    assert list(all_files(engine)) == ["a.wav"]


def test_unreadable_audio_is_kept_not_fatal(tmp_path, engine):
    lib = tmp_path / "library"
    lib.mkdir()
    (lib / "broken.mp3").write_bytes(b"this is not really an mp3")
    sync_library(engine, lib)
    f = all_files(engine)["broken.mp3"]
    assert f.duration_seconds is None
    assert f.tag_title is None


def test_database_uses_wal_mode(engine):
    with engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"


def test_formatting():
    assert format_size(500) == "500 bytes"
    assert format_size(1536) == "1.5 KB"
    assert format_duration(125) == "2:05"
    assert format_duration(3725) == "1:02:05"
    assert format_duration(147 * 86400 + 7 * 3600 + 61) == "147d 7h 1m"
    assert format_duration(None) == "unknown"
    assert format_bitrate(64000) == "64 kbps"
    assert format_bitrate(None) == "unknown"


@pytest.fixture
def client(tmp_path, monkeypatch):
    import app.main as main
    monkeypatch.setattr(main, "LIBRARY_DIR", tmp_path / "library")
    monkeypatch.setattr(main, "_engine", make_engine(tmp_path / "data" / "airwave.db"))
    return TestClient(main.app)


def test_library_page_lists_files_and_tags(tmp_path, client):
    lib = tmp_path / "library"
    path = lib / "Suspense 1943-05-13 - Donovan's Brain.wav"
    make_wav(path, 1)
    add_tags(path, artist=TPE1(encoding=3, text="Suspense"))

    page = client.get("/")   # first visit scans automatically
    assert page.status_code == 200
    assert "Donovan&#39;s Brain" in page.text
    assert "Suspense" in page.text
    assert "1 audio file" in page.text
    assert "1 new" in page.text

    make_wav(lib / "second.wav", 1)
    assert client.post("/rescan", follow_redirects=False).status_code == 303
    page = client.get("/")
    assert "2 audio files" in page.text
    assert "1 new, 0 changed, 0 removed, 1 unchanged" in page.text


def test_library_page_is_split_into_pages(tmp_path, client, monkeypatch):
    import app.main as main
    monkeypatch.setattr(main, "PAGE_SIZE", 2)
    for name in ("a", "b", "c"):
        make_wav(tmp_path / "library" / f"{name}.wav", 1)

    first = client.get("/")
    assert "a.wav" in first.text and "c.wav" not in first.text
    assert "page 1 of 2" in first.text
    second = client.get("/?page=2")
    assert "c.wav" in second.text and "a.wav" not in second.text
