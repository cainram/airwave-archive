"""Tests for importing a spreadsheet (CSV). Run with:  pytest"""

import pytest
from fastapi.testclient import TestClient
from mutagen.id3 import TPE1
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.csvimport import guess_columns, read_date, read_table, read_time, read_zone
from app.db import MANUAL, AudioFile, make_engine
from test_scanner import add_tags, make_wav


@pytest.mark.parametrize("text, expected", [
    ("1939-04-14", (1939, 4, 14)), ("1939-04", (1939, 4, None)), ("1939", (1939, None, None)),
    ("4/14/1939", (1939, 4, 14)), ("4/14/39", (1939, 4, 14)), ("04.14.1939", (1939, 4, 14)),
    ("April 14, 1939", (1939, 4, 14)), ("Apr 14th 1939", (1939, 4, 14)),
    ("14 April 1939", (1939, 4, 14)), ("Friday, April 14, 1939", (1939, 4, 14)),
    ("April 1939", (1939, 4, None)), ("1943-xx-xx", (1943, None, None)),
])
def test_dates_as_people_write_them(text, expected):
    assert read_date(text) == (expected, None)


def test_dates_that_cannot_be_right():
    assert "doesn't have a day 31" in read_date("4/31/1939")[1]
    assert "no month 14" in read_date("14/4/1939")[1]          # month first, the American way
    assert read_date("sometime in spring")[1]


def test_times_and_zones():
    assert read_time("22:00") == ("22:00", None)
    assert read_time("10:00 PM") == ("22:00", None)
    assert read_time("10pm") == ("22:00", None)
    assert read_time("12 a.m.") == ("00:00", None)
    assert read_time("9:30") == ("09:30", None)
    assert read_time("10")[1]                                   # morning or evening?
    assert read_zone("Pacific") == ("America/Los_Angeles", None)
    assert read_zone("EST") == ("America/New_York", None)
    assert read_zone("US Eastern") == ("America/New_York", None)
    assert read_zone("Mars")[1]


def test_columns_are_recognised_by_name():
    assert guess_columns(["Show", "Air Date", "Ep. #", "Episode Title", "Starring",
                          "Description", "Mystery", "Date"]) == [
        "series", "date", "episode_number", "title", "cast", "notes", "", ""]   # 2nd date unused
    assert guess_columns(["#", "Title"]) == ["episode_number", "title"]


def test_reading_spreadsheets_from_excel():
    semicolons = "Title;Date\r\nThe Patriot;4/14/1939\r\n".encode("cp1252")
    assert read_table(semicolons) == (["Title", "Date"], [["The Patriot", "4/14/1939"]])
    with_mark = "﻿Title,Notes\n\"Our Town\",\"Wilder, Thornton\"\n\n".encode("utf-8")
    assert read_table(with_mark) == (["Title", "Notes"], [["Our Town", "Wilder, Thornton"]])


# ---------------------------------------------------------------- in the app


@pytest.fixture
def client(tmp_path, monkeypatch):
    import app.main as main
    lib = tmp_path / "library"
    for name in ("patriot.wav", "private lives.wav", "our town.wav", "c2c.wav"):
        make_wav(lib / name, 1)
    add_tags(lib / "c2c.wav", artist=TPE1(encoding=3, text="Coast to Coast AM"))
    monkeypatch.setattr(main, "LIBRARY_DIR", lib)
    monkeypatch.setattr(main, "_engine", make_engine(tmp_path / "data" / "airwave.db"))
    client = TestClient(main.app)
    client.get("/")   # first visit scans
    with Session(main._engine) as session:
        client.ids = {f.name: f.id for f in session.scalars(select(AudioFile))}
    for name, day in (("patriot.wav", "14"), ("private lives.wav", "21")):
        client.post(f"/files/{client.ids[name]}/edit", data={
            "series": "The Campbell Playhouse", "air_year": "1939", "air_month": "4", "air_day": day})
    client.post(f"/files/{client.ids['our town.wav']}/edit", data={
        "series": "The Campbell Playhouse", "episode_number": "40", "title": "My Own Title"})
    client.data_dir = tmp_path / "data"
    return client


def episode(client, name):
    import app.main as main
    with Session(main._engine) as session:
        return session.get(AudioFile, client.ids[name]).episode


def upload(client, text, series=""):
    sent = client.post("/import", data={"series": series},
                       files={"spreadsheet": ("log.csv", text.encode(), "text/csv")},
                       follow_redirects=False)
    assert sent.status_code == 303, sent.text
    return sent.headers["location"]


