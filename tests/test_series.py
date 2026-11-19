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
    assert "No series yet" in page and titles_in_order(page) == ["Untitled Episode"]


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


# ------------------------------------------------------------------ genres

CAMPBELL = "/series/genres?name=The%20Campbell%20Playhouse"


def test_a_series_starts_without_genres_and_can_be_given_some(client):
    page = client.get("/series/episodes", params={"name": "The Campbell Playhouse"}).text
    assert 'class="genre"' not in page
    assert "Edit genres…" in page
    form = client.get(CAMPBELL).text
    assert "Genres you've used" not in form   # nothing used yet
    client.post(CAMPBELL, data={"new_genres": " Drama ,  anthology  drama, "})
    page = client.get("/series/episodes", params={"name": "The Campbell Playhouse"}).text
    assert re.findall(r'<a class="genre" href="[^"]+">([^<]+)</a>', page) == ["anthology drama", "Drama"]
    tiles = client.get("/series").text
    assert '<span class="tile-genres small">anthology drama · Drama</span>' in tiles
    assert client.get("/series?view=list").text.count("anthology drama · Drama") == 1


def test_genres_used_before_are_offered_and_keep_their_spelling(client):
    client.post(CAMPBELL, data={"new_genres": "Drama"})
    abbott = "/series/genres?name=Abbott%20and%20Costello"
    form = client.get(abbott).text
    assert 'name="genre" value="Drama"' in form
    assert not re.search(r'value="Drama"\s+checked', form)
    client.post(abbott, data={"new_genres": "COMEDY, drama"})
    client.post(abbott, data={"genre": ["Drama", "COMEDY"], "new_genres": "comedy"})
    tiles = client.get("/series").text
    assert "COMEDY · Drama" in tiles   # "drama" took the spelling already in use
    assert "comedy" not in tiles.replace("COMEDY", "")


def test_unticking_takes_a_genre_off(client):
    client.post(CAMPBELL, data={"new_genres": "Drama, Mystery"})
    assert re.search(r'value="Mystery"\s+checked', client.get(CAMPBELL).text)
    client.post(CAMPBELL, data={"genre": "Drama"})
    tiles = client.get("/series").text
    assert "Mystery" not in tiles and "Drama" in tiles
    client.post(CAMPBELL, data={})
    assert 'class="tile-genres' not in client.get("/series").text


def test_genres_follow_a_renamed_series_and_join_when_merged(client):
    client.post(CAMPBELL, data={"new_genres": "Drama"})
    client.post("/series/genres?name=Abbott%20and%20Costello", data={"new_genres": "Comedy, drama"})
    client.post("/series/rename?name=The%20Campbell%20Playhouse",
                data={"new_name": "Abbott and Costello"})
    page = client.get("/series/episodes", params={"name": "Abbott and Costello"}).text
    assert re.findall(r'<a class="genre" href="[^"]+">([^<]+)</a>', page) == ["Comedy", "Drama"]
    assert "Genres you've used" in client.get("/series/genres?name=Abbott%20and%20Costello").text


def test_genres_page_for_a_missing_series_is_not_found(client):
    assert client.get("/series/genres?name=Nobody").status_code == 404
    assert client.post("/series/genres?name=Nobody", data={"new_genres": "X"}).status_code == 404


GENRE_MENU = r'<a href="(/genre\?name=[^"]+)">([^<]+) <span class="muted small">· (\d+) series</span></a>'
GENRE_LABEL = r'<a class="genre" href="(/genre\?name=[^"]+)">([^<]+)<span class="genre-count"[^>]*>(\d+)</span></a>'


def test_each_genre_has_a_page_of_its_series(client):
    client.post(CAMPBELL, data={"new_genres": "Drama, Comedy"})
    client.post("/series/genres?name=Abbott%20and%20Costello", data={"new_genres": "Comedy"})
    page = client.get("/genre?name=Comedy").text
    assert "<h1>Comedy</h1>" in page
    assert re.findall(r'<span class="tile-name">([^<]+)</span>', page) == [
        "Abbott and Costello", "The Campbell Playhouse"]
    assert "2 series" in page and "6 episodes" in page
    assert '<a class="button" href="/series">← All series</a>' in page
    drama = client.get("/genre?name=Drama").text
    assert re.findall(r'<span class="tile-name">([^<]+)</span>', drama) == ["The Campbell Playhouse"]
    # Other capitals go to the usual address; a genre no series has isn't found.
    moved = client.get("/genre?name=comedy", follow_redirects=False)
    assert moved.status_code == 303 and moved.headers["location"] == "/genre?name=Comedy"
    assert client.get("/genre?name=Western").status_code == 404
    assert client.get("/genre").status_code == 404


