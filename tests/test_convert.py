"""Tests for converting recordings (Phase 13). Run with:  pytest

Most of these use made-up answers from ffprobe, so they run anywhere. The
last ones use the real ffmpeg, and are skipped where it isn't installed.
"""

import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import convert
from app.convert import Plan, mp3_rate, plan_for
from app.db import AudioFile, InboxItem, make_engine
from test_scanner import make_wav


def probed(*streams, duration="1800.0", bit_rate=None):
    return {"streams": list(streams),
            "format": {"duration": duration, **({"bit_rate": bit_rate} if bit_rate else {})}}


def sound(codec, channels=1, bit_rate="48000", **more):
    return {"codec_type": "audio", "codec_name": codec, "channels": channels,
            "bit_rate": bit_rate, "disposition": {"default": 1}, **more}


def test_opus_in_webm_becomes_mp3_with_a_little_headroom():
    plan = plan_for("Lone Ranger 1941-01-01.webm", probed(sound("opus", 1, "48000")))
    assert plan.ok and not plan.copy and plan.extension == ".mp3"
    assert (plan.kind, plan.codec, plan.kbps, plan.mono, plan.target_kbps) == ("WebM", "Opus", 48, True, 64)
    assert plan.description == ("WebM file. Its sound is Opus, 48 kbps, mono. "
                                "It will be converted to MP3 (mono, 64 kbps).")
    assert plan.duration == 1800.0


def test_aac_and_mp3_sound_is_copied_unchanged():
    plan = plan_for("show.mp4", probed({"codec_type": "video", "codec_name": "h264", "nb_frames": "5000"},
                                       sound("aac", 2, "128000")))
    assert plan.copy and plan.extension == ".m4a" and not plan.still_picture
    assert "copied into an .m4a file unchanged, with no loss of quality" in plan.description
    plan = plan_for("show.mkv", probed(sound("mp3", 1, "32000")))
    assert plan.copy and plan.extension == ".mp3"


def test_a_video_that_is_one_picture():
    plan = plan_for("show.mkv", probed({"codec_type": "video", "codec_name": "mjpeg",
                                        "disposition": {"attached_pic": 1}}, sound("vorbis")))
    assert plan.still_picture and "(a still picture with sound)" in plan.description


def test_a_cover_picture_in_webm_and_the_whole_files_bitrate():
    plan = plan_for("show.webm", probed(
        {"codec_type": "video", "codec_name": "vp9", "tags": {"DURATION": "00:00:00.000000000"}},
        sound("opus", 1, None), bit_rate="39992"))
    assert plan.still_picture and not plan.video and plan.kbps == 40 and plan.target_kbps == 56
    assert plan.description.startswith("WebM file (a still picture with sound). Its sound is Opus, 40 kbps")


def test_real_video_is_called_video():
    plan = plan_for("show.mkv", probed({"codec_type": "video", "codec_name": "h264", "nb_frames": "900"},
                                       sound("mp3", 1, "48000")))
    assert plan.video and plan.description.startswith("MKV video file. Its sound is MP3")
    assert plan_for("show.mkv", probed(sound("mp3"))).description.startswith("MKV file.")


def test_the_command_with_and_without_a_picture(tmp_path):
    plan = plan_for("show.webm", probed(sound("opus", 1, "48000")))
    plain = convert.command(plan, tmp_path / "in.webm", tmp_path / "out.mp3")
    assert "-vn" in plain and "attached_pic" not in plain
    with_cover = convert.command(plan, tmp_path / "in.webm", tmp_path / "out.mp3", tmp_path / "c.jpg")
    assert "-vn" not in with_cover and "attached_pic" in with_cover and str(tmp_path / "c.jpg") in with_cover
    assert "comment=Cover (front)" in with_cover


def test_what_the_inbox_says_about_pictures():
    still = plan_for("show.webm", probed({"codec_type": "video", "codec_name": "mjpeg", "index": 0,
                                          "disposition": {"attached_pic": 1}}, sound("opus")))
    assert still.picture_stream == 0 and still.description.endswith("Its picture becomes the episode's artwork.")
    video = plan_for("show.mp4", probed({"codec_type": "video", "codec_name": "h264", "index": 0,
                                         "nb_frames": "900"}, sound("aac")))
    assert "The video is left out (if it's one picture all the way through" in video.description


def test_the_sounds_own_length_counts():
    plan = plan_for("show.mkv", probed({"codec_type": "video", "codec_name": "h264", "nb_frames": "900",
                                        "duration": "22.0"},
                                       sound("opus", tags={"DURATION": "00:00:12.040000000"}), duration="22.0"))
    assert plan.duration == 12.04
    assert plan_for("show.mp4", probed(sound("aac", duration="12.5"), duration="13.0")).duration == 12.5
    assert plan_for("show.ogg", probed(sound("vorbis"), duration="9.0")).duration == 9.0


def test_files_that_cant_be_converted():
    assert not plan_for("x.webm", None).ok
    assert "can't be read" in plan_for("x.webm", None).description
    nothing = plan_for("x.mkv", probed({"codec_type": "video", "codec_name": "h264"}))
    assert not nothing.ok and nothing.description == "This MKV file has no sound in it."


def test_mp3_bitrates():
    assert mp3_rate(48, True) == 64          # 48 x 1.25 = 60 -> 64
    assert mp3_rate(20, True) == 32          # never below 32
    assert mp3_rate(64, False) == 80
    assert mp3_rate(192, False) == 128       # capped
    assert mp3_rate(None, True) == 64 and mp3_rate(None, False) == 128


