"""Tests for editing many episodes at once, and renaming a series. Run with:  pytest"""

import pytest
from fastapi.testclient import TestClient
from mutagen.id3 import TPE1
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.bulk import apply_bulk, read_bulk_form
from app.db import MANUAL, AudioFile, Episode, SeriesArt, make_engine
from test_scanner import add_tags, make_wav


def test_reading_the_form():
    changes, time, errors = read_bulk_form({
        "do-network": "set", "network": " CBS ", "do-host": "clear", "host": "ignored",
        "do-cast": "keep", "cast": "ignored too", "do-time": "set", "air_time": "22:00",
        "air_timezone": "America/Los_Angeles"})
    assert changes == {"network": "CBS", "host": None}
    assert time == {"time": "22:00", "zone": "America/Los_Angeles"}
    assert errors == {}
    _c, _t, errors = read_bulk_form({"do-series": "set", "series": "  ", "do-time": "set",
                                     "air_time": "25:00"})
    assert set(errors) == {"series", "time"}


def test_a_time_only_goes_on_episodes_with_a_full_date():
    dated = Episode(air_year=1996, air_month=5, air_day=2)
    year_only = Episode(air_year=1996)
    time = {"time": "22:00", "zone": "America/Los_Angeles"}
    assert apply_bulk(dated, {}, time) == (True, False)
    assert (dated.air_time, dated.air_timezone) == ("22:00", "America/Los_Angeles")
    assert apply_bulk(year_only, {}, time) == (False, True)
    assert year_only.air_time is None
    assert apply_bulk(dated, {"network": "KOA"}, {"time": None}) == (True, False)
    assert dated.air_time is None and dated.network == "KOA"
    assert dated.sources["network"] == MANUAL


# ---------------------------------------------------------------- in the app


@pytest.fixture
def client(tmp_path, monkeypatch):
    import app.main as main
    lib = tmp_path / "library"
    for day in (1, 2, 3):
        path = lib / f"Coast to Coast - Mar 0{day} 2010.wav"
        make_wav(path, 1)
        add_tags(path, artist=TPE1(encoding=3, text="Coast to Coast"))
    make_wav(lib / "Patriot.wav", 1)
    add_tags(lib / "Patriot.wav", artist=TPE1(encoding=3, text="Campbell Playhouse"))
    monkeypatch.setattr(main, "LIBRARY_DIR", lib)
    monkeypatch.setattr(main, "_engine", make_engine(tmp_path / "data" / "airwave.db"))
    client = TestClient(main.app)
    client.get("/")   # first visit scans
    # Give two of the three Coast to Coast episodes a full air date.
    for day in (1, 2):
        client.post(f"/files/{day}/edit", data={"series": "Coast to Coast", "air_year": "2010",
                                                "air_month": "3", "air_day": str(day)})
    return client


def episodes(client) -> dict[str, Episode]:
    import app.main as main
    with Session(main._engine) as session:
        return {f.name: f.episode for f in session.scalars(select(AudioFile))}


def test_pages_offer_ticking_and_editing(client):
    page = client.get("/series/episodes?name=Coast%20to%20Coast").text
    assert page.count('form="pick" name="ids"') == 3 and "data-select-all" in page
    assert "Edit ticked…" in page and "Edit all 3…" in page and "Rename…" in page
    assert client.get("/library").text.count('form="pick" name="ids"') == 4