def test_series_page_lists_the_genres_and_a_series_links_to_them(client):
    assert 'class="genre-list"' not in client.get("/series").text   # none in use yet
    client.post(CAMPBELL, data={"new_genres": "Drama, Comedy"})
    client.post("/series/genres?name=Abbott%20and%20Costello", data={"new_genres": "Comedy"})
    for view in ("/series", "/series?view=list"):
        assert re.findall(GENRE_LABEL, client.get(view).text) == [
            ("/genre?name=Comedy", "Comedy", "2"), ("/genre?name=Drama", "Drama", "1")]
    # On a phone: a drop-down instead.
    phone = client.get("/series", headers={"User-Agent": "iPhone Mobile Safari"}).text
    assert re.findall(GENRE_MENU, phone) == [
        ("/genre?name=Comedy", "Comedy", "2"), ("/genre?name=Drama", "Drama", "1")]
    assert "Genres <span class=\"muted small\">(2)</span>" in phone and 'class="genre-list"' not in phone
    page = client.get("/series/episodes", params={"name": "The Campbell Playhouse"}).text
    assert '<a class="genre" href="/genre?name=Comedy">Comedy</a>' in page
    # A genre no series uses any more drops off the list.
    client.post(CAMPBELL, data={"genre": "Comedy"})
    assert re.findall(GENRE_LABEL, client.get("/series").text) == [
        ("/genre?name=Comedy", "Comedy", "2")]


def test_search_finds_series_by_genre(client):
    client.post(CAMPBELL, data={"new_genres": "Anthology Drama"})
    page = client.get("/search", params={"q": "drama"}).text
    assert re.findall(GENRE_LABEL, page) == [("/genre?name=Anthology%20Drama", "Anthology Drama", "1")]
    assert re.findall(r'<span class="tile-name">([^<]+)</span>', page) == ["The Campbell Playhouse"]
    # Words from the name and the genre together.
    page = client.get("/search", params={"q": "campbell drama"}).text
    assert re.findall(r'<span class="tile-name">([^<]+)</span>', page) == ["The Campbell Playhouse"]
    assert "Nothing found" not in page
    assert "<h2>Genres</h2>" not in client.get("/search", params={"q": "abbott"}).text


def genre_rows(page: str) -> list[tuple[str, str, list[str]]]:
    """The home page's "Recently added in" rows: (link, genre, series names)."""
    rows = re.findall(r'<h2 id="genre-heading-\d"><a href="([^"]+)">Recently added in ([^<]+?) <span'
                      r'.*?<ul class="shelf">(.*?)</ul>', page, re.S)
    return [(link, genre, re.findall(r'tile-name">([^<]+)<', tiles)) for link, genre, tiles in rows]


def test_home_page_rows_by_genre(client):
    import app.main as main
    assert genre_rows(client.get("/").text) == []   # no genres yet: no rows
    with Session(main._engine) as session:
        files = {f.name: f for f in session.scalars(select(AudioFile))}
        long_ago = datetime(2020, 1, 1)
        for f in files.values():
            f.first_seen = long_ago
        files["abbott.wav"].first_seen = long_ago + timedelta(days=30)
        files["rebecca.wav"].first_seen = long_ago + timedelta(days=20)
        files["ep2.wav"].first_seen = long_ago + timedelta(days=10)
        session.commit()
    escape = "/series/genres?name=Escape%20%26%20Suspense%20%2F%20Mixed"
    client.post(CAMPBELL, data={"new_genres": "Drama, Comedy, Anthology"})
    client.post("/series/genres?name=Abbott%20and%20Costello", data={"new_genres": "Comedy"})
    client.post(escape, data={"new_genres": "Thriller, Drama"})
    page = client.get("/")
    rows = genre_rows(page.text)
    # Comedy has the newest (Abbott); Anthology and Drama come next (Campbell,
    # A–Z between equals); Thriller's newest is older, so it has no row.
    assert rows == [
        ("/genre?name=Comedy", "Comedy", ["Abbott and Costello", "The Campbell Playhouse"]),
        ("/genre?name=Anthology", "Anthology", ["The Campbell Playhouse"]),
        ("/genre?name=Drama", "Drama", ["The Campbell Playhouse", "Escape &amp; Suspense / Mixed"]),
    ]
    text = page.text
    assert text.index("Recently added series") < text.index("Recently added in Comedy")
    # "Recently added series" shows each series' genres under its name; the genre rows don't.
    recent = text.split(">Recently added series</h2>")[1].split("</ul>")[0]
    assert '<span class="tile-name">The Campbell Playhouse</span>\n        <span class="tile-genres small">Anthology · Comedy · Drama</span>' in recent
    assert "tile-genres" not in text.split("Recently added in Comedy")[1]
    # Fewer genres in use: fewer rows.
    client.post(CAMPBELL, data={})
    client.post(escape, data={})
    assert genre_rows(client.get("/").text) == [
        ("/genre?name=Comedy", "Comedy", ["Abbott and Costello"])]


