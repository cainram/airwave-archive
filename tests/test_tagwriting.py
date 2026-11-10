"""Tests for writing details into the library's own files (Phase 10a).
Run with:  pytest"""

import mutagen
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import tagging, tagwriting
from app.db import AudioFile, Episode, make_engine
from app.scanner import read_audio
from app.tagging import read_airwave_tags
from test_export import make_mp3
from test_scanner import make_wav

SUSPENSE = "Suspense/Suspense 1943-05-13.mp3"


@pytest.fixture
def client(tmp_path, monkeypatch):
    import app.main as main
    lib = tmp_path / "library"
    make_mp3(lib / SUSPENSE, frames=400)          # about 10 seconds
    make_wav(lib / "Art Bell/Art Bell Hour 1.wav", 1)
    make_wav(lib / "Art Bell/Art Bell Hour 2.wav", 1)
    monkeypatch.setattr(main, "LIBRARY_DIR", lib)
    monkeypatch.setattr(main, "_engine", make_engine(tmp_path / "data" / "airwave.db"))
    client = TestClient(main.app)
    client.get("/")                       # the first visit scans
    client.lib = lib
    f = the_file("Suspense 1943-05-13.mp3")
    client.post(f"/files/{f.id}/edit", data=dict(
        series="Suspense", title="Donovan's Brain", air_year="1943", air_month="5", air_day="13",
        network="CBS", notes="Orson Welles stars."))
    return client


def engine():
    import app.main as main
    return main._engine


def the_file(name):
    with Session(engine()) as session:
        f = session.scalar(select(AudioFile).where(AudioFile.name == name))
        session.expunge_all()
        return f


def episode_of(name):
    with Session(engine()) as session:
        f = session.scalar(select(AudioFile).where(AudioFile.name == name))
        ep = f.episode
        session.expunge_all()
        return ep


def write(client, *names, confirm="yes"):
    ids = [str(episode_of(name).id) for name in names]
    return client.post("/tags/write", data={"ids": ids, "back": "/library", "confirm": confirm},
                       follow_redirects=False)


def test_the_preview_lists_every_change(client):
    ep = episode_of("Suspense 1943-05-13.mp3")
    page = client.get(f"/tags/write?ids={ep.id}&back=/files/1").text
    assert "This changes the files in your library." in page and "The sound isn't touched." in page
    assert "Suspense/Suspense 1943-05-13.mp3" in page
    assert "<td>Title</td><td><span class=\"muted\">(none)</span></td><td>Donovan&#39;s Brain</td>" in page
    assert "<td>Date</td>" in page and "<td>1943-05-13</td>" in page
    assert "<td>Comment</td>" in page and "Orson Welles stars." in page
    assert "Airwave tags: " in page and "<td>Station / network</td>" in page
    assert tagwriting.NEW_ID in page
    assert "Write the details into\n    1 file</button>" in page
    # Nothing has been written yet.
    assert read_audio(client.lib / SUSPENSE).tags.get("title") is None


def test_the_episode_page_offers_it(client):
    f = the_file("Suspense 1943-05-13.mp3")
    page = client.get(f"/files/{f.id}").text
    assert f'href="/tags/write?ids={f.episode_id}&amp;back=/files/{f.id}">Write details into the file…' in page


def test_nothing_is_written_without_confirming(client):
    before = (client.lib / SUSPENSE).read_bytes()
    assert write(client, "Suspense 1943-05-13.mp3", confirm="").status_code == 400
    assert (client.lib / SUSPENSE).read_bytes() == before


