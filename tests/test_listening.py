"""Tests for continue listening and played / not played. Run with:  pytest"""

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import AudioFile, Listening, make_engine
from app.listening import Progress, time_left
from test_scanner import make_wav

SHOW = "Art Bell 1996-05-02 Hour {}.wav"


@pytest.fixture
def client(tmp_path, monkeypatch):
    import app.main as main
    lib = tmp_path / "library"
    for hour in (1, 2, 3):
        make_wav(lib / SHOW.format(hour), 60)          # each part one minute long
    make_wav(lib / "Suspense 1943-05-13.wav", 120)
    monkeypatch.setattr(main, "LIBRARY_DIR", lib)
    monkeypatch.setattr(main, "_engine", make_engine(tmp_path / "data" / "airwave.db"))
    client = TestClient(main.app)
    client.get("/")
    ids = files(client)
    client.post(f"/files/{ids[SHOW.format(1)]}/parts",
                data={"part": [str(ids[SHOW.format(h)]) for h in (1, 2, 3)]})
    return client


def files(client) -> dict[str, int]:
    import app.main as main
    with Session(main._engine) as session:
        return {f.name: f.id for f in session.scalars(select(AudioFile))}


def episode_of(client, name) -> int:
    import app.main as main
    with Session(main._engine) as session:
        return session.scalars(select(AudioFile.episode_id).where(AudioFile.name == name)).one()


def place(client, name):
    import app.main as main
    with Session(main._engine) as session:
        row = session.get(Listening, episode_of(client, name))
        return None if row is None else (row.file_id, row.position, row.played)


def test_time_left_in_words():
    assert time_left(Progress(60, 23 * 60, 10)) == "23 min left"
    assert time_left(Progress(60, 65 * 60, 10)) == "1 hr 5 min left"
    assert time_left(Progress(60, 120 * 60, 10)) == "2 hr left"
    assert time_left(Progress(60, 20, 99)) == "less than a minute left"
    assert time_left(Progress(60, None, None)) == "started"


def test_saving_the_place_and_resuming(client):
    ids, show = files(client), episode_of(client, SHOW.format(1))
    sent = client.post(f"/listening/{show}", data={"file": ids[SHOW.format(2)], "at": "30"})
    assert sent.status_code == 204
    assert place(client, SHOW.format(1)) == (ids[SHOW.format(2)], 30.0, False)

    page = client.get("/files/%d" % ids[SHOW.format(1)]).text
    # The big button picks up in part 2, a little before where it stopped.
    assert f'data-resume-file="{ids[SHOW.format(2)]}" data-resume-at="27.0"' in page
    assert "▶ Resume" in page and "2 min left" in page        # 90 of 180 seconds heard
    # Part 3's own button starts part 3 from the beginning.
    part3 = client.get("/files/%d" % ids[SHOW.format(3)]).text
    button = re.search(rf'data-src="/audio/{ids[SHOW.format(3)]}"[^>]*>', part3)[0]
    assert "data-resume-file" not in button
    assert part3.count("data-resume-file") == 3     # the big one, and parts 1 and 2 in the list

    front = client.get("/").text
    assert "Continue listening" in front and "50% heard" in front


def test_a_place_in_the_first_seconds_doesnt_count(client):
    ids = files(client)
    show = episode_of(client, "Suspense 1943-05-13.wav")
    client.post(f"/listening/{show}", data={"file": ids["Suspense 1943-05-13.wav"], "at": "4"})
    assert "Continue listening" not in client.get("/").text
    assert "data-resume-file" not in client.get(f"/files/{ids['Suspense 1943-05-13.wav']}").text


def test_hearing_the_end_marks_it_played(client):
    ids = files(client)
    show = episode_of(client, "Suspense 1943-05-13.wav")
    client.post(f"/listening/{show}", data={"file": ids["Suspense 1943-05-13.wav"], "at": "60"})
    client.post(f"/listening/{show}", data={"file": ids["Suspense 1943-05-13.wav"], "at": "0", "finished": "1"})
    assert place(client, "Suspense 1943-05-13.wav") == (None, 0.0, True)
    assert "Continue listening" not in client.get("/").text
    assert "✓ Played" in client.get("/series/episodes").text
    # Listening again un-marks it.
    client.post(f"/listening/{show}", data={"file": ids["Suspense 1943-05-13.wav"], "at": "20"})
    assert place(client, "Suspense 1943-05-13.wav") == (ids["Suspense 1943-05-13.wav"], 20.0, False)


