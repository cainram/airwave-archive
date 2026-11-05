"""Tests for episodes made of several files ("parts"). Run with:  pytest

There's no real multi-part recording to test with, so these make short
silent files named like a long show saved in hours.
"""

import re

import pytest
from fastapi.testclient import TestClient
from mutagen.id3 import TIT2, TPE1
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import MANUAL, AudioFile, Episode, make_engine, natural_key
from test_scanner import add_tags, make_wav

SHOW = "Art Bell 1996-05-02 Hour {}.wav"


@pytest.fixture
def client(tmp_path, monkeypatch):
    import app.main as main
    lib = tmp_path / "library"
    for hour in (1, 2, 3):
        make_wav(lib / SHOW.format(hour), hour)       # 1, 2 and 3 seconds long
        add_tags(lib / SHOW.format(hour), title=TIT2(encoding=3, text=f"Hour {hour}"),
                 artist=TPE1(encoding=3, text="Coast to Coast AM"))
    make_wav(lib / "Other Show.wav", 1)
    monkeypatch.setattr(main, "LIBRARY_DIR", lib)
    monkeypatch.setattr(main, "_engine", make_engine(tmp_path / "data" / "airwave.db"))
    client = TestClient(main.app)
    client.get("/")   # first visit scans
    client.lib = lib
    return client


def by_name(client) -> dict[str, AudioFile]:
    import app.main as main
    with Session(main._engine) as session:
        rows = session.scalars(select(AudioFile).options(main.WITH_PARTS)).unique().all()
        return {row.name: row for row in rows}


def file_id(client, hour) -> int:
    return by_name(client)[SHOW.format(hour)].id


def episode_count(client) -> int:
    import app.main as main
    with Session(main._engine) as session:
        return session.scalar(select(func.count(Episode.id)))


def group(client, lead, *hours):
    return client.post(f"/files/{file_id(client, lead)}/parts",
                       data={"part": [str(file_id(client, h)) for h in hours]},
                       follow_redirects=False)


def test_file_names_sort_like_people_expect():
    names = ["Hour 10.mp3", "Hour 2.mp3", "hour 1.mp3"]
    assert sorted(names, key=natural_key) == ["hour 1.mp3", "Hour 2.mp3", "Hour 10.mp3"]


def test_the_parts_page_offers_nearby_files(client):
    page = client.get(f"/files/{file_id(client, 1)}/parts")
    assert page.status_code == 200
    for hour in (1, 2, 3):
        assert SHOW.format(hour) in page.text
    assert 'value="%d" checked' % file_id(client, 1) in page.text   # itself, ticked
    assert 'value="%d" checked' % file_id(client, 2) not in page.text


def test_grouping_makes_one_episode_that_plays_straight_through(client):
    assert episode_count(client) == 4
    saved = group(client, 1, 3, 1, 2)   # ticked in any order
    assert saved.status_code == 303 and saved.headers["location"] == f"/files/{file_id(client, 1)}"

    files = by_name(client)
    parts = [files[SHOW.format(h)] for h in (1, 2, 3)]
    assert [p.part_number for p in parts] == [1, 2, 3]
    assert len({p.episode_id for p in parts}) == 1
    assert episode_count(client) == 2          # the empty ones are gone
    assert parts[0].episode.sources["parts"] == MANUAL
    assert parts[0].episode.title == "Hour 1"  # details from the first part's tags

    # The library shows the broadcast once, with its total length.
    library = client.get("/").text
    assert SHOW.format(1) in library and SHOW.format(2) not in library
    assert "3 parts" in library and "0:06" in library

    # Its page lists the parts, and Play has all three in order.
    page = client.get(f"/files/{parts[1].id}").text
    assert "Part 2 of 3" in page and "Change parts…" in page
    queue = re.search(r'data-queue="([^"]*)"', page)[1].split()
    assert queue == [f"/audio/{p.id}" for p in parts]


