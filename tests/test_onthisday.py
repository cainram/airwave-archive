"""Tests for the "On this day" feed. Run with:  pytest"""

import re
from datetime import date, datetime, timedelta
from xml.etree import ElementTree as ET

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import onthisday
from app.db import AudioFile, Episode, Feed, FeedSeries, make_engine, utcnow
from test_export import make_mp3


def feed_in(zone="America/Chicago", at="03:00"):
    return Feed(kind=onthisday.KIND, token="x", title="On this day", roll_time=at, roll_zone=zone,
                newest_first=True, created_at=datetime(2020, 1, 1), updated_at=datetime(2020, 1, 1))


def test_the_day_turns_over_at_the_release_time():
    feed = feed_in()
    # 3:00 AM US Central on November 22nd is 9:00 UTC (standard time).
    assert onthisday.day_start(feed, date(2026, 11, 22)) == datetime(2026, 11, 22, 9, 0)
    assert onthisday.today(feed, datetime(2026, 11, 22, 8, 59)) == date(2026, 11, 21)
    assert onthisday.today(feed, datetime(2026, 11, 22, 9, 0)) == date(2026, 11, 22)
    # Summer time: 3:00 AM is 8:00 UTC.
    assert onthisday.day_start(feed, date(2026, 7, 4)) == datetime(2026, 7, 4, 8, 0)
    assert onthisday.next_turn(feed, datetime(2026, 11, 22, 12, 0)) == datetime(2026, 11, 23, 9, 0)


def test_february_29th_comes_out_on_the_28th_in_other_years():
    assert onthisday._dates_of(date(2027, 2, 28)) == [(2, 28), (2, 29)]
    assert onthisday._dates_of(date(2028, 2, 28)) == [(2, 28)]
    assert onthisday._dates_of(date(2028, 2, 29)) == [(2, 29)]


def test_titles_put_the_year_first():
    f = AudioFile(folder="", name="x.mp3")
    ep = Episode(series="Suspense", title="Donovan's Brain", air_year=1944)
    assert onthisday.item_title(ep, f) == "1944 · Suspense: Donovan's Brain"
    ep = Episode(series="Coast to Coast AM", title="Coast to Coast AM - Dec 21 2009", air_year=2009)
    assert onthisday.item_title(ep, f) == "2009 · Coast to Coast AM - Dec 21 2009"


# ------------------------------------------------------------ with a library

TODAY = None


@pytest.fixture
def client(tmp_path, monkeypatch):
    import app.main as main
    lib = tmp_path / "library"
    names = ["today-1944.mp3", "today-1950.mp3", "two-days-ago.mp3", "ten-days-ago.mp3",
             "year-only.mp3", "other-series.mp3", "part1.mp3", "part2.mp3"]
    for name in names:
        make_mp3(lib / name)
    monkeypatch.setattr(main, "LIBRARY_DIR", lib)
    monkeypatch.setattr(main, "_engine", make_engine(tmp_path / "data" / "airwave.db"))
    client = TestClient(main.app)
    client.get("/")
    with Session(main._engine) as session:
        feed = onthisday.the_feed(session)
        today = onthisday.today(feed, utcnow())
        files = {f.name: f for f in session.scalars(select(AudioFile))}
        ago = lambda days: today - timedelta(days=days)
        for name, (series, title, year, day) in {
                "today-1944.mp3": ("Suspense", "Donovan's Brain", 1944, today),
                "today-1950.mp3": ("Suspense", "The Hitchhiker", 1950, today),
                "two-days-ago.mp3": ("Suspense", "Sorry, Wrong Number", 1943, ago(2)),
                "ten-days-ago.mp3": ("Suspense", "Too Long Ago", 1945, ago(10)),
                "year-only.mp3": ("Suspense", "Only a Year", 1946, None),
                "other-series.mp3": ("Gunsmoke", "Matt Gets It", 1952, today),
                "part1.mp3": ("Coast to Coast AM", "Art Bell", 1996, today),
                "part2.mp3": ("Coast to Coast AM", "Art Bell", 1996, today)}.items():
            ep = files[name].episode
            ep.series, ep.title, ep.air_year = series, title, year
            ep.air_month, ep.air_day = (day.month, day.day) if day else (None, None)
        session.commit()
        ids = {name: f.id for name, f in files.items()}
        feed_id = feed.id
    client.post(f"/files/{ids['part1.mp3']}/parts", data={"part": [str(ids["part1.mp3"]), str(ids["part2.mp3"])]})
    client.engine, client.ids, client.feed_id = main._engine, ids, feed_id
    return client


def tick(client, *series, at=None):
    """Tick series (as if ``at`` long ago, when given)."""
    sent = client.post(f"/feeds/{client.feed_id}/edit",
                       data={"series": list(series), "roll_time": "03:00", "roll_zone": "America/Chicago"},
                       follow_redirects=False)
    assert sent.status_code == 303
    if at is not None:
        with Session(client.engine) as session:
            for row in session.scalars(select(FeedSeries)):
                row.added_at = at
            session.commit()


