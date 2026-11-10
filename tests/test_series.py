"""Tests for the series list and series pages. Run with:  pytest"""

import re
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import AudioFile, make_engine
from app.listening import mark, save_place
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
    page = client.get("/series?view=list").text
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
    link = re.search(r'href="([^"]*)">Escape &amp; Suspense', client.get("/series?view=list").text)[1]
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
    assert href in client.get("/library").text                        # library
    assert href in client.get(f"/files/{client.ids['patriot.wav']}").text   # episode page


def test_an_episodes_back_button_goes_to_its_series(client):
    page = client.get(f"/files/{client.ids['ep2.wav']}").text
    assert ('<nav class="back"><a class="button" href="/series/episodes?name=Escape%20%26%20Suspense%20%2F%20Mixed">'
            '← Escape &amp; Suspense / Mixed</a></nav>') in page
    page = client.get(f"/files/{client.ids['loose.wav']}").text
    assert '<a class="button" href="/series/episodes">← No series yet</a>' in page


def test_series_tiles_show_initials_without_pictures(client):
    page = client.get("/series").text
    assert '<ul class="tiles">' in page
    assert ">CP</span>" in page            # The Campbell Playhouse, no picture
    assert "5 episodes · 1938–1939" in page


def test_menu_shows_where_you_are(client):
    home = '<a href="/" data-paths="/" aria-current="page">Home</a>'
    series = ('<a href="/series" data-paths="/series /library /files /review /import /groups /organize"'
              ' aria-current="page">Series</a>')
    front = client.get("/").text
    assert home in front and series not in front
    assert series in client.get("/series").text and home not in client.get("/series").text
    assert series in client.get("/series/episodes").text
    # The Library isn't in the menu (owner's choice, 2026-11-10): its pages count as Series.
    assert series in client.get("/library").text
    assert series in client.get(f"/files/{client.ids['patriot.wav']}").text
    menu = front.split('id="menu"')[1].split("</nav>")[0]
    assert ">Library<" not in menu
    assert menu.index(">Home<") < menu.index(">Series<") < menu.index(">Calendar<")
    # Pages fetched by htmx bring the menu along, to replace the old one.
    partial = client.get("/series", headers={"HX-Request": "true"}).text
    assert 'hx-swap-oob="true"' in partial and 'aria-current="page">Series' in partial


def shelf(page: str, heading: str) -> list[str] | None:
    """The series names in one of the home page's rows (None: no such row)."""
    if f">{heading}</h2>" not in page:
        return None
    row = page.split(f">{heading}</h2>")[1].split("</ul>")[0]
    return re.findall(r'tile-name">([^<]+)<', row)


def test_home_page_rows(client):
    import app.main as main
    page = client.get("/").text
    assert "<title>Airwave Archive</title>" in page and '<ul class="tiles">' not in page
    assert shelf(page, "Most played") is None and "Continue listening" not in page
    assert "Play an episode and it will show up here." in page
    # Everything was first seen in the same scan: the newest-named first
    # among equals isn't promised, but all named series are there (not the
    # episodes without a series).
    assert sorted(shelf(page, "Recently added series")) == [
        "Abbott and Costello", "Escape &amp; Suspense / Mixed", "The Campbell Playhouse"]

    with Session(main._engine) as session:
        files = {f.name: f for f in session.scalars(select(AudioFile))}
        long_ago = datetime(2020, 1, 1)
        for f in files.values():
            f.first_seen = long_ago
        # A new episode of an old series brings the series to the front.
        files["rebecca.wav"].first_seen = long_ago + timedelta(days=30)
        files["abbott.wav"].first_seen = long_ago + timedelta(days=10)
        # Listened to: three of Campbell's (one only started), one of Escape's.
        mark(session, [files["patriot.wav"].episode, files["private.wav"].episode,
                       files["ep2.wav"].episode], played=True)
        save_place(session, files["someday.wav"].episode, files["someday.wav"].id, 60)
        # Barely started (under 10 seconds) doesn't count.
        save_place(session, files["abbott.wav"].episode, files["abbott.wav"].id, 3)
        session.commit()
    page = client.get("/").text
    assert shelf(page, "Recently added series") == [
        "The Campbell Playhouse", "Abbott and Costello", "Escape &amp; Suspense / Mixed"]
    assert shelf(page, "Most played") == ["The Campbell Playhouse", "Escape &amp; Suspense / Mixed"]
    assert "Continue listening" in page and "Play an episode" not in page
    assert page.index("Continue listening") < page.index("Recently added") < page.index("Most played")


def test_an_empty_library_says_how_to_start(tmp_path, monkeypatch):
    import app.main as main
    (tmp_path / "library").mkdir()
    monkeypatch.setattr(main, "LIBRARY_DIR", tmp_path / "library")
    monkeypatch.setattr(main, "_engine", make_engine(tmp_path / "data" / "airwave.db"))
    page = TestClient(main.app).get("/").text
    assert "No episodes yet." in page and "Recently added" not in page


def test_the_series_page_has_the_librarys_tools(client):
    page = client.get("/series").text
    for item in ['<input type="hidden" name="back" value="/series"><button type="submit">Rescan</button>',
                 'href="/review/filenames">Review file names', 'href="/groups/suggested">Find possible groups',
                 'href="/import">Import spreadsheet…', 'href="/library/convert">Convert odd formats…',
                 'href="/organize/history">Past renames', 'href="/library">All files (Library)']:
        assert item in page
    assert '<a class="button" href="/series">← Series</a>' in client.get("/library").text
