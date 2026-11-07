"""Tests for podcast feeds (RSS). Run with:  pytest"""

import re
from datetime import datetime, timedelta
from xml.etree import ElementTree as ET

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import AudioFile, Feed, make_engine
from test_export import make_flac, make_mp3
from test_scanner import make_wav

ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"


@pytest.fixture
def client(tmp_path, monkeypatch):
    import app.main as main
    lib = tmp_path / "library"
    make_mp3(lib / "Suspense" / "a.mp3")
    make_mp3(lib / "Suspense" / "b.mp3")
    make_mp3(lib / "Suspense" / "c.mp3")
    make_flac(lib / "Suspense" / "d.flac")
    make_wav(lib / "c2c" / "Art Bell 1996-05-02 Hour 1.wav", 1)
    make_wav(lib / "c2c" / "Art Bell 1996-05-02 Hour 2.wav", 1)
    monkeypatch.setattr(main, "LIBRARY_DIR", lib)
    monkeypatch.setattr(main, "_engine", make_engine(tmp_path / "data" / "airwave.db"))
    client = TestClient(main.app)
    client.get("/")
    with Session(main._engine) as session:
        files = {f.name: f for f in session.scalars(select(AudioFile))}
        long_ago = datetime(2020, 1, 1)
        for name, (title, year, month, day) in {
                "a.mp3": ("The Hitchhiker", 1942, 9, 2), "b.mp3": ("Sorry, Wrong Number", 1943, 5, 25),
                "c.mp3": (None, 1944, None, None), "d.flac": ("Donovan's Brain", None, None, None)}.items():
            ep = files[name].episode
            ep.series, ep.title = "Suspense", title
            ep.air_year, ep.air_month, ep.air_day = year, month, day
            files[name].first_seen = long_ago
        ep = files["b.mp3"].episode
        ep.air_time, ep.air_timezone, ep.network, ep.cast = "20:00", "America/New_York", "CBS", "Agnes Moorehead"
        for f in files.values():
            if f.name.startswith("Art Bell"):
                f.episode.series, f.episode.title = "Coast to Coast AM", "Art Bell"
        session.commit()
        ids = {name: f.id for name, f in files.items()}
    client.post(f"/files/{ids['Art Bell 1996-05-02 Hour 1.wav']}/parts",
                data={"part": [str(ids["Art Bell 1996-05-02 Hour 1.wav"]), str(ids["Art Bell 1996-05-02 Hour 2.wav"])]})
    client.ids, client.lib, client.engine = ids, lib, main._engine
    return client


def make(client, series="Suspense", title=None) -> int:
    sent = client.post("/feeds", data={"series": series, "title": title or series, "description": ""},
                       follow_redirects=False)
    assert sent.status_code == 303
    return int(re.search(r"/feeds/(\d+)", sent.headers["location"])[1])


def token(client, feed_id) -> str:
    with Session(client.engine) as session:
        return session.get(Feed, feed_id).token


def read(client, feed_id) -> ET.Element:
    sent = client.get(f"/feeds/{token(client, feed_id)}/feed.xml")
    assert sent.status_code == 200 and sent.headers["content-type"].startswith("application/rss+xml")
    return ET.fromstring(sent.content).find("channel")


def test_making_a_feed_from_a_series(client):
    assert "Make a feed…" in client.get("/series/episodes?name=Suspense").text
    form = " ".join(client.get("/feeds/new?series=Suspense").text.split())
    assert 'value="Suspense"' in form and "all 4 episodes" in form and "starts as a draft" in form
    assert client.get("/feeds/new?series=Nothing").status_code == 404
    # A name is needed.
    sent = client.post("/feeds", data={"series": "Suspense", "title": "  "})
    assert sent.status_code == 400 and "Give the podcast a name." in sent.text

    feed_id = make(client, title="Suspense!")
    page = " ".join(client.get(f"/feeds/{feed_id}?made=1").text.split())
    assert "Feed made. It's a draft" in page and ">Draft<" in page and ">Publish</button>" in page
    assert "The Hitchhiker" in page and "4 episodes" in page
    assert "1 FLAC recording" in page          # may not play on an iPhone
    listing = client.get("/feeds").text
    assert "Suspense!" in listing and ">Draft<" in listing
    assert '<a href="/feeds" data-paths="/feeds" aria-current="page">Feeds</a>' in listing


def test_drafts_cant_be_read_until_published(client):
    feed_id = make(client)
    secret = token(client, feed_id)
    assert len(secret) >= 32
    assert client.get(f"/feeds/{secret}/feed.xml").status_code == 404
    assert client.get(f"/feeds/{secret}/audio/{client.ids['a.mp3']}.mp3").status_code == 404
    assert "podcast://" not in client.get(f"/feeds/{feed_id}").text

    client.post(f"/feeds/{feed_id}/publish", data={"published": "1"})
    page = client.get(f"/feeds/{feed_id}?published=yes").text
    assert "Published. Podcast apps can read it now" in page and "<svg" in page
    assert f'href="podcast://testserver/feeds/{secret}/feed.xml"' in page
    assert f'value="http://testserver/feeds/{secret}/feed.xml"' in page
    assert client.get(f"/feeds/{secret}/feed.xml").status_code == 200
    assert client.get("/feeds/wrong-token/feed.xml").status_code == 404

    client.post(f"/feeds/{feed_id}/publish", data={"published": "0"})
    assert client.get(f"/feeds/{secret}/feed.xml").status_code == 404


