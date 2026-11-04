"""Tests for playing recordings and the episode page. Run with:  pytest"""

import pytest
from fastapi.testclient import TestClient
from mutagen.id3 import COMM, TIT2

from app.db import make_engine
from test_scanner import add_tags, make_wav

HTMX = {"HX-Request": "true"}


@pytest.fixture
def lib(tmp_path):
    lib = tmp_path / "library"
    path = lib / "Campbell" / "OurTown.wav"
    make_wav(path, 2)
    add_tags(path, title=TIT2(encoding=3, text="Our Town"),
             comment=COMM(encoding=3, lang="eng", desc="", text="Cast: Orson Welles"))
    return lib


@pytest.fixture
def client(tmp_path, lib, monkeypatch):
    import app.main as main
    monkeypatch.setattr(main, "LIBRARY_DIR", lib)
    monkeypatch.setattr(main, "_engine", make_engine(tmp_path / "data" / "airwave.db"))
    client = TestClient(main.app)
    client.get("/")  # first visit scans the library
    return client


def test_audio_streams_whole_file(client, lib):
    response = client.get("/audio/1")
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    assert response.content == (lib / "Campbell" / "OurTown.wav").read_bytes()


def test_audio_can_jump_ahead(client, lib):
    # Browsers ask for a "range" of bytes when you skip ahead.
    response = client.get("/audio/1", headers={"Range": "bytes=100-199"})
    assert response.status_code == 206
    assert response.content == (lib / "Campbell" / "OurTown.wav").read_bytes()[100:200]


def test_audio_for_unknown_or_missing_file_is_404(client, lib):
    assert client.get("/audio/999").status_code == 404
    (lib / "Campbell" / "OurTown.wav").unlink()   # gone since the last scan
    assert client.get("/audio/1").status_code == 404


def test_file_page_shows_details_and_play_button(client):
    page = client.get("/files/1")
    assert page.status_code == 200
    assert "<h1>Our Town</h1>" in page.text
    assert "Cast: Orson Welles" in page.text
    assert 'data-src="/audio/1"' in page.text
    assert client.get("/files/999").status_code == 404


def test_library_rows_link_to_file_page_and_play(client):
    page = client.get("/")
    assert 'href="/files/1"' in page.text
    assert 'data-src="/audio/1"' in page.text


def test_htmx_gets_only_the_middle_of_the_page(client):
    full = client.get("/files/1")
    assert 'id="player"' in full.text and "<html" in full.text

    partial = client.get("/files/1", headers=HTMX)
    assert 'id="player"' not in partial.text and "<html" not in partial.text
    assert "<title>Our Town · Airwave Archive</title>" in partial.text
    assert "<h1>Our Town</h1>" in partial.text

    # The Back button may also fetch a page this way.
    restore = client.get("/", headers={"HX-History-Restore-Request": "true"})
    assert 'id="player"' not in restore.text


def test_rescan_from_htmx_reloads_just_the_middle(client):
    response = client.post("/rescan", headers=HTMX, follow_redirects=False)
    assert response.status_code == 200
    assert '"path": "/"' in response.headers["HX-Location"]


def test_tag_codes_get_plain_names(client):
    from app.main import tag_label
    assert tag_label("TIT2") == ("Title", "TIT2")
    assert tag_label("COMM:ID3v1 Comment:eng") == ("Comment", "ID3v1 Comment")
    assert tag_label("\xa9nam") == ("Title", "\xa9nam")
    assert tag_label("title") == ("Title", "title")
    assert tag_label("SOMETHING") == ("SOMETHING", "")
    page = client.get("/files/1")
    assert "Title" in page.text and "Comment" in page.text