def test_writing_the_tags(client):
    sent = write(client, "Suspense 1943-05-13.mp3")
    assert sent.headers["location"] == "/library?tags_written=1"
    path = client.lib / SUSPENSE
    tags = read_audio(path).tags
    assert (tags["title"], tags["artist"], tags["album"], tags["date"], tags["comment"]) == (
        "Donovan's Brain", "Suspense", "Suspense", "1943-05-13", "Orson Welles stars.")
    airwave = read_airwave_tags(path)
    assert airwave["SERIES"] == "Suspense" and airwave["NETWORK"] == "CBS" and airwave["AIR_DATE"] == "1943-05-13"
    ep = episode_of("Suspense 1943-05-13.mp3")
    assert ep.uid and airwave["ID"] == ep.uid
    assert [p.name for p in path.parent.iterdir()] == ["Suspense 1943-05-13.mp3"]   # no hidden copy left
    # The app's record already matches the file, so a scan sees nothing changed.
    f = the_file("Suspense 1943-05-13.mp3")
    assert (f.size_bytes, f.mtime_ns) == (path.stat().st_size, path.stat().st_mtime_ns)
    assert f.tag_title == "Donovan's Brain"
    assert "Wrote the details into 1 file." in client.get("/library?tags_written=1").text
    # Afterwards: up to date, and a rescan changes nothing.
    client.post("/rescan")
    after = episode_of("Suspense 1943-05-13.mp3")
    assert (after.title, after.network, after.sources) == ("Donovan's Brain", "CBS", ep.sources)
    page = client.get(f"/tags/write?ids={ep.id}").text
    assert "Already up to date: nothing to write." in page and "There's nothing to write." in page


def test_the_owners_edits_still_win_after_writing(client):
    write(client, "Suspense 1943-05-13.mp3")
    f = the_file("Suspense 1943-05-13.mp3")
    client.post(f"/files/{f.id}/edit", data=dict(series="Suspense", title="Donovan's Brain (rebroadcast)",
                                                  air_year="1943", air_month="5", air_day="13"))
    client.post("/rescan")
    assert episode_of("Suspense 1943-05-13.mp3").title == "Donovan's Brain (rebroadcast)"
    page = client.get(f"/tags/write?ids={f.episode_id}").text
    assert "<td>Donovan&#39;s Brain</td><td>Donovan&#39;s Brain (rebroadcast)</td>" in page


def test_parts_get_their_numbers(client):
    first, second = the_file("Art Bell Hour 1.wav"), the_file("Art Bell Hour 2.wav")
    client.post(f"/files/{first.id}/parts", data={"part": [str(first.id), str(second.id)]})
    client.post(f"/files/{first.id}/edit", data=dict(series="Coast to Coast AM", title="Area 51 caller"))
    assert "Write details into the files…" in client.get(f"/files/{first.id}").text
    assert write(client, "Art Bell Hour 1.wav").headers["location"] == "/library?tags_written=2"
    one = read_audio(client.lib / "Art Bell/Art Bell Hour 1.wav").tags
    two = read_audio(client.lib / "Art Bell/Art Bell Hour 2.wav").tags
    assert (one["title"], one["track"]) == ("Area 51 caller (part 1 of 2)", "1/2")
    assert (two["title"], two["track"]) == ("Area 51 caller (part 2 of 2)", "2/2")
    assert read_airwave_tags(client.lib / "Art Bell/Art Bell Hour 2.wav")["PART"] == "2/2"


def test_a_failure_leaves_the_file_as_it_was(client, monkeypatch):
    path = client.lib / SUSPENSE
    before = path.read_bytes()

    def broken(path, episode, part=None, parts=1):
        path.write_bytes(b"half written")
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(tagwriting, "write_tags", broken)
    page = write(client, "Suspense 1943-05-13.mp3").text
    assert "Not every file could be written" in page and "No space left on device" in page
    assert "left exactly as it was" in page
    assert path.read_bytes() == before
    assert [p.name for p in path.parent.iterdir()] == ["Suspense 1943-05-13.mp3"]


def test_a_copy_that_doesnt_read_back_isnt_used(client, monkeypatch):
    path = client.lib / SUSPENSE
    before = path.read_bytes()

    def shortened(copy, episode, part=None, parts=1):
        copy.write_bytes(copy.read_bytes()[:417 * 3])     # much shorter: something went wrong
        return True
    monkeypatch.setattr(tagwriting, "write_tags", shortened)
    assert "didn&#39;t read back properly" in write(client, "Suspense 1943-05-13.mp3").text
    assert path.read_bytes() == before


def test_a_file_changed_meanwhile_isnt_replaced(client, monkeypatch):
    path = client.lib / SUSPENSE

    def meddling(copy, episode, part=None, parts=1):
        tagging.write_tags(copy, episode, part, parts)
        make_mp3(path, frames=401)                        # edited outside the app
        return True
    monkeypatch.setattr(tagwriting, "write_tags", meddling)
    assert "changed while its tags were being written" in write(client, "Suspense 1943-05-13.mp3").text
    assert read_audio(path).tags.get("title") is None


