"""Tests for the upload inbox. Run with:  pytest"""

import io

import pytest
from fastapi.testclient import TestClient
from mutagen.id3 import TDRC, TIT2, TPE1
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import InboxItem, make_engine
from app.inbox import free_name, safe_name
from test_scanner import add_tags, make_wav


def test_safe_names():
    assert safe_name("Suspense 1943-05-13.MP3") == "Suspense 1943-05-13.mp3"
    assert safe_name("../../etc/evil.mp3") == "evil.mp3"           # no folders
    assert safe_name("C:\\Users\\me\\show.mp3") == "show.mp3"
    assert safe_name('what? "a" show.mp3') == "what_ _a_ show.mp3"
    assert safe_name(".hidden.mp3") == "hidden.mp3"
    assert safe_name("notes.txt") is None                          # not audio
    assert safe_name("") is None


def test_names_are_never_reused(tmp_path):
    (tmp_path / "show.mp3").write_bytes(b"1")
    (tmp_path / "show (2).mp3").write_bytes(b"2")
    assert free_name(tmp_path, "show.mp3") == "show (3).mp3"
    assert free_name(tmp_path, "other.mp3") == "other.mp3"


@pytest.fixture
def client(tmp_path, monkeypatch):
    import app.main as main
    lib, box = tmp_path / "library", tmp_path / "inbox"
    lib.mkdir()
    monkeypatch.setattr(main, "LIBRARY_DIR", lib)
    monkeypatch.setattr(main, "INBOX_DIR", box)
    monkeypatch.setattr(main, "_engine", make_engine(tmp_path / "data" / "airwave.db"))
    client = TestClient(main.app)
    client.get("/")
    client.box, client.lib = box, lib
    return client


def wav_bytes(tmp_path, name, **tags) -> bytes:
    path = tmp_path / "made" / name
    make_wav(path, 1)
    if tags:
        add_tags(path, **tags)
    return path.read_bytes()


def items(client) -> dict[str, InboxItem]:
    import app.main as main
    with Session(main._engine) as session:
        return {i.name: i for i in session.scalars(select(InboxItem))}


def test_uploading_files_into_the_inbox(client, tmp_path):
    tagged = wav_bytes(tmp_path, "a.wav", title=TIT2(encoding=3, text="Donovan's Brain"),
                       artist=TPE1(encoding=3, text="Suspense"), date=TDRC(encoding=3, text="1943"))
    sent = client.post("/inbox/upload", files=[
        ("files", ("Suspense 1943-05-13 - Donovans Brain.wav", tagged, "audio/wav")),
        ("files", ("CampbellPlayhouse39-04-14ThePatriot.wav", wav_bytes(tmp_path, "b.wav"), "audio/wav")),
        ("files", ("notes.txt", b"not audio", "text/plain")),
    ], follow_redirects=False)
    assert sent.headers["location"] == "/inbox?uploaded=2&skipped=1"
    assert sorted(p.name for p in client.box.iterdir()) == [
        "CampbellPlayhouse39-04-14ThePatriot.wav", "Suspense 1943-05-13 - Donovans Brain.wav"]
    assert list(client.lib.iterdir()) == []                         # the library is untouched

    page = client.get(sent.headers["location"]).text
    assert "Uploaded 2 files." in page and "1 wasn&#39;t an audio file" in page
    found = items(client)
    suspense = found["Suspense 1943-05-13 - Donovans Brain.wav"]
    # Tags first (title, series); the name gives the fuller date.
    assert (suspense.series, suspense.title) == ("Suspense", "Donovan's Brain")
    assert (suspense.air_year, suspense.air_month, suspense.air_day) == (1943, 5, 13)
    patriot = found["CampbellPlayhouse39-04-14ThePatriot.wav"]
    assert (patriot.series, patriot.title, patriot.air_day) == ("Campbell Playhouse", "The Patriot", 14)
    assert 'value="1939-04-14"' in page


def test_an_upload_never_replaces_a_waiting_file(client, tmp_path):
    for _ in range(2):
        client.post("/inbox/upload", files=[("files", ("show.wav", wav_bytes(tmp_path, "x.wav"), "audio/wav"))])
    assert sorted(p.name for p in client.box.iterdir()) == ["show (2).wav", "show.wav"]


def test_files_copied_in_by_hand_appear_and_gone_ones_disappear(client):
    make_wav(client.box / "copied.wav", 1)
    assert "copied.wav" in client.get("/inbox").text
    (client.box / "copied.wav").unlink()
    assert "copied.wav" not in client.get("/inbox").text
    assert items(client) == {}


def test_fixing_details(client):
    make_wav(client.box / "mystery.wav", 1)
    client.get("/inbox")
    item = items(client)["mystery.wav"]
    bad = client.post("/inbox", data={f"series-{item.id}": "Suspense", f"title-{item.id}": "Typed",
                                      f"episode_number-{item.id}": "", f"date-{item.id}": "1943-02-30"})
    assert bad.status_code == 400 and "doesn&#39;t have a day 30" in bad.text
    assert 'value="Typed"' in bad.text                     # what was typed is kept
    assert items(client)["mystery.wav"].title is None      # nothing saved
    client.post("/inbox", data={f"series-{item.id}": "Suspense", f"title-{item.id}": "Typed",
                                f"episode_number-{item.id}": "42", f"date-{item.id}": "1943-05"})
    item = items(client)["mystery.wav"]
    assert (item.series, item.title, item.episode_number) == ("Suspense", "Typed", "42")
    assert (item.air_year, item.air_month, item.air_day) == (1943, 5, None)


def test_listening_and_rejecting(client):
    make_wav(client.box / "unwanted.wav", 1)
    make_wav(client.lib / "unwanted.wav", 1)               # same name in the library
    client.get("/inbox")
    item = items(client)["unwanted.wav"]
    sound = client.get(f"/inbox/{item.id}/audio")
    assert sound.status_code == 200 and sound.headers["content-type"] == "audio/wav"
    sent = client.post(f"/inbox/{item.id}/reject", follow_redirects=False)
    assert sent.headers["location"] == "/inbox?rejected=1"
    assert not (client.box / "unwanted.wav").exists()
    assert (client.lib / "unwanted.wav").exists()           # the library is never touched
    assert client.get(f"/inbox/{item.id}/audio").status_code == 404
    assert client.post(f"/inbox/{item.id}/reject").status_code == 404


def test_the_menu_has_the_inbox(client):
    assert '<a href="/inbox" data-paths="/inbox"' in client.get("/").text