def titles(client) -> list[str]:
    with Session(client.engine) as session:
        feed = session.get(Feed, client.feed_id)
        return [item.title for item in onthisday.items(session, feed)]


def test_always_first_on_the_feeds_page_and_starts_empty(client):
    client.post("/feeds", data={"series": "Gunsmoke", "title": "Gunsmoke", "description": ""})
    page = client.get("/feeds").text
    assert page.index("On this day") < page.index(">Gunsmoke<")
    assert "Anniversaries · no series chosen yet" in page
    assert titles(client) == []
    with Session(client.engine) as session:     # only ever one
        onthisday.the_feed(session)
        assert len(session.scalars(select(Feed).where(Feed.kind == onthisday.KIND)).all()) == 1
    assert "No series chosen yet." in client.get(f"/feeds/{client.feed_id}").text


def test_its_own_edit_page(client):
    page = client.get(f"/feeds/{client.feed_id}/edit").text
    assert 'name="series" value="Suspense"' in page and not re.search(r'name="series"[^>]*\s+checked', page)
    assert "4 of 5" in page                       # Suspense: 4 with a full air date
    tick(client, "Suspense")
    page = client.get(f"/feeds/{client.feed_id}/edit").text
    assert re.search(r'value="Suspense"[^>]*\s+checked', page)
    bad = client.post(f"/feeds/{client.feed_id}/edit", data={"roll_time": "late", "roll_zone": "Mars"})
    assert bad.status_code == 400 and "Give a time of day" in bad.text


def test_a_series_ticked_today_brings_todays_anniversaries(client):
    tick(client, "Suspense", "Coast to Coast AM")
    assert titles(client) == ["1944 · Suspense: Donovan's Brain", "1950 · Suspense: The Hitchhiker",
                              "1996 · Coast to Coast AM: Art Bell (part 1 of 2)",
                              "1996 · Coast to Coast AM: Art Bell (part 2 of 2)"]
    page = client.get("/feeds").text
    assert "Anniversaries of 2 series · 3 broadcasts today" in page


def test_the_last_7_days_once_ticked_long_enough(client):
    tick(client, "Suspense", at=datetime(2020, 1, 1))
    assert titles(client) == ["1944 · Suspense: Donovan's Brain", "1950 · Suspense: The Hitchhiker",
                              "1943 · Suspense: Sorry, Wrong Number"]     # not 10 days ago, nor year only
    tick(client)                                                          # untick everything
    assert titles(client) == []


def test_the_feed_itself(client):
    tick(client, "Suspense")
    client.post(f"/feeds/{client.feed_id}/publish", data={"published": "1"})
    with Session(client.engine) as session:
        token = session.get(Feed, client.feed_id).token
    root = ET.fromstring(client.get(f"/feeds/{token}/feed.xml").content)
    channel = root.find("channel")
    assert "first aired on this day" in channel.find("description").text
    assert [i.find("title").text for i in channel.findall("item")] == [
        "1944 · Suspense: Donovan's Brain", "1950 · Suspense: The Hitchhiker"]
    assert client.get(f"/feeds/{token}/audio/{client.ids['today-1944.mp3']}.mp3").status_code == 200
    assert client.get(f"/feeds/{token}/audio/{client.ids['other-series.mp3']}.mp3").status_code == 404
    assert client.get(f"/feeds/{token}/audio/{client.ids['year-only.mp3']}.mp3").status_code == 404
    page = client.get(f"/feeds/{client.feed_id}").text
    assert "broadcasts.</strong>" in page and "Donovan" in page and "today" in page


def test_it_cant_be_deleted_or_picked_into(client):
    assert client.post(f"/feeds/{client.feed_id}/delete", data={"confirm": "yes"}).status_code == 400
    assert "Delete the feed" not in client.get(f"/feeds/{client.feed_id}").text
    page = client.get("/feeds/add", params={"ids": [client.ids["today-1944.mp3"]]}).text
    assert "On this day" not in page


def test_a_renamed_series_stays_ticked(client):
    tick(client, "Suspense")
    with Session(client.engine) as session:
        ep_id = session.get(AudioFile, client.ids["today-1944.mp3"]).episode_id
    sent = client.post("/series/rename", params={"name": "Suspense"}, data={"new_name": "Suspense!"})
    assert sent.status_code in (200, 303)
    with Session(client.engine) as session:
        assert session.scalars(select(FeedSeries.series)).all() == ["Suspense!"]
        assert session.get(Episode, ep_id).series == "Suspense!"


