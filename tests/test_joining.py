"""Tests for joining multi-part episodes (the plan and the preview). Run with:  pytest"""

import shutil
import subprocess
import time

import mutagen
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import joining
from app.db import AudioFile, Episode, Listening, make_engine, utcnow
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
    # The same words on every part don't tell them apart.
    files = [file(f"Show Hr{n}.mp3", "Old title") for n in (1, 2)]
    assert joining.chapter_names(files, "Show") == ["Hour 1", "Hour 2"]


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


def test_tidying_a_leftover_hour_1_from_the_title():
    assert joining.tidy_title("Coast to Coast AM - Hour 1") == "Coast to Coast AM"
    assert joining.tidy_title("Coast To Coast AM - Dec 21 2009 - Hour 1 (part 1 of 4)") == \
        "Coast To Coast AM - Dec 21 2009 - Hour 1"
    assert joining.tidy_title("Show (part 1 of 4)") == "Show"
    assert joining.tidy_title("Show Hr. 1") == "Show" and joining.tidy_title("Show, Pt 01") == "Show"
    for title in ("Part 2 of the Mystery", "The Hour of Decision", "Show - Hour 2", "Hour 1", None):
        assert joining.tidy_title(title) is None


def test_the_preview_shows_the_title_tidied(client):
    with Session(client.engine) as session:
        episode = session.scalars(select(Episode).where(Episode.title == "Coast to Coast AM")).one()
        episode.title = "Coast to Coast AM - Hour 1"
        session.commit()
    page = client.get("/advanced/join/preview", params={"series": "Coast to Coast AM"}).text
    assert "Title tidied: <q>Coast to Coast AM - Hour 1</q> → <q>Coast to Coast AM</q>" in page
    assert 'action="/advanced/join"' in page and "Join 2 episodes" in page


def test_chapters_file():
    text = joining.chapters_text(["Hour 1", "Open Lines; = calls"], [60.0, 30.5])
    assert text.startswith(";FFMETADATA1\n")
    assert "START=60000\nEND=90500\ntitle=Open Lines\; \\= calls" in text


def test_joining_needs_the_preview_first(client):
    assert client.post("/advanced/join", data={"series": "Suspense"}).status_code == 400


# ------------------------------------------------------- with real ffmpeg

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg isn't installed here")


def tone(path, seconds, mono=False, kbps=64, picture=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=f=440:d={seconds}"]
    if picture:
        cmd += ["-i", str(picture), "-map", "0:a", "-map", "1:v", "-c:v", "copy",
                "-disposition:v", "attached_pic"]
    cmd += ["-ar", "44100", "-ac", "1" if mono else "2", "-b:a", f"{kbps}k",
            "-metadata", "title=Old title", str(path)]
    subprocess.run(cmd, check=True)


@pytest.fixture
def real(tmp_path, monkeypatch):
    """A library with one three-part episode: parts 1 and 2 stereo, part 3 mono."""
    import app.main as main
    lib = tmp_path / "library"
    picture = tmp_path / "cover.jpg"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=64x64:d=1",
                    "-frames:v", "1", str(picture)], check=True)
    tone(lib / "Night Show 1996-05-02 Hour 1.mp3", 6, picture=picture)
    tone(lib / "Night Show 1996-05-02 Hour 2.mp3", 5)
    tone(lib / "Night Show 1996-05-02 Hour 3.mp3", 4, mono=True, kbps=32)
    monkeypatch.setattr(main, "LIBRARY_DIR", lib)
    monkeypatch.setattr(main, "_engine", make_engine(tmp_path / "data" / "airwave.db"))
    client = TestClient(main.app)
    client.get("/")
    with Session(main._engine) as session:
        files = sorted(session.scalars(select(AudioFile)), key=lambda f: f.name)
        ids = [f.id for f in files]
        files[2].tag_comment = "Open lines."
        session.commit()
    client.post(f"/files/{ids[0]}/parts", data={"part": [str(i) for i in ids]})
    with Session(main._engine) as session:
        episode = session.get(AudioFile, ids[0]).episode
        episode.series, episode.title, episode.notes = "Night Show", "Night Show - Hour 1", None
        session.add(Listening(episode_id=episode.id, file_id=ids[1], position=2.0, updated_at=utcnow()))
        session.commit()
    client.lib, client.engine, client.ids = lib, main._engine, ids
    return client


