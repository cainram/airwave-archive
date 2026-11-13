"""Tests for importing zips (Airwave exports, or any zip of recordings)
into the inbox. Run with:  pytest"""

import html
import io
import zipfile

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import AudioFile, Episode, InboxItem, SeriesArt, SeriesGenre, make_engine
from app.genres import genres_of
from app.inbox import airwave_details
from test_export import make_mp3
from test_scanner import make_wav


def server(tmp_path, monkeypatch, name):
    """A separate Airwave Archive (its own library, inbox and database)."""
    import app.main as main
    root = tmp_path / name
    lib, box = root / "library", root / "inbox"
    lib.mkdir(parents=True)
    monkeypatch.setattr(main, "LIBRARY_DIR", lib)
    monkeypatch.setattr(main, "INBOX_DIR", box)
    monkeypatch.setattr(main, "_engine", make_engine(root / "data" / "airwave.db"))
    client = TestClient(main.app)
    client.lib, client.box, client.engine = lib, box, main._engine
    return client


def png() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (40, 40), (200, 30, 30)).save(out, "PNG")
    return out.getvalue()


@pytest.fixture
def exported(tmp_path, monkeypatch) -> bytes:
    """An export zip from a first server: two Suspense episodes with full
    details, and a two-part Coast to Coast AM episode."""
    sender = server(tmp_path, monkeypatch, "sender")
    make_mp3(sender.lib / "otr" / "a.mp3")
    make_mp3(sender.lib / "otr" / "b.mp3")
    make_wav(sender.lib / "c2c" / "Art Bell 1996-05-02 Hour 1.wav", 1)
    make_wav(sender.lib / "c2c" / "Art Bell 1996-05-02 Hour 2.wav", 1)
    sender.get("/")
    with Session(sender.engine) as session:
        files = {f.name: f for f in session.scalars(select(AudioFile))}
        for name, day in (("a.mp3", 8), ("b.mp3", 15)):
            ep = files[name].episode
            ep.series, ep.title, ep.episode_number = "Suspense", f"Story {day}", str(day)
            ep.air_year, ep.air_month, ep.air_day = 1942, 10, day
            ep.air_time, ep.air_timezone = "21:00", "America/New_York"
            ep.network, ep.cast, ep.notes = "CBS", "Orson Welles", "A good one."
        for f in files.values():
            if f.name.startswith("Art Bell"):
                f.episode.series = "Coast to Coast AM"
        session.commit()
        ids = [files["Art Bell 1996-05-02 Hour 1.wav"].id, files["Art Bell 1996-05-02 Hour 2.wav"].id]
    sender.post(f"/files/{ids[0]}/parts", data={"part": [str(i) for i in ids]})
    sender.post("/series/picture?name=Suspense", files={"picture": ("cover.png", png(), "image/png")})
    sender.post("/series/genres?name=Suspense", data={"new_genres": "Thriller, Mystery"})
    with Session(sender.engine) as session:
        episode_ids = [str(e.id) for e in session.scalars(select(Episode))]
    data = sender.get("/export/download", params={"ids": episode_ids}).content
    zipfile.ZipFile(io.BytesIO(data)).testzip()
    return data


class Page:
    """A reply, with its text read as a browser shows it."""
    def __init__(self, response):
        self.status_code, self.text = response.status_code, html.unescape(response.text)


def upload(client, name, data, mime="application/zip"):
    return Page(client.post("/inbox/upload", files=[("files", (name, data, mime))]))


def waiting(client) -> dict[str, InboxItem]:
    with Session(client.engine) as session:
        return {i.name: i for i in session.scalars(select(InboxItem))}