def test_a_missing_file_is_left_alone(client):
    (client.lib / SUSPENSE).unlink()
    ep = episode_of("Suspense 1943-05-13.mp3")
    page = client.get(f"/tags/write?ids={ep.id}").text
    assert "The file isn&#39;t in the library any more. It will be left alone." in page
    assert "There's nothing to write." in page


def test_an_episode_without_details_has_nothing_to_write(client):
    ep = episode_of("Art Bell Hour 1.wav")
    page = client.get(f"/tags/write?ids={ep.id}").text
    assert "This episode has no details yet, so there's nothing to write." in page
    assert "There's nothing to write." in page and "Write the details into" not in page


def test_leftovers_are_cleared_after_a_restart(tmp_path):
    lib = tmp_path / "library"
    make_wav(lib / "Show" / ".tagging-abcd1234.wav", 1)
    make_wav(lib / "Show" / "Show.wav", 1)
    tagwriting.clear_leftovers(lib)
    assert [p.name for p in (lib / "Show").iterdir()] == ["Show.wav"]


def test_other_formats(tmp_path):
    """FLAC keeps Vorbis comments: the same details go in."""
    from test_export import make_flac
    path = tmp_path / "show.flac"
    make_flac(path)
    ep = Episode(series="Suspense", title="The Hitch-Hiker", air_year=1942, uid="abc")
    f = AudioFile(folder="", name="show.flac", missing=False, part_number=None)
    ep.files = [f]
    plan = tagwriting.plan_file(f, ep, tmp_path)
    assert plan.problem is None and [c.label for c in plan.changes] == ["Title", "Artist", "Album", "Date"]
    tagwriting.write_file(f, ep, tmp_path)
    audio = mutagen.File(path)
    assert audio["TITLE"] == ["The Hitch-Hiker"] and audio["AIRWAVE_ID"] == ["abc"]
    assert tagwriting.plan_file(f, ep, tmp_path).up_to_date


# ------------------------------------------------------- many at once

import time


@pytest.fixture
def series(client, monkeypatch):
    """A series of 12 episodes: more than are written straight away."""
    monkeypatch.setattr(tagwriting, "_job", None)
    for day in range(1, 13):
        make_mp3(client.lib / "Escape" / f"Escape 1950-03-{day:02d}.mp3", frames=200)
    client.post("/rescan")
    for day in range(1, 13):
        f = the_file(f"Escape 1950-03-{day:02d}.mp3")
        client.post(f"/files/{f.id}/edit", data=dict(series="Escape", title=f"Story {day}",
                                                      air_year="1950", air_month="3", air_day=str(day)))
    return client


def finish():
    for _ in range(200):
        job = tagwriting.current_job()
        if job is not None and job.finished:
            return job
        time.sleep(0.05)
    raise AssertionError("writing didn't finish")


def written_titles(client):
    return [read_audio(client.lib / "Escape" / f"Escape 1950-03-{day:02d}.mp3").tags.get("title")
            for day in range(1, 13)]


def test_the_menus_offer_it(series):
    page = series.get("/series/episodes?name=Escape").text
    assert 'href="/tags/write?whole_series=Escape&back=/series/episodes%3Fname%3DEscape">Write details into files…' in page
    assert 'formaction="/tags/write">Write details into files…</button>' in page
    assert 'formaction="/tags/write">Write details into files…</button>' in series.get("/library").text


def test_a_big_preview_is_summed_up(series, monkeypatch):
    monkeypatch.setattr(tagwriting, "SHOWN", 5)
    page = series.get("/tags/write?whole_series=Escape&back=/library").text
    assert "Escape: 12 episodes, 12 files" in page
    assert "<strong>12 files</strong> will be written." in page
    assert "Tags that change:" in page and "Title (12), Artist (12), Album (12), Date (12), and Airwave tags." in page
    assert page.count('class="tag-plan"') == 5 and "…and 7 more files, with changes like the ones above." in page
    assert '<input type="hidden" name="whole_series" value="Escape">' in page and 'name="ids"' not in page


