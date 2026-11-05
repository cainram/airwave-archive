"""The calendar: episodes by the date they first went on the air.

Only what's known is used. An episode whose day is known sits on that day
(the day it started, as broadcast, in its own time zone); one known only
to the month is listed as "sometime in" that month; one known only to the
year isn't on the month calendar at all (it's counted, so nothing seems
lost). Nothing is ever put on a made-up day.

Named "airdates" so it doesn't clash with Python's own "calendar".
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field

from sqlalchemy import func, select, tuple_
from sqlalchemy.orm import Session

from app.db import FIRST_PARTS, WITH_PARTS, AudioFile, Episode

# Weeks start on Sunday, as on American calendars.
WEEKS = calendar.Calendar(firstweekday=calendar.SUNDAY)
DAY_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]


@dataclass
class Month:
    year: int
    month: int
    weeks: list[list[int]]                  # day numbers, 0 = not in this month
    days: dict[int, list[AudioFile]] = field(default_factory=dict)   # day -> first parts
    sometime: list[AudioFile] = field(default_factory=list)          # day unknown
    year_only: int = 0                      # episodes of this year with no month

    @property
    def name(self) -> str:
        return f"{calendar.month_name[self.month]} {self.year}"

    @property
    def count(self) -> int:
        return sum(len(files) for files in self.days.values()) + len(self.sometime)


def month_view(session: Session, year: int, month: int) -> Month:
    """Everything that aired in one month, by day, in time order (unknown
    times last)."""
    view = Month(year, month, WEEKS.monthdayscalendar(year, month))
    files = session.scalars(
        select(AudioFile).join(Episode, AudioFile.episode_id == Episode.id)
        .where(Episode.air_year == year, Episode.air_month == month, FIRST_PARTS)
        .options(WITH_PARTS)
        .order_by(Episode.air_day, Episode.air_time.is_(None), Episode.air_time,
                  Episode.series, Episode.title, AudioFile.name)
    ).unique().all()
    for f in files:
        day = f.episode.air_day
        if day:
            view.days.setdefault(day, []).append(f)
        else:
            view.sometime.append(f)
    view.year_only = session.scalar(
        select(func.count()).select_from(Episode)
        .where(Episode.air_year == year, Episode.air_month.is_(None))) or 0
    return view


def _months_known():
    return (select(Episode.air_year, Episode.air_month)
            .where(Episode.air_year.is_not(None), Episode.air_month.is_not(None)))


def latest_month(session: Session) -> tuple[int, int] | None:
    """The most recent month anything in the collection aired in."""
    row = session.execute(_months_known().order_by(
        Episode.air_year.desc(), Episode.air_month.desc()).limit(1)).first()
    return tuple(row) if row else None


def nearest_month(session: Session, year: int, month: int, later: bool) -> tuple[int, int] | None:
    """The closest month before (or after) this one that has episodes."""
    here = tuple_(Episode.air_year, Episode.air_month)
    if later:
        query = _months_known().where(here > (year, month)).order_by(
            Episode.air_year, Episode.air_month)
    else:
        query = _months_known().where(here < (year, month)).order_by(
            Episode.air_year.desc(), Episode.air_month.desc())
    row = session.execute(query.limit(1)).first()
    return tuple(row) if row else None


def next_month(year: int, month: int, step: int) -> tuple[int, int]:
    """The month ``step`` months away (-1: the one before)."""
    index = year * 12 + (month - 1) + step
    return index // 12, index % 12 + 1


def years_with_episodes(session: Session) -> list[int]:
    return list(session.scalars(
        select(Episode.air_year).where(Episode.air_year.is_not(None))
        .group_by(Episode.air_year).order_by(Episode.air_year)))
