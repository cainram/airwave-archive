"""Tests for the upload inbox. Run with:  pytest"""

import io

import pytest
from fastapi.testclient import TestClient
from mutagen.id3 import TALB, TDRC, TIT2, TPE1
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import AudioFile, InboxItem, make_engine
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


def test_episode_numbers_come_from_names_and_tags(client):
    make_wav(client.box / "Black Museum - 07 - A Can Of Weedkiller.wav", 1)
    tagged = client.box / "Black Museum Weedkiller.wav"
    make_wav(tagged, 1)
    add_tags(tagged, album=TALB(encoding=3, text="xx/xx/1952 episode 09"))
    client.get("/inbox")
    found = items(client)
    named = found["Black Museum - 07 - A Can Of Weedkiller.wav"]
    assert (named.series, named.episode_number, named.title) == ("Black Museum", "7", "A Can Of Weedkiller")
    assert found["Black Museum Weedkiller.wav"].episode_number == "9"


def test_a_blank_episode_number_is_filled_in_but_a_typed_one_is_kept(client):
    import app.main as main
    make_wav(client.box / "Black Museum - 07 - A Can Of Weedkiller.wav", 1)
    make_wav(client.box / "Black Museum - 08 - The Canvas Bag.wav", 1)
    client.get("/inbox")
    with Session(main._engine) as session:   # as if read before numbers were understood
        for item in session.scalars(select(InboxItem)):
            item.episode_number = None if "07" in item.name else "108"
        session.commit()
    client.get("/inbox")
    found = items(client)
    assert found["Black Museum - 07 - A Can Of Weedkiller.wav"].episode_number == "7"
    assert found["Black Museum - 08 - The Canvas Bag.wav"].episode_number == "108"


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


def test_a_big_inbox_can_be_saved(client):
    """700 waiting files send 2,800 boxes: more than the web framework's
    usual limit of 1,000 (the error was "Too many fields")."""
    make_wav(client.box / "mystery.wav", 1)
    client.get("/inbox")
    item = items(client)["mystery.wav"]
    data = {f"{key}-{n}": "" for n in range(100_000, 100_700)
            for key in ("series", "episode_number", "date", "title")}
    data.update({f"series-{item.id}": "Suspense", f"title-{item.id}": "Typed",
                 f"episode_number-{item.id}": "", f"date-{item.id}": ""})
    sent = client.post("/inbox", data=data, follow_redirects=False)
    assert sent.status_code == 303, sent.text
    assert items(client)["mystery.wav"].title == "Typed"


def test_more_than_a_thousand_files_can_be_uploaded_at_once(client):
    files = [("files", (f"notes {n}.txt", b"x")) for n in range(1001)]
    sent = client.post("/inbox/upload", files=files, follow_redirects=False)
    assert sent.headers["location"] == "/inbox?uploaded=0&skipped=1001"


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


# ------------------------------------------------------- adding to the library

def test_folder_names():
    from app.inbox import folder_name
    assert folder_name("Suspense") == "Suspense"
    assert folder_name("Lux Radio Theatre: Hollywood/USA") == "Lux Radio Theatre_ Hollywood_USA"
    assert folder_name("../..") == "_"                  # never climbs out of the library
    assert folder_name("...") == "Unsorted"
    assert folder_name(None) == "Unsorted"


def episode_of(name):
    import app.main as main
    with Session(main._engine) as session:
        f = session.scalars(select(AudioFile).where(AudioFile.name == name)).one()
        return f.folder, f.episode.series, f.episode.title, f.episode.episode_number, \
            (f.episode.air_year, f.episode.air_month, f.episode.air_day), f.episode.sources


def test_adding_a_file_to_the_library(client):
    make_wav(client.box / "new show.wav", 1)
    client.get("/inbox")
    item = items(client)["new show.wav"]
    # What's typed on the page is saved first, then the file is added.
    sent = client.post(f"/inbox/{item.id}/add", follow_redirects=False, data={
        f"series-{item.id}": "Suspense", f"title-{item.id}": "Donovan's Brain",
        f"episode_number-{item.id}": "7", f"date-{item.id}": "1943-05-13"})
    assert sent.headers["location"].startswith("/inbox?added=")
    assert not (client.box / "new show.wav").exists()
    assert (client.lib / "Suspense" / "new show.wav").exists()      # a new folder for the series
    folder, series, title, number, date, sources = episode_of("new show.wav")
    assert (folder, series, title, number, date) == ("Suspense", "Suspense", "Donovan's Brain", "7", (1943, 5, 13))
    assert sources["title"] == "manual" and sources["air_date"] == "manual"
    assert items(client) == {}
    page = client.get(sent.headers["location"]).text
    assert "Added “new show.wav” to your library" in page and "<strong>Suspense</strong>" in page
    assert "Donovan&#39;s Brain" in client.get("/series/episodes?name=Suspense").text


