"""Tests for the inbox's converting queue (Phase 13b). Run with:  pytest

Most use a stand-in for ffmpeg, so they run anywhere; the last ones use
the real thing and are skipped where it isn't installed.
"""

import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import convert, converting
from app.db import AudioFile, InboxItem, make_engine
from test_convert import probed, sound
from test_scanner import make_wav

WEBM = "The Lone Ranger 1941-01-01 - Hi-Yo Silver.webm"


@pytest.fixture
def client(tmp_path, monkeypatch):
    import app.main as main
    lib, box = tmp_path / "library", tmp_path / "inbox"
    lib.mkdir()
    box.mkdir()
    monkeypatch.setattr(main, "LIBRARY_DIR", lib)
    monkeypatch.setattr(main, "INBOX_DIR", box)
    monkeypatch.setattr(main, "_engine", make_engine(tmp_path / "data" / "airwave.db"))
    client = TestClient(main.app)
    client.get("/")
    client.box, client.lib, client.tmp = box, lib, tmp_path
    return client


@pytest.fixture
def pretend(monkeypatch, tmp_path):
    """A stand-in ffmpeg: "converting" writes a short WAV (named .mp3)."""
    monkeypatch.setattr(convert, "available", lambda: True)
    monkeypatch.setattr(convert, "probe", lambda path: probed(sound("opus", 1, "48000"), duration="2.0"))
    monkeypatch.setattr(convert, "check", lambda plan, made: None)
    calls = []

    def run(plan, source, target, progress=None, stop=None):
        calls.append(source.name)
        if progress:
            progress(0.5)
        make_wav(target, 2)
    monkeypatch.setattr(convert, "run", run)
    return calls


def engine():
    import app.main as main
    return main._engine


def item(name) -> InboxItem | None:
    with Session(engine()) as session:
        found = session.scalar(select(InboxItem).where(InboxItem.name == name))
        session.expunge_all()
        return found


def form(client) -> dict:
    import re
    page = client.get("/inbox").text
    return dict(re.findall(r'<input id="[^"]+" name="([^"]+)" type="text" value="([^"]*)"', page))


def test_an_upload_is_queued_and_converted(client, pretend):
    (client.box / WEBM).write_bytes(b"\x1aE\xdf\xa3 pretend")
    page = client.get("/inbox").text
    assert "Converting 1 file." in page and 'hx-get="/inbox/converting"' in page
    assert 'hx-target="this"' in page          # replaces only itself, not the whole page
    assert "Waiting to convert" in page and "Cancel converting" in page
    assert item(WEBM).convert_status == "waiting"
    # The owner types a title while it waits.
    data = form(client)
    key = next(k for k in data if k.startswith("title-"))
    data[key] = "Hi-Yo Silver, Away!"
    client.post("/inbox", data=data)

    assert converting.convert_next(engine(), client.box) is True
    assert pretend == [WEBM]
    names = sorted(p.name for p in client.box.iterdir())
    assert names == ["The Lone Ranger 1941-01-01 - Hi-Yo Silver.mp3"]      # original deleted
    new = item(names[0])
    assert new.title == "Hi-Yo Silver, Away!" and new.series == "The Lone Ranger"   # details kept
    assert new.air_year == 1941 and new.convert_status is None and new.convert_json is None
    assert converting.convert_next(engine(), client.box) is False         # nothing left
    # It's an ordinary waiting file now: it plays and can be added.
    page = client.get("/inbox").text
    assert "Converting 1 file" not in page and ">Add to library</button>" in page
    client.post(f"/inbox/{new.id}/add", data=form(client))
    with Session(engine()) as session:
        assert session.scalar(select(AudioFile.name)) == names[0]


def test_the_converting_box_updates_itself(client, pretend):
    (client.box / WEBM).write_bytes(b"pretend")
    client.get("/inbox")
    box = client.get("/inbox/converting").text
    assert "Converting 1 file" in box and "waiting its turn" in box
    with Session(engine()) as session:
        row = session.scalar(select(InboxItem))
        row.convert_status, row.convert_progress = "converting", 0.42
        session.commit()
    assert 'value="42"' in client.get("/inbox/converting").text
    converting.recover(engine(), client.box)          # (as after a restart)
    converting.convert_next(engine(), client.box)
    done = client.get("/inbox/converting").text
    assert "Converting finished" in done and 'form="inbox-form"' in done and "hx-get" not in done


def test_a_failure_keeps_the_original(client, pretend, monkeypatch):
    def broken(plan, source, target, progress=None, stop=None):
        target.write_bytes(b"half")
        raise RuntimeError("Invalid data found when processing input")
    monkeypatch.setattr(convert, "run", broken)
    (client.box / WEBM).write_bytes(b"pretend")
    client.get("/inbox")
    converting.convert_next(engine(), client.box)
    assert [p.name for p in client.box.iterdir()] == [WEBM]       # no half-made file left
    row = item(WEBM)
    assert row.convert_status == "failed" and "Invalid data" in row.convert_error
    page = client.get("/inbox").text
    assert "Converting failed" in page and "Invalid data found" in page and "Try again" in page
    client.post(f"/inbox/{row.id}/convert/retry", data=form(client))
    assert item(WEBM).convert_status == "waiting"


