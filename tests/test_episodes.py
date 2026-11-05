"""Tests for episode details, editing, and missing files. Run with:  pytest"""

import os
import sqlite3
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import MANUAL, SCHEMA_VERSION, AudioFile, Episode, make_engine
from app.episodes import apply_edits, format_air_date, read_form
from app.scanner import sync_library
from test_scanner import make_wav

# ---------------------------------------------------------------- air dates


def form(**typed):
    return read_form(typed)


def test_partial_dates_are_accepted():
    assert form(air_year="1939")[1] == {}
    values, errors = form(air_year="1939", air_month="4")
    assert errors == {} and (values["air_year"], values["air_month"], values["air_day"]) == (1939, 4, None)
    values, errors = form(air_year="1939", air_month="4", air_day="14", air_time="8:05",
                          air_timezone="America/New_York")
    assert errors == {}
    assert values["air_time"] == "08:05"


def test_impossible_dates_are_explained():
    assert "air_month" in form(air_month="4")[1]                         # month, no year
    assert "air_day" in form(air_year="1939", air_day="14")[1]           # day, no month
    assert "doesn't have a day 30" in form(air_year="1943", air_month="2", air_day="30")[1]["air_day"]
    assert "air_year" in form(air_year="nineteen")[1]
    assert "air_year" in form(air_year="1700")[1]
    assert "air_time" in form(air_year="1939", air_month="4", air_time="20:00")[1]   # no day
    assert "air_time" in form(air_year="1939", air_month="4", air_day="1", air_time="25:00")[1]
    assert "air_timezone" in form(air_timezone="America/New_York")[1]    # zone, no time
    assert "air_timezone" in form(air_year="1939", air_month="4", air_day="1", air_time="20:00",
                                  air_timezone="Mars/Olympus")[1]


def test_air_dates_show_only_what_is_known():
    ep = Episode(air_year=1939)
    assert format_air_date(ep) == "1939, date unknown"
    assert format_air_date(ep, short=True) == "1939"
    ep.air_month = 4
    assert format_air_date(ep) == "April 1939, day unknown"
    assert format_air_date(ep, short=True) == "Apr 1939"
    ep.air_day = 14
    assert format_air_date(ep) == "Friday, 14 April 1939, time unknown"
    assert format_air_date(ep, short=True) == "14 Apr 1939"
    ep.air_time = "20:00"
    assert format_air_date(ep) == "Friday, 14 April 1939, 8:00 PM time zone unknown"
    ep.air_timezone = "America/New_York"
    assert format_air_date(ep) == "Friday, 14 April 1939, 8:00 PM US Eastern"
    assert format_air_date(Episode()) == ""
    assert format_air_date(None) == ""


def test_only_changed_fields_are_marked_manual():
    ep = Episode(series="Campbell Playhouse", sources_json=None)
    values, _ = form(series="Campbell Playhouse", title="The Patriot", air_year="1939")
    changed = apply_edits(ep, values)
    assert sorted(changed) == ["air_date", "title"]
    assert ep.sources == {"title": MANUAL, "air_date": MANUAL}
    assert ep.has_manual_edits


# ------------------------------------------------------- scanning and edits


@pytest.fixture
def engine(tmp_path):
    return make_engine(tmp_path / "data" / "airwave.db")


def files(engine):
    with Session(engine) as session:
        return {f.name: f for f in session.scalars(select(AudioFile))}


def edit(engine, name, **values):
    with Session(engine) as session:
        row = session.scalars(select(AudioFile).where(AudioFile.name == name)).one()
        full, errors = read_form(values)
        assert errors == {}
        apply_edits(row.episode, full)
        session.commit()


def test_every_file_gets_its_own_episode(tmp_path, engine):
    lib = tmp_path / "library"
    make_wav(lib / "a.wav", 1)
    make_wav(lib / "b.wav", 1)
    sync_library(engine, lib)
    found = files(engine)
    assert found["a.wav"].episode is not None
    assert found["a.wav"].episode_id != found["b.wav"].episode_id


def test_edits_survive_a_rescan(tmp_path, engine):
    lib = tmp_path / "library"
    make_wav(lib / "patriot.wav", 1)
    sync_library(engine, lib)
    edit(engine, "patriot.wav", cast="Orson Welles", air_year="1939")
    make_wav(lib / "patriot.wav", 2)   # the file itself changes
    run = sync_library(engine, lib)
    assert run.changed == 1
    ep = files(engine)["patriot.wav"].episode
    assert ep.cast == "Orson Welles" and ep.air_year == 1939


