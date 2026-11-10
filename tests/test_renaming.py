"""Tests for renaming and organizing library files (Phase 10b).
Run with:  pytest"""

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import renaming
from app.db import AudioFile, Episode, Renamed, RenameBatch, make_engine
from test_export import make_mp3
from test_scanner import make_wav

BACK = "/series/episodes?name=Gunsmoke"


@pytest.fixture
def client(tmp_path, monkeypatch):
    import app.main as main
    lib = tmp_path / "library"
    make_mp3(lib / "RUSC.com/guns.1952.09.13_Home_Surgery.mp3")
    make_mp3(lib / "RUSC.com/guns.1952.04.26_Billy_the_Kid.mp3", frames=41)
    make_mp3(lib / "RUSC.com/1nit.1941.03.25_Love_and_Gazozza.mp3", frames=42)
    (lib / "RUSC.com/cover.jpg").write_bytes(b"not really a picture")
    monkeypatch.setattr(main, "LIBRARY_DIR", lib)
    monkeypatch.setattr(main, "_engine", make_engine(tmp_path / "data" / "airwave.db"))
    client = TestClient(main.app)
    client.get("/")                       # the first visit scans
    client.lib = lib
    edit(client, "guns.1952.09.13_Home_Surgery.mp3", series="Gunsmoke", title="Home Surgery",
         air_year="1952", air_month="9", air_day="13")
    edit(client, "guns.1952.04.26_Billy_the_Kid.mp3", series="Gunsmoke", title="Billy the Kid",
         air_year="1952", air_month="4", air_day="26")
    edit(client, "1nit.1941.03.25_Love_and_Gazozza.mp3", series="One Night Stand",
         title="Love and Gazozza", air_year="1941", air_month="3", air_day="25")
    return client


def engine():
    import app.main as main
    return main._engine


def edit(client, name, **details):
    f = the_file(name)
    client.post(f"/files/{f.id}/edit", data=details)


def the_file(name):
    with Session(engine()) as session:
        f = session.scalar(select(AudioFile).where(AudioFile.name == name))
        session.expunge_all()
        return f


def episode_ids(*names):
    return [str(the_file(name).episode_id) for name in names]


def text(response):
    """A page's text, with runs of spaces and line breaks made one space."""
    return re.sub(r"\s+", " ", response.text)


def organize(client, data, confirm="yes"):
    response = client.post("/organize", data={**data, "back": BACK, "confirm": confirm})
    response.page = text(response)
    return response


def files_on_disk(lib):
    return sorted(p.relative_to(lib).as_posix() for p in lib.rglob("*") if p.is_file())


# ------------------------------------------------------------- the names

def ep(**fields):
    return Episode(**fields)


def test_names_follow_the_owners_pattern():
    f = AudioFile(folder="x", name="old.MP3")
    e = ep(series="Gunsmoke", title="Home Surgery", air_year=1952, air_month=9, air_day=13)
    e.files = [f]
    assert renaming.name_for(f, e) == "Gunsmoke - 1952-09-13 - Home Surgery.mp3"
    assert renaming.folder_for(e) == "Gunsmoke"
    # Only what's known of the date (never invented), and no stray dashes.
    e.air_day = None
    assert renaming.name_for(f, e) == "Gunsmoke - 1952-09 - Home Surgery.mp3"
    e.air_month, e.title = None, None
    assert renaming.name_for(f, e) == "Gunsmoke - 1952.mp3"
    e.air_year, e.title = None, "Home Surgery"
    assert renaming.name_for(f, e) == "Gunsmoke - Home Surgery.mp3"
    # Neither a date nor a title: nothing to name it by.
    e.title = None
    assert renaming.name_for(f, e) is None
    # No series: no series in the name, and the "Unsorted" folder.
    e.series, e.title = None, "Home Surgery"
    assert renaming.name_for(f, e) == "Home Surgery.mp3"
    assert renaming.folder_for(e) == "Unsorted"