def test_an_export_arrives_filled_in(tmp_path, monkeypatch, exported):
    receiver = server(tmp_path, monkeypatch, "receiver")
    page = upload(receiver, "Airwave - 3 episodes.zip", exported).text
    assert "Unpacked the zip: 4 recordings are waiting below." in page
    assert "All of them came with their details from Airwave Archive." in page
    assert not list(receiver.box.glob("*.zip")) and not list(receiver.box.glob(".*"))
    items = waiting(receiver)
    assert set(items) == {"a.mp3", "b.mp3", "Art Bell 1996-05-02 Hour 1.wav",
                          "Art Bell 1996-05-02 Hour 2.wav"}
    a = items["a.mp3"]
    assert (a.series, a.title, a.episode_number) == ("Suspense", "Story 8", "8")
    assert (a.air_year, a.air_month, a.air_day) == (1942, 10, 8)
    assert a.extra["network"] == "CBS" and a.extra["air_time"] == "21:00"
    assert a.extra["picture"] and items["Art Bell 1996-05-02 Hour 2.wav"].extra["part"] == 2
    # The page shows the extra details.
    assert "Aired at 9:00 PM US Eastern" in page and "Station: CBS" in page and "Part 2 of 2" in page
    assert a.extra["genres"] == ["Mystery", "Thriller"] and "Genres: Mystery, Thriller" in page
    assert "genres" not in items["Art Bell 1996-05-02 Hour 1.wav"].extra
    assert "Add all 4 to library" in page

    page = receiver.post("/inbox/add-all", follow_redirects=True).text
    assert "Added 4 files to your library" in page
    with Session(receiver.engine) as session:
        episodes = {e.series: e for e in session.scalars(select(Episode))}
        assert len(session.scalars(select(Episode)).all()) == 3     # the two parts are one
        c2c = episodes["Coast to Coast AM"]
        assert [f.part_number for f in c2c.parts] == [1, 2]
        assert {f.folder for f in c2c.files} == {"Coast to Coast AM"}
        story = session.scalar(select(Episode).where(Episode.title == "Story 15"))
        assert (story.air_time, story.air_timezone, story.network, story.cast) == (
            "21:00", "America/New_York", "CBS", "Orson Welles")
        assert story.notes == "A good one." and len(story.uid) == 32
        assert story.sources["network"] == "manual"
        assert session.get(SeriesArt, "Suspense") is not None       # the picture came too
        assert session.get(SeriesArt, "Coast to Coast AM") is None
        assert genres_of(session) == {"Suspense": ["Mystery", "Thriller"]}   # the genres came too


def test_genres_you_set_are_kept(tmp_path, monkeypatch, exported):
    receiver = server(tmp_path, monkeypatch, "receiver")
    with Session(receiver.engine) as session:
        session.add(SeriesGenre(series="Suspense", genre="Anthology"))
        session.commit()
    upload(receiver, "Airwave - 3 episodes.zip", exported)
    receiver.post("/inbox/add-all")
    with Session(receiver.engine) as session:
        assert genres_of(session) == {"Suspense": ["Anthology"]}


def test_episodes_you_have_are_spotted(tmp_path, monkeypatch, exported):
    receiver = server(tmp_path, monkeypatch, "receiver")
    upload(receiver, "first.zip", exported)
    receiver.post("/inbox/add-all")
    # The same zip again: every episode is already here.
    page = upload(receiver, "again.zip", exported).text
    assert page.count("Already in your library:") == 4
    assert "Add to library</button>" not in page and "to library</button>" not in page
    page = receiver.post("/inbox/add-all", follow_redirects=True).text
    assert "Added 0 files" in page and "4 were left in the inbox because you already have" in page
    assert len(waiting(receiver)) == 4
    page = receiver.post("/inbox/reject-duplicates", follow_redirects=True).text
    assert "4 files were deleted from the inbox." in page and not waiting(receiver)
    with Session(receiver.engine) as session:
        assert len(session.scalars(select(AudioFile)).all()) == 4      # nothing doubled


def test_a_missing_part_joins_its_episode(tmp_path, monkeypatch, exported):
    receiver = server(tmp_path, monkeypatch, "receiver")
    upload(receiver, "x.zip", exported)
    items = waiting(receiver)
    receiver.post(f"/inbox/{items['Art Bell 1996-05-02 Hour 1.wav'].id}/add")
    page = receiver.get("/inbox").text
    assert page.count("Already in your library:") == 0               # part 2 is still to come
    receiver.post(f"/inbox/{items['Art Bell 1996-05-02 Hour 2.wav'].id}/add")
    with Session(receiver.engine) as session:
        hour2 = session.scalar(select(AudioFile).where(AudioFile.name.like("%Hour 2%")))
        assert hour2.part_number == 2 and len(hour2.episode.files) == 2


def test_changing_the_date_drops_the_time(tmp_path, monkeypatch, exported):
    receiver = server(tmp_path, monkeypatch, "receiver")
    upload(receiver, "x.zip", exported)
    item = waiting(receiver)["a.mp3"]
    form = {f"{k}-{item.id}": v for k, v in
            {"series": "Suspense", "episode_number": "8", "title": "Story 8", "date": "1942-10-09"}.items()}
    receiver.post(f"/inbox/{item.id}/add", data=form)
    with Session(receiver.engine) as session:
        ep = session.scalar(select(Episode).where(Episode.title == "Story 8"))
        assert (ep.air_day, ep.air_time, ep.air_timezone) == (9, None, None)
        assert ep.network == "CBS"