def test_gone_file_with_edits_is_kept_as_missing_and_reconnected(tmp_path, engine):
    lib = tmp_path / "library"
    make_wav(lib / "edited.wav", 1)
    make_wav(lib / "plain.wav", 1)
    sync_library(engine, lib)
    edit(engine, "edited.wav", title="Kept")
    original = files(engine)["edited.wav"]

    (lib / "edited.wav").rename(tmp_path / "away.wav")
    (lib / "plain.wav").unlink()
    run = sync_library(engine, lib)
    assert run.removed == 2
    found = files(engine)
    assert list(found) == ["edited.wav"]          # the plain one is forgotten
    assert found["edited.wav"].missing
    with Session(engine) as session:              # and so is its empty episode
        assert session.scalar(select(func.count(Episode.id))) == 1

    # A second scan doesn't count it as removed again.
    assert sync_library(engine, lib).removed == 0

    # It comes back, in a different folder: reconnected to its details.
    (lib / "Moved").mkdir()
    (tmp_path / "away.wav").rename(lib / "Moved" / "edited.wav")
    run = sync_library(engine, lib)
    assert (run.added, run.changed) == (0, 1)
    back = files(engine)["edited.wav"]
    assert not back.missing and back.folder == "Moved"
    assert back.id == original.id and back.episode.title == "Kept"


def test_file_moved_between_scans_keeps_its_details(tmp_path, engine):
    lib = tmp_path / "library"
    make_wav(lib / "show.wav", 1)
    sync_library(engine, lib)
    edit(engine, "show.wav", series="Suspense")
    (lib / "Suspense").mkdir()
    (lib / "show.wav").rename(lib / "Suspense" / "show.wav")
    run = sync_library(engine, lib)
    assert (run.added, run.changed, run.removed) == (0, 1, 0)
    moved = files(engine)["show.wav"]
    assert moved.folder == "Suspense" and moved.episode.series == "Suspense"