def test_names_are_safe_everywhere():
    f = AudioFile(folder="", name="a.mp3")
    e = ep(series="Suspense: The Classics", title='Who? "Me" / You*', air_year=1943)
    e.files = [f]
    assert renaming.name_for(f, e) == "Suspense - The Classics - 1943 - Who Me - You.mp3"
    assert renaming.folder_for(e) == "Suspense - The Classics"
    e.title = "A" * 400
    name = renaming.name_for(f, e)
    assert len(name) <= 180 and name.endswith(".mp3") and name.startswith("Suspense - The Classics - 1943 - AAA")
    assert renaming.folder_for(ep(series="...")) == "Unsorted"


def test_parts_get_part_numbers():
    one, two = AudioFile(folder="", name="h1.mp3", part_number=1), AudioFile(folder="", name="h2.mp3", part_number=2)
    e = ep(series="Coast To Coast AM", air_year=2009, air_month=3, air_day=14)
    e.files = [one, two]
    assert renaming.name_for(one, e) == "Coast To Coast AM - 2009-03-14 - Part 1.mp3"
    assert renaming.name_for(two, e) == "Coast To Coast AM - 2009-03-14 - Part 2.mp3"


# ------------------------------------------------------------- the preview

def test_the_preview_changes_nothing(client):
    page = text(client.get(f"/organize?whole_series=Gunsmoke&back={BACK}"))
    assert "This renames and moves files in your library." in page
    assert "Gunsmoke: 2 episodes, 2 files" in page
    assert "<code>RUSC.com/guns.1952.09.13_Home_Surgery.mp3</code>" in page
    assert "<code>Gunsmoke/Gunsmoke - 1952-09-13 - Home Surgery.mp3</code>" in page
    assert "will be renamed, into 1 folder" in page
    assert "Rename 2 files</button>" in page
    assert len(files_on_disk(client.lib)) == 4 and (client.lib / "RUSC.com/cover.jpg").exists()


def test_the_menus_offer_it(client):
    page = client.get(BACK).text
    assert 'href="/organize?whole_series=Gunsmoke&back=/series/episodes%3Fname%3DGunsmoke">Rename and organize files…' in page
    assert 'formaction="/organize">Rename and organize files…' in page
    assert "Rename the series…" in page
    f = the_file("guns.1952.09.13_Home_Surgery.mp3")
    page = client.get(f"/files/{f.id}").text
    assert f'href="/organize?ids={f.episode_id}&amp;back=/files/{f.id}">Rename the file from the details…' in page


def test_nothing_happens_without_confirming(client):
    assert organize(client, {"whole_series": "Gunsmoke"}, confirm="").status_code == 400
    assert len(files_on_disk(client.lib)) == 4


# ------------------------------------------------------------- renaming

def test_renaming_a_series_moves_its_files_and_keeps_track(client):
    before = the_file("guns.1952.09.13_Home_Surgery.mp3")
    page = organize(client, {"whole_series": "Gunsmoke"}).page
    assert "Renamed 2 files" in page and "Put them back</button>" in page
    assert files_on_disk(client.lib) == [
        "Gunsmoke/Gunsmoke - 1952-04-26 - Billy the Kid.mp3",
        "Gunsmoke/Gunsmoke - 1952-09-13 - Home Surgery.mp3",
        "RUSC.com/1nit.1941.03.25_Love_and_Gazozza.mp3",
        "RUSC.com/cover.jpg",       # One Night Stand is still there: the picture stays
    ]
    after = the_file("Gunsmoke - 1952-09-13 - Home Surgery.mp3")
    assert (after.id, after.episode_id, after.folder) == (before.id, before.episode_id, "Gunsmoke")
    # A scan sees nothing new: same files, same episodes, details kept.
    client.post("/rescan")
    with Session(engine()) as session:
        assert session.scalar(select(func.count()).select_from(AudioFile)) == 3
        assert session.scalar(select(func.count()).select_from(Episode)) == 3
        assert session.get(Episode, before.episode_id).title == "Home Surgery"
    assert client.get(f"/files/{after.id}").status_code == 200


