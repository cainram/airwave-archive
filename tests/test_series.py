"""Tests for the series list and series pages. Run with:  pytest"""

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import AudioFile, make_engine
from app.series import sort_key, years_text
from test_scanner import make_wav

# file name -> details typed for it (None: left without a series)
EPISODES = {
    "patriot.wav": dict(series="The Campbell Playhouse", title="The Patriot",
                        air_year="1939", air_month="4", air_day="14"),
    "private.wav": dict(series="The Campbell Playhouse", title="Private Lives",
                        air_year="1939", air_month="4", air_day="21"),
    "rebecca.wav": dict(series="The Campbell Playhouse", title="Rebecca", air_year="1938",
                        air_month="12", air_day="9"),
    "someday.wav": dict(series="The Campbell Playhouse", title="Some day in 1939",
                        air_year="1939"),
    "nodate.wav": dict(series="The Campbell Playhouse", title="No date at all"),
    "ep10.wav": dict(series="Escape & Suspense / Mixed", episode_number="10", title="Ten"),
    "ep2.wav": dict(series="Escape & Suspense / Mixed", episode_number="2", title="Two"),
    "abbott.wav": dict(series="Abbott and Costello", title="Who's on First"),
    "loose.wav": None,
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    import app.main as main
    lib = tmp_path / "library"
    for name in EPISODES:
        make_wav(lib / name, 2)
    monkeypatch.setattr(main, "LIBRARY_DIR", lib)
    monkeypatch.setattr(main, "_engine", make_engine(tmp_path / "data" / "airwave.db"))
    client = TestClient(main.app)
    client.get("/")   # first visit scans
    with Session(main._engine) as session:
        ids = {f.name: f.id for f in session.scalars(select(AudioFile))}
    for name, details in EPISODES.items():
        if details:
            client.post(f"/files/{ids[name]}/edit", data=details)
    client.ids = ids
    return client


def titles_in_order(page: str) -> list[str]:
    return re.findall(r'<a class="title" href="/files/\d+">([^<]+)</a>', page)


def test_sorting_ignores_the_and_reads_numbers():
    names = ["The Shadow", "Abbott and Costello", "an Hour 10", "Hour 9", "the campbell playhouse"]
    assert sorted(names, key=sort_key) == [
        "Abbott and Costello", "the campbell playhouse", "Hour 9", "an Hour 10", "The Shadow"]
    assert years_text(1938, 1939) == "1938–1939"
    assert years_text(1939, 1939) == "1939"
    assert years_text(None, None) == ""


def test_series_list_has_totals_in_name_order(client):
    page = client.get("/series").text
    names = re.findall(r'<a class="title" href="[^"]*">([^<]+)</a>', page)
    assert names == ["Abbott and Costello", "The Campbell Playhouse",
                     "Escape &amp; Suspense / Mixed", "No series yet"]
    assert "4 series" in page and "9 episodes" in page
    row = page.split("The Campbell Playhouse</a>")[1].split("</tr>")[0]
    assert ">5<" in row and "1938–1939" in row and "0:10" in row


def test_series_page_lists_episodes_in_air_date_order(client):
    page = client.get("/series/episodes?name=The%20Campbell%20Playhouse")
    assert page.status_code == 200
    assert titles_in_order(page.text) == [
        "Rebecca", "The Patriot", "Private Lives", "Some day in 1939", "No date at all"]
    assert "Dec 9, 1938" in page.text and "date unknown" in page.text
    assert ">Ep.<" not in page.text      # no episode numbers in this series
    assert "5 episodes · 1938–1939" in " ".join(page.text.split())


def test_numbered_episodes_go_in_number_order(client):
    # The link from the list works for names with "&" and "/".
    link = re.search(r'href="([^"]*)">Escape &amp; Suspense', client.get("/series").text)[1]
    page = client.get(link.replace("&amp;", "&")).text
    assert titles_in_order(page) == ["Two", "Ten"]
    assert ">Ep.<" in page


def test_episodes_without_a_series(client):
    page = client.get("/series/episodes").text
    assert "No series yet" in page and titles_in_order(page) == ["loose.wav"]


def test_an_unknown_series_is_not_found(client):
    assert client.get("/series/episodes?name=Nothing%20Here").status_code == 404


def test_series_names_link_to_their_pages(client):
    href = 'href="/series/episodes?name=The%20Campbell%20Playhouse"'
    assert href in client.get("/").text                               # library
    assert href in client.get(f"/files/{client.ids['patriot.wav']}").text   # episode page


def test_menu_shows_where_you_are(client):
    page = client.get("/series").text
    assert '<a href="/series" aria-current="page">Series</a>' in page
    assert '<a href="/" aria-current="page">Library</a>' in client.get("/").text
    # Pages fetched by htmx bring the menu along, to replace the old one.
    partial = client.get("/series", headers={"HX-Request": "true"}).text
    assert 'hx-swap-oob="true"' in partial and 'aria-current="page">Series' in partial
