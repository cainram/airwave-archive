"""Tests for reading details from file names, and the review page. Run with:  pytest"""

import pytest
from fastapi.testclient import TestClient
from mutagen.id3 import TDRC, TIT2, TPE1
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import MANUAL, AudioFile, make_engine
from app.episodes import apply_confirmed, read_date_text
from app.filenames import parse_filename, tidy
from test_scanner import add_tags, make_wav


@pytest.mark.parametrize("name, series, number, title, date", [
    ("CampbellPlayhouse39-04-14ThePatriot.mp3", "Campbell Playhouse", None, "The Patriot", (1939, 4, 14)),
    ("Suspense 1943-05-13 - Donovan's Brain.mp3", "Suspense", None, "Donovan's Brain", (1943, 5, 13)),
    ("Suspense_1943-xx-xx_Unknown.mp3", "Suspense", None, "Unknown", (1943, None, None)),
    ("Suspense 1943-05-xx.mp3", "Suspense", None, None, (1943, 5, None)),
    ("1943-05-13 Suspense - Donovan's Brain.mp3", "Suspense", None, "Donovan's Brain", (1943, 5, 13)),
    ("Suspense.1943.05.13.Donovans.Brain.mp3", "Suspense", None, "Donovans Brain", (1943, 5, 13)),
    ("Suspense 19430513 #42 - Donovan's Brain.flac", "Suspense", "42", "Donovan's Brain", (1943, 5, 13)),
    ("Shadow 37-09-26 Ep 3 - The Death House Rescue.mp3", "Shadow", "3", "The Death House Rescue", (1937, 9, 26)),
    # Month names, and "Hour 1" / "Part 2" isn't taken as a title.
    ("Coast to Coast - Mar 01 2010 - Hour 1.mp3", "Coast to Coast", None, None, (2010, 3, 1)),
    ("Art Bell March 2nd, 1996 Part 3.mp3", "Art Bell", None, None, (1996, 3, 2)),
    ("C2C 01-Mar-2010 hr2.mp3", "C2C", None, None, (2010, 3, 1)),
    ("Art Bell Sept 9 1997 - Area 51 Caller (Hour 2).mp3", "Art Bell", None, "Area 51 Caller", (1997, 9, 9)),
])
def test_names_are_read(name, series, number, title, date):
    guess = parse_filename(name)
    assert (guess.series, guess.episode_number, guess.title) == (series, number, title)
    assert (guess.year, guess.month, guess.day) == date


@pytest.mark.parametrize("name", [
    "The 39 Steps.mp3",            # a number, not a date
    "Show 1943-02-30.mp3",          # no such day
    "Show 1943-13-01.mp3",          # no such month
    "Escape - Three Skeleton Key.mp3",
    "Marvel 12 2010.mp3",           # "Mar" inside a word
    "Show 00 Mar 2010.mp3",         # no day 0
    "Show Feb 30 1943.mp3",
])
def test_names_without_a_real_date_give_no_guess(name):
    assert parse_filename(name) is None


def test_tidy_words():
    assert tidy("CampbellPlayhouse") == "Campbell Playhouse"
    assert tidy("_Donovans_Brain_") == "Donovans Brain"
    assert tidy(" - ") is None


def test_typed_dates():
    assert read_date_text("1939-04-14") == ((1939, 4, 14), None)
    assert read_date_text("1939-4") == ((1939, 4, None), None)
    assert read_date_text("1939-xx-xx") == ((1939, None, None), None)
    assert read_date_text("1939") == ((1939, None, None), None)
    assert read_date_text("") == (None, None)
    assert "doesn't have a day 31" in read_date_text("1939-04-31")[1]
    assert read_date_text("April 1939")[1]
    assert read_date_text("1939-13")[1]


def test_confirmed_values_count_as_manual_and_keep_the_time_only_on_the_same_day():
    from app.db import Episode
    ep = Episode(air_year=1939, air_month=4, air_day=14, air_time="21:00",
                 air_timezone="America/New_York", title="Same")
    changed = apply_confirmed(ep, {"title": "Same", "series": "", "date": (1939, 4, 14)})
    assert sorted(changed) == ["air_date", "title"]   # confirmed, though unchanged
    assert ep.air_time == "21:00"                       # same day: time kept
    assert ep.series is None                             # empty box: left alone
    assert ep.sources == {"title": MANUAL, "air_date": MANUAL}
    apply_confirmed(ep, {"date": (1939, 4, 21)})
    assert ep.air_time is None and ep.air_timezone is None