def test_the_feed_itself(client):
    feed_id = make(client, title="Suspense!")
    client.post(f"/feeds/{feed_id}/publish", data={"published": "1"})
    channel = read(client, feed_id)
    assert channel.findtext("title") == "Suspense!"
    assert channel.findtext(ITUNES + "block") == "Yes"
    items = channel.findall("item")
    # Oldest first, in air-date order (the undated one last); a serial show.
    assert channel.findtext(ITUNES + "type") == "serial"
    assert [i.findtext("title") for i in items] == ["The Hitchhiker", "Sorry, Wrong Number", "c", "Donovan's Brain"]
    dates = [i.findtext("pubDate") for i in items]
    assert len(set(dates)) == 4      # a second apart, so apps keep the order
    # The real air date is only in the description; nothing is made up.
    wrong = items[1].findtext("description")
    assert "Aired: Tuesday, May 25th, 1943, 8:00 PM US Eastern" in wrong
    assert "Station: CBS" in wrong and "Cast: Agnes Moorehead" in wrong
    assert "Aired: 1944, date unknown" in items[2].findtext("description")
    assert "Air date unknown" in items[3].findtext("description")
    # The recordings are reachable, from where the app was reached.
    secret = token(client, feed_id)
    enclosure = items[0].find("enclosure")
    url = enclosure.get("url")
    assert url == f"http://testserver/feeds/{secret}/audio/{client.ids['a.mp3']}.mp3"
    assert enclosure.get("type") == "audio/mpeg"
    original = (client.lib / "Suspense" / "a.mp3").read_bytes()
    assert enclosure.get("length") == str(len(original))
    assert client.get(url).content == original
    part = client.get(url, headers={"Range": "bytes=0-99"})
    assert part.status_code == 206 and len(part.content) == 100
    assert items[0].find("guid").text == f"airwave-file-{client.ids['a.mp3']}"
    # A recording of another series isn't sent through this feed.
    other = client.ids["Art Bell 1996-05-02 Hour 1.wav"]
    assert client.get(f"/feeds/{secret}/audio/{other}.wav").status_code == 404


def test_new_episodes_join_the_feed_as_they_arrive(client):
    feed_id = make(client)
    client.post(f"/feeds/{feed_id}/publish", data={"published": "1"})
    before = [i.findtext("pubDate") for i in read(client, feed_id).findall("item")]
    make_mp3(client.lib / "Suspense" / "e.mp3")
    client.post("/rescan")
    with Session(client.engine) as session:
        f = session.scalar(select(AudioFile).where(AudioFile.name == "e.mp3"))
        f.episode.series, f.episode.title, f.episode.air_year = "Suspense", "Three Skeleton Key", 1950
        f.first_seen = session.get(Feed, feed_id).created_at + timedelta(days=3)
        session.commit()
    items = read(client, feed_id).findall("item")
    assert items[-1].findtext("title") == "Three Skeleton Key"     # added later: at the end
    assert [i.findtext("pubDate") for i in items[:-1]] == before     # the others don't change


def test_parts_are_separate_items(client):
    feed_id = make(client, series="Coast to Coast AM")
    client.post(f"/feeds/{feed_id}/publish", data={"published": "1"})
    titles = [i.findtext("title") for i in read(client, feed_id).findall("item")]
    assert titles == ["Art Bell (part 1 of 2)", "Art Bell (part 2 of 2)"]


def test_editing_renaming_and_deleting(client):
    feed_id = make(client)
    page = client.get(f"/feeds/{feed_id}/edit").text
    assert 'value="Suspense"' in page
    client.post(f"/feeds/{feed_id}/edit", data={"title": "Radio Thrillers", "description": "Old shows."})
    client.post(f"/feeds/{feed_id}/publish", data={"published": "1"})
    channel = read(client, feed_id)
    assert channel.findtext("title") == "Radio Thrillers" and channel.findtext("description") == "Old shows."
    # Renaming the series: the feed follows it.
    client.post("/series/rename?name=Suspense", data={"new_name": "Suspense Theatre"})
    assert len(read(client, feed_id).findall("item")) == 4

    secret = token(client, feed_id)
    assert "Your recordings aren't touched" in client.get(f"/feeds/{feed_id}/delete").text
    assert client.post(f"/feeds/{feed_id}/delete").status_code == 400      # not confirmed
    page = client.post(f"/feeds/{feed_id}/delete", data={"confirm": "yes"}, follow_redirects=True).text
    assert "The feed was deleted." in page and "No feeds yet." in page
    assert client.get(f"/feeds/{secret}/feed.xml").status_code == 404
    assert (client.lib / "Suspense" / "a.mp3").is_file()
    with Session(client.engine) as session:
        assert len(session.scalars(select(AudioFile)).all()) == 6