def test_plans_keep_in_the_database():
    plan = plan_for("a.wma", probed(sound("wmav2", 2, "64000")))
    assert Plan.from_json(plan.to_json()) == plan and Plan.from_json(None) is None
    assert plan.codec == "WMA" and plan.kind == "WMA (Windows Media Audio)"


def test_the_upload_window_offers_every_kind():
    from app.inbox import is_audio, upload_accept
    accept = upload_accept().split(",")
    for wanted in (".mp3", ".webm", ".mkv", ".mp4", ".ra", ".wma", ".ogg", ".opus", ".zip",
                   "audio/*", "video/webm", "video/x-matroska", "audio/x-ms-wma"):
        assert wanted in accept
    assert is_audio("Lone Ranger.webm") and is_audio("show.RA") and not is_audio("notes.txt")


# ------------------------------------------------------------ the inbox

@pytest.fixture
def client(tmp_path, monkeypatch):
    import app.main as main
    lib, box = tmp_path / "library", tmp_path / "inbox"
    lib.mkdir()
    monkeypatch.setattr(main, "LIBRARY_DIR", lib)
    monkeypatch.setattr(main, "INBOX_DIR", box)
    monkeypatch.setattr(main, "_engine", make_engine(tmp_path / "data" / "airwave.db"))
    client = TestClient(main.app)
    client.get("/")
    client.box, client.lib = box, lib
    return client


def pretend_ffmpeg(monkeypatch, answer):
    monkeypatch.setattr(convert, "available", lambda: True)
    monkeypatch.setattr(convert, "probe", lambda path: answer)


def test_a_file_to_convert_waits_in_the_inbox(client, monkeypatch, tmp_path):
    pretend_ffmpeg(monkeypatch, probed(sound("opus", 1, "48000"), duration="1500.5"))
    make_wav(tmp_path / "made.wav", 1)
    sent = client.post("/inbox/upload", follow_redirects=False, files=[
        # (ffprobe is pretend here: the usual tag reader can't open the "WebM")
        ("files", ("Lone Ranger 1941-01-01.webm", b"\x1aE\xdf\xa3 pretend webm", "video/webm")),
        ("files", ("Suspense 1943-05-13.wav", (tmp_path / "made.wav").read_bytes(), "audio/wav")),
    ])
    assert sent.headers["location"] == "/inbox?uploaded=2"
    page = client.get("/inbox").text
    assert "Waiting to convert" in page and "It will be converted to MP3 (mono, 64 kbps)" in page
    assert "Converting 1 file." in page and "Add all" not in page               # just one to add
    assert page.count(">Add to library</button>") == 1                         # the WAV's only
    assert 'data-src="/inbox/' in page and page.count("data-play") == 1          # only the WAV plays now
    import app.main as main
    with Session(main._engine) as session:
        webm = session.scalar(select(InboxItem).where(InboxItem.name.like("%.webm")))
        assert webm.duration_seconds == 1500.5 and webm.series == "Lone Ranger"
        assert Plan.from_json(webm.convert_json).target_kbps == 64
    # Adding everything leaves it waiting.
    client.post("/inbox/add-all", data={})
    assert [p.name for p in client.box.iterdir()] == ["Lone Ranger 1941-01-01.webm"]
    refused = client.post(f"/inbox/{webm.id}/add", data={})
    assert "needs converting first" in refused.text


def test_without_ffmpeg_the_inbox_says_so(client, monkeypatch, tmp_path):
    monkeypatch.setattr(convert, "available", lambda: False)
    monkeypatch.setattr(convert, "probe", lambda path: None)
    make_wav(client.box / "Old Show.ra", 1)
    page = client.get("/inbox").text
    assert "Converting isn't available" in page and "ffmpeg isn't installed" in page
    assert "damaged" not in page


# ------------------------------------------------------- with real ffmpeg

needs_ffmpeg = pytest.mark.skipif(not convert.available(), reason="ffmpeg isn't installed here")


@needs_ffmpeg
def test_real_files_are_looked_inside(tmp_path):
    make_wav(tmp_path / "tone.wav", 2)
    ffmpeg = convert.ffmpeg_path()
    made = {
        "show.webm": ["-c:a", "libopus", "-b:a", "32k"],
        "show.mkv": ["-c:a", "libmp3lame", "-b:a", "32k"],
        "show.mp4": ["-c:a", "aac", "-b:a", "48k"],
        "show.ogg": ["-c:a", "libvorbis"],
    }
    for name, codec in made.items():
        subprocess.run([ffmpeg, "-v", "error", "-y", "-i", str(tmp_path / "tone.wav"),
                        "-metadata", "title=The Tone", *codec, str(tmp_path / name)], check=True)
    plans = {name: plan_for(name, convert.probe(tmp_path / name)) for name in made}
    assert all(plan.ok for plan in plans.values())
    assert (plans["show.webm"].codec, plans["show.webm"].extension, plans["show.webm"].copy) == ("Opus", ".mp3", False)
    assert (plans["show.mkv"].codec, plans["show.mkv"].copy, plans["show.mkv"].extension) == ("MP3", True, ".mp3")
    assert (plans["show.mp4"].codec, plans["show.mp4"].copy, plans["show.mp4"].extension) == ("AAC", True, ".m4a")
    assert plans["show.ogg"].codec == "Vorbis" and plans["show.webm"].mono
    assert 1.9 < plans["show.webm"].duration < 2.2
    assert convert.tags_from(convert.probe(tmp_path / "show.webm"))["title"] == "The Tone"
    (tmp_path / "junk.webm").write_bytes(b"not really a video")
    assert convert.probe(tmp_path / "junk.webm") is None