def test_a_new_file_that_fails_its_check(client, pretend, monkeypatch):
    monkeypatch.setattr(convert, "check", lambda plan, made: "The new file couldn't be opened.")
    (client.box / WEBM).write_bytes(b"pretend")
    client.get("/inbox")
    converting.convert_next(engine(), client.box)
    assert [p.name for p in client.box.iterdir()] == [WEBM]
    assert item(WEBM).convert_error == "The new file couldn't be opened."


def test_cancelling(client, pretend):
    (client.box / WEBM).write_bytes(b"pretend")
    client.get("/inbox")
    sent = client.post(f"/inbox/{item(WEBM).id}/convert/cancel", data=form(client), follow_redirects=False)
    assert sent.status_code == 303
    assert item(WEBM).convert_status == "cancelled"
    assert converting.convert_next(engine(), client.box) is False and pretend == []
    page = client.get("/inbox").text
    assert "Converting cancelled" in page and "Try again" in page and "Converting 1 file" not in page
    assert item(WEBM).convert_status == "cancelled"               # a page visit doesn't requeue it


def test_cancelling_midway_stops_ffmpeg(client, pretend, monkeypatch):
    def slow(plan, source, target, progress=None, stop=None):
        target.write_bytes(b"part")
        with Session(engine()) as session:                       # the owner presses Cancel...
            session.scalar(select(InboxItem)).convert_status = "cancelled"
            session.commit()
        if stop():                                               # ...and the helper notices
            raise convert.Stopped()
    monkeypatch.setattr(convert, "run", slow)
    (client.box / WEBM).write_bytes(b"pretend")
    client.get("/inbox")
    converting.convert_next(engine(), client.box)
    assert [p.name for p in client.box.iterdir()] == [WEBM]
    assert item(WEBM).convert_status == "cancelled"


def test_rejecting_while_waiting(client, pretend):
    (client.box / WEBM).write_bytes(b"pretend")
    client.get("/inbox")
    client.post(f"/inbox/{item(WEBM).id}/reject")
    assert converting.convert_next(engine(), client.box) is False and list(client.box.iterdir()) == []


def test_after_a_restart(client, pretend):
    (client.box / WEBM).write_bytes(b"pretend")
    (client.box / ".converting-abc123.mp3").write_bytes(b"half made")
    client.get("/inbox")
    with Session(engine()) as session:
        session.scalar(select(InboxItem)).convert_status = "converting"
        session.commit()
    converting.recover(engine(), client.box)
    assert item(WEBM).convert_status == "waiting"
    assert not (client.box / ".converting-abc123.mp3").exists()


def test_a_name_thats_taken_gets_a_number(client, pretend):
    make_wav(client.box / "Show.mp3", 1)
    (client.box / "Show.webm").write_bytes(b"pretend")
    client.get("/inbox")
    converting.convert_next(engine(), client.box)
    assert sorted(p.name for p in client.box.iterdir()) == ["Show (2).mp3", "Show.mp3"]


# ------------------------------------------------------- with real ffmpeg

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg isn't installed here")


@needs_ffmpeg
@pytest.mark.parametrize("name, codec, becomes", [
    ("Lone Ranger 1941-01-01.webm", ["-c:a", "libopus", "-b:a", "32k"], "Lone Ranger 1941-01-01.mp3"),
    ("Suspense 1943-05-13.mp4", ["-c:a", "aac", "-b:a", "48k"], "Suspense 1943-05-13.m4a"),
    ("Gunsmoke 1952-04-26.wma", ["-c:a", "wmav2", "-b:a", "64k"], "Gunsmoke 1952-04-26.mp3"),
])
def test_really_converting(client, tmp_path, name, codec, becomes):
    make_wav(tmp_path / "tone.wav", 3)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(tmp_path / "tone.wav"),
                    "-metadata", "title=The Tone", *codec, str(client.box / name)], check=True)
    client.get("/inbox")
    assert converting.convert_next(engine(), client.box) is True
    assert [p.name for p in client.box.iterdir()] == [becomes]
    new = item(becomes)
    assert 2.5 < new.duration_seconds < 3.5 and new.title == "The Tone"
    found = convert.probe(client.box / becomes)
    codec_now = [s["codec_name"] for s in found["streams"] if s["codec_type"] == "audio"]
    assert codec_now == (["aac"] if becomes.endswith(".m4a") else ["mp3"])
    client.post(f"/inbox/{new.id}/add", data=form(client))
    with Session(engine()) as session:
        assert session.scalar(select(AudioFile.name)) == becomes


@needs_ffmpeg
def test_a_damaged_file_fails_plainly(client):
    (client.box / "Broken.webm").write_bytes(b"\x1aE\xdf\xa3 not really")
    page = client.get("/inbox").text
    assert "be read: it may be damaged" in page and item("Broken.webm").convert_status is None   # not queued
