"""Tests for the calendar. Run with:  pytest"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.airdates import next_month
from app.db import AudioFile, make_engine
from app.episodes import day_heading
from test_scanner import make_wav

SHOWS = {   # file name -> (year, month, day, time, zone)
    "wotw.wav": (1938, 10, 30, "20:00", "America/New_York"),
    "late.wav": (1938, 10, 30, None, None),
    "early.wav": (1938, 10, 30, "19:00", None),
    "sometime.wav": (1938, 10, None, None, None),
    "yearonly.wav": (1938, None, None, None, None),
    "later.wav": (1940, 2, 3, None, None),
    "earlier.wav": (1935, 7, 4, None, None),
    "undated.wav": (None, None, None, None, None),
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    import app.main as main
    lib = tmp_path / "library"
    for name in SHOWS:
        make_wav(lib / name, 1)
    monkeypatch.setattr(main, "LIBRARY_DIR", lib)
    monkeypatch.setattr(main, "_engine", make_engine(tmp_path / "data" / "airwave.db"))
    client = TestClient(main.app)
    client.get("/")
    with Session(main._engine) as session:
        for f in session.scalars(select(AudioFile)):
            ep = f.episode
            (ep.air_year, ep.air_month, ep.air_day, ep.air_time, ep.air_timezone) = SHOWS[f.name]
            ep.title = f.name.removesuffix(".wav").title()
        session.commit()
    return client


def test_months_and_headings():
    assert next_month(1938, 12, 1) == (1939, 1)
    assert next_month(1939, 1, -1) == (1938, 12)
    assert day_heading(1938, 10, 30) == "Sunday, October 30th, 1938"


def test_a_month(client):
    page = client.get("/calendar?year=1938&month=10").text
    assert "<h1>October 1938</h1>" in page and "4 episodes aired this month" in page
    assert "Sunday, October 30th, 1938" in page
    # In time order, unknown time last.
    day = page[page.index('<section class="calendar-day" id="day-30">'):]
    assert day.index(">Early<") < day.index(">Wotw<") < day.index(">Late<")
    assert "8:00 PM US Eastern" in page and "7:00 PM" in page
    assert "Sometime in October 1938" in page and "Sometime" in page
    assert "1 episode from 1938 has only the year known" in page
    assert "Yearonly" not in page and "Undated" not in page
    # The grid: 30th has 3 episodes; October 1938 starts on a Saturday.
    assert 'href="#day-30"' in page and '<span class="day-count">3</span>' in page
    assert page.count('class="outside"') == 6 + 5     # 6 blank days before the 1st, 5 after the 31st
    # Jumping to months with recordings, across the gaps.
    page = page.replace("&amp;", "&")
    assert 'href="/calendar?year=1935&month=7">⇤ Earlier' in page
    assert 'href="/calendar?year=1940&month=2">Later' in page
    assert 'href="/calendar?year=1938&month=9"' in page and 'href="/calendar?year=1938&month=11"' in page


def test_the_calendar_opens_on_the_latest_month(client):
    page = client.get("/calendar").text
    assert "<h1>February 1940</h1>" in page and "Later recordings" not in page
    assert client.get("/calendar?year=1938&month=13").text.count("<h1>February 1940</h1>") == 1


def test_an_empty_month(client):
    page = client.get("/calendar?year=1937&month=1").text
    assert "Nothing in your collection aired this month" in page
    assert 'aria-current="page">Calendar' in page or 'href="/calendar" data-paths="/calendar" aria-current="page"' in page


# ---------------------------------------------------------------- weeks

def test_time_spans_including_overnight():
    from app.db import Episode
    from app.episodes import format_air_span
    def show(time, zone, hours):
        return Episode(air_year=1996, air_month=5, air_day=2, air_time=time, air_timezone=zone,
                       files=[AudioFile(duration_seconds=hours * 3600)])
    assert format_air_span(show("20:00", "America/New_York", 0.5)) == "8:00 PM – 8:30 PM US Eastern"
    assert format_air_span(show("22:00", "America/Los_Angeles", 5)) == "10:00 PM – 3:00 AM (next day) US Pacific"
    assert format_air_span(show("19:00", None, 1)) == "7:00 PM – 8:00 PM"
    assert format_air_span(show(None, None, 1)) == "time unknown"


def test_week_names():
    from datetime import date
    from app.airdates import Week, week_start
    assert week_start(date(1938, 10, 30)) == date(1938, 10, 30)        # a Sunday
    assert week_start(date(1938, 11, 2)) == date(1938, 10, 30)
    assert Week(date(1938, 10, 30), []).name == "October 30th – November 5th, 1938"
    assert Week(date(1938, 10, 2), []).name == "October 2nd – 8th, 1938"
    assert Week(date(1939, 12, 31), []).name == "December 31st, 1939 – January 6th, 1940"


def test_a_week(client):
    page = client.get("/calendar/week?date=11/2/1938").text      # any day in the week, American style
    assert "<h1>October 30th – November 5th, 1938</h1>" in page and "3 episodes aired this week" in page
    sunday = page[page.index('id="day-1938-10-30"'):page.index('id="day-1938-10-31"')]
    assert sunday.index(">Early<") < sunday.index(">Wotw<") < sunday.index(">Late<")
    assert "8:00 PM – 8:00 PM US Eastern" in sunday         # 1-second test file
    assert page.count(">Nothing<") == 6
    assert "Sometime" not in page
    assert "1 episode from October 1938 has no known day" in page
    page = page.replace("&amp;", "&")
    assert 'href="/calendar/week?date=1935-06-30">⇤ Earlier' in page   # July 4th 1935's week
    assert 'href="/calendar/week?date=1940-01-28">Later' in page       # Feb 3rd 1940's week
    assert 'href="/calendar?year=1938&month=11">Month view' in page


def test_the_week_view_opens_on_the_latest_broadcast_and_explains_bad_dates(client):
    assert "January 28th – February 3rd, 1940" in client.get("/calendar/week").text
    bad = client.get("/calendar/week?date=1938")
    assert bad.status_code == 400 and "at least a month" in bad.text
    assert "doesn" in client.get("/calendar/week?date=1938-02-30").text
    assert "September 25th – October 1st, 1938" in client.get("/calendar/week?date=1938-10").text  # the week of the 1st


def test_the_month_links_to_its_weeks(client):
    page = client.get("/calendar?year=1938&month=10").text
    assert 'href="/calendar/week?date=1938-10-30">Week</a>' in page
    assert 'href="/calendar/week?date=1938-10-30">Week view</a>' in page
