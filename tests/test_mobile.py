"""Tests for the mobile version (phones: for listening, not managing the
library). Run with:  pytest"""

import re

import pytest
from fastapi.testclient import TestClient

from app.db import make_engine
from test_scanner import make_wav

IPHONE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
          "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1")
ANDROID = ("Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 (KHTML, like Gecko) "
           "Chrome/130.0.0.0 Mobile Safari/537.36")
IPAD = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/18.0 Safari/605.1.15")   # what an iPad says


@pytest.fixture
def client(tmp_path, monkeypatch):
    import app.main as main
    lib, box = tmp_path / "library", tmp_path / "inbox"
    make_wav(lib / "Suspense" / "Suspense 1943-05-13.wav", 1)
    make_wav(box / "waiting.wav", 1)
    monkeypatch.setattr(main, "LIBRARY_DIR", lib)
    monkeypatch.setattr(main, "INBOX_DIR", box)
    monkeypatch.setattr(main, "_engine", make_engine(tmp_path / "data" / "airwave.db"))
    client = TestClient(main.app)
    client.get("/")
    client.get("/inbox")
    return client


def menu(page: str) -> list[str]:
    nav = page.split('id="menu"')[1].split("</nav>")[0]
    return re.findall(r'data-paths="[^"]*"[^>]*>([A-Za-z]+)', nav)


def footer_link(page: str) -> str:
    return re.search(r'class="view-switch-link"><a href="([^"]+)"[^>]*>([^<]+)<', page).groups()


@pytest.mark.parametrize("agent", [IPHONE, ANDROID])
def test_a_phone_gets_the_mobile_version(client, agent):
    page = client.get("/", headers={"User-Agent": agent}).text
    assert menu(page) == ["Home", "Series", "Feeds"]           # no Calendar, no Inbox
    assert 'class="mobile"' in page and "menu-count" not in page
    assert footer_link(page) == ("/view?to=desktop", "Desktop version")
    # Clicking around (just the middle of the page) keeps the mobile menu.
    partial = client.get("/series", headers={"User-Agent": agent, "HX-Request": "true"}).text
    assert menu(partial) == ["Home", "Series", "Feeds"]


@pytest.mark.parametrize("agent", [IPAD, "testclient"])
def test_an_ipad_or_a_computer_gets_the_desktop_version(client, agent):
    page = client.get("/", headers={"User-Agent": agent}).text
    assert menu(page) == ["Home", "Series", "Calendar", "Feeds", "Inbox"]
    assert 'class="mobile"' not in page and "menu-count" in page
    assert footer_link(page) == ("/view?to=mobile", "Mobile version")


def test_the_footer_switches_versions_and_remembers(client):
    sent = client.get("/view?to=desktop", follow_redirects=False,
                      headers={"User-Agent": IPHONE,
                               "Referer": "http://testserver/series/episodes?name=Suspense"})
    assert sent.status_code == 303 and sent.headers["location"] == "/series/episodes?name=Suspense"
    assert "airwave-view=desktop" in sent.headers["set-cookie"]
    page = client.get("/", headers={"User-Agent": IPHONE}).text   # (the cookie is kept)
    assert menu(page) == ["Home", "Series", "Calendar", "Feeds", "Inbox"]
    assert footer_link(page) == ("/view?to=mobile", "Mobile version")
    client.get("/view?to=mobile", headers={"User-Agent": IPHONE})
    assert menu(client.get("/", headers={"User-Agent": "testclient"}).text) == ["Home", "Series", "Feeds"]


def test_the_switch_only_goes_back_inside_the_app(client):
    sent = client.get("/view?to=mobile", follow_redirects=False,
                      headers={"Referer": "https://example.com//elsewhere"})
    assert sent.headers["location"] == "/"
    sent = client.get("/view?to=nonsense", follow_redirects=False)
    assert sent.headers["location"] == "/" and "set-cookie" not in sent.headers
