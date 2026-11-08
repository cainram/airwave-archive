"""Tests for suggested groups (files that look like parts of one
broadcast). Run with:  pytest"""

import pytest
from fastapi.testclient import TestClient
from mutagen.id3 import TDRC
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import AudioFile, GroupDismissal, make_engine
from app.grouping import find_marker, label_for, suggestions
from test_scanner import add_tags, make_wav


@pytest.mark.parametrize("name, kind, number, total", [
    ("Coast to Coast - Mar 01 2010 - Hour 1.mp3", "hour", 1, None),
    ("Art Bell 1996-05-02 Hr. 3.mp3", "hr", 3, None),
    ("Lux Radio Theatre - Part 2 of 2 - Rebecca.mp3", "part", 2, 2),
    ("Show_pt_4.mp3", "pt", 4, None),
    ("The Lone Ranger 1941-01-01 (1 of 2).mp3", "of", 1, 2),
    ("Bob and Ray Side B.mp3", "side", 2, None),
    ("Bob and Ray side 1.mp3", "side", 1, None),
])
def test_part_markers(name, kind, number, total):
    m = find_marker(name)
    assert (m.kind, m.number, m.total) == (kind, number, total)


@pytest.mark.parametrize("name", [
    "Suspense 1943-05-13 - Donovan's Brain.mp3",   # no marker
    "The Party 2.mp3",                              # "Party" isn't "Part"
    "Departure 1.mp3",
    "Christmas Hour 1 Part 2.mp3",                  # two markers: unclear
    "Escape - Three Skeleton Key.mp3",
])
def test_names_without_one_clear_marker(name):
    assert find_marker(name) is None


def test_the_label_drops_the_marker():
    assert label_for(find_marker("Coast to Coast - Mar 01 2010 - Hour 1.mp3")) == "Coast to Coast - Mar 01 2010"
    assert label_for(find_marker("Lux - Part 1 - Rebecca.mp3")) == "Lux - Rebecca"
    assert label_for(find_marker("Lone Ranger (1 of 2).mp3")) == "Lone Ranger"


C2C = "Coast to Coast - Mar 01 2010 - Hour {}.mp3"


@pytest.fixture
def client(tmp_path, monkeypatch):
    import app.main as main
    lib = tmp_path / "library"
    for hour in (1, 2, 3):
        make_wav(lib / "C2C" / C2C.format(hour), 1)
    # Two sides of one record.
    for side in "AB":
        make_wav(lib / "Bob and Ray" / f"Bob and Ray 1952 Side {side}.wav", 1)
    # Not suggested: a part missing in the middle...
    for hour in (1, 3):
        make_wav(lib / "Gaps" / f"Gap Show Hour {hour}.wav", 1)
    # ...a lone part...
    make_wav(lib / "Lone" / "Lone Show Part 1.wav", 1)
    # ...parts in different folders...
    make_wav(lib / "Split1" / "Split Show Hour 1.wav", 1)
    make_wav(lib / "Split2" / "Split Show Hour 2.wav", 1)
    # ..."1 of 3" with only two found...
    for n in (1, 2):
        make_wav(lib / "Short" / f"Short Show ({n} of 3).wav", 1)
    # ...and two parts whose date tags disagree.
    for hour, date in ((1, "1996-05-02"), (2, "1996-05-03")):
        path = lib / "Dates" / f"Date Show Hour {hour}.wav"
        make_wav(path, 1)
        add_tags(path, date=TDRC(encoding=3, text=date))
    # Matching date tags are fine.
    for hour in (1, 2):
        path = lib / "Dates" / f"Same Day Hour {hour}.wav"
        make_wav(path, 1)
        add_tags(path, date=TDRC(encoding=3, text="1996-05-02"))
    monkeypatch.setattr(main, "LIBRARY_DIR", lib)
    monkeypatch.setattr(main, "_engine", make_engine(tmp_path / "data" / "airwave.db"))
    client = TestClient(main.app)
    client.get("/")   # first visit scans
    return client


def found(client) -> list[str]:
    import app.main as main
    with Session(main._engine) as session:
        return [s.label for s in suggestions(session)]