def set_added(client, when: dict[str, datetime]) -> None:
    """Pretend these files were first seen at these times."""
    import app.main as main
    with Session(main._engine) as session:
        for f in session.scalars(select(AudioFile)):
            f.first_seen = when.get(f.name, datetime(2020, 1, 1))
        session.commit()


def test_series_list_sorts_by_date_added(client):
    set_added(client, {"rebecca.wav": datetime(2026, 11, 19, 12), "abbott.wav": datetime(2025, 3, 2, 12),
                       "ep2.wav": datetime(2026, 1, 5, 12)})
    names = lambda page: re.findall(r'<a class="title" href="[^"]*">([^<]+)</a>', page)
    page = client.get("/series?view=list").text
    assert names(page)[0] == "Abbott and Costello"         # every visit starts in name order
    assert 'href="/series?view=list&amp;sort=added"' in page
    assert 'href="/series?view=list&amp;dir=desc"' in page  # clicking "Series" again: Z to A
    assert "Nov 19, 2026" in page                          # the newest recording's day
    newest = names(client.get("/series?view=list&sort=added").text)
    assert newest == ["The Campbell Playhouse", "Escape &amp; Suspense / Mixed", "Abbott and Costello",
                      "No series yet"]
    oldest = client.get("/series?view=list&sort=added&dir=asc").text
    assert names(oldest)[:3] == ["Abbott and Costello", "Escape &amp; Suspense / Mixed",
                                 "The Campbell Playhouse"]
    assert names(client.get("/series?view=list&dir=desc").text)[:3] == [
        "Escape &amp; Suspense / Mixed", "The Campbell Playhouse", "Abbott and Costello"]
    assert names(client.get("/series?view=list&sort=nonsense").text)[0] == "Abbott and Costello"


def test_library_sorts_by_series_title_and_date_added(client):
    set_added(client, {"ep10.wav": datetime(2026, 11, 19, 12), "patriot.wav": datetime(2026, 11, 18, 12)})
    page = client.get("/library").text
    # Series by series ("The" ignored), each in air-date order; no series last.
    assert titles_in_order(page) == ["Who&#39;s on First", "Rebecca", "The Patriot", "Private Lives",
                                     "Some day in 1939", "No date at all", "Two", "Ten", "Untitled Episode"]
    assert 'aria-sort="ascending"' in page and 'href="/library?sort=added"' in page
    by_title = titles_in_order(client.get("/library?sort=title").text)
    assert by_title[:4] == ["No date at all", "The Patriot", "Private Lives", "Rebecca"]
    newest = client.get("/library?sort=added").text
    assert titles_in_order(newest)[:2] == ["Ten", "The Patriot"]
    assert "Nov 19, 2026" in newest
    # The tick-box menu comes back to the same order.
    assert 'name="back" value="/library?sort=added&amp;page=1"' in newest


CAMPBELL_PAGE = "/series/episodes?name=The%20Campbell%20Playhouse"


