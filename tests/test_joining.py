"""Tests for joining multi-part episodes (the plan and the preview). Run with:  pytest"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import joining
from app.db import AudioFile, Episode, make_engine
from test_export import make_flac


def make_mp3(path, mono=False, rate48=False, frames=40):
    """A tiny silent MP3 (MPEG-1 Layer III, 128 kbps): 44.1 kHz stereo,
    or mono, or 48 kHz."""
    path.parent.mkdir(parents=True, exist_ok=True)
    header = bytes([0xFF, 0xFB, 0x94 if rate48 else 0x90, 0xC4 if mono else 0x64])
    path.write_bytes((header + b"\x00" * ((384 if rate48 else 417) - 4)) * frames)


@pytest.fixture
def client(tmp_path, monkeypatch):
    import app.main as main
    lib = tmp_path / "library"
    make_mp3(lib / "c2c" / "Coast 1996-05-02 Hour 1.mp3")
    make_mp3(lib / "c2c" / "Coast 1996-05-02 Hour 2.mp3")
    make_mp3(lib / "c2c" / "Coast 1996-05-02 Hour 3.mp3", mono=True)
    make_mp3(lib / "c2c" / "Coast 1996-05-03 Hour 1.mp3")
    make_mp3(lib / "c2c" / "Coast 1996-05-03 Hour 2.mp3")
    make_mp3(lib / "c2c" / "Coast 1996-05-03.mp3")                 # a file of that name already
    make_mp3(lib / "otr" / "Suspense Part 1.mp3")
    make_flac(lib / "otr" / "Suspense Part 2.flac")
    make_mp3(lib / "otr" / "Lone.mp3")
    monkeypatch.setattr(main, "LIBRARY_DIR", lib)
    monkeypatch.setattr(main, "_engine", make_engine(tmp_path / "data" / "airwave.db"))
    client = TestClient(main.app)
    client.get("/")
    with Session(main._engine) as session:
        files = {f.name: f for f in session.scalars(select(AudioFile))}
        for f in files.values():
            f.episode.series = "Coast to Coast AM" if f.folder == "c2c" else "Suspense"
        files["Coast 1996-05-02 Hour 2.mp3"].tag_title = "Coast to Coast AM - Hour 2"
        files["Coast 1996-05-02 Hour 3.mp3"].tag_title = "Hour 3 - Open Lines"
        files["Coast 1996-05-02 Hour 3.mp3"].tag_comment = "Callers on remote viewing."
        session.commit()
        ids = {name: f.id for name, f in files.items()}
    for names in (["Coast 1996-05-02 Hour 1.mp3", "Coast 1996-05-02 Hour 2.mp3", "Coast 1996-05-02 Hour 3.mp3"],
                  ["Coast 1996-05-03 Hour 1.mp3", "Coast 1996-05-03 Hour 2.mp3"],
                  ["Suspense Part 1.mp3", "Suspense Part 2.flac"]):
        client.post(f"/files/{ids[names[0]]}/parts", data={"part": [str(ids[n]) for n in names]})
    with Session(main._engine) as session:
        for f in session.scalars(select(AudioFile)):
            if f.name.startswith("Coast 1996-05-02"):
                f.episode.title = "Coast to Coast AM"
                f.episode.notes = "Typed by hand."
        session.commit()
    client.lib, client.engine = lib, main._engine
    return client


def test_reading_a_parts_format(tmp_path):
    make_mp3(tmp_path / "a.mp3")
    make_mp3(tmp_path / "b.mp3", mono=True, rate48=True)
    assert joining.read_sound(tmp_path / "a.mp3") == (joining.Sound("mp3", 44100, 2), 128)
    sound, _ = joining.read_sound(tmp_path / "b.mp3")
    assert sound == joining.Sound("mp3", 48000, 1) and sound.words() == "MP3, 48 kHz, mono"
    (tmp_path / "junk.mp3").write_bytes(b"not audio")
    assert joining.read_sound(tmp_path / "junk.mp3") == (None, None)


def test_re_encoding_a_little_above_the_parts_own_bitrate():
    assert joining.reencode_kbps(32, 1) == 40
    assert joining.reencode_kbps(128, 2) == 160
    assert joining.reencode_kbps(None, 1) == 64
    assert joining.reencode_kbps(320, 2) == 320


def file(name, title=None):
    return AudioFile(folder="", name=name, tag_title=title)


def test_chapter_names():
    # Messy tags: every part's title says "Hour 1"; the file names are right.
    files = [file(f"Coast - Dec 21 2009 - Hour {n}.mp3", f"Coast To Coast AM - Dec 21 2009 - Hour 1 (part {n} of 4)")
             for n in (1, 2, 3, 4)]
    assert joining.chapter_names(files, "Coast To Coast AM - Dec 21 2009 - Hour 1") == \
        ["Hour 1", "Hour 2", "Hour 3", "Hour 4"]
    # The titles' markers when the file names have none; "Part 2" when neither helps.
    files = [file(f"show{n}.mp3", f"Show (part {n} of 2)") for n in (1, 2)]
    assert joining.chapter_names(files, "Show") == ["Part 1", "Part 2"]
    files = [file(f"show{n}.mp3", "Show") for n in (1, 2)]
    assert joining.chapter_names(files, "Show") == ["Part 1", "Part 2"]
    # A part's own words are kept; "Hr 2" is tidied.
    files = [file("Show Hr1.mp3", "Show - Hour 1"), file("Show Hr2.mp3", "Show - Hour 2 - Open Lines")]
    assert joining.chapter_names(files, "Show") == ["Hour 1", "Show - Hour 2 - Open Lines"]


def test_the_series_to_choose_from(client):
    assert "/advanced/join" in client.get("/advanced").text
    page = client.get("/advanced/join").text
    assert 'value="Coast to Coast AM"' in page and 'value="Suspense"' in page
    with Session(client.engine) as session:
        assert joining.series_with_parts(session) == [
            {"name": "Coast to Coast AM", "episodes": 2}, {"name": "Suspense", "episodes": 1}]


def test_the_preview(client):
    before = {p: p.read_bytes() for p in client.lib.rglob("*.*")}
    page = client.get("/advanced/join/preview",
                      params={"series": ["Coast to Coast AM", "Suspense"]}).text
    assert "2 episodes (5 files) can be joined." in page
    assert "1 without touching the sound; 1 with a part re-encoded" in page
    assert "1 episode can't be joined yet" in page and "convert first" in page
    # The three-hour show: part 3 is mono, the odd one out.
    assert "c2c/Coast 1996-05-02.mp3" in page and "Re-encode part 3" in page
    assert "MP3, 44.1 kHz, mono, re-encoded at 160 kbps" in page
    assert "chapter “Hour 1”" in page and "chapter “Hour 2”" in page
    assert "chapter “Hour 3 - Open Lines”" in page
    assert "Added to the notes: <q>Part 3: Callers on remote viewing.</q>" in page
    # The other: a file of the joined name is there already.
    assert "c2c/Coast 1996-05-03 (joined).mp3" in page and "Quick join" in page
    assert "Nothing has been changed yet." in page
    assert {p: p.read_bytes() for p in client.lib.rglob("*.*")} == before


def test_the_odd_one_out_is_the_one_with_less_time(client):
    with Session(client.engine) as session:
        for f in session.scalars(select(AudioFile).where(AudioFile.name.like("Coast 1996-05-02%"))):
            f.duration_seconds = 3600 if "Hour 3" in f.name else 600   # mono: most of the time
        session.commit()
        episode = session.scalars(select(Episode).where(Episode.title == "Coast to Coast AM")).one()
        plan = joining.plan_episode(episode, client.lib, {})
    assert plan.sound == joining.Sound("mp3", 44100, 1)
    assert [p.reencode for p in plan.parts] == [True, True, False]


def test_nothing_ticked(client):
    assert "No series were ticked." in client.get("/advanced/join/preview").text