def test_the_last_files_take_the_picture_and_the_folder_goes(client):
    organize(client, {"whole_series": "Gunsmoke"})
    page = organize(client, {"whole_series": "One Night Stand"}).page
    assert "moved 1 folder picture along with them" in page and "1 empty folder was removed" in page
    assert files_on_disk(client.lib) == [
        "Gunsmoke/Gunsmoke - 1952-04-26 - Billy the Kid.mp3",
        "Gunsmoke/Gunsmoke - 1952-09-13 - Home Surgery.mp3",
        "One Night Stand/One Night Stand - 1941-03-25 - Love and Gazozza.mp3",
        "One Night Stand/cover.jpg",
    ]
    assert not (client.lib / "RUSC.com").exists()


def test_putting_back(client):
    organize(client, {"whole_series": "Gunsmoke"})
    page = organize(client, {"whole_series": "One Night Stand"}).page
    with Session(engine()) as session:
        last = session.scalar(select(func.max(RenameBatch.id)))
    assert f'action="/organize/{last}/undo"' in page
    page = text(client.post(f"/organize/{last}/undo", data={"back": BACK}))
    assert "Put 1 file back where it was, and moved 1 folder picture along with them." in page
    assert "Put it back</button>" not in page
    page = text(client.post(f"/organize/{last - 1}/undo", data={"back": BACK}))
    assert "Put 2 files back where they were." in page
    assert files_on_disk(client.lib) == [
        "RUSC.com/1nit.1941.03.25_Love_and_Gazozza.mp3", "RUSC.com/cover.jpg",
        "RUSC.com/guns.1952.04.26_Billy_the_Kid.mp3", "RUSC.com/guns.1952.09.13_Home_Surgery.mp3",
    ]
    assert the_file("guns.1952.09.13_Home_Surgery.mp3").folder == "RUSC.com"
    with Session(engine()) as session:
        assert all(b.undone_at for b in session.scalars(select(RenameBatch)))


def test_a_file_moved_again_isnt_put_back(client):
    organize(client, {"whole_series": "Gunsmoke"})
    with Session(engine()) as session:
        first = session.scalar(select(func.max(RenameBatch.id)))
    edit(client, "Gunsmoke - 1952-09-13 - Home Surgery.mp3", series="Gunsmoke",
         title="Surgery at Home", air_year="1952", air_month="9", air_day="13")
    organize(client, {"whole_series": "Gunsmoke"})
    page = text(client.post(f"/organize/{first}/undo", data={"back": BACK}))
    assert "Put 1 file back" in page and "It has been moved or renamed again since." in page
    assert "Gunsmoke/Gunsmoke - 1952-09-13 - Surgery at Home.mp3" in files_on_disk(client.lib)


def test_a_taken_name_gets_a_number(client):
    make_mp3(client.lib / "Gunsmoke/Gunsmoke - 1952-09-13 - Home Surgery.mp3", frames=50)
    client.post("/rescan")
    # Two episodes that would get the same name, too.
    edit(client, "guns.1952.04.26_Billy_the_Kid.mp3", series="Gunsmoke", title="Home Surgery",
         air_year="1952", air_month="9", air_day="13")
    ids = episode_ids("guns.1952.09.13_Home_Surgery.mp3", "guns.1952.04.26_Billy_the_Kid.mp3")
    page = text(client.get(f"/organize?ids={ids[0]}&ids={ids[1]}"))
    assert "Gunsmoke - 1952-09-13 - Home Surgery (2).mp3" in page
    assert "Gunsmoke - 1952-09-13 - Home Surgery (3).mp3" in page
    assert "a number is added: that name is taken" in page
    organize(client, {"ids": ids})
    names = files_on_disk(client.lib)
    assert "Gunsmoke/Gunsmoke - 1952-09-13 - Home Surgery.mp3" in names       # untouched
    assert (client.lib / "Gunsmoke/Gunsmoke - 1952-09-13 - Home Surgery.mp3").stat().st_size == 50 * 417
    assert "Gunsmoke/Gunsmoke - 1952-09-13 - Home Surgery (2).mp3" in names
    assert "Gunsmoke/Gunsmoke - 1952-09-13 - Home Surgery (3).mp3" in names


