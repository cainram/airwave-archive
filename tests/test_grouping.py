"""Tests for suggested groups (files that look like parts of one
broadcast). Run with:  pytest"""

import pytest
from fastapi.testclient import TestClient
from mutagen.id3 import TDRC
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import AudioFile, GroupDismissal, InboxItem, make_engine
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


# ---------------------------------------------------- the Library's notice

def test_the_library_mentions_new_suggestions_until_seen(client):
    page = client.get("/library").text
    assert "3 new possible groups" in page and 'href="/groups/suggested">Review them' in page
    client.get("/groups/suggested")                  # looking at them...
    assert "new possible group" not in client.get("/library").text
    # ...until a scan turns up another.
    import app.main as main
    for hour in (1, 2):
        make_wav(main.LIBRARY_DIR / "Late" / f"Late Show Hour {hour}.wav", 1)
    client.post("/rescan")
    assert "1 new possible group:" in client.get("/library").text


# ------------------------------------------------------------ the inbox

INBOX_SHOW = "Art Bell 1996-05-02 Hour {}.wav"


@pytest.fixture
def inbox_client(tmp_path, monkeypatch):
    import app.main as main
    lib, box = tmp_path / "library", tmp_path / "inbox"
    make_wav(lib / "Art Bell" / "Art Bell 1996-05-01.wav", 1)   # the series' folder
    for hour in (1, 2, 3):
        make_wav(box / INBOX_SHOW.format(hour), 1)
    make_wav(box / "Suspense 1943-05-13.wav", 1)
    monkeypatch.setattr(main, "LIBRARY_DIR", lib)
    monkeypatch.setattr(main, "INBOX_DIR", box)
    monkeypatch.setattr(main, "_engine", make_engine(tmp_path / "data" / "airwave.db"))
    client = TestClient(main.app)
    client.get("/")
    client.box, client.lib = box, lib
    return client


def inbox_form(client) -> dict:
    """What the inbox page's form would send, as the owner left it."""
    import re
    page = client.get("/inbox").text
    data: dict[str, list[str]] = {}
    for name, value in re.findall(r'<input id="[^"]+" name="([^"]+)" type="text" value="([^"]*)"', page):
        data.setdefault(name, []).append(value)
    data["together"] = re.findall(r'name="together" value="([^"]+)" checked', page)
    return data


def library_episodes(client) -> dict[str, tuple]:
    import app.main as main
    with Session(main._engine) as session:
        return {f.name: (f.folder, f.episode_id, f.part_number, f.episode.title, f.episode.air_day)
                for f in session.scalars(select(AudioFile))}


def test_the_inbox_shows_a_suggested_group_together(inbox_client):
    page = inbox_client.get("/inbox").text
    assert "Add these 3 files as one episode" in page and "Art Bell 1996-05-02" in page
    assert page.count('name="together"') == 1 and "Part 2 of 3" in page
    first, second = page.index(INBOX_SHOW.format(1)), page.index(INBOX_SHOW.format(2))
    assert first < second < page.index(INBOX_SHOW.format(3)) < page.index("Suspense 1943-05-13.wav")


def test_adding_all_with_the_group_ticked_makes_one_episode(inbox_client):
    sent = inbox_client.post("/inbox/add-all", data=inbox_form(inbox_client), follow_redirects=False)
    assert "grouped=1" in sent.headers["location"]
    assert "went in as one episode" in inbox_client.get(sent.headers["location"]).text
    eps = library_episodes(inbox_client)
    parts = [eps[INBOX_SHOW.format(h)] for h in (1, 2, 3)]
    assert {p[1] for p in parts} == {parts[0][1]} and [p[2] for p in parts] == [1, 2, 3]
    assert {p[0] for p in parts} == {"Art Bell"}                   # next to the series
    assert parts[0][4] == 2                                        # May 2nd, from the name
    assert eps["Suspense 1943-05-13.wav"][2] is None               # on its own
    assert list(inbox_client.box.iterdir()) == []


def test_adding_one_part_adds_the_whole_ticked_group(inbox_client):
    import app.main as main
    data = inbox_form(inbox_client)        # (opening the inbox lists its files)
    with Session(main._engine) as session:
        hour2 = session.scalar(select(InboxItem.id).where(InboxItem.name == INBOX_SHOW.format(2)))
    sent = inbox_client.post(f"/inbox/{hour2}/add", data=data, follow_redirects=False)
    assert sent.status_code == 303
    eps = library_episodes(inbox_client)
    assert [eps[INBOX_SHOW.format(h)][2] for h in (1, 2, 3)] == [1, 2, 3]
    assert "Suspense 1943-05-13.wav" not in eps                    # still waiting


def test_unticked_groups_go_in_separately(inbox_client):
    data = inbox_form(inbox_client)
    data["together"] = []
    inbox_client.post("/inbox/add-all", data=data)
    eps = library_episodes(inbox_client)
    assert len({eps[INBOX_SHOW.format(h)][1] for h in (1, 2, 3)}) == 3
    assert all(eps[INBOX_SHOW.format(h)][2] is None for h in (1, 2, 3))


def test_dismissing_an_inbox_group(inbox_client):
    data = inbox_form(inbox_client)
    data["dismiss"] = data["together"][0]
    data["title-1"] = ["Typed before dismissing"]
    sent = inbox_client.post("/inbox/groups/dismiss", data=data, follow_redirects=False)
    assert sent.headers["location"] == "/inbox?not_grouped=1"
    page = inbox_client.get(sent.headers["location"]).text
    assert "won't be suggested as one broadcast again" in page and 'name="together"' not in page
    assert "Typed before dismissing" in page                       # what was typed is kept
    inbox_client.post("/inbox/add-all", data=inbox_form(inbox_client))
    eps = library_episodes(inbox_client)
    assert all(eps[INBOX_SHOW.format(h)][2] is None for h in (1, 2, 3))


def test_a_group_dismissed_in_the_inbox_isnt_suggested_in_the_library(inbox_client):
    data = inbox_form(inbox_client)
    data["dismiss"] = data["together"][0]
    inbox_client.post("/inbox/groups/dismiss", data=data)
    inbox_client.post("/inbox/add-all", data=inbox_form(inbox_client))
    assert found(inbox_client) == []
    assert "new possible group" not in inbox_client.get("/library").text