def test_its_own_picture(client):
    page = client.get("/feeds").text
    with Session(client.engine) as session:
        art = onthisday.picture()
        assert art and art == __import__("app.feeds", fromlist=["x"]).feed_picture(
            session, session.get(Feed, client.feed_id), [])
    assert f"/art/{art}.jpg" in page
    assert client.get(f"/art/{art}.jpg").status_code == 200
    assert "the feed's own picture, a tuning dial" in client.get(f"/feeds/{client.feed_id}/picture").text


# ------------------------------------------------- too many in one day

def add_episodes(client, series, month, day, years):
    """Episodes of a series broadcast on this day of the year in these
    years (records only: no files needed to count them)."""
    with Session(client.engine) as session:
        for year in years:
            session.add(Episode(series=series, title=f"{series} {year}", air_year=year,
                                air_month=month, air_day=day, created_at=utcnow(), updated_at=utcnow()))
        session.commit()


def crowding(client, *series):
    with Session(client.engine) as session:
        return onthisday.crowding(session, series)


def test_no_warning_while_every_day_is_fine(client):
    add_episodes(client, "Dreamland", 11, 22, range(1994, 2003))    # 9 ...
    add_episodes(client, "Escape", 11, 22, [1950])                   # ... and 1: 10 is fine
    assert crowding(client) is None
    assert crowding(client, "Escape", "Dreamland") is None


def test_a_series_with_too_many_on_one_day_is_named(client):
    add_episodes(client, "Dreamland", 11, 22, range(1993, 2008))    # 15 on November 22nd
    add_episodes(client, "Dreamland", 11, 23, range(1993, 1998))    # 5 on the 23rd
    found = crowding(client, "Escape", "Dreamland")
    assert found == {"to_blame": [{"name": "Dreamland", "most": 15,
                                   "day_words": "November 22nd", "years_words": "from 1993 to 2007"}]}


def test_a_show_in_parts_counts_once_and_feb_29th_counts_with_the_28th(client):
    add_episodes(client, "Dimension X", 2, 28, range(1950, 1960))           # 10 on the 28th
    assert crowding(client, "Dimension X") is None
    add_episodes(client, "Dimension X", 2, 29, [1952])                       # an 11th that day
    assert crowding(client, "Dimension X")["to_blame"][0]["day_words"] == "February 28th"


def test_several_series_together(client):
    add_episodes(client, "Fort Laramie", 4, 14, range(1950, 1956))
    add_episodes(client, "Escape", 4, 14, range(1950, 1954))
    add_episodes(client, "Dragnet", 4, 14, [1951])
    add_episodes(client, "X Minus One", 4, 14, [1955])
    found = crowding(client, "Fort Laramie", "Escape", "Dragnet", "X Minus One")
    assert found == {"together": 12, "day_words": "April 14th",
                     "biggest": [{"name": "Fort Laramie", "most": 6}, {"name": "Escape", "most": 4},
                                 {"name": "Dragnet", "most": 1}]}
    assert crowding(client, "Fort Laramie", "Escape") is None


def test_the_edit_page_warns_above_both_save_buttons(client):
    add_episodes(client, "Dreamland", 11, 22, range(1993, 2008))
    page = client.get(f"/feeds/{client.feed_id}/edit").text
    assert "Some days could bring" not in page
    assert page.count('class="crowding"') == 2
    tick(client, "Dreamland")
    page = client.get(f"/feeds/{client.feed_id}/edit").text
    assert page.count("<strong>Dreamland</strong> alone has up to 15 on one day") == 2
    assert page.index('id="crowding-top"') < page.index("<h2>Series</h2>") < page.index('id="crowding-bottom"')


def test_the_warning_is_checked_again_as_series_are_ticked(client):
    add_episodes(client, "Dreamland", 11, 22, range(1993, 2008))
    add_episodes(client, "<Odd> & Co", 11, 22, range(1980, 1992))
    url = f"/feeds/{client.feed_id}/crowding"
    sent = client.post(url, data={"series": ["Dreamland", "<Odd> & Co"]}, headers={"HX-Request": "true"})
    assert sent.status_code == 200
    assert sent.text.count('hx-swap-oob="true"') == 2
    assert 'id="crowding-top"' in sent.text and 'id="crowding-bottom"' in sent.text
    assert "<strong>Dreamland</strong>: up to 15 on one day (November 22nd, from 1993 to 2007)" in sent.text
    assert "&lt;Odd&gt; &amp; Co" in sent.text and "<Odd>" not in sent.text
    assert "feeds of their own" in sent.text
    sent = client.post(url, data={"series": ["Suspense"]})
    assert "Some days could bring" not in sent.text and sent.text.count('class="crowding"') == 2
    client.post("/feeds", data={"series": "Gunsmoke", "title": "Gunsmoke", "description": ""})
    with Session(client.engine) as session:
        ordinary = session.scalar(select(Feed).where(Feed.id != client.feed_id))
    assert client.post(f"/feeds/{ordinary.id}/crowding", data={}).status_code == 404