def test_old_database_is_updated_with_a_backup(tmp_path):
    db = tmp_path / "data" / "airwave.db"
    db.parent.mkdir()
    # A database as Phase 2 left it.
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE audio_files (
            id INTEGER PRIMARY KEY, folder VARCHAR NOT NULL, name VARCHAR NOT NULL,
            size_bytes INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
            duration_seconds FLOAT, bitrate INTEGER, tag_title VARCHAR,
            tag_artist VARCHAR, tag_album VARCHAR, tag_date VARCHAR, tag_genre VARCHAR,
            tag_track VARCHAR, tag_comment VARCHAR, tags_json VARCHAR,
            first_seen DATETIME NOT NULL, last_read DATETIME NOT NULL,
            CONSTRAINT uq_audio_files_path UNIQUE (folder, name));
        CREATE TABLE scan_runs (
            id INTEGER PRIMARY KEY, started_at DATETIME NOT NULL, seconds FLOAT NOT NULL,
            added INTEGER NOT NULL, changed INTEGER NOT NULL, removed INTEGER NOT NULL,
            unchanged INTEGER NOT NULL, library_missing BOOLEAN NOT NULL);
        INSERT INTO audio_files VALUES (1, '', 'old.wav', 8044, 1, 1.0, 64000, 'Old Show',
            NULL, NULL, NULL, NULL, NULL, NULL, NULL, '2026-11-04 00:00:00', '2026-11-04 00:00:00');
        PRAGMA user_version = 1;
    """)
    conn.close()

    engine = make_engine(db)

    assert (tmp_path / "data" / f"airwave.db.before-v{SCHEMA_VERSION}").exists()
    with engine.connect() as c:
        assert c.exec_driver_sql("PRAGMA user_version").scalar() == SCHEMA_VERSION
    old = files(engine)["old.wav"]
    assert old.tag_title == "Old Show" and not old.missing
    # The next scan gives it an episode.
    lib = tmp_path / "library"
    lib.mkdir()
    make_wav(lib / "new.wav", 1)
    sync_library(engine, lib)
    assert files(engine)["new.wav"].episode is not None


# ---------------------------------------------------------------- web pages


@pytest.fixture
def client(tmp_path, monkeypatch):
    import app.main as main
    lib = tmp_path / "library"
    make_wav(lib / "CampbellPlayhouse39-04-14ThePatriot.wav", 1)
    monkeypatch.setattr(main, "LIBRARY_DIR", lib)
    monkeypatch.setattr(main, "_engine", make_engine(tmp_path / "data" / "airwave.db"))
    client = TestClient(main.app)
    client.get("/")   # first visit scans
    return client


GOOD = {"series": "The Campbell Playhouse", "title": "The Patriot", "cast": "Orson Welles\nAnna May Wong",
        "air_year": "1939", "air_month": "4", "air_day": "14", "air_time": "20:00",
        "air_timezone": "America/New_York"}


def test_edit_form_saves_details(client):
    page = client.get("/files/1/edit")
    assert page.status_code == 200 and "Original air date" in page.text

    saved = client.post("/files/1/edit", data=GOOD, follow_redirects=False)
    assert saved.status_code == 303 and saved.headers["location"] == "/files/1"

    page = client.get("/files/1")
    assert "<h1>The Patriot</h1>" in page.text
    assert "Friday, 14 April 1939, 8:00 PM US Eastern" in page.text
    assert "Anna May Wong" in page.text
    library = client.get("/")
    assert "The Patriot" in library.text and "14 Apr 1939" in library.text
    assert "The Campbell Playhouse" in library.text


def test_edit_form_explains_mistakes_and_keeps_what_was_typed(client):
    bad = dict(GOOD, air_day="31")   # April has 30 days
    page = client.post("/files/1/edit", data=bad)
    assert page.status_code == 200
    assert "April 1939 doesn&#39;t have a day 31" in page.text
    assert "Anna May Wong" in page.text            # nothing typed is lost
    assert "The Patriot" not in client.get("/").text   # and nothing was saved


def test_edit_from_htmx_keeps_the_player_going(client):
    saved = client.post("/files/1/edit", data=GOOD, headers={"HX-Request": "true"})
    assert '"path": "/files/1"' in saved.headers["HX-Location"]


def test_missing_file_is_shown_and_can_be_forgotten(client, tmp_path):
    client.post("/files/1/edit", data=GOOD)
    (tmp_path / "library" / "CampbellPlayhouse39-04-14ThePatriot.wav").unlink()
    client.post("/rescan")

    library = client.get("/")
    assert "1 missing" in library.text and 'data-src="/audio/1"' not in library.text
    page = client.get("/files/1")
    assert "This file is missing" in page.text
    assert client.get("/audio/1").status_code == 404

    assert client.post("/files/1/forget", follow_redirects=False).status_code == 303
    assert client.get("/files/1").status_code == 404


def test_files_still_present_cannot_be_forgotten(client):
    assert client.post("/files/1/forget").status_code == 400


# ------------------------------------------------------ filling in from tags

from mutagen.id3 import COMM, TDRC, TIT2, TPE1  # noqa: E402

from app.db import TAGS  # noqa: E402
from app.episodes import date_from_tag  # noqa: E402
from test_scanner import add_tags  # noqa: E402


def test_dates_from_tags_keep_only_known_parts():
    assert date_from_tag("1939") == (1939, None, None)
    assert date_from_tag("1939-04") == (1939, 4, None)
    assert date_from_tag("1939-04-14") == (1939, 4, 14)
    assert date_from_tag("1939-04-14T20:00:00") == (1939, 4, 14)   # time ignored: no zone
    assert date_from_tag("1939-04-31") == (1939, 4, None)          # no such day
    assert date_from_tag("April 14, 1939") == (None, None, None)   # unclear: unknown
    assert date_from_tag("39") == (None, None, None)
    assert date_from_tag(None) == (None, None, None)


def tagged_wav(path, title="The Patriot", artist="The Campbell Playhouse", date="1939",
               comment="Originally aired April 14, 1939\nCast: Orson Welles"):
    make_wav(path, 1)
    add_tags(path, title=TIT2(encoding=3, text=title), artist=TPE1(encoding=3, text=artist),
             date=TDRC(encoding=3, text=date),
             comment=COMM(encoding=3, lang="eng", desc="", text=comment))


def test_scan_fills_in_details_from_tags(tmp_path, engine):
    lib = tmp_path / "library"
    tagged_wav(lib / "patriot.wav")
    sync_library(engine, lib)
    ep = files(engine)["patriot.wav"].episode
    assert ep.series == "The Campbell Playhouse"
    assert ep.title == "The Patriot"
    assert (ep.air_year, ep.air_month, ep.air_day) == (1939, None, None)
    assert ep.notes.startswith("Originally aired April 14, 1939")
    assert ep.sources == {"series": TAGS, "title": TAGS, "air_date": TAGS, "notes": TAGS}
    assert not ep.has_manual_edits


def test_manual_edits_win_over_tags(tmp_path, engine):
    lib = tmp_path / "library"
    tagged_wav(lib / "patriot.wav")
    sync_library(engine, lib)
    edit(engine, "patriot.wav", series="Campbell Playhouse", title="The Patriot",
         notes="", air_year="1939", air_month="4", air_day="14")

    tagged_wav(lib / "patriot.wav", title="Retitled in the tags")
    os.utime(lib / "patriot.wav", ns=(1, 1))
    sync_library(engine, lib)

    ep = files(engine)["patriot.wav"].episode
    assert ep.series == "Campbell Playhouse"             # typed by hand: kept
    assert ep.notes is None                              # cleared by hand: stays cleared
    assert (ep.air_month, ep.air_day) == (4, 14)
    assert ep.title == "Retitled in the tags"            # not edited (same value), so follows tags


def test_details_from_tags_follow_tag_changes(tmp_path, engine):
    lib = tmp_path / "library"
    tagged_wav(lib / "show.wav", date="1939")
    sync_library(engine, lib)
    make_wav(lib / "show.wav", 2)                        # same file, tags gone
    sync_library(engine, lib)
    ep = files(engine)["show.wav"].episode
    assert ep.series is None and ep.title is None and ep.air_year is None
    assert ep.sources == {}


def test_upgrade_fills_in_existing_episodes(tmp_path):
    db = tmp_path / "data" / "airwave.db"
    lib = tmp_path / "library"
    tagged_wav(lib / "patriot.wav")
    engine = make_engine(db)
    sync_library(engine, lib)
    # Make it look like a version 2 database, before filling in from tags.
    with engine.begin() as conn:
        conn.exec_driver_sql("UPDATE episodes SET series=NULL, title=NULL, notes=NULL, "
                             "air_year=NULL, sources_json=NULL")
        conn.exec_driver_sql("PRAGMA user_version=2")
    engine.dispose()

    engine = make_engine(db)
    ep = files(engine)["patriot.wav"].episode
    assert ep.series == "The Campbell Playhouse" and ep.air_year == 1939


def test_pages_say_what_came_from_tags(tmp_path, monkeypatch):
    import app.main as main
    lib = tmp_path / "library"
    tagged_wav(lib / "patriot.wav")
    monkeypatch.setattr(main, "LIBRARY_DIR", lib)
    monkeypatch.setattr(main, "_engine", make_engine(tmp_path / "data" / "airwave.db"))
    client = TestClient(main.app)
    client.get("/")
    page = client.get("/files/1")
    assert "from file tags" in page.text
    assert "1939, date unknown" in page.text
    assert "filled in from the file's tags" in client.get("/files/1/edit").text


def test_saving_the_form_unchanged_keeps_tag_values_automatic(tmp_path, monkeypatch):
    import app.main as main
    lib = tmp_path / "library"
    tagged_wav(lib / "patriot.wav", comment="Originally aired April 14, 1939\r\n\r\nCast:\r\nOrson Welles")
    monkeypatch.setattr(main, "LIBRARY_DIR", lib)
    monkeypatch.setattr(main, "_engine", make_engine(tmp_path / "data" / "airwave.db"))
    client = TestClient(main.app)
    client.get("/")
    ep = files(main._engine)["patriot.wav"].episode
    # What a browser sends back when Save is pressed without changing anything
    # (browsers send line endings in textareas as \r\n).
    unchanged = {"series": ep.series, "title": ep.title, "notes": ep.notes.replace("\n", "\r\n"),
                 "air_year": "1939"}
    client.post("/files/1/edit", data=unchanged)
    assert files(main._engine)["patriot.wav"].episode.sources == {
        "series": TAGS, "title": TAGS, "notes": TAGS, "air_date": TAGS}