def test_editing_ticked_episodes(client):
    eps = episodes(client)
    ids = [eps[f"Coast to Coast - Mar 0{d} 2010.wav"].id for d in (1, 3)]
    form = client.get("/episodes/edit", params={"ids": ids, "back": "/library"})
    assert "Edit 2 episodes at once" in form.text
    sent = client.post("/episodes/edit", data={
        "ids": [str(i) for i in ids], "back": "/library",
        "do-network": "set", "network": "Premiere Networks",
        "do-host": "set", "host": "George Noory",
        "do-time": "set", "air_time": "22:00", "air_timezone": "America/Los_Angeles"},
        follow_redirects=False)
    assert sent.headers["location"] == "/library?changed=2&skipped=1"
    after = episodes(client)
    one, two, three = (after[f"Coast to Coast - Mar 0{d} 2010.wav"] for d in (1, 2, 3))
    assert one.network == three.network == "Premiere Networks" and two.network is None
    assert one.air_time == "22:00" and three.air_time is None   # Mar 03 has no full date
    assert one.sources["host"] == MANUAL
    notice = " ".join(client.get(sent.headers["location"]).text.split())
    assert "Changed 2 episodes." in notice and "1 episode was not given the start time" in notice


def test_editing_a_whole_series_and_nothing_else_is_touched(client):
    client.post("/episodes/edit", data={"whole_series": "Coast to Coast", "back": "/",
                                        "do-network": "set", "network": "KOA"})
    after = episodes(client)
    assert {after[f"Coast to Coast - Mar 0{d} 2010.wav"].network for d in (1, 2, 3)} == {"KOA"}
    assert after["Patriot.wav"].network is None


def test_mistakes_are_explained_and_nothing_is_saved(client):
    page = client.post("/episodes/edit", data={"whole_series": "Coast to Coast", "back": "/",
                                               "do-host": "set", "host": "", "do-network": "set",
                                               "network": "KOA"})
    assert page.status_code == 400 and "Type the new value" in page.text
    assert 'value="KOA"' in page.text                       # what was typed is kept
    assert episodes(client)["Patriot.wav"].network is None
    assert all(e.network is None for e in episodes(client).values())
    nothing = client.post("/episodes/edit", data={"back": "/", "do-network": "set", "network": "X"})
    assert nothing.status_code == 400 and "No episodes were ticked" in nothing.text


def test_changing_the_series_of_ticked_episodes_goes_to_the_new_series(client):
    eps = episodes(client)
    sent = client.post("/episodes/edit", data={
        "ids": [str(eps["Patriot.wav"].id)], "back": "/library",
        "do-series": "set", "series": "The Campbell Playhouse"}, follow_redirects=False)
    assert sent.headers["location"].startswith("/series/episodes?name=The%20Campbell%20Playhouse")


def test_renaming_a_series(client):
    import app.main as main
    with Session(main._engine) as session:     # it has an uploaded picture
        session.add(SeriesArt(series="Coast to Coast", art_hash="0" * 20,
                              uploaded_at=main.utcnow()))
        session.commit()
    page = client.get("/series/rename?name=Coast%20to%20Coast")
    assert page.status_code == 200 and "all 3 episodes" in page.text
    sent = client.post("/series/rename?name=Coast%20to%20Coast",
                       data={"new_name": "  Coast to Coast  AM "}, follow_redirects=False)
    assert sent.headers["location"] == "/series/episodes?name=Coast%20to%20Coast%20AM&renamed=3"
    after = episodes(client)
    assert {after[f"Coast to Coast - Mar 0{d} 2010.wav"].series for d in (1, 2, 3)} == {"Coast to Coast AM"}
    assert after["Coast to Coast - Mar 03 2010.wav"].sources["series"] == MANUAL
    with Session(main._engine) as session:     # the picture came along
        assert session.get(SeriesArt, "Coast to Coast AM") is not None
        assert session.get(SeriesArt, "Coast to Coast") is None
    assert "Renamed 3 episodes." in client.get(sent.headers["location"]).text
    assert client.post("/series/rename?name=Coast%20to%20Coast%20AM",
                       data={"new_name": " "}).status_code == 400


def test_renaming_into_another_series_joins_them(client):
    client.post("/series/rename?name=Campbell%20Playhouse", data={"new_name": "Coast to Coast"})
    page = client.get("/series/episodes?name=Coast%20to%20Coast").text
    assert "4 episodes" in page
