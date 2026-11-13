"""Tests for pictures (artwork). Run with:  pytest"""

import io
import sqlite3

import pytest
from fastapi.testclient import TestClient
from mutagen.id3 import APIC, TPE1
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.artwork import art_path, folder_and_parents, folder_image_rank, save_picture
from app.db import SCHEMA_VERSION, AudioFile, FolderArt, SeriesArt, make_engine
from app.scanner import sync_library
from test_scanner import add_tags, make_wav


def picture(colour="red", size=(1200, 800), kind="JPEG", mode="RGB") -> bytes:
    data = io.BytesIO()
    Image.new(mode, size, colour).save(data, kind)
    return data.getvalue()


def test_folder_picture_names():
    assert folder_image_rank("cover.jpg") < folder_image_rank("Folder.PNG")
    assert folder_image_rank("cover.jpg") < folder_image_rank("cover.png")
    assert folder_image_rank("holiday.jpg") is None
    assert folder_image_rank("cover.txt") is None
    assert folder_and_parents("Shows/Suspense/1943") == ["Shows/Suspense/1943", "Shows/Suspense", "Shows"]
    assert folder_and_parents("") == [""]


def test_pictures_are_saved_small_and_once(tmp_path):
    first = save_picture(picture(), tmp_path)
    saved = art_path(tmp_path, first)
    with Image.open(saved) as image:
        assert image.format == "JPEG" and max(image.size) == 600
    when = saved.stat().st_mtime_ns
    assert save_picture(picture(), tmp_path) == first        # the same picture...
    assert saved.stat().st_mtime_ns == when                   # ...isn't saved again
    # See-through PNGs work; things that aren't pictures don't.
    assert save_picture(picture((0, 0, 0, 0), (50, 50), "PNG", "RGBA"), tmp_path)
    assert save_picture(b"not a picture at all", tmp_path) is None
    assert save_picture(None, tmp_path) is None