# ---------------------------------------------------------------- review page


@pytest.fixture
def client(tmp_path, monkeypatch):
    import app.main as main
    lib = tmp_path / "library"
    for name, title in [("CampbellPlayhouse39-04-14ThePatriot.wav", "The Patriot"),
                        ("CampbellPlayhouse39-04-21PrivateLives.wav", "Private Lives"),
                        ("NoDateHere.wav", None)]:
        make_wav(lib / name, 1)
        if title:
            add_tags(lib / name, title=TIT2(encoding=3, text=title),
                     artist=TPE1(encoding=3, text="The Campbell Playhouse"),
                     date=TDRC(encoding=3, text="1939"))
    monkeypatch.setattr(main, "LIBRARY_DIR", lib)
    monkeypatch.setattr(main, "_engine", make_engine(tmp_path / "data" / "airwave.db"))
    client = TestClient(main.app)
    client.get("/")
    return client


def episode(client, name):
    import app.main as main
    with Session(main._engine) as session:
        return session.scalars(select(AudioFile).where(AudioFile.name == name)).one().episode


def test_review_page_shows_new_guesses(client):
    page = client.get("/review/filenames")
    assert page.status_code == 200
    assert "CampbellPlayhouse39-04-14ThePatriot.wav" in page.text
    assert 'value="1939-04-14"' in page.text
    assert "now: 1939" in page.text                      # what's saved now
    assert "NoDateHere.wav" not in page.text
    assert "2 files to review" in page.text


def test_saving_ticked_rows(client):
    ids = "1,2"
    data = {"ids": ids,
            "accept-1": "on", "series-1": "The Campbell Playhouse", "episode_number-1": "",
            "date-1": "1939-04-14", "title-1": "The Patriot",
            # row 2 left unticked
            "series-2": "Campbell Playhouse", "date-2": "1939-04-21", "title-2": "Private Lives"}
    saved = client.post("/review/filenames", data=data, follow_redirects=False)
    assert saved.status_code == 303 and saved.headers["location"] == "/review/filenames?saved=1"

    ep = episode(client, "CampbellPlayhouse39-04-14ThePatriot.wav")
    assert (ep.air_year, ep.air_month, ep.air_day) == (1939, 4, 14)
    assert ep.sources["air_date"] == MANUAL and ep.sources["series"] == MANUAL
    assert episode(client, "CampbellPlayhouse39-04-21PrivateLives.wav").air_month is None

    page = client.get("/review/filenames?saved=1")
    assert "Saved 1 episode." in page.text
    assert "CampbellPlayhouse39-04-14ThePatriot.wav" not in page.text   # nothing new left


def test_a_bad_date_saves_nothing_and_keeps_what_was_typed(client):
    data = {"ids": "1,2",
            "accept-1": "on", "series-1": "X", "date-1": "1939-04-14", "title-1": "Kept typing",
            "accept-2": "on", "series-2": "Y", "date-2": "1939-04-31", "title-2": "Bad"}
    page = client.post("/review/filenames", data=data)
    assert page.status_code == 200
    assert "Nothing was saved yet" in page.text
    assert "doesn&#39;t have a day 31" in page.text or "doesn't have a day 31" in page.text
    assert 'value="Kept typing"' in page.text
    assert episode(client, "CampbellPlayhouse39-04-14ThePatriot.wav").air_month is None


def test_guesses_that_replace_your_own_edits_start_unticked(client):
    client.post("/files/2/edit", data={"title": "Private LIves", "series": "The Campbell Playhouse",
                                       "air_year": "1939"})
    page = client.get("/review/filenames")
    card = page.text.split("CampbellPlayhouse39-04-21PrivateLives.wav")[0].rsplit("<section", 1)[1]
    assert "checked" not in card
    assert "now: Private LIves (you set this)" in page.text


def test_library_links_to_review(client):
    assert 'href="/review/filenames"' in client.get("/library").text


def test_a_corrected_card_does_not_come_back(client):
    data = {"ids": "1", "accept-1": "on", "series-1": "The Campbell Playhouse",
            "date-1": "1939-04-14", "title-1": "The Patriot"}
    client.post("/review/filenames", data=data)
    # The name still says "Campbell Playhouse", but the owner chose otherwise.
    assert "CampbellPlayhouse39-04-14ThePatriot.wav" not in client.get("/review/filenames").text