def test_a_plain_zip_of_recordings(tmp_path, monkeypatch):
    receiver = server(tmp_path, monkeypatch, "receiver")
    make_mp3(tmp_path / "made" / "x.mp3")
    sound = (tmp_path / "made" / "x.mp3").read_bytes()
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as zf:
        zf.writestr("Shows/Suspense 1943-05-13 The Hitchhiker.mp3", sound)
        zf.writestr("../../escape.mp3", sound)
        zf.writestr("__MACOSX/Shows/._Suspense.mp3", b"junk")
        zf.writestr("readme.txt", "hello")
        zf.writestr("manifest.json", "{not json")
    page = upload(receiver, "shows.zip", data.getvalue()).text
    assert "4 recording" not in page and "2 recordings are waiting below." in page
    assert "came with" not in page
    items = waiting(receiver)
    assert set(items) == {"Suspense 1943-05-13 The Hitchhiker.mp3", "escape.mp3"}
    assert (tmp_path / "receiver" / "inbox" / "escape.mp3").is_file()
    assert not (tmp_path / "escape.mp3").exists()
    item = items["Suspense 1943-05-13 The Hitchhiker.mp3"]
    assert item.series == "Suspense" and item.air_day == 13 and not item.extra


def test_a_broken_zip_is_explained(tmp_path, monkeypatch):
    receiver = server(tmp_path, monkeypatch, "receiver")
    sent = upload(receiver, "broken.zip", b"this is not a zip")
    assert sent.status_code == 400
    assert "“broken.zip” isn't a zip file the app can open." in sent.text
    assert not list(receiver.box.iterdir())
    empty = io.BytesIO()
    with zipfile.ZipFile(empty, "w") as zf:
        zf.writestr("notes.txt", "no audio here")
    assert "There are no recordings in “empty.zip”." in upload(receiver, "empty.zip", empty.getvalue()).text


def test_a_zip_copied_into_the_inbox_folder(tmp_path, monkeypatch, exported):
    receiver = server(tmp_path, monkeypatch, "receiver")
    receiver.box.mkdir(parents=True)
    (receiver.box / "From Sam.zip").write_bytes(exported)
    page = receiver.get("/inbox").text
    assert "Zips to unpack (1)" in page and "From Sam.zip" in page
    page = receiver.post("/inbox/unpack", data={"name": "From Sam.zip"}, follow_redirects=True).text
    assert "4 recordings are waiting below." in page and "Zips to unpack" not in page
    assert receiver.post("/inbox/unpack", data={"name": "../x.zip"}).status_code == 404


def test_airwave_details_that_dont_make_sense_are_left_out():
    main, extra = airwave_details({
        "SERIES": "Suspense", "AIR_DATE": "1942-13-40", "AIR_TIME": "21:00",
        "TIME_ZONE": "Mars/Olympus", "PART": "3/2", "ID": "../../bad", "CAST": "  ",
    })
    assert main == {"series": "Suspense", "episode_number": None, "title": None}
    assert extra == {}
    main, extra = airwave_details({"air_date": "1942-10", "air_time": "21:00", "part": "1/2",
                                   "time_zone": "America/New_York", "id": "a" * 32})
    assert (main["air_year"], main["air_month"], main["air_day"]) == (1942, 10, None)
    assert extra == {"part": 1, "parts": 2, "uid": "a" * 32}     # no time without a full day


def test_a_tagged_copy_on_its_own_brings_its_details(tmp_path, monkeypatch, exported):
    """A file from an export, uploaded by itself (no zip, no manifest)."""
    receiver = server(tmp_path, monkeypatch, "receiver")
    sound = zipfile.ZipFile(io.BytesIO(exported)).read("Suspense/b.mp3")
    receiver.post("/inbox/upload", files=[("files", ("b.mp3", sound, "audio/mpeg"))])
    item = waiting(receiver)["b.mp3"]
    assert item.title == "Story 15" and item.air_day == 15
    assert item.extra["network"] == "CBS" and len(item.extra["uid"]) == 32
    assert item.extra["genres"] == ["Mystery", "Thriller"]          # from its Airwave tag