# ---------------------------------------------------------------- in the app


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A library with two series. Coast to Coast has its logo inside each
    file; Campbell Playhouse has a cover.jpg in its folder."""
    import app.main as main
    lib = tmp_path / "library"
    for hour in (1, 2):
        path = lib / "C2C" / f"Coast to Coast - Mar 01 2010 - Hour {hour}.wav"
        make_wav(path, 1)
        add_tags(path, artist=TPE1(encoding=3, text="Coast to Coast AM"),
                 art=APIC(encoding=3, mime="image/jpeg", type=3, desc="", data=picture("blue")))
    for name in ("patriot.wav", "our town.wav"):
        make_wav(lib / "Campbell" / name, 1)
        add_tags(lib / "Campbell" / name, artist=TPE1(encoding=3, text="Campbell Playhouse"))
    (lib / "Campbell" / "cover.jpg").write_bytes(picture("green"))
    (lib / "cover.jpg").write_bytes(picture("yellow"))   # at the top: not for every series
    make_wav(lib / "Loose" / "loose.wav", 1)
    monkeypatch.setattr(main, "LIBRARY_DIR", lib)
    monkeypatch.setattr(main, "_engine", make_engine(tmp_path / "data" / "airwave.db"))
    client = TestClient(main.app)
    client.get("/")   # first visit scans
    client.lib = lib
    client.art_dir = tmp_path / "data" / "artwork"
    return client


def files(client) -> dict[str, AudioFile]:
    import app.main as main
    with Session(main._engine) as session:
        return {f.name: f for f in session.scalars(select(AudioFile))}


def colour_of(client, url) -> tuple:
    response = client.get(url)
    assert response.status_code == 200 and response.headers["content-type"] == "image/jpeg"
    assert "immutable" in response.headers["cache-control"]
    with Image.open(io.BytesIO(response.content)) as image:
        return image.convert("RGB").getpixel((5, 5))


def picture_urls(page: str) -> list[str]:
    import re
    return re.findall(r'<img class="art[^"]*" src="([^"]+)"', page)


def test_pictures_inside_files_are_found_and_shared(client):
    found = files(client)
    hours = [found[f"Coast to Coast - Mar 01 2010 - Hour {h}.wav"] for h in (1, 2)]
    assert hours[0].art_hash and hours[0].art_hash == hours[1].art_hash   # one logo, saved once
    assert len(list(client.art_dir.rglob("*.jpg"))) == 3    # logo + two folder pictures
    url = picture_urls(client.get(f"/files/{hours[0].id}").text)[0]
    assert colour_of(client, url)[2] > 200   # blue


def test_series_tiles_use_the_folder_picture_first(client):
    page = client.get("/series").text
    tiles = page.split('class="tile"')[1:]
    by_name = {t.split('tile-name">')[1].split("<")[0]: t for t in tiles}
    assert colour_of(client, picture_urls(by_name["Campbell Playhouse"])[0])[1] > 100   # green
    assert colour_of(client, picture_urls(by_name["Coast to Coast AM"])[0])[2] > 200    # blue
    # The yellow picture at the top of the library isn't used for the others.
    assert picture_urls(by_name["No series yet"]) == []
    assert "art-none" in by_name["No series yet"]


def test_an_episode_without_its_own_picture_uses_its_folders(client):
    patriot = files(client)["patriot.wav"]
    url = picture_urls(client.get(f"/files/{patriot.id}").text)[0]
    assert colour_of(client, url)[1] > 100   # green, from Campbell/cover.jpg


def test_folder_pictures_follow_the_library(client):
    import app.main as main
    (client.lib / "Campbell" / "cover.jpg").unlink()
    (client.lib / "Campbell" / "folder.png").write_bytes(picture("purple", kind="PNG"))
    client.post("/rescan")
    with Session(main._engine) as session:
        assert session.get(FolderArt, "Campbell").name == "folder.png"
    (client.lib / "Campbell" / "folder.png").unlink()
    client.post("/rescan")
    with Session(main._engine) as session:
        assert session.get(FolderArt, "Campbell") is None
    patriot = files(client)["patriot.wav"]
    assert picture_urls(client.get(f"/files/{patriot.id}").text) == []


def test_unknown_or_odd_picture_names_are_not_found(client):
    assert client.get("/art/0123456789abcdef0123.jpg").status_code == 404
    assert client.get("/art/..%2F..%2Fairwave.db.jpg").status_code == 404


def test_an_older_database_reads_every_file_again_for_pictures(tmp_path):
    lib = tmp_path / "library"
    path = lib / "show.wav"
    make_wav(path, 1)
    add_tags(path, art=APIC(encoding=3, mime="image/jpeg", type=3, desc="", data=picture()))
    db = tmp_path / "data" / "airwave.db"
    engine = make_engine(db)
    sync_library(engine, lib)                 # no art folder: pictures not saved
    engine.dispose()
    with sqlite3.connect(db) as conn:         # pretend it's a version 5 database
        conn.execute("ALTER TABLE audio_files DROP COLUMN art_hash")
        conn.execute("PRAGMA user_version = 5")
    engine = make_engine(db)
    run = sync_library(engine, lib, tmp_path / "data" / "artwork")
    assert run.changed == 1
    with Session(engine) as session:
        assert session.scalars(select(AudioFile)).one().art_hash
    assert (tmp_path / "data" / f"airwave.db.before-v{SCHEMA_VERSION}").exists()


# ---------------------------------------------------------------- uploading


def upload(client, name, data, filename="picture.jpg"):
    from urllib.parse import quote
    return client.post(f"/series/picture?name={quote(name)}",
                       files={"picture": (filename, data, "image/jpeg")}, follow_redirects=False)


def series_tile(client, name) -> str:
    page = client.get("/series").text
    return next(t for t in page.split('class="tile"')[1:] if f'tile-name">{name}<' in t)


def test_an_uploaded_picture_wins_and_can_be_removed(client):
    page = client.get("/series/episodes?name=Campbell%20Playhouse").text
    assert 'href="/series/picture?name=Campbell%20Playhouse"' in page
    assert "picture file in the series" in client.get("/series/picture?name=Campbell%20Playhouse").text

    sent = upload(client, "Campbell Playhouse", picture("red"))
    assert sent.status_code == 303
    assert sent.headers["location"] == "/series/episodes?name=Campbell%20Playhouse"
    assert colour_of(client, picture_urls(series_tile(client, "Campbell Playhouse"))[0])[0] > 200   # red
    # Kept with the uploads, not with the copies a scan can make again.
    assert len(list((client.art_dir.parent / "uploaded-artwork").rglob("*.jpg"))) == 1
    assert "a picture you uploaded" in client.get("/series/picture?name=Campbell%20Playhouse").text
    # Episodes without a picture of their own use it too.
    patriot = files(client)["patriot.wav"]
    assert colour_of(client, picture_urls(client.get(f"/files/{patriot.id}").text)[0])[0] > 200

    # A rescan doesn't undo it.
    client.post("/rescan")
    assert colour_of(client, picture_urls(series_tile(client, "Campbell Playhouse"))[0])[0] > 200

    client.post("/series/picture/remove?name=Campbell%20Playhouse")
    assert colour_of(client, picture_urls(series_tile(client, "Campbell Playhouse"))[0])[1] > 100   # green again


def test_an_uploaded_phone_photo_is_turned_upright(client):
    photo = Image.new("RGB", (400, 200), "red")
    exif = photo.getexif()
    exif[0x0112] = 6   # "turn me a quarter turn": stored sideways, as phones do
    data = io.BytesIO()
    photo.save(data, "JPEG", exif=exif)
    upload(client, "Coast to Coast AM", data.getvalue())
    import app.main as main
    with Session(main._engine) as session:
        art = session.get(SeriesArt, "Coast to Coast AM").art_hash
    with Image.open(art_path(client.art_dir.parent / "uploaded-artwork", art)) as saved:
        assert saved.size == (200, 400)   # taller than wide, the right way up


def test_uploading_something_that_is_not_a_picture(client):
    sent = upload(client, "Campbell Playhouse", b"this is a letter, not a picture", "letter.jpg")
    assert sent.status_code == 400 and "isn&#39;t a picture the app can read" in sent.text
    sent = upload(client, "Campbell Playhouse", b"")
    assert sent.status_code == 400 and "choose a picture first" in sent.text
    assert client.get("/series/picture?name=Nothing%20Like%20This").status_code == 404
    assert upload(client, "Nothing Like This", picture()).status_code == 404


def test_renaming_a_whole_series_keeps_its_uploaded_picture(client):
    upload(client, "Campbell Playhouse", picture("red"))
    found = files(client)
    for name in ("patriot.wav", "our town.wav"):
        client.post(f"/files/{found[name].id}/edit", data={"series": "The Campbell Playhouse"})
        # (after the first, one episode still has the old name: nothing moves yet)
    tile = series_tile(client, "The Campbell Playhouse")
    assert colour_of(client, picture_urls(tile)[0])[0] > 200   # red came along
    import app.main as main
    with Session(main._engine) as session:
        assert session.get(SeriesArt, "Campbell Playhouse") is None


def test_the_player_can_find_an_episodes_picture(client):
    found = files(client)
    hour = found["Coast to Coast - Mar 01 2010 - Hour 1.wav"]
    page = client.get(f"/files/{hour.id}").text
    assert f'data-art="/files/{hour.id}/art"' in page
    sent = client.get(f"/files/{hour.id}/art", follow_redirects=False)
    assert sent.status_code == 307 and sent.headers["location"] == f"/art/{hour.art_hash}.jpg"
    assert client.get(f"/files/{found['loose.wav'].id}/art").status_code == 404   # no picture


def test_an_uploaded_picture_is_used_for_every_episode_even_with_its_own(client):
    """Coast to Coast has its logo inside each file; once a picture is
    uploaded for the series, every episode shows that one instead."""
    import re
    found = files(client)
    hour = found["Coast to Coast - Mar 01 2010 - Hour 1.wav"]
    blue = hour.art_hash
    upload(client, "Coast to Coast AM", picture("red"))
    import app.main as main
    with Session(main._engine) as session:
        red = session.get(SeriesArt, "Coast to Coast AM").art_hash
    assert red != blue
    # The episode's page, the player bar and Continue listening.
    assert picture_urls(client.get(f"/files/{hour.id}").text) == [f"/art/{red}.jpg"]
    sent = client.get(f"/files/{hour.id}/art", follow_redirects=False)
    assert sent.headers["location"] == f"/art/{red}.jpg"
    # A podcast feed's episodes too.
    sent = client.post("/feeds", data={"title": "Mix", "ids": [str(hour.episode_id),
                                                               str(found["patriot.wav"].episode_id)]},
                       follow_redirects=False)
    feed_id = int(re.search(r"/feeds/(\d+)", sent.headers["location"])[1])
    client.post(f"/feeds/{feed_id}/publish", data={"published": "1"})
    with Session(main._engine) as session:
        from app.db import Feed
        secret = session.get(Feed, feed_id).token
    xml = client.get(f"/feeds/{secret}/feed.xml").text
    assert blue not in xml and red in xml
    # Taking the upload away brings back the file's own picture.
    client.post("/series/picture/remove?name=Coast%20to%20Coast%20AM")
    assert picture_urls(client.get(f"/files/{hour.id}").text) == [f"/art/{blue}.jpg"]
