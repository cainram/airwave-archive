"""Series: the list of all series, the episodes of one series, and the
home page's rows of series (recently added, most played).

A series is simply the "Series" detail of its episodes: every episode with
the same series name belongs to the same series. (To merge two spellings,
change the series name on the episodes.)
"""

from __future__ import annotations

import re

from sqlalchemy import Integer, case, cast, desc, func, select
from sqlalchemy.orm import Session, object_session

from app import genres
from app.artwork import folder_art
from app.db import (
    FIRST_PARTS, WITH_PARTS, AudioFile, Episode, Feed, FolderArt, Listening, SeriesAbout, SeriesArt,
    SeriesGenre,
    natural_key, utcnow,
)
from app.listening import STARTED

# Words ignored at the start of a name when sorting, so "The Shadow" is
# listed under S.
LEADING_ARTICLE = re.compile(r"^(?:the|a|an)\s+", re.I)


def sort_key(name: str) -> list:
    return natural_key(LEADING_ARTICLE.sub("", name.strip()))


def sql_sort_key(column):
    """sort_key for the database: lower case, without "The", "A" or "An"
    at the start (numbers sort as text here)."""
    low = func.lower(func.trim(column))
    return case((low.like("the %"), func.trim(func.substr(low, 5))),
                (low.like("an %"), func.trim(func.substr(low, 4))),
                (low.like("a %"), func.trim(func.substr(low, 3))), else_=low)


def years_text(first: int | None, last: int | None) -> str:
    """'1939–1940', '1939', or '' when no year is known."""
    if first is None:
        return ""
    return str(first) if first == last else f"{first}–{last}"


def _in_series(name: str | None):
    return Episode.series.is_(None) if name is None else Episode.series == name


def _totals(session: Session, where=None) -> list[dict]:
    present = AudioFile.missing.is_(False)
    query = (
        select(Episode.series,
               func.count(func.distinct(Episode.id)),
               func.sum(AudioFile.duration_seconds).filter(present),
               func.sum(AudioFile.size_bytes).filter(present),
               func.min(Episode.air_year), func.max(Episode.air_year))
        .join(AudioFile, AudioFile.episode_id == Episode.id)
        .group_by(Episode.series)
    )
    query = query.add_columns(func.max(AudioFile.first_seen))
    if where is not None:
        query = query.where(where)
    return [
        {"name": name, "episodes": episodes, "seconds": seconds or 0, "bytes": size or 0,
         "years": years_text(first, last), "added": added}
        for name, episodes, seconds, size, first, last, added in session.execute(query)
    ]


def folder_pictures(session: Session) -> dict[str, str]:
    """Folder -> fingerprint of its picture (cover.jpg...)."""
    rows = session.execute(
        select(FolderArt.folder, FolderArt.art_hash).where(FolderArt.art_hash.is_not(None)))
    return {folder: art for folder, art in rows}


def uploaded_pictures(session: Session) -> dict[str, str]:
    """Series name -> fingerprint of the picture uploaded for it."""
    return {name: art for name, art in session.execute(
        select(SeriesArt.series, SeriesArt.art_hash))}


def _add_art(session: Session, found: list[dict], where=None) -> None:
    """Give each series a picture ("art") and say where it's from
    ("art_from": "upload", "folder" or "files"): an uploaded picture, else
    the folder picture of the folder most of its files are in (or one
    above it), else a picture from inside its files."""
    query = (select(Episode.series, AudioFile.folder, func.count(),
                    func.max(AudioFile.art_hash))
             .join(AudioFile, AudioFile.episode_id == Episode.id)
             .group_by(Episode.series, AudioFile.folder))
    if where is not None:
        query = query.where(where)
    folders: dict = {}   # series -> [(files, folder, picture in files)]
    for name, folder, count, inside in session.execute(query):
        folders.setdefault(name, []).append((count, folder, inside))
    pictures = folder_pictures(session)
    uploads = uploaded_pictures(session)
    for series in found:
        places = sorted(folders.get(series["name"], []), key=lambda p: (-p[0], p[1]))
        choices = [
            ("upload", uploads.get(series["name"])),
            ("folder", next((art for _c, folder, _i in places
                             if (art := folder_art(pictures, folder))), None)),
            ("files", next((inside for _c, _f, inside in places if inside), None)),
        ]
        series["art_from"], series["art"] = next(
            ((where, art) for where, art in choices if art), (None, None))


# The Series list's orders: sort -> whether it starts newest / Z first.
SERIES_SORTS = {"series": False, "added": True}