def test_marking_played_and_not_played(client):
    ids = files(client)
    a, b = episode_of(client, SHOW.format(1)), episode_of(client, "Suspense 1943-05-13.wav")
    client.post(f"/listening/{b}", data={"file": ids["Suspense 1943-05-13.wav"], "at": "60"})
    sent = client.post("/episodes/played", data={"ids": [a, b], "played": "1", "back": "/library"},
                       follow_redirects=False)
    assert sent.headers["location"] == "/library?marked=2&played=1"
    assert place(client, SHOW.format(1))[2] and place(client, "Suspense 1943-05-13.wav") == (None, 0.0, True)
    page = client.get(sent.headers["location"]).text
    assert "Marked 2 episodes as played." in page and page.count("✓ Played") == 2

    client.post("/episodes/played", data={"ids": [b], "played": "0", "back": "/library"})
    assert place(client, "Suspense 1943-05-13.wav") == (None, 0.0, False)
    assert "Mark played" in client.get(f"/files/{ids['Suspense 1943-05-13.wav']}").text


def test_a_forgotten_episode_takes_its_place_with_it(client):
    ids = files(client)
    show = episode_of(client, "Suspense 1943-05-13.wav")
    client.post(f"/listening/{show}", data={"file": ids["Suspense 1943-05-13.wav"], "at": "60"})
    import app.main as main
    (main.LIBRARY_DIR / "Suspense 1943-05-13.wav").unlink()
    client.post("/rescan")
    with Session(main._engine) as session:
        assert session.get(Listening, show) is None


def test_bad_reports_are_refused(client):
    assert client.post("/listening/99999", data={"file": "1", "at": "3"}).status_code == 404
    show = episode_of(client, "Suspense 1943-05-13.wav")
    assert client.post(f"/listening/{show}", data={"file": "x", "at": "3"}).status_code == 400
    # A file from another episode is ignored.
    client.post(f"/listening/{show}", data={"file": files(client)[SHOW.format(1)], "at": "30"})
    assert "Continue listening" not in client.get("/").text


def test_removing_a_card_from_continue_listening(client):
    ids, show = files(client), episode_of(client, SHOW.format(1))
    client.post(f"/listening/{show}", data={"file": ids[SHOW.format(2)], "at": "30"})
    page = client.get("/").text
    assert "Continue listening" in page and f'action="/listening/{show}/dismiss"' in page

    sent = client.post(f"/listening/{show}/dismiss", follow_redirects=False)
    assert sent.status_code == 303 and sent.headers["location"] == "/"
    assert "Continue listening" not in client.get("/").text
    # The place is kept: the episode still resumes.
    assert place(client, SHOW.format(1)) == (ids[SHOW.format(2)], 30.0, False)
    assert "▶ Resume" in client.get(f"/files/{ids[SHOW.format(1)]}").text
    # The same place reported again (a paused tab) doesn't bring it back...
    client.post(f"/listening/{show}", data={"file": ids[SHOW.format(2)], "at": "30.4"})
    assert "Continue listening" not in client.get("/").text
    # ...listening on does.
    client.post(f"/listening/{show}", data={"file": ids[SHOW.format(2)], "at": "45"})
    assert "Continue listening" in client.get("/").text
    assert client.post("/listening/99999/dismiss").status_code == 404


def test_only_the_four_most_recent_cards_show(tmp_path, monkeypatch):
    import app.main as main
    lib = tmp_path / "library"
    names = [f"Show {n}.wav" for n in range(1, 6)]
    for name in names:
        make_wav(lib / name, 60)
    monkeypatch.setattr(main, "LIBRARY_DIR", lib)
    monkeypatch.setattr(main, "_engine", make_engine(tmp_path / "data" / "airwave.db"))
    client = TestClient(main.app)
    client.get("/")
    ids = files(client)

    def cards() -> list[str]:
        row = client.get("/").text.split('class="continue-list"')[1].split("</ul>")[0]
        return re.findall(r'href="/files/\d+">(Show \d)', row)

    for name in names:                  # started one after another
        client.post(f"/listening/{episode_of(client, name)}", data={"file": ids[name], "at": "30"})
    assert cards() == ["Show 5", "Show 4", "Show 3", "Show 2"]
    # Listening to Show 1 again brings it back to the front; Show 2 drops off.
    client.post(f"/listening/{episode_of(client, names[0])}", data={"file": ids[names[0]], "at": "40"})
    assert cards() == ["Show 1", "Show 5", "Show 4", "Show 3"]