def test_a_series_has_an_about_text(client):
    assert "series-about" not in client.get(CAMPBELL_PAGE).text            # none to start with
    assert "Edit about…" in client.get(CAMPBELL_PAGE).text
    form = client.get("/series/about?name=The%20Campbell%20Playhouse")
    assert form.status_code == 200 and "About this series" in form.text
    client.post("/series/about?name=The%20Campbell%20Playhouse",
                data={"about": "  Orson Welles' radio plays.   \r\n\r\n\r\n\r\nSponsored by Campbell's.  "})
    page = client.get(CAMPBELL_PAGE).text
    assert "<p class=\"series-about\">Orson Welles&#39; radio plays.\n\nSponsored by Campbell&#39;s.</p>" in page
    # On a phone it's shown too (but not the Edit button).
    phone = client.get(CAMPBELL_PAGE, headers={"User-Agent": "iPhone Mobile"}).text
    assert "Orson Welles" in phone and "Edit about…" not in phone
    # Folded: on a phone behind a button, on a computer to two lines with "more".
    assert '<summary class="button">About this series</summary>' in phone and "data-about" not in phone
    assert "data-about" in page and 'class="more-button"' in page and "About this series</summary>" not in page
    # Too long is cut; empty takes it off.
    client.post("/series/about?name=The%20Campbell%20Playhouse", data={"about": "x" * 5000})
    assert "x" * 1000 + "</p>" in client.get(CAMPBELL_PAGE).text
    client.post("/series/about?name=The%20Campbell%20Playhouse", data={"about": "  "})
    assert "series-about" not in client.get(CAMPBELL_PAGE).text
    assert client.get("/series/about?name=Nobody").status_code == 404


def test_about_follows_a_renamed_series(client):
    client.post("/series/about?name=The%20Campbell%20Playhouse", data={"about": "Welles."})
    client.post("/series/rename?name=The%20Campbell%20Playhouse", data={"new_name": "Campbell Playhouse"})
    assert "Welles." in client.get("/series/episodes?name=Campbell%20Playhouse").text
    # Joining two series: the one renamed into keeps its own text.
    client.post("/series/about?name=Abbott%20and%20Costello", data={"about": "Comedy."})
    client.post("/series/rename?name=Campbell%20Playhouse", data={"new_name": "Abbott and Costello"})
    page = client.get("/series/episodes?name=Abbott%20and%20Costello").text
    assert "Comedy." in page and "Welles." not in page


def test_a_new_feed_starts_with_the_series_about(client):
    client.post("/series/about?name=The%20Campbell%20Playhouse", data={"about": "Welles & co."})
    form = client.get("/feeds/new?series=The%20Campbell%20Playhouse").text
    assert ">Welles &amp; co.</textarea>" in form
    assert "></textarea>" in client.get("/feeds/new?series=Abbott%20and%20Costello").text


def test_wikipedia_text_and_its_source(client):
    form = client.get("/series/about?name=The%20Campbell%20Playhouse").text
    # The search opens in a new tab, in the browser: the app never contacts Wikipedia.
    assert ('href="https://en.wikipedia.org/w/index.php?search=The+Campbell+Playhouse+radio&amp;fulltext=1" '
            'target="_blank" rel="noopener noreferrer">Search Wikipedia') in form
    pasted = ("The Campbell Playhouse was an American radio drama series.[1] It starred Orson Welles.[2][a]\n\n"
              "It ran on CBS[citation needed] from 1938 to 1940.[note 3] Box [13] stays? No: [13] goes.")
    bad = client.post("/series/about?name=The%20Campbell%20Playhouse",
                      data={"about": pasted, "source": "wikipedia"})
    assert bad.status_code == 400 and "starting with https://" in bad.text
    assert "American radio drama series.[1]" in bad.text                 # what was typed is kept
    assert "series-about" not in client.get(CAMPBELL_PAGE).text         # nothing saved
    client.post("/series/about?name=The%20Campbell%20Playhouse",
                data={"about": pasted, "source": " https://en.wikipedia.org/wiki/The_Campbell_Playhouse "})
    page = client.get(CAMPBELL_PAGE).text
    assert ("The Campbell Playhouse was an American radio drama series. It starred Orson Welles.\n\n"
            "It ran on CBS from 1938 to 1940. Box stays? No: goes.</p>") in page
    assert ('Source:\n      <a href="https://en.wikipedia.org/wiki/The_Campbell_Playhouse" target="_blank" '
            'rel="noopener noreferrer">Wikipedia</a>') in page
    # It follows a renamed series, and another site is credited by its name.
    client.post("/series/rename?name=The%20Campbell%20Playhouse", data={"new_name": "Campbell Playhouse"})
    assert ">Wikipedia</a>" in client.get("/series/episodes?name=Campbell%20Playhouse").text
    client.post("/series/about?name=Campbell%20Playhouse",
                data={"about": "Welles.", "source": "https://www.otrr.org/campbell"})
    assert ">otrr.org</a>" in client.get("/series/episodes?name=Campbell%20Playhouse").text
    client.post("/series/about?name=Campbell%20Playhouse", data={"about": "Welles.", "source": ""})
    assert "Source:" not in client.get("/series/episodes?name=Campbell%20Playhouse").text
