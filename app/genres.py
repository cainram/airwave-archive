"""Genres: the owner's own labels for a series ("Western", "Comedy").

A series can have several genres, or none (every series starts with
none). The list of genres is free: the owner makes one up by typing it,
and from then on it's offered as a tick box. A genre is "in use" while a
series that still has episodes has it; once no such series has it, it
drops off the list by itself.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import Episode, SeriesGenre

MAX_LENGTH = 40   # characters in one genre's name


def tidy(text: str) -> str:
    """A typed genre with extra spaces taken out, and cut to a sensible
    length."""
    return " ".join(text.split())[:MAX_LENGTH].strip()


def _order(genre: str) -> str:
    return genre.casefold()


def _with_episodes():
    """Series names that still have episodes (a genre left on a series
    whose episodes were all deleted doesn't count)."""
    return select(Episode.series).where(Episode.series.is_not(None)).distinct()


def genres_in_use(session: Session) -> list[str]:
    """Every genre some series has, in A–Z order (ignoring capitals)."""
    found = session.scalars(
        select(SeriesGenre.genre).where(SeriesGenre.series.in_(_with_episodes())).distinct())
    return sorted(found, key=_order)


def genres_of(session: Session, names: list[str] | None = None) -> dict[str, list[str]]:
    """Series name -> its genres (A–Z), for these series (or all). Series
    without genres aren't in the result."""
    query = select(SeriesGenre.series, SeriesGenre.genre)
    if names is not None:
        query = query.where(SeriesGenre.series.in_([n for n in names if n]))
    found: dict[str, list[str]] = {}
    for series, genre in session.execute(query):
        found.setdefault(series, []).append(genre)
    return {series: sorted(genres, key=_order) for series, genres in found.items()}


def add_genres(session: Session, series: list[dict]) -> None:
    """Give each series dict (from app.series) a "genres" list."""
    found = genres_of(session, [s["name"] for s in series if s["name"]])
    for s in series:
        s["genres"] = found.get(s["name"], []) if s["name"] else []


def parse_typed(text: str) -> list[str]:
    """New genres typed in the box: commas separate several."""
    return [g for g in (tidy(part) for part in text.split(",")) if g]


def set_genres(session: Session, name: str, wanted: list[str]) -> list[str]:
    """Give a series exactly these genres (taking off any others). A genre
    that matches one already in use apart from capitals takes that one's
    spelling, so "western" and "Western" don't become two genres. Returns
    the genres saved."""
    spelling = {g.casefold(): g for g in genres_in_use(session)}
    chosen: dict[str, str] = {}
    for genre in wanted:
        genre = tidy(genre)
        if genre:
            key = genre.casefold()
            chosen.setdefault(key, spelling.get(key, genre))
    for row in session.scalars(select(SeriesGenre).where(SeriesGenre.series == name)):
        if row.genre.casefold() not in chosen or row.genre != chosen[row.genre.casefold()]:
            session.delete(row)
    session.flush()
    have = set(session.scalars(select(SeriesGenre.genre).where(SeriesGenre.series == name)))
    for genre in chosen.values():
        if genre not in have:
            session.add(SeriesGenre(series=name, genre=genre))
    return sorted(chosen.values(), key=_order)


def follow_rename(session: Session, old: str, new: str) -> None:
    """A series' genres move to its new name. Joining two series keeps
    the genres of both."""
    moving = session.scalars(select(SeriesGenre).where(SeriesGenre.series == old)).all()
    if not moving:
        return
    have = {g.casefold() for g in session.scalars(
        select(SeriesGenre.genre).where(SeriesGenre.series == new))}
    for row in moving:
        session.delete(row)
    session.flush()
    for row in moving:
        if row.genre.casefold() not in have:
            session.add(SeriesGenre(series=new, genre=row.genre))
            have.add(row.genre.casefold())



def spelled(session: Session, typed: str) -> str | None:
    """The genre in use that matches what was typed, apart from capitals
    (None if no series has it)."""
    key = tidy(typed).casefold()
    return next((g for g in genres_in_use(session) if g.casefold() == key), None) if key else None


def series_with(session: Session, genre: str) -> list[str]:
    """The names of the series (still with episodes) that have this genre."""
    return list(session.scalars(select(SeriesGenre.series).where(
        SeriesGenre.genre == genre, SeriesGenre.series.in_(_with_episodes()))))


def with_counts(session: Session) -> list[tuple[str, int]]:
    """Every genre in use with how many series have it, A–Z."""
    rows = session.execute(
        select(SeriesGenre.genre, func.count()).where(SeriesGenre.series.in_(_with_episodes()))
        .group_by(SeriesGenre.genre)).all()
    return sorted(((g, n) for g, n in rows), key=lambda row: _order(row[0]))
