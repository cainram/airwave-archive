"""Tests for exporting episodes as a zip of tagged copies. Run with:  pytest"""

import io
import json
import struct
import zipfile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import AudioFile, Episode, make_engine
from app.tagging import read_airwave_tags, write_tags
from test_scanner import make_wav


def make_mp3(path, frames=40):
    """A tiny silent MP3: MPEG-1 Layer III frames, 128 kbps, 44.1 kHz."""
    path.parent.mkdir(parents=True, exist_ok=True)
    frame = b"\xff\xfb\x90\x64" + b"\x00" * 413
    path.write_bytes(frame * frames)


def make_flac(path):
    """A FLAC file with only its required "streaminfo" header (no audio)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    info = struct.pack(">HH", 4096, 4096) + b"\x00" * 6
    # 44100 Hz, 1 channel, 16 bits, 0 samples (packed into 8 bytes), then an MD5.
    info += (44100 << 44 | 0 << 41 | 15 << 36).to_bytes(8, "big") + b"\x00" * 16
    path.write_bytes(b"fLaC" + bytes([0x80, 0, 0, 34]) + info)


def episode(**details):
    values = dict(uid="abc123", series="Suspense", title="Donovan's Brain", episode_number="42",
                  air_year=1942, air_month=10, air_day=8, air_time="21:00",
                  air_timezone="America/New_York", network="CBS", host=None,
                  cast="Orson Welles", notes="A scientist keeps a brain alive.")
    values.update(details)
    return Episode(**values)


@pytest.mark.parametrize("make", [make_wav, make_mp3, make_flac])
def test_writing_tags_in_each_format(tmp_path, make):
    import mutagen
    suffix = {make_wav: ".wav", make_mp3: ".mp3", make_flac: ".flac"}[make]
    path = tmp_path / ("show" + suffix)
    make(path, 1) if make is make_wav else make(path)
    assert write_tags(path, episode(), part=2, parts=3)
    found = read_airwave_tags(path)
    assert found["ID"] == "abc123" and found["SERIES"] == "Suspense" and found["PART"] == "2/3"
    assert found["AIR_DATE"] == "1942-10-08" and found["TIME_ZONE"] == "America/New_York"
    assert found["CAST"] == "Orson Welles" and "HOST" not in found
    tags = mutagen.File(path).tags
    text = str(tags)
    assert "Donovan's Brain (part 2 of 3)" in text and "1942-10-08" in text
    # Writing again replaces the Airwave tags (no doubles).
    write_tags(path, episode(cast="Agnes Moorehead"), part=2, parts=3)
    assert read_airwave_tags(path)["CAST"] == "Agnes Moorehead"
    assert str(mutagen.File(path).tags).count("Orson Welles") == 0


@pytest.mark.parametrize("make", [make_wav, make_mp3, make_flac])
def test_the_series_genres_go_in_the_genre_tag(tmp_path, make):
    from app.scanner import read_audio
    suffix = {make_wav: ".wav", make_mp3: ".mp3", make_flac: ".flac"}[make]
    path = tmp_path / ("show" + suffix)
    make(path, 1) if make is make_wav else make(path)
    write_tags(path, episode(), genres=["Drama", "Mystery"])
    assert read_audio(path).tags["genre"] == "Drama; Mystery"     # two values, shown together
    assert read_airwave_tags(path)["GENRES"] == "Drama; Mystery"
    write_tags(path, episode(), genres=["Thriller"])
    assert read_audio(path).tags["genre"] == "Thriller"
    # A series without genres leaves the file's own genre as it was.
    write_tags(path, episode())
    assert read_audio(path).tags["genre"] == "Thriller"
    assert "GENRES" not in read_airwave_tags(path)


def test_partial_dates_stay_partial(tmp_path):
    path = tmp_path / "show.mp3"
    make_mp3(path)
    write_tags(path, episode(air_month=None, air_day=None, air_time=None, air_timezone=None))
    found = read_airwave_tags(path)
    assert found["AIR_DATE"] == "1942" and "AIR_TIME" not in found


@pytest.fixture
def client(tmp_path, monkeypatch):
    import app.main as main
    lib = tmp_path / "library"
    make_mp3(lib / "otr" / "Suspense 1942-10-08.mp3")
    make_mp3(lib / "otr" / "Suspense 1942-10-15.mp3")
    make_wav(lib / "Art Bell 1996-05-02 Hour 1.wav", 1)
    make_wav(lib / "Art Bell 1996-05-02 Hour 2.wav", 1)
    monkeypatch.setattr(main, "LIBRARY_DIR", lib)
    monkeypatch.setattr(main, "_engine", make_engine(tmp_path / "data" / "airwave.db"))
    client = TestClient(main.app)
    client.get("/")
    with Session(main._engine) as session:
        for f in session.scalars(select(AudioFile)):
            if f.name.startswith("Suspense"):
                f.episode.series, f.episode.title = "Suspense", f.name[9:19]
                f.episode.air_year, f.episode.air_month, f.episode.air_day = 1942, 10, int(f.name[17:19])
            else:
                f.episode.series = "Coast to Coast AM"
        session.commit()
    ids = {f.name: f.id for f in session_files(main)}
    client.post(f"/files/{ids['Art Bell 1996-05-02 Hour 1.wav']}/parts",
                data={"part": [str(ids["Art Bell 1996-05-02 Hour 1.wav"]), str(ids["Art Bell 1996-05-02 Hour 2.wav"])]})
    client.lib = lib
    return client


def session_files(main):
    with Session(main._engine) as session:
        return list(session.scalars(select(AudioFile)))


def unzip(response) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(response.content))


def test_exporting_a_series(client):
    original = (client.lib / "otr" / "Suspense 1942-10-08.mp3").read_bytes()
    page = client.get("/export?whole_series=Suspense&back=/series/episodes?name=Suspense").text
    assert "2 episodes" in page and "Download zip" in page and 'hx-boost="false"' in page
    sent = client.get("/export/download?whole_series=Suspense")
    assert sent.status_code == 200 and sent.headers["content-type"] == "application/zip"
    assert "Airwave%20-%20Suspense%20-%20" in sent.headers["content-disposition"]
    zf = unzip(sent)
    names = zf.namelist()
    assert names[0] == "README.txt" and names[-1] == "manifest.json"
    assert "Suspense/Suspense 1942-10-08.mp3" in names
    assert all(i.compress_type == zipfile.ZIP_STORED for i in zf.infolist())
    found = json.loads(zf.read("manifest.json"))
    assert found["format"] == "airwave-archive-export" and len(found["episodes"]) == 2
    first = found["episodes"][0]
    assert first["series"] == "Suspense" and first["air_date"] == "1942-10-08" and len(first["id"]) == 32
    # The copy is tagged; the original isn't touched.
    copy = client.lib.parent / "copy.mp3"
    copy.write_bytes(zf.read("Suspense/Suspense 1942-10-08.mp3"))
    assert read_airwave_tags(copy)["ID"] == first["id"]
    assert (client.lib / "otr" / "Suspense 1942-10-08.mp3").read_bytes() == original
    # The same episode keeps the same ID next time.
    again = json.loads(unzip(client.get("/export/download?whole_series=Suspense")).read("manifest.json"))
    assert again["episodes"][0]["id"] == first["id"]
    assert not list((client.lib.parent / "data" / "export-temp").iterdir())   # nothing left behind


def test_exports_carry_the_series_genres(client):
    from app.scanner import read_audio
    client.post("/series/genres?name=Suspense", data={"new_genres": "Thriller, Mystery"})
    zf = unzip(client.get("/export/download?whole_series=Suspense"))
    found = json.loads(zf.read("manifest.json"))
    assert found["series"] == [{"name": "Suspense", "picture": None, "genres": ["Mystery", "Thriller"]}]
    copy = client.lib.parent / "copy.mp3"
    copy.write_bytes(zf.read("Suspense/Suspense 1942-10-08.mp3"))
    assert read_audio(copy).tags["genre"] == "Mystery; Thriller"
    assert "genre" not in read_audio(client.lib / "otr" / "Suspense 1942-10-08.mp3").tags


def test_exporting_ticked_episodes_with_parts(client):
    import app.main as main
    with Session(main._engine) as session:
        show = session.scalars(select(Episode).where(Episode.series == "Coast to Coast AM")).one()
    page = client.get(f"/export?ids={show.id}").text
    assert "1 episode," in page and "2 recordings" in page
    zf = unzip(client.get(f"/export/download?ids={show.id}"))
    entry = json.loads(zf.read("manifest.json"))["episodes"][0]
    assert [f["part"] for f in entry["files"]] == [1, 2]
    assert entry["files"][1]["path"] == "Coast to Coast AM/Art Bell 1996-05-02 Hour 2.wav"


def test_nothing_to_export(client):
    assert "no episodes were ticked" in client.get("/export").text
    assert client.get("/export/download").status_code == 404
