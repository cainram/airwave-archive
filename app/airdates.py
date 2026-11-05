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
from datetime import date, timedelta

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


# ---------------------------------------------------------------- weeks

@dataclass
class Week:
    start: date                                   # the Sunday
    days: list[tuple[date, list[AudioFile]]]      # 7 days, each with its first parts
    no_day: list[tuple[int, int, int]] = field(default_factory=list)   # (year, month, how many)

    @property
    def end(self) -> date:
        return self.start + timedelta(days=6)

    @property
    def count(self) -> int:
        return sum(len(files) for _day, files in self.days)

    @property
    def name(self) -> str:
        """'October 30th – November 5th, 1938' and similar."""
        from app.episodes import ordinal
        start, end = self.start, self.end
        first = f"{calendar.month_name[start.month]} {ordinal(start.day)}"
        last = (ordinal(end.day) if end.month == start.month
                else f"{calendar.month_name[end.month]} {ordinal(end.day)}")
        if start.year != end.year:
            return f"{first}, {start.year} – {last}, {end.year}"
        return f"{first} – {last}, {end.year}"


def week_start(day: date) -> date:
    """The Sunday on or before ``day``."""
    return day - timedelta(days=(day.weekday() + 1) % 7)


def _day(d: date):
    return (d.year, d.month, d.day)


_FULL_DATE = tuple_(Episode.air_year, Episode.air_month, Episode.air_day)


def week_view(session: Session, any_day: date) -> Week:
    """The week (Sunday to Saturday) holding ``any_day``: each day's
    episodes in time order (unknown times last)."""
    start = week_start(any_day)
    end = start + timedelta(days=6)
    files = session.scalars(
        select(AudioFile).join(Episode, AudioFile.episode_id == Episode.id)
        .where(Episode.air_day.is_not(None), _FULL_DATE >= _day(start), _FULL_DATE <= _day(end),
               FIRST_PARTS)
        .options(WITH_PARTS)
        .order_by(Episode.air_year, Episode.air_month, Episode.air_day,
                  Episode.air_time.is_(None), Episode.air_time,
                  Episode.series, Episode.title, AudioFile.name)
    ).unique().all()
    by_day: dict[date, list[AudioFile]] = {}
    for f in files:
        ep = f.episode
        by_day.setdefault(date(ep.air_year, ep.air_month, ep.air_day), []).append(f)
    week = Week(start, [(start + timedelta(days=i), by_day.get(start + timedelta(days=i), []))
                        for i in range(7)])
    for year, month in sorted({(start.year, start.month), (end.year, end.month)}):
        count = session.scalar(
            select(func.count()).select_from(Episode)
            .where(Episode.air_year == year, Episode.air_month == month,
                   Episode.air_day.is_(None))) or 0
        if count:
            week.no_day.append((year, month, count))
    return week


def latest_day(session: Session) -> date | None:
    """The most recent day anything in the collection aired on."""
    row = session.execute(
        select(Episode.air_year, Episode.air_month, Episode.air_day)
        .where(Episode.air_day.is_not(None))
        .order_by(Episode.air_year.desc(), Episode.air_month.desc(), Episode.air_day.desc())
        .limit(1)).first()
    return date(*row) if row else None


def nearest_week(session: Session, start: date, later: bool) -> date | None:
    """The Sunday of the closest week before (or after) the week starting
    ``start`` that has episodes on known days."""
    query = select(Episode.air_year, Episode.air_month, Episode.air_day).where(
        Episode.air_day.is_not(None))
    if later:
        query = query.where(_FULL_DATE > _day(start + timedelta(days=6))).order_by(
            Episode.air_year, Episode.air_month, Episode.air_day)
    else:
        query = query.where(_FULL_DATE < _day(start)).order_by(
            Episode.air_year.desc(), Episode.air_month.desc(), Episode.air_day.desc())
    row = session.execute(query.limit(1)).first()
    return week_start(date(*row)) if row else None
