"""Tests for the Settings page. Run with:  pytest"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import onthisday, settings
from app.db import Feed, make_engine
from test_export import make_mp3

PHONE = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Mobile/15E148"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    import app.main as main
    make_mp3(tmp_path / "library" / "Suspense" / "a.mp3")
    monkeypatch.setattr(main, "LIBRARY_DIR", tmp_path / "library")
    monkeypatch.setattr(main, "_engine", make_engine(tmp_path / "data" / "airwave.db"))
    client = TestClient(main.app)
    client.get("/")
    with Session(main._engine) as session:
        for f in session.scalars(select(main.AudioFile)):
            f.episode.series = "Suspense"
        session.commit()
    client.engine = main._engine
    return client


def test_the_cog_and_the_page(client):
    home = client.get("/").text
    assert 'class="settings-cog" href="/settings"' in home
    assert home.index("theme-switch") < home.index("settings-cog") < home.index("search-box")
    page = client.get("/settings").text
    assert '<option value="America/Chicago" selected>' in page       # US Central to start
    assert '<p class="advanced-link"><a class="button danger" href="/advanced">Advanced Tools</a></p>' in page
    assert 'aria-current="page"' in page.split("settings-cog")[1].split("</a>")[0]
    assert 'href="/settings">← Settings</a>' in client.get("/advanced").text


def test_your_time_zone_starts_new_feeds(client):
    sent = client.post("/settings", data={"time_zone": "Europe/London"}, follow_redirects=False)
    assert sent.status_code == 303
    assert "Saved." in client.get("/settings?saved=1").text
    with Session(client.engine) as session:
        assert settings.time_zone(session) == "Europe/London"
    assert '<option value="Europe/London" selected>' in client.get("/feeds/new?series=Suspense").text
    with Session(client.engine) as session:
        assert onthisday.the_feed(session).roll_zone == "Europe/London"


def test_a_wrong_time_zone(client):
    sent = client.post("/settings", data={"time_zone": "Mars"})
    assert sent.status_code == 400 and "Pick a time zone from the list." in sent.text


def test_no_cog_on_a_phone(client):
    assert "settings-cog" not in client.get("/", headers=PHONE).text