def test_a_whole_series_is_written_in_the_background(series):
    sent = series.post("/tags/write", data={"whole_series": "Escape", "back": "/library", "confirm": "yes"},
                       follow_redirects=False)
    assert sent.headers["location"] == "/tags/writing"
    job = finish()
    assert (job.files, job.checked, job.written, job.problems) == (12, 12, 12, [])
    assert written_titles(series) == [f"Story {day}" for day in range(1, 13)]
    page = series.get("/tags/writing").text
    assert "Finished." in page and "Wrote the details into 12 files." in page and "hx-trigger" not in page
    # Again: everything is up to date now.
    series.post("/tags/write", data={"whole_series": "Escape", "back": "/library", "confirm": "yes"})
    job = finish()
    assert (job.written, job.up_to_date) == (0, 12)
    assert "No files needed writing." in series.get("/tags/writing/status").text


def test_progress_and_stopping(series, monkeypatch):
    real = tagwriting.write_file

    def slow(f, episode, library):
        real(f, episode, library)
        if tagwriting.current_job().written == 2:
            page = " ".join(series.get("/tags/writing/status").text.split())
            assert "Writing…" in page and "Checked 3 of 12 files: wrote 2." in page
            assert 'hx-trigger="every 2s"' in page and "Stop writing" in page
            series.post("/tags/writing/stop")
    monkeypatch.setattr(tagwriting, "write_file", slow)
    series.post("/tags/write", data={"whole_series": "Escape", "back": "/library", "confirm": "yes"})
    job = finish()
    assert job.stopped and job.written == 3
    assert sum(1 for t in written_titles(series) if t) == 3            # the rest untouched
    page = series.get("/tags/writing").text
    assert "Stopped." in page and "Wrote the details into 3 files." in page and "The other 9 were left untouched" in page


def test_problems_are_listed_at_the_end(series):
    (series.lib / "Escape" / "Escape 1950-03-05.mp3").unlink()
    series.post("/tags/write", data={"whole_series": "Escape", "back": "/library", "confirm": "yes"})
    job = finish()
    assert job.written == 11
    page = series.get("/tags/writing").text
    assert "This file was left exactly as it was:" in page
    assert "Escape/Escape 1950-03-05.mp3</code>: The file isn&#39;t in the library any more." in page


def test_one_batch_at_a_time(series, monkeypatch):
    monkeypatch.setattr(tagwriting, "_job", tagwriting.Job(episode_ids=[1], files=5000, back="/library"))
    page = series.get("/tags/write?whole_series=Escape").text
    assert "Details are being written into other files right now." in page and "Write the details into" not in page
    sent = series.post("/tags/write", data={"whole_series": "Escape", "confirm": "yes"}, follow_redirects=False)
    assert sent.headers["location"] == "/tags/writing"
    assert written_titles(series) == [None] * 12


def test_a_few_ticked_are_written_straight_away(series):
    ids = [str(episode_of(f"Escape 1950-03-{day:02d}.mp3").id) for day in (1, 2, 3)]
    sent = series.post("/tags/write", data={"ids": ids, "back": "/series/episodes?name=Escape", "confirm": "yes"},
                       follow_redirects=False)
    assert sent.headers["location"] == "/series/episodes?name=Escape&tags_written=3"
    assert tagwriting.current_job() is None
    assert "Wrote the details into 3 files." in series.get(sent.headers["location"]).text


# ------------------------------------------------------------ the Advanced page

def test_the_advanced_page_is_only_reached_by_its_address(series):
    page = series.get("/advanced").text
    assert "These tools change your library files, all of them at once." in page
    assert 'href="/advanced/tags">Preview…' in page
    assert "15 files" in page                     # Suspense, Art Bell's 2 parts, 12 Escape
    for address in ["/", "/series", "/library", "/series/episodes?name=Escape", "/inbox", "/feeds"]:
        assert "/advanced" not in series.get(address).text


def test_writing_details_into_every_file(series):
    page = series.get("/advanced/tags").text
    assert "Your whole library:" in page and "15 files" in page
    assert "as an example of what will happen to the rest" in page
    assert 'action="/advanced/tags"' in page and "Write the details into every file</button>" in page
    assert series.post("/advanced/tags", data={}).status_code == 400
    sent = series.post("/advanced/tags", data={"confirm": "yes"}, follow_redirects=False)
    assert sent.headers["location"] == "/tags/writing"
    job = finish()
    # Escape (12) and Suspense (1) are written; Art Bell has no details yet.
    assert (job.files, job.checked, job.written, job.up_to_date, job.problems) == (15, 15, 13, 2, [])
    assert job.back == "/advanced"
    assert 'href="/tags/writing">Writing now' not in series.get("/advanced").text