def test_files_already_named_or_without_details_are_left_alone(client):
    organize(client, {"whole_series": "Gunsmoke"})
    page = text(client.get("/organize?whole_series=Gunsmoke"))
    assert "2 files already named this way." in page and "There's nothing to rename." in page
    make_wav(client.lib / "Misc/talk.wav", 1)
    client.post("/rescan")
    f = the_file("talk.wav")
    page = text(client.get(f"/organize?ids={f.episode_id}"))
    assert "neither an air date nor a title" in page and "There's nothing to rename." in page


def test_a_missing_file_is_left_alone(client):
    (client.lib / "RUSC.com/guns.1952.09.13_Home_Surgery.mp3").unlink()
    page = organize(client, {"whole_series": "Gunsmoke"}).page
    assert "Renamed 1 file" in page and "The file isn&#39;t in the library any more." in page


def test_a_restart_halfway_is_tidied_up(client):
    with Session(engine()) as session:
        f = session.scalar(select(AudioFile).where(AudioFile.name == "guns.1952.09.13_Home_Surgery.mp3"))
        g = session.scalar(select(AudioFile).where(AudioFile.name == "guns.1952.04.26_Billy_the_Kid.mp3"))
        batch = RenameBatch(label="Gunsmoke", made_at=f.first_seen)
        session.add(batch)
        # Written down; the first really moved before the restart, the second didn't.
        session.add(Renamed(batch=batch, file_id=f.id, old_folder="RUSC.com", old_name=f.name,
                            new_folder="Gunsmoke", new_name="Home Surgery.mp3"))
        session.add(Renamed(batch=batch, file_id=g.id, old_folder="RUSC.com", old_name=g.name,
                            new_folder="Gunsmoke", new_name="Billy.mp3"))
        session.commit()
        f_id = f.id
    (client.lib / "Gunsmoke").mkdir()
    (client.lib / "RUSC.com/guns.1952.09.13_Home_Surgery.mp3").rename(client.lib / "Gunsmoke/Home Surgery.mp3")
    import app.main as main
    main.finish_renaming()
    with Session(engine()) as session:
        f = session.get(AudioFile, f_id)
        assert (f.folder, f.name) == ("Gunsmoke", "Home Surgery.mp3")
        records = session.scalars(select(Renamed)).all()
        assert [(r.new_name, r.done) for r in records] == [("Home Surgery.mp3", True)]


# ------------------------------------------------------------ past renames

def test_past_renames_lists_batches_and_puts_an_older_one_back(client):
    assert "No files have been renamed yet." in text(client.get("/organize/history"))
    assert 'href="/organize/history">Past renames' in client.get("/library").text
    organize(client, {"whole_series": "Gunsmoke"})
    organize(client, {"whole_series": "One Night Stand"})
    page = text(client.get("/organize/history"))
    # Newest first, with the files in a fold-out.
    assert page.index("<h2>One Night Stand</h2>") < page.index("<h2>Gunsmoke</h2>")
    assert "Renamed 2 files," in page and "Renamed 1 file and 1 folder picture," in page
    assert "<code>RUSC.com/guns.1952.09.13_Home_Surgery.mp3</code>" in page
    assert "<code>Gunsmoke/Gunsmoke - 1952-09-13 - Home Surgery.mp3</code>" in page
    with Session(engine()) as session:
        first = session.scalar(select(func.min(RenameBatch.id)))
    assert page.count("Put them back</button>") == 1 and page.count("Put it back</button>") == 1
    # Put the older one back, from here.
    done = text(client.post(f"/organize/{first}/undo", data={"back": "/organize/history"}))
    assert "Put 2 files back where they were." in done and 'href="/organize/history">Done' in done
    assert "RUSC.com/guns.1952.09.13_Home_Surgery.mp3" in files_on_disk(client.lib)
    page = text(client.get("/organize/history"))
    assert "<strong>Put back " in page and "(put back)" in page
    assert page.count("back</button>") == 1          # only One Night Stand's is left


