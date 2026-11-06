"""Tests for deleting recordings from the library. Run with:  pytest"""

import html

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import AudioFile, Episode, Listening, make_engine
from app.deleting import file_path
from test_scanner import make_wav

SHOW = "Art Bell 1996-05-02 Hour {}.wav"


@pytest.fixture
def client(tmp_path, monkeypatch):
    import app.main as main
    lib = tmp_path / "library"
    for hour in (1, 2):
        make_wav(lib / "c2c" / SHOW.format(hour), 1)
    make_wav(lib / "otr" / "Suspense 1943-05-13.wav", 1)
    make_wav(lib / "otr" / "Suspense 1943-05-20.wav", 1)
    (tmp_path / "outside.wav").write_bytes(b"keep me")
    monkeypatch.setattr(main, "LIBRARY_DIR", lib)
    monkeypatch.setattr(main, "_engine", make_engine(tmp_path / "data" / "airwave.db"))
    client = TestClient(main.app)
    client.get("/")
    with Session(main._engine) as session:
        for f in session.scalars(select(AudioFile)):
            f.episode.series = "Suspense" if f.name.startswith("Suspense") else "Coast to Coast AM"
        session.commit()
    ids = files(client)
    client.post(f"/files/{ids[SHOW.format(1)]}/parts",
                data={"part": [str(ids[SHOW.format(h)]) for h in (1, 2)]})
    client.lib = lib
    return client


def files(client) -> dict[str, int]:
    import app.main as main
    with Session(main._engine) as session:
        return {f.name: f.id for f in session.scalars(select(AudioFile))}


def episode_of(name) -> int:
    import app.main as main
    with Session(main._engine) as session:
        return session.scalars(select(AudioFile.episode_id).where(AudioFile.name == name)).one()


def test_the_page_asks_first_and_deletes_nothing(client):
    ids, show = files(client), episode_of(SHOW.format(1))
    assert f'href="/delete?ids={show}&amp;back=/files/{ids[SHOW.format(1)]}"' in client.get(
        f"/files/{ids[SHOW.format(1)]}").text
    page = html.unescape(client.get(f"/delete?ids={show}&back=/files/{ids[SHOW.format(1)]}").text)
    assert "This deletes 2 recordings" in page and "Delete permanently" in page
    assert f"c2c/{SHOW.format(1)}" in page and f"c2c/{SHOW.format(2)}" in page
    assert (client.lib / "c2c" / SHOW.format(1)).is_file()            # nothing happened yet
    # Without the confirmation, nothing is deleted either.
    assert client.post("/delete", data={"ids": str(show)}).status_code == 400
    assert (client.lib / "c2c" / SHOW.format(1)).is_file()


def test_deleting_an_episode_with_its_parts(client):
    ids, show = files(client), episode_of(SHOW.format(1))
    client.post(f"/listening/{show}", data={"file": ids[SHOW.format(2)], "at": "30"})
    sent = client.post("/delete", data={"ids": str(show), "back": f"/files/{ids[SHOW.format(1)]}",
                                        "confirm": "yes"}, follow_redirects=False)
    assert sent.status_code == 303
    assert sent.headers["location"].startswith("/library?deleted=2&freed=")   # the series is gone
    assert not (client.lib / "c2c" / SHOW.format(1)).exists()
    assert not (client.lib / "c2c" / SHOW.format(2)).exists()
    assert (client.lib / "c2c").is_dir()                                  # folders stay
    assert (client.lib / "otr" / "Suspense 1943-05-13.wav").is_file()     # others untouched
    import app.main as main
    with Session(main._engine) as session:
        assert session.get(Episode, show) is None and session.get(Listening, show) is None
        assert len(session.scalars(select(AudioFile)).all()) == 2
    assert "Deleted 2 recordings" in client.get(sent.headers["location"]).text
    assert client.get("/search?q=Bell").status_code == 200


def test_deleting_ticked_episodes_returns_to_the_series(client):
    one = episode_of("Suspense 1943-05-13.wav")
    back = "/series/episodes?name=Suspense"
    assert "Delete ticked…" in client.get(back).text
    sent = client.post("/delete", data={"ids": str(one), "back": back, "confirm": "yes"},
                       follow_redirects=False)
    assert sent.headers["location"].startswith(back + "&deleted=1")
    assert not (client.lib / "otr" / "Suspense 1943-05-13.wav").exists()
    assert (client.lib / "otr" / "Suspense 1943-05-20.wav").is_file()


def test_a_file_already_gone_is_just_removed_from_the_app(client):
    one = episode_of("Suspense 1943-05-13.wav")
    (client.lib / "otr" / "Suspense 1943-05-13.wav").unlink()
    page = client.get(f"/delete?ids={one}").text
    assert "already gone from the disk" in page
    sent = client.post("/delete", data={"ids": str(one), "back": "/library", "confirm": "yes"},
                       follow_redirects=False)
    assert sent.headers["location"] == "/library?deleted=0&freed=0"
    assert "Removed from the app" in client.get(sent.headers["location"]).text


def test_nothing_outside_the_library_is_ever_touched(tmp_path):
    lib = tmp_path / "library"
    lib.mkdir()
    assert file_path(lib, AudioFile(folder="../", name="outside.wav")) is None
    assert file_path(lib, AudioFile(folder="", name="../outside.wav")) is None
    assert file_path(lib, AudioFile(folder="a/b", name="x.mp3")) == lib.resolve() / "a" / "b" / "x.mp3"