def parts(client) -> dict[str, int | None]:
    import app.main as main
    with Session(main._engine) as session:
        return {f.name: f.part_number for f in session.scalars(select(AudioFile))}


def test_only_sensible_sets_are_suggested(client):
    assert found(client) == ["Bob and Ray 1952", "Coast to Coast - Mar 01 2010", "Same Day"]
    page = client.get("/groups/suggested").text
    assert "Coast to Coast - Mar 01 2010" in page and "3 parts" in page
    assert page.count('name="group"') == 3 and page.count("checked") >= 4   # ticked by default
    # Looking groups nothing.
    assert all(number is None for number in parts(client).values())


def test_the_library_menu_offers_the_check(client):
    assert 'href="/groups/suggested"' in client.get("/library").text


def test_approving_ticked_groups(client):
    import app.main as main
    with Session(main._engine) as session:
        by_label = {s.label: s.signature for s in suggestions(session)}
    sent = client.post("/groups/approve", data={"group": [by_label["Coast to Coast - Mar 01 2010"],
                                                          by_label["Bob and Ray 1952"]]},
                       follow_redirects=False)
    assert sent.headers["location"] == "/groups/suggested?approved=2"
    numbers = parts(client)
    assert [numbers[C2C.format(h)] for h in (1, 2, 3)] == [1, 2, 3]
    assert numbers["Bob and Ray 1952 Side A.wav"] == 1 and numbers["Bob and Ray 1952 Side B.wav"] == 2
    assert numbers["Same Day Hour 1.wav"] is None          # not ticked, not grouped
    assert found(client) == ["Same Day"]
    assert "Grouped 2 broadcasts" in client.get(sent.headers["location"]).text
    # Approving something no longer suggested (or made up) does nothing.
    assert client.post("/groups/approve", data={"group": ["nonsense"]},
                       follow_redirects=False).headers["location"] == "/groups/suggested?approved=0"


def test_dismissed_suggestions_stay_dismissed(client):
    import app.main as main
    with Session(main._engine) as session:
        sig = next(s.signature for s in suggestions(session) if s.label == "Same Day")
    sent = client.post("/groups/dismiss", data={"dismiss": sig}, follow_redirects=False)
    assert sent.headers["location"] == "/groups/suggested?dismissed=1"
    assert "Same Day" not in found(client)
    client.post("/rescan")
    assert "Same Day" not in found(client)
    with Session(main._engine) as session:
        assert session.get(GroupDismissal, sig).label == "Same Day"
        assert "Same Day" in [s.label for s in suggestions(session, include_dismissed=True)]
    assert parts(client)["Same Day Hour 1.wav"] is None


def test_a_new_part_makes_a_dismissed_set_new_again(client, tmp_path):
    import app.main as main
    with Session(main._engine) as session:
        sig = next(s.signature for s in suggestions(session) if s.label == "Bob and Ray 1952")
    client.post("/groups/dismiss", data={"dismiss": sig})
    assert "Bob and Ray 1952" not in found(client)
    make_wav(main.LIBRARY_DIR / "Bob and Ray" / "Bob and Ray 1952 Side C.wav", 1)
    client.post("/rescan")
    assert "Bob and Ray 1952" in found(client)


def test_files_already_grouped_arent_suggested(client):
    import app.main as main
    with Session(main._engine) as session:
        ids = {f.name: f.id for f in session.scalars(select(AudioFile))}
    client.post(f"/files/{ids[C2C.format(1)]}/parts",
                data={"part": [str(ids[C2C.format(h)]) for h in (1, 2)]})
    assert "Coast to Coast - Mar 01 2010" not in found(client)   # Hour 3 alone isn't a set


def test_nothing_to_suggest(tmp_path, monkeypatch):
    import app.main as main
    make_wav(tmp_path / "library" / "Suspense 1943-05-13.wav", 1)
    monkeypatch.setattr(main, "LIBRARY_DIR", tmp_path / "library")
    monkeypatch.setattr(main, "_engine", make_engine(tmp_path / "data" / "airwave.db"))
    client = TestClient(main.app)
    client.get("/")
    assert "No suggestions" in client.get("/groups/suggested").text