# ------------------------------------------------------------ the whole library

@pytest.fixture
def no_job(monkeypatch):
    monkeypatch.setattr(renaming, "_job", None)


def finish():
    import time
    for _ in range(500):
        job = renaming.current_job()
        if job is not None and job.finished:
            return job
        time.sleep(0.01)
    raise AssertionError("renaming didn't finish")


def test_the_advanced_page_offers_it(client, no_job):
    page = client.get("/advanced").text
    assert "Rename and organize every file" in page and 'href="/advanced/organize">Preview…' in page
    page = text(client.get("/advanced/organize"))
    assert "Your whole library: 3 episodes, 3 files" in page and "will be renamed, into 2 folders" in page
    assert 'action="/advanced/organize"' in page and "Rename 3 files in the whole library</button>" in page
    assert len(files_on_disk(client.lib)) == 4                     # nothing moved yet


def test_renaming_the_whole_library_in_the_background(client, no_job):
    assert client.post("/advanced/organize", data={}).status_code == 400
    sent = client.post("/advanced/organize", data={"confirm": "yes"}, follow_redirects=False)
    assert sent.headers["location"] == "/advanced/renaming"
    job = finish()
    assert (job.total, job.checked, job.moved, job.pictures, job.folders_removed, job.problems) == (
        3, 3, 3, 0, 0, [])
    assert files_on_disk(client.lib) == [
        "Gunsmoke/Gunsmoke - 1952-04-26 - Billy the Kid.mp3",
        "Gunsmoke/Gunsmoke - 1952-09-13 - Home Surgery.mp3",
        "One Night Stand/One Night Stand - 1941-03-25 - Love and Gazozza.mp3",
        "RUSC.com/cover.jpg",      # its recordings went to two series: which one's picture is it?
    ]
    page = text(client.get("/advanced/renaming"))
    assert "<strong>Finished.</strong> Renamed 3 files." in page
    assert "hx-trigger" not in page and "Put them back</button>" in page
    assert "<h2>The whole library</h2>" in text(client.get("/organize/history"))
    # Put back, from the result.
    text(client.post(f"/organize/{job.batch_id}/undo", data={"back": "/organize/history"}))
    assert len([n for n in files_on_disk(client.lib) if n.startswith("RUSC.com/")]) == 4
    assert not (client.lib / "Gunsmoke").exists()


def test_stopping_partway(client, no_job, monkeypatch):
    real = renaming._move

    def slow(session, batch, library, *args):
        real(session, batch, library, *args)
        if renaming.current_job().moved == 0 and args[-1] is not None:
            page = text(client.get("/advanced/renaming/status"))
            assert "Renaming…" in page and 'hx-trigger="every 2s"' in page and "Stop renaming" in page
            client.post("/advanced/renaming/stop")
    monkeypatch.setattr(renaming, "_move", slow)
    client.post("/advanced/organize", data={"confirm": "yes"})
    job = finish()
    assert job.stop and (job.checked, job.moved) == (1, 1)
    page = text(client.get("/advanced/renaming/status"))
    assert "<strong>Stopped.</strong> Renamed 1 file." in page and "The other 2 were left as they were" in page
    # One at a time: nothing else renames or writes while a run is going.
    job.finished = False
    assert client.post("/organize", data={"whole_series": "Gunsmoke", "back": BACK, "confirm": "yes"},
                       follow_redirects=False).headers["location"] == "/advanced/renaming"
    assert "Files are being written or renamed right now." in text(client.get("/organize?whole_series=Gunsmoke"))
    job.finished = True
