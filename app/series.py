"""Series: the list of all series, and the episodes of one series.

A series is simply the "Series" detail of its episodes: every episode with
the same series name belongs to the same series. (To merge two spellings,
change the series name on the episodes.)
"""

from __future__ import annotations

import re

from sqlalchemy import Integer, cast, func, select
from sqlalchemy.orm import Session

from app.db import FIRST_PARTS, WITH_PARTS, AudioFile, Episode, natural_key

# Words ignored at the start of a name when sorting, so "The Shadow" is
# listed under S.
LEADING_ARTICLE = re.compile(r"^(?:the|a|an)\s+", re.I)


def sort_key(name: str) -> list:
    return natural_key(LEADING_ARTICLE.sub("", name.strip()))


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
    if where is not None:
        query = query.where(where)
    return [
        {"name": name, "episodes": episodes, "seconds": seconds or 0, "bytes": size or 0,
         "years": years_text(first, last)}
        for name, episodes, seconds, size, first, last in session.execute(query)
    ]


def all_series(session: Session) -> list[dict]:
    """Every series with its totals (episodes, seconds, bytes, years), in
    name order. Episodes without a series come last, under the name None."""
    found = _totals(session)
    return sorted(found, key=lambda s: (s["name"] is None, sort_key(s["name"] or "")))


def one_series(session: Session, name: str | None) -> dict | None:
    """One series' totals, or None if it has no episodes."""
    found = _totals(session, _in_series(name))
    return found[0] if found else None


def series_files(session: Session, name: str | None, page: int, page_size: int):
    """One series' episodes in air-date order, as their first files (with
    all parts loaded). Returns (files, how many in all)."""
    query = (select(AudioFile).join(Episode, AudioFile.episode_id == Episode.id)
             .where(_in_series(name), FIRST_PARTS))
    total = session.scalar(select(func.count()).select_from(query.subquery()))
    files = session.scalars(
        query.options(WITH_PARTS)
        # Known dates first, in order; within a year, a date known only to
        # the month (or year) comes after the full dates. Then by title.
        .order_by(Episode.air_year.is_(None), Episode.air_year,
                  Episode.air_month.is_(None), Episode.air_month,
                  Episode.air_day.is_(None), Episode.air_day,
                  Episode.air_time.is_(None), Episode.air_time,
                  # Episode numbers as numbers where they are ("2" before "10").
                  Episode.episode_number.is_(None),
                  cast(Episode.episode_number, Integer), Episode.episode_number,
                  Episode.title, AudioFile.name)
        .offset((page - 1) * page_size).limit(page_size)
    ).unique().all()
    return files, total