def test_details_and_edits_belong_to_all_parts(client):
    group(client, 1, 1, 2, 3)
    client.post(f"/files/{file_id(client, 3)}/edit",
                data={"title": "Area 51 Caller Night", "air_year": "1996"})
    files = by_name(client)
    assert {files[SHOW.format(h)].episode.title for h in (1, 2, 3)} == {"Area 51 Caller Night"}
    assert "one of 3 parts" in client.get(f"/files/{file_id(client, 2)}/edit").text


def test_later_parts_tags_are_not_used(client):
    group(client, 1, 1, 2, 3)
    # Part 2's tags change; a rescan must not make the broadcast "Hour 2 (new)".
    path = client.lib / SHOW.format(2)
    make_wav(path, 2.5)
    add_tags(path, title=TIT2(encoding=3, text="Hour 2 (new)"))
    client.post("/rescan")
    assert by_name(client)[SHOW.format(2)].episode.title == "Hour 1"


def test_splitting_gives_each_file_its_own_details_again(client):
    group(client, 1, 1, 2, 3)
    client.post(f"/files/{file_id(client, 1)}/edit", data={"title": "Kept on the first"})
    client.post(f"/files/{file_id(client, 2)}/parts", data={"part": [str(file_id(client, 1))]})
    files = by_name(client)
    assert [files[SHOW.format(h)].part_number for h in (1, 2, 3)] == [None, None, None]
    assert files[SHOW.format(1)].episode.title == "Kept on the first"
    assert files[SHOW.format(2)].episode.title == "Hour 2"      # from its tags
    assert episode_count(client) == 4
    assert "3 parts" not in client.get("/").text


def test_moving_a_part_from_another_group(client):
    group(client, 1, 1, 2)
    group(client, 3, 3, 2)          # take Hour 2 away from the first group
    files = by_name(client)
    assert files[SHOW.format(1)].part_number is None    # left on its own
    assert files[SHOW.format(1)].episode.sources.get("parts") is None
    assert [files[SHOW.format(h)].part_number for h in (2, 3)] == [1, 2]


def test_grouping_with_nothing_ticked_asks_again(client):
    sent = client.post(f"/files/{file_id(client, 1)}/parts", data={}, follow_redirects=False)
    assert "error=none" in sent.headers["location"]
    assert "tick at least one" in client.get(sent.headers["location"]).text


def test_a_missing_part_is_kept_skipped_when_playing_and_can_be_forgotten(client):
    group(client, 1, 1, 2, 3)
    (client.lib / SHOW.format(2)).unlink()
    client.post("/rescan")
    files = by_name(client)
    assert files[SHOW.format(2)].missing            # kept: grouping is the owner's work
    page = client.get(f"/files/{file_id(client, 1)}").text
    queue = re.search(r'data-queue="([^"]*)"', page)[1].split()
    assert queue == [f"/audio/{file_id(client, 1)}", f"/audio/{file_id(client, 3)}"]

    client.post(f"/files/{file_id(client, 2)}/forget")
    files = by_name(client)
    assert SHOW.format(2) not in files
    assert [files[SHOW.format(h)].part_number for h in (1, 3)] == [1, 2]


def test_an_overnight_broadcast_uses_the_length_of_all_parts(client, monkeypatch):
    import app.main as main
    group(client, 1, 1, 2, 3)
    with Session(main._engine) as session:   # pretend each part is an hour and a half
        for row in session.scalars(select(AudioFile).where(AudioFile.part_number.is_not(None))):
            row.duration_seconds = 90 * 60
        session.commit()
    client.post(f"/files/{file_id(client, 1)}/edit",
                data={"air_year": "1996", "air_month": "5", "air_day": "2",
                      "air_time": "22:00", "air_timezone": "America/Los_Angeles"})
    page = client.get(f"/files/{file_id(client, 2)}").text
    assert "until about 2:30 AM on Friday, May 3rd" in page


def test_review_page_ignores_later_parts(client):
    group(client, 1, 1, 2, 3)
    page = client.get("/review/filenames").text
    assert SHOW.format(1) in page
    assert SHOW.format(2) not in page and SHOW.format(3) not in page