def wait_for_joining():
    for _ in range(600):
        if not joining.busy():
            return joining.current_job()
        time.sleep(0.05)
    raise AssertionError("joining didn't finish")


@needs_ffmpeg
def test_really_joining(real):
    sent = real.post("/advanced/join", data={"series": "Night Show", "confirm": "yes"})
    assert sent.status_code == 200
    job = wait_for_joining()
    assert (job.joined, job.deleted, job.problems) == (1, 3, [])
    assert sorted(p.name for p in real.lib.iterdir()) == ["Night Show 1996-05-02.mp3"]
    joined = real.lib / "Night Show 1996-05-02.mp3"
    audio = mutagen.File(joined)
    assert 14.5 < audio.info.length < 15.5
    assert (audio.info.sample_rate, audio.info.channels) == (44100, 2)
    chapters = sorted(audio.tags.getall("CHAP"), key=lambda c: c.start_time)
    assert [c.sub_frames["TIT2"].text[0] for c in chapters] == ["Hour 1", "Hour 2", "Hour 3"]
    assert [round(c.start_time / 1000) for c in chapters] == [0, 6, 11]
    assert str(audio.tags["TIT2"]) == "Night Show" and audio.tags.getall("APIC")
    with Session(real.engine) as session:
        files = session.scalars(select(AudioFile)).all()
        assert [(f.id, f.name, f.part_number) for f in files] == \
            [(real.ids[0], "Night Show 1996-05-02.mp3", None)]
        episode = files[0].episode
        assert episode.title == "Night Show" and episode.notes == "Part 3: Open lines."
        assert episode.sources["title"] == "manual"
        place = session.get(Listening, episode.id)
        assert place.file_id == real.ids[0] and 7.5 < place.position < 8.5   # 2 s into part 2
        assert 14.5 < files[0].duration_seconds < 15.5
    page = real.get("/advanced/joining").text
    assert "Finished." in page and "Joined 1 episode; 3 parts deleted." in page


@needs_ffmpeg
def test_a_joined_file_that_doesnt_check_out_leaves_the_parts(real):
    with Session(real.engine) as session:
        session.get(AudioFile, real.ids[1]).duration_seconds = 50     # the parts "add up" to 60 s
        session.commit()
    before = {p.name: p.read_bytes() for p in real.lib.iterdir()}
    real.post("/advanced/join", data={"series": "Night Show", "confirm": "yes"})
    job = wait_for_joining()
    assert job.joined == 0 and "the parts add up to 60" in job.problems[0][1]
    assert {p.name: p.read_bytes() for p in real.lib.iterdir()} == before     # nothing left behind
    with Session(real.engine) as session:
        assert session.scalars(select(Episode)).one().title == "Night Show - Hour 1"
        assert len(session.scalars(select(AudioFile)).all()) == 3


@needs_ffmpeg
def test_stopping_leaves_the_parts(real, monkeypatch):
    real_run = joining.run_command

    def slow(cmd, duration, progress=None, stop=None):
        joining.stop()
        time.sleep(1.1)              # the stop question is asked at most once a second
        real_run(cmd, duration, progress, stop)

    monkeypatch.setattr(joining, "run_command", slow)
    before = sorted(p.name for p in real.lib.iterdir())
    real.post("/advanced/join", data={"series": "Night Show", "confirm": "yes"})
    job = wait_for_joining()
    assert job.stop and job.joined == 0 and not job.problems
    assert sorted(p.name for p in real.lib.iterdir()) == before
    assert "Stopped." in real.get("/advanced/joining").text
