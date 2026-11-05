"""Tests for searching. Run with:  pytest"""

import re
import sqlite3

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import SCHEMA_VERSION, AudioFile, make_engine
from app.search import highlight, to_query
from app.scanner import sync_library
from test_scanner import make_wav

DETAILS = {
    "patriot.wav": dict(series="The Campbell Playhouse", title="The Patriot", host="Orson Welles",
                        cast="Orson Welles\nAnna May Wong", air_year="1939"),
    "our town.wav": dict(series="The Campbell Playhouse", title="Our Town", cast="Orson Welles",
                         notes="Thornton Wilder's play, with Pépé the stage manager."),
    "c2c.wav": dict(series="Coast to Coast AM", title="Open Lines", host="George Noory",
                    network="Premiere Networks", notes="Callers talk about the town of Roswell."),
    "Suspense Ep 42.wav": dict(series="Suspense", episode_number="42", title="Donovan's Brain"),
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    import app.main as main
    lib = tmp_path / "library"
    for name in DETAILS:
        make_wav(lib / name, 1)
    monkeypatch.setattr(main, "LIBRARY_DIR", lib)
    monkeypatch.setattr(main, "_engine", make_engine(tmp_path / "data" / "airwave.db"))
    client = TestClient(main.app)
    client.get("/")   # first visit scans
    with Session(main._engine) as session:
        client.ids = {f.name: f.id for f in session.scalars(select(AudioFile))}
    for name, details in DETAILS.items():
        client.post(f"/files/{client.ids[name]}/edit", data=details)
    return client


def found(client, q) -> list[str]:
    page = client.get("/search", params={"q": q}).text
    return re.findall(r'<a class="title" href="/files/\d+">([^<]+)</a>', page)


def test_typed_words_become_a_safe_query():
    assert to_query('orson welle') == '"orson"* "welle"*'
    assert to_query('"our town" wilder') == '"our town"* "wilder"*'
    assert to_query('NOT OR AND (x)') == '"NOT"* "OR"* "AND"* "x"*'   # just words
    assert to_query('  ;;  ') is None
    assert str(highlight("<b>\x02Welles\x03</b>")) == "&lt;b&gt;<mark>Welles</mark>&lt;/b&gt;"


def test_finding_by_any_detail(client):
    assert found(client, "anna may wong") == ["The Patriot"]                  # cast
    assert found(client, "noory") == ["Open Lines"]                           # host
    assert found(client, "premiere") == ["Open Lines"]                        # station
    assert found(client, "thornton wilder") == ["Our Town"]                   # notes
    assert found(client, "42") == ["Donovan&#39;s Brain"]                     # episode number
    assert found(client, "patriot.wav") == ["The Patriot"]                    # file name


def test_words_in_any_order_partial_and_without_accents(client):
    assert set(found(client, "welles orson")) == {"The Patriot", "Our Town"}
    assert set(found(client, "welle")) == {"The Patriot", "Our Town"}         # cut short
    assert found(client, "pepe") == ["Our Town"]                              # Pépé
    assert found(client, "WELLES patriot") == ["The Patriot"]                 # every word must match


def test_quoted_words_must_be_together(client):
    assert set(found(client, "town")) == {"Our Town", "Open Lines"}
    assert found(client, '"our town"') == ["Our Town"]


def test_best_matches_come_first(client):
    # "town" is in Our Town's title, but only in Open Lines' notes.
    assert found(client, "town") == ["Our Town", "Open Lines"]


def test_results_show_series_snippets_and_play_buttons(client):
    page = client.get("/search", params={"q": "campbell"}).text
    assert 'class="tile" href="/series/episodes?name=The%20Campbell%20Playhouse"' in page
    page = client.get("/search", params={"q": "roswell"}).text
    assert "<mark>Roswell</mark>" in page and "data-play" in page
    assert "Nothing found" in client.get("/search", params={"q": "zzzz"}).text
    assert "Type a few words" in client.get("/search").text
    assert 'class="search-box"' in client.get("/library").text      # the box is on every page


def test_the_index_follows_edits_renames_and_forgetting(client):
    client.post(f"/files/{client.ids['c2c.wav']}/edit", data={"title": "Area 51 Callers",
                                                              "series": "Coast to Coast AM"})
    assert found(client, "area 51") == ["Area 51 Callers"]
    assert found(client, "open lines") == []
    client.post("/series/rename?name=Suspense", data={"new_name": "Suspense Theatre"})
    page = client.get("/search", params={"q": "theatre"}).text
    assert "Donovan&#39;s Brain" in page
    # The file leaves the library. It has edits, so it's kept as "missing";
    # forgetting it then removes it from the search too.
    import app.main as main
    (main.LIBRARY_DIR / "Suspense Ep 42.wav").unlink()
    client.post("/rescan")
    client.post(f"/files/{client.ids['Suspense Ep 42.wav']}/forget")
    assert found(client, "donovan") == []


def test_an_older_database_gets_an_index_of_what_it_had(tmp_path):
    lib = tmp_path / "library"
    make_wav(lib / "Suspense - Donovans Brain.wav", 1)
    db = tmp_path / "data" / "airwave.db"
    engine = make_engine(db)
    sync_library(engine, lib)
    engine.dispose()
    with sqlite3.connect(db) as conn:     # pretend it's version 7, with no index
        conn.execute("DROP TABLE episode_search")
        conn.execute("PRAGMA user_version = 7")
    engine = make_engine(db)
    with Session(engine) as session:
        from app.search import search
        results, total = search(session, "donovans", 10)
    assert total == 1
    assert (tmp_path / "data" / f"airwave.db.before-v{SCHEMA_VERSION}").exists()