def test_the_library_links_to_import(client):
    assert 'href="/import"' in client.get("/library").text


def test_matching_by_series_and_date_with_a_preview(client):
    preview_url = upload(client, (
        "Series,Air Date,Title,Starring,Network,Time,Zone\n"
        "The Campbell Playhouse,1939-04-14,The Patriot,Orson Welles,CBS,8:00 PM,Eastern\n"
        "The Campbell Playhouse,April 21 1939,Private Lives,\"Orson Welles, Gertrude Lawrence\",CBS,,\n"
        "The Campbell Playhouse,1939-06-02,Not In The Library,,,,\n"))
    page = client.get(preview_url).text
    text = " ".join(page.split())
    assert "<strong>2</strong> would change an episode" in text and "1 couldn't be matched" in text
    assert "matched by series and date" in page and "Row 4" in page
    assert "Nothing has been saved yet" in text
    assert episode(client, "patriot.wav").title is None          # still nothing saved

    token = preview_url.split("/import/")[1].split("?")[0]
    columns = ["series", "date", "title", "cast", "network", "time", "timezone"]
    saved = client.post(f"/import/{token}", data={"col": columns, "row": ["2", "3"]},
                        follow_redirects=False)
    assert saved.headers["location"] == "/import?saved=2"
    patriot = episode(client, "patriot.wav")
    assert (patriot.title, patriot.cast, patriot.network) == ("The Patriot", "Orson Welles", "CBS")
    assert (patriot.air_time, patriot.air_timezone) == ("20:00", "America/New_York")
    assert patriot.sources["title"] == MANUAL and patriot.sources["air_date"] == MANUAL
    assert episode(client, "private lives.wav").cast == "Orson Welles, Gertrude Lawrence"
    assert not list((client.data_dir / "imports").glob("*.csv"))    # tidied away
    assert "Saved 2 episodes." in client.get("/import?saved=2").text


def test_matching_by_file_name_and_by_episode_number(client):
    url = upload(client, "File,Title\nc2c.mp3,Area 51 Callers\n")   # any ending
    assert "matched by file name" in client.get(url).text
    url = upload(client, "#,Synopsis\n40,A small town in New Hampshire.\n",
                 series="The Campbell Playhouse")
    assert "matched by series and episode number" in client.get(url).text


def test_replacing_your_own_values_starts_unticked(client):
    url = upload(client, "Series,Episode,Title\nThe Campbell Playhouse,40,Our Town\n")
    page = client.get(url).text
    card = page.split("Row 2")[0].rsplit("<section", 1)[1]
    assert "checked" not in card and "replaces what you set" in page


def test_rows_with_mistakes_are_explained_and_cannot_be_saved(client):
    url = upload(client, "Series,Date,Time\nThe Campbell Playhouse,1939-04-14,25:00\n"
                         "The Campbell Playhouse,4/31/1939,\n")
    page = client.get(url).text
    assert "Can&#39;t read the time" in page or "isn&#39;t a time" in page
    assert "doesn&#39;t have a day 31" in page
    assert "disabled" in page


def test_a_row_that_fits_two_episodes_is_not_matched(client):
    client.post(f"/files/{client.ids['c2c.wav']}/edit", data={
        "series": "The Campbell Playhouse", "air_year": "1939", "air_month": "4", "air_day": "14"})
    url = upload(client, "Series,Date,Title\nThe Campbell Playhouse,1939-04-14,Which One\n")
    assert "Fits 2 episodes" in client.get(url).text


def test_changing_which_column_holds_what(client):
    url = upload(client, "Show,When,Blurb\nThe Campbell Playhouse,1939-04-14,Very good\n")
    token = url.split("/import/")[1].split("?")[0]
    page = client.get(f"/import/{token}", params={"col": ["series", "date", ""]}).text
    assert "Very good" not in page.split("Which column")[1].split("</details>")[1]
    page = client.get(f"/import/{token}", params={"col": ["series", "date", "notes"]}).text
    assert "Very good" in page.split("</details>")[1]


def test_bad_uploads(client):
    page = client.post("/import", files={"spreadsheet": ("x.csv", b"", "text/csv")})
    assert page.status_code == 400 and "choose a CSV file" in page.text
    page = client.post("/import", files={"spreadsheet": ("x.csv", b"just one line", "text/csv")})
    assert page.status_code == 400
    assert client.get("/import/0123456789abcdef").status_code == 404
    assert client.get("/import/..%2F..%2Fairwave").status_code == 404
