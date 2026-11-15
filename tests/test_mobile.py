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


# ------------------------------------------------------ step 2: series and episodes

def phone(client, url):
    return client.get(url, headers={"User-Agent": IPHONE}).text


def computer(client, url):
    return client.get(url).text


CURATING = ["Edit details", "Write details into the file", "Rename the file", "Group with other files",
            "Delete…", "Tags in the file", "File name", "from file tags"]


def test_the_episode_page_is_for_listening(client):
    client.post("/files/1/edit", data={"series": "Suspense", "title": "Donovan's Brain",
                                       "air_year": "1943", "air_month": "5", "air_day": "13"})
    page = phone(client, "/files/1")
    for words in CURATING:
        assert words not in page, words
    assert "☰" not in page.split('id="content"')[1]
    assert '<button class="button" type="submit" name="played" value="1">Mark played</button>' in page
    assert "<h2>About this episode</h2>" in page and "<dt>Aired</dt>" in page
    assert "<dt>Host</dt>" not in page                  # nothing to say: no row
    desktop = computer(client, "/files/1")
    for words in CURATING[:6]:
        assert words in desktop, words


def test_parts_are_numbered_not_named(client):
    import app.main as main
    make_wav(main.LIBRARY_DIR / "Suspense" / "Suspense 1943-05-13 Hour 2.wav", 1)
    client.post("/rescan")
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    from app.db import AudioFile
    with Session(main._engine) as session:
        second = session.scalar(select(AudioFile.id).where(AudioFile.name.contains("Hour 2")))
    client.post("/files/1/parts", data={"part": ["1", str(second)]})
    page = phone(client, "/files/1")
    parts = page.split("<h2>Parts</h2>")[1].split("</ol>")[0]
    assert re.findall(r">(Part \d)<", parts) == ["Part 1", "Part 2"]
    assert ".wav" not in page.split("<h2>Parts</h2>")[1] and "Split into separate files" not in page
    assert "Split into separate files" in computer(client, "/files/1")


def test_a_missing_recording_just_isnt_available(client):
    import app.main as main
    client.post("/files/1/edit", data={"series": "Suspense", "title": "Donovan's Brain"})
    (main.LIBRARY_DIR / "Suspense" / "Suspense 1943-05-13.wav").unlink()
    client.post("/rescan")
    page = phone(client, "/files/1")
    assert "This recording isn't available right now." in page and "Forget this recording" not in page
    assert "Forget this recording" in computer(client, "/files/1")


def test_the_series_pages_are_for_listening(client):
    client.post("/files/1/edit", data={"series": "Suspense", "title": "Donovan's Brain"})
    page = phone(client, "/series")
    assert "Rescan" not in page and "All files (Library)" not in page and 'class="view-switch"' not in page
    assert '<ul class="tiles">' in phone(client, "/series?view=list")        # tiles only
    assert "Rescan" in computer(client, "/series") and 'class="view-switch"' in computer(client, "/series")
    page = phone(client, "/series/episodes?name=Suspense")
    assert "Edit genres…" not in page and "With ticked" not in page and 'type="checkbox"' not in page
    assert " KB" not in page and " MB" not in page                         # no size total
    assert "Donovan&#39;s Brain" in page and "data-play" in page or "play" in page
    desktop = computer(client, "/series/episodes?name=Suspense")
    assert "Edit genres…" in desktop and "With ticked" in desktop and 'type="checkbox"' in desktop


# ------------------------------------------------------ step 3: feeds, and managing pages

def make_feeds(client):
    client.post("/files/1/edit", data={"series": "Suspense", "title": "Donovan's Brain"})
    for title, publish in (("Out", True), ("Not yet", False)):
        sent = client.post("/feeds", data={"series": "Suspense", "title": title, "description": ""},
                           follow_redirects=False)
        feed_id = int(re.search(r"/feeds/(\d+)", sent.headers["location"])[1])
        if publish:
            client.post(f"/feeds/{feed_id}/publish", data={"published": "1"})
    return 1


def test_feeds_are_read_only(client):
    make_feeds(client)
    listing = phone(client, "/feeds")
    assert ">Out</a>" in listing and "Not yet" not in listing            # only published ones
    assert "Draft" not in listing and "Make a feed" not in listing
    assert "Not yet" in computer(client, "/feeds")
    page = phone(client, "/feeds/1")
    for words in ("Edit details", "Change picture", "New private link", "Unpublish", "Delete the feed",
                  "<svg", "Take ticked out", 'type="checkbox"'):
        assert words not in page, words
    assert "Open in Podcasts" in page and 'data-copy="#feed-address"' in page
    assert "Donovan&#39;s Brain" in page                                  # what's in it
    desktop = computer(client, "/feeds/1")
    assert "Edit details" in desktop and "<svg" in desktop
    assert "isn't published yet" in phone(client, "/feeds/2")


@pytest.mark.parametrize("url", ["/inbox", "/library", "/advanced", "/files/1/edit", "/import",
                                 "/review/filenames", "/export?ids=1", "/feeds/1/edit",
                                 "/series/genres?name=Suspense"])
def test_managing_pages_say_to_use_a_computer(client, url):
    make_feeds(client)
    page = client.get(url, headers={"User-Agent": IPHONE,
                                    "Referer": "http://testserver/series/episodes?name=Suspense"}).text
    assert "<h1>Use a computer for this</h1>" in page
    assert '<a class="button" href="/series/episodes?name=Suspense">← Back</a>' in page
    assert "Use a computer for this" not in computer(client, url)


def test_the_note_goes_home_without_somewhere_to_go_back_to(client):
    page = phone(client, "/inbox")
    assert '<a class="button" href="/">← Back</a>' in page



def test_the_file_and_tags_fold_away_on_the_desktop(client):
    page = computer(client, "/files/1")
    assert '<details class="fold">\n<summary><h2>File</h2></summary>' in page
    assert '<details class="fold">\n<summary><h2>Tags in the file</h2></summary>' in page
    assert '<details class="fold" open' not in page                 # closed to start with