def all_series(session: Session, sort: str = "series", reverse: bool = False) -> list[dict]:
    """Every series with its totals (episodes, seconds, bytes, years,
    "added": when its newest recording was added) and picture, in name
    order, or (sort="added") by when added; reverse: Z to A, or newest
    first. Episodes without a series come last, under the name None."""
    found = _totals(session)
    _add_art(session, found)
    genres.add_genres(session, found)
    found.sort(key=lambda s: sort_key(s["name"] or ""), reverse=reverse and sort != "added")
    if sort == "added":   # (a stable sort: ties stay in name order)
        found.sort(key=lambda s: s["added"], reverse=reverse)
    return sorted(found, key=lambda s: s["name"] is None)


def one_series(session: Session, name: str | None) -> dict | None:
    """One series' totals and picture, or None if it has no episodes."""
    found = _totals(session, _in_series(name))
    _add_art(session, found, _in_series(name))
    genres.add_genres(session, found)
    return found[0] if found else None


def _named(session: Session, names: list[str]) -> list[dict]:
    """These series, in this order, with their totals and pictures."""
    if not names:
        return []
    where = Episode.series.in_(names)
    found = {s["name"]: s for s in _totals(session, where)}
    _add_art(session, list(found.values()), where)
    genres.add_genres(session, list(found.values()))
    return [found[name] for name in names if name in found]


def in_genre(session: Session, genre: str) -> list[dict]:
    """The series that have this genre, with their totals and pictures,
    in name order."""
    return _named(session, sorted(genres.series_with(session, genre), key=sort_key))


def recently_added(session: Session, limit: int = 20) -> list[dict]:
    """The home page's "Recently added": series by their newest recording
    (when the app first saw it), newest first. An old series with a new
    episode counts. No time limit, so it's never empty after a long break."""
    newest = func.max(AudioFile.first_seen)
    rows = session.execute(
        select(Episode.series, newest)
        .join(AudioFile, AudioFile.episode_id == Episode.id)
        .where(Episode.series.is_not(None), AudioFile.missing.is_(False))
        .group_by(Episode.series)
        .order_by(newest.desc(), Episode.series).limit(limit))
    return _named(session, [name for name, _ in rows])


def recently_added_by_genre(session: Session, rows: int = 3, limit: int = 20) -> list[tuple[str, list[dict]]]:
    """The home page's "Recently added in [genre]" rows: the genres whose
    series had a recording added most recently (up to `rows` of them),
    each with its series newest first, like "Recently added"."""
    newest = func.max(AudioFile.first_seen)
    found = session.execute(
        select(SeriesGenre.genre, Episode.series, newest)
        .join(Episode, Episode.series == SeriesGenre.series)
        .join(AudioFile, AudioFile.episode_id == Episode.id)
        .where(AudioFile.missing.is_(False))
        .group_by(SeriesGenre.genre, Episode.series)).all()
    by_genre: dict[str, list[tuple]] = {}
    for genre, series, when in found:
        by_genre.setdefault(genre, []).append((when, series))
    for series in by_genre.values():
        series.sort(key=lambda row: sort_key(row[1]))
        series.sort(key=lambda row: row[0], reverse=True)   # newest first, then A–Z
    chosen = sorted(by_genre, key=str.casefold)
    chosen.sort(key=lambda genre: by_genre[genre][0][0], reverse=True)
    return [(genre, _named(session, [name for _when, name in by_genre[genre][:limit]]))
            for genre in chosen[:rows]]


def most_played(session: Session, limit: int = 20) -> list[dict]:
    """The home page's "Most played": series by how many of their episodes
    have been listened to (played to the end, or started), most first.
    (The app doesn't count plays of the same episode.)"""
    heard = func.count(Listening.episode_id)
    rows = session.execute(
        select(Episode.series, heard)
        .join(Listening, Listening.episode_id == Episode.id)
        .where(Episode.series.is_not(None),
               Listening.played.is_(True) | (Listening.position >= STARTED))
        .group_by(Episode.series)
        .order_by(heard.desc(), func.max(Listening.updated_at).desc()).limit(limit))
    return _named(session, [name for name, _ in rows])


def episode_art(session: Session, f: AudioFile) -> str | None:
    """An episode's picture: the one uploaded for its series (so every
    episode of the series looks the same), else from inside its files
    (first part first), else its folder's."""
    parts = f.episode.parts if f.episode else [f]
    series = f.episode.series if f.episode else None
    uploaded = session.get(SeriesArt, series) if series else None
    return ((uploaded.art_hash if uploaded else None)
            or next((p.art_hash for p in parts if p.art_hash), None)
            or folder_art(folder_pictures(session), f.folder))


def set_uploaded_picture(session: Session, name: str, art_hash: str | None) -> None:
    """Use an uploaded picture for a series, or (None) stop using one."""
    row = session.get(SeriesArt, name)
    if art_hash is None:
        if row is not None:
            session.delete(row)
        return
    if row is None:
        row = SeriesArt(series=name)
        session.add(row)
    row.art_hash, row.uploaded_at = art_hash, utcnow()