def test_files_go_next_to_their_series_and_never_replace_anything(client):
    make_wav(client.lib / "otr_suspense" / "show.wav", 1)
    client.post("/rescan")
    import app.main as main
    with Session(main._engine) as session:
        session.scalars(select(AudioFile)).one().episode.series = "Suspense"
        session.commit()
    make_wav(client.box / "show.wav", 2)
    client.get("/inbox")
    item = items(client)["show.wav"]
    client.post(f"/inbox/{item.id}/add", data={
        f"series-{item.id}": "Suspense", f"title-{item.id}": "", f"episode_number-{item.id}": "",
        f"date-{item.id}": ""})
    assert sorted(p.name for p in (client.lib / "otr_suspense").iterdir()) == ["show (2).wav", "show.wav"]
    assert episode_of("show (2).wav")[:2] == ("otr_suspense", "Suspense")


def test_a_file_without_a_series_goes_to_unsorted(client):
    make_wav(client.box / "who knows.wav", 1)
    client.get("/inbox")
    item = items(client)["who knows.wav"]
    client.post(f"/inbox/{item.id}/add")
    assert (client.lib / "Unsorted" / "who knows.wav").exists()
    assert episode_of("who knows.wav")[4] == (None, None, None)     # no date invented


def test_a_bad_date_stops_the_add(client):
    make_wav(client.box / "show.wav", 1)
    client.get("/inbox")
    item = items(client)["show.wav"]
    sent = client.post(f"/inbox/{item.id}/add", data={
        f"series-{item.id}": "Suspense", f"title-{item.id}": "", f"episode_number-{item.id}": "",
        f"date-{item.id}": "1943-13"})
    assert sent.status_code == 400
    assert (client.box / "show.wav").exists() and list(client.lib.iterdir()) == []


def test_moving_across_disks_copies_then_removes(tmp_path, monkeypatch):
    import os
    from app import inbox
    source = tmp_path / "in" / "a.wav"
    make_wav(source, 1)
    original = source.read_bytes()
    real_rename = os.rename
    def no_rename(old, new):                 # like moving between two disks
        if str(old) == str(source):
            raise OSError(18, "Invalid cross-device link")
        return real_rename(old, new)
    monkeypatch.setattr(os, "rename", no_rename)
    moved = inbox.move_into(source, tmp_path / "lib" / "Suspense")
    assert moved.read_bytes() == original and not source.exists()
    assert [p.name for p in moved.parent.iterdir()] == ["a.wav"]    # no leftover temporary file


def test_adding_everything_at_once(client):
    for name in ("a.wav", "b.wav", "c.wav"):
        make_wav(client.box / name, 1)
    client.get("/inbox")
    found = items(client)
    data = {}
    for name, series in (("a.wav", "Suspense"), ("b.wav", "Suspense"), ("c.wav", "")):
        i = found[name].id
        data.update({f"series-{i}": series, f"title-{i}": "", f"episode_number-{i}": "",
                     f"date-{i}": ""})
    data[f"date-{found['c.wav'].id}"] = "1943-02-30"            # one bad date: nothing is added
    assert client.post("/inbox/add-all", data=data).status_code == 400
    assert len(list(client.box.iterdir())) == 3 and list(client.lib.iterdir()) == []

    data[f"date-{found['c.wav'].id}"] = "1943"
    sent = client.post("/inbox/add-all", data=data, follow_redirects=False)
    assert sent.headers["location"] == "/inbox?added_all=3&folder=Suspense&folder=Unsorted"
    assert list(client.box.iterdir()) == [] and items(client) == {}
    assert sorted(p.name for p in (client.lib / "Suspense").iterdir()) == ["a.wav", "b.wav"]
    assert episode_of("c.wav")[4] == (1943, None, None)
    page = client.get(sent.headers["location"]).text
    assert "Added 3 files to your library" in page and "<strong>Unsorted</strong>" in page
