"""Tests for converting library files in place (Phase 13d, an advanced
technique). Run with:  pytest

Most use a stand-in for ffmpeg, so they run anywhere; the last ones use
the real thing and are skipped where it isn't installed.
"""

import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import convert, converting
from app.db import AudioFile, Episode, LibraryConversion, Listening, make_engine
from test_convert import probed, sound
from test_scanner import make_wav


@pytest.fixture
def pretend(monkeypatch):
    """A stand-in ffmpeg: "converting" writes a short WAV under the new name."""
    monkeypatch.setattr(convert, "available", lambda: True)
    monkeypatch.setattr(convert, "probe", lambda path: probed(sound("opus", 1, "48000"), duration="2.0"))
    monkeypatch.setattr(convert, "check", lambda plan, made: None)
    calls = []

    def run(plan, source, target, progress=None, stop=None, cover=None):
        calls.append(source.name)
        make_wav(target, 2)
    monkeypatch.setattr(convert, "run", run)
    return calls


@pytest.fixture
def client(tmp_path, monkeypatch, pretend):
    import app.main as main
    lib = tmp_path / "library"
    (lib / "Lone Ranger").mkdir(parents=True)
    (lib / "Lone Ranger" / "Lone Ranger 1941-01-01.webm").write_bytes(b"\x1aE\xdf\xa3 pretend")
    make_wav(lib / "Suspense 1943-05-13.wav", 1)
    monkeypatch.setattr(main, "LIBRARY_DIR", lib)
    monkeypatch.setattr(main, "INBOX_DIR", tmp_path / "inbox")
    monkeypatch.setattr(main, "_engine", make_engine(tmp_path / "data" / "airwave.db"))
    client = TestClient(main.app)
    client.get("/")          # the first visit scans
    client.lib = lib
    return client


def engine():
    import app.main as main
    return main._engine


def the_file(name):
    with Session(engine()) as session:
        f = session.scalar(select(AudioFile).where(AudioFile.name == name))
        session.expunge_all()
        return f


def run_queue(client):
    return converting.convert_next_library(engine(), client.lib)


def queue_it(client, *names):
    ids = [str(the_file(name).id) for name in names]
    return client.post("/library/convert", data={"ids": ids, "confirm": "yes"}, follow_redirects=False)


WEBM = "Lone Ranger 1941-01-01.webm"


def test_a_scan_finds_odd_formats(client):
    f = the_file(WEBM)
    assert f is not None and convert.Plan.from_json(f.convert_json).extension == ".mp3"
    page = client.get("/library").text
    assert "1 file in your library is" in page and 'href="/library/convert">Review and convert' in page
    assert 'href="/library/convert">Convert odd formats' in page


def test_the_confirmation_page(client):
    page = client.get("/library/convert").text
    assert "Advanced: this changes your library." in page and "deleted for good" in page
    assert "Lone Ranger 1941-01-01.mp3" in page and "It will be converted to MP3" in page
    assert 'name="ids"' in page and "Convert the ticked files and delete the originals" in page
    assert "Suspense" not in page                                    # plays fine: not listed
    # Nothing happens without the confirmation.
    assert client.post("/library/convert", data={"ids": [str(the_file(WEBM).id)]}).status_code == 400
    assert run_queue(client) is False


def test_converting_in_place_keeps_everything(client, pretend):
    before = the_file(WEBM)
    # The owner has typed details and listened to part of it.
    with Session(engine()) as session:
        f = session.get(AudioFile, before.id)
        f.episode.title, f.episode.sources = "Hi-Yo Silver", {"title": "manual"}
        session.add(Listening(episode_id=f.episode_id, file_id=f.id, position=40.0, updated_at=f.first_seen))
        session.commit()
    sent = queue_it(client, WEBM)
    assert sent.headers["location"] == "/library?convert_queued=1"
    page = client.get("/library").text
    assert "Converting 1 library file." in page and 'hx-get="/library/converting"' in page
    assert "1 file in your library is" not in page                  # asked for already

    assert run_queue(client) is True and pretend == [WEBM]
    assert sorted(p.name for p in (client.lib / "Lone Ranger").iterdir()) == ["Lone Ranger 1941-01-01.mp3"]
    after = the_file("Lone Ranger 1941-01-01.mp3")
    assert after.id == before.id and after.episode_id == before.episode_id      # the same record
    assert after.convert_json is None and after.duration_seconds == 2.0
    with Session(engine()) as session:
        assert session.get(Episode, after.episode_id).title == "Hi-Yo Silver"
        assert session.get(Listening, after.episode_id).file_id == after.id
        assert session.scalar(select(func.count()).select_from(LibraryConversion)) == 0
    assert "Converting finished" in client.get("/library/converting").text
    # A scan afterwards sees nothing new, gone or missing.
    client.post("/rescan")
    with Session(engine()) as session:
        assert session.scalar(select(func.count(AudioFile.id))) == 2
        assert session.scalar(select(func.count(Episode.id))) == 2
    assert the_file("Lone Ranger 1941-01-01.mp3").missing is False
    assert "in your library is" not in client.get("/library").text