# "About this series": a few sentences, plain text (line breaks kept).
MAX_ABOUT = 1000   # characters


def tidy_about(text: str) -> str:
    """Typed "About" text: spaces at the ends of lines and extra blank
    lines taken out, Windows line ends made plain."""
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def about_of(session: Session, name: str | None) -> str | None:
    """A series' "About" text, or None."""
    row = session.get(SeriesAbout, name) if name else None
    return row.text if row else None


def set_about(session: Session, name: str, text: str) -> None:
    """Save a series' "About" text (empty: take it off)."""
    text = tidy_about(text)[:MAX_ABOUT].strip()
    row = session.get(SeriesAbout, name)
    if not text:
        if row is not None:
            session.delete(row)
    elif row is None:
        session.add(SeriesAbout(series=name, text=text, updated_at=utcnow()))
    elif row.text != text:
        row.text, row.updated_at = text, utcnow()


def about_of_episode(episode: Episode) -> str | None:
    """The "About" text of an episode's series, for writing into its
    files' tags. The episode must still be in an open session."""
    session = object_session(episode)
    if session is None:
        raise ValueError("The episode isn't in an open session")
    return about_of(session, episode.series)


def use_imported_about(session: Session, name: str | None, text: str | None) -> None:
    """Give a series the "About" text that came with an imported file, but
    only if it has none of its own yet."""
    if not name or not text or not isinstance(text, str):
        return
    session.flush()
    if session.get(SeriesAbout, name) is None:
        set_about(session, name, text)


def follow_rename(session: Session, old: str | None, new: str | None) -> None:
    """After an episode's series name changed: if no episode is left with
    the old name, the series' feeds and genres follow it to the new name,
    and its uploaded picture and "About" text move too (unless the new
    name has its own)."""
    if not old or not new or old == new:
        return
    session.flush()
    if session.scalar(select(func.count(Episode.id)).where(Episode.series == old)):
        return   # other episodes still use the old name
    for feed in session.scalars(select(Feed).where(Feed.series == old)):
        feed.series = new
    genres.follow_rename(session, old, new)
    about = session.get(SeriesAbout, old)
    if about is not None:
        if session.get(SeriesAbout, new) is None:
            session.add(SeriesAbout(series=new, text=about.text, updated_at=about.updated_at))
        session.delete(about)
    picture = session.get(SeriesArt, old)
    if picture is None or session.get(SeriesArt, new) is not None:
        return
    set_uploaded_picture(session, new, picture.art_hash)
    session.delete(picture)


# Episodes in air-date order: known dates first, in order; within a year,
# a date known only to the month (or year) comes after the full dates.
# Then by episode number (as numbers where they are: "2" before "10"),
# then title.
AIR_ORDER = (Episode.air_year.is_(None), Episode.air_year,
             Episode.air_month.is_(None), Episode.air_month,
             Episode.air_day.is_(None), Episode.air_day,
             Episode.air_time.is_(None), Episode.air_time,
             Episode.episode_number.is_(None),
             cast(Episode.episode_number, Integer), Episode.episode_number,
             Episode.title)


# The Library page's orders: sort -> (columns, newest / Z first by default).
# The series order is the series' episodes in air-date order, series by
# series; files with no episode use their artist tag.
_LIBRARY_SERIES = func.coalesce(Episode.series, func.nullif(AudioFile.tag_artist, ""))
_LIBRARY_TITLE = func.coalesce(func.nullif(Episode.title, ""), func.nullif(AudioFile.tag_title, ""),
                               AudioFile.name)
LIBRARY_SORTS = {"series": False, "title": False, "added": True}


def library_order(sort: str, reverse: bool) -> list:
    """The order_by columns for the Library page (see LIBRARY_SORTS)."""
    turn = desc if reverse else (lambda column: column)
    if sort == "title":
        return [turn(sql_sort_key(_LIBRARY_TITLE)), AudioFile.name]
    if sort == "added":
        return [turn(AudioFile.first_seen), AudioFile.folder, AudioFile.name]
    return [_LIBRARY_SERIES.is_(None), turn(sql_sort_key(_LIBRARY_SERIES)), _LIBRARY_SERIES,
            *AIR_ORDER, AudioFile.folder, AudioFile.name]


def series_files(session: Session, name: str | None, page: int, page_size: int):
    """One series' episodes in air-date order, as their first files (with
    all parts loaded). Returns (files, how many in all)."""
    query = (select(AudioFile).join(Episode, AudioFile.episode_id == Episode.id)
             .where(_in_series(name), FIRST_PARTS))
    total = session.scalar(select(func.count()).select_from(query.subquery()))
    files = session.scalars(
        query.options(WITH_PARTS)
        .order_by(*AIR_ORDER, AudioFile.name)
        .offset((page - 1) * page_size).limit(page_size)
    ).unique().all()
    return files, total