def test_a_failure_keeps_the_original(client, monkeypatch):
    def broken(plan, source, target, progress=None, stop=None, cover=None):
        target.write_bytes(b"half")
        raise RuntimeError("Invalid data found when processing input")
    monkeypatch.setattr(convert, "run", broken)
    queue_it(client, WEBM)
    run_queue(client)
    assert [p.name for p in (client.lib / "Lone Ranger").iterdir()] == [WEBM]   # nothing half-made left
    page = client.get("/library").text
    assert "1 couldn't be converted" in page
    page = client.get("/library/convert").text
    assert "Converting failed: Invalid data found" in page and 'name="ids"' in page
    queue_it(client, WEBM)                                             # tick it and try again
    with Session(engine()) as session:
        assert session.scalar(select(LibraryConversion.status)) == "waiting"


def test_stopping(client, pretend):
    queue_it(client, WEBM)
    sent = client.post("/library/convert/cancel", follow_redirects=False)
    assert sent.headers["location"] == "/library?convert_cancelled=1"
    assert run_queue(client) is False and pretend == []
    assert "1 file in your library is" in client.get("/library").text   # offered again


def test_an_original_that_changes_meanwhile_isnt_replaced(client, monkeypatch):
    def meddling(plan, source, target, progress=None, stop=None, cover=None):
        make_wav(target, 2)
        source.write_bytes(b"something else entirely, and longer")   # edited outside the app
    monkeypatch.setattr(convert, "run", meddling)
    queue_it(client, WEBM)
    run_queue(client)
    assert [p.name for p in (client.lib / "Lone Ranger").iterdir()] == [WEBM]
    with Session(engine()) as session:
        job = session.scalar(select(LibraryConversion))
        assert job.status == "failed" and "changed while it was converting" in job.error


def test_a_name_thats_taken_gets_a_number(client):
    make_wav(client.lib / "Lone Ranger" / "Lone Ranger 1941-01-01.mp3", 1)
    client.post("/rescan")
    queue_it(client, WEBM)
    run_queue(client)
    assert sorted(p.name for p in (client.lib / "Lone Ranger").iterdir()) == [
        "Lone Ranger 1941-01-01 (2).mp3", "Lone Ranger 1941-01-01.mp3"]


def test_a_part_of_a_group_stays_in_its_group(client):
    make_wav(client.lib / "Lone Ranger" / "Lone Ranger 1941-01-01 Part 2.wav", 1)
    client.post("/rescan")
    first, second = the_file(WEBM), the_file("Lone Ranger 1941-01-01 Part 2.wav")
    client.post(f"/files/{first.id}/parts", data={"part": [str(first.id), str(second.id)]})
    part = the_file(WEBM).part_number
    assert part is not None
    queue_it(client, WEBM)
    run_queue(client)
    after = the_file("Lone Ranger 1941-01-01.mp3")
    assert after.part_number == part and after.episode_id == the_file("Lone Ranger 1941-01-01 Part 2.wav").episode_id


def test_after_a_restart(client):
    queue_it(client, WEBM)
    with Session(engine()) as session:
        session.scalar(select(LibraryConversion)).status = "converting"
        session.commit()
    (client.lib / "Lone Ranger" / ".converting-abc.mp3").write_bytes(b"half")
    converting.recover(engine(), client.lib / ".." / "inbox", client.lib)
    with Session(engine()) as session:
        assert session.scalar(select(LibraryConversion.status)) == "waiting"
    assert not (client.lib / "Lone Ranger" / ".converting-abc.mp3").exists()


# ------------------------------------------------------- with real ffmpeg

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg isn't installed here")


@needs_ffmpeg
def test_really_converting_a_library_file(tmp_path, monkeypatch):
    import app.main as main
    lib = tmp_path / "library"
    (lib / "Shows").mkdir(parents=True)
    make_wav(tmp_path / "tone.wav", 3)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(tmp_path / "tone.wav"),
                    "-metadata", "title=The Tone", "-c:a", "wmav2", "-b:a", "64k",
                    str(lib / "Shows" / "Old Show 1952-04-26.wma")], check=True)
    monkeypatch.setattr(main, "LIBRARY_DIR", lib)
    monkeypatch.setattr(main, "_engine", make_engine(tmp_path / "data" / "airwave.db"))
    client = TestClient(main.app)
    client.get("/library")
    f = the_file("Old Show 1952-04-26.wma")
    assert convert.Plan.from_json(f.convert_json).target_kbps == 80
    client.post("/library/convert", data={"ids": [str(f.id)], "confirm": "yes"})
    assert converting.convert_next_library(engine(), lib) is True
    assert [p.name for p in (lib / "Shows").iterdir()] == ["Old Show 1952-04-26.mp3"]
    after = the_file("Old Show 1952-04-26.mp3")
    assert after.id == f.id and 2.5 < after.duration_seconds < 3.5 and after.tag_title == "The Tone"
