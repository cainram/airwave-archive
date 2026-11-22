"""The "On this day" feed (owner's idea; built 2026-11-22): a podcast feed
the app makes itself, always at the top of the Feeds page. Each day it
brings out the episodes of the ticked series that first aired on that
day, in any year: on December 21st, every December 21st broadcast.

- Only episodes with a full air date (year, month and day) are in it:
  the app never guesses a day.
- The day turns over at the feed's release time, in its time zone (3:00
  AM US Central to start with, like rolling feeds), so the day's
  anniversaries are on the phone in the morning.
- The feed holds the last 7 days, so a phone that missed a day or two
  still catches up. Older ones drop off the feed (a podcast app keeps what
  it downloaded).
- Each item is dated when its day came out (never with its air date, a
  ground rule) and titled with its year first: "1944 · Suspense: Donovan's
  Brain". The real air date is in the description.
- Broadcasts of February 29th come out on February 28th in other years.
- It starts with no series ticked. Ticking a series brings its
  anniversaries from that day on; unticking it takes them out of the feed.
- Nothing runs in the background: the feed is worked out whenever it's
  read, as rolling feeds are.
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app import rolling, settings
from app.db import AudioFile, Episode, Feed, FeedSeries, utcnow
from app.episodes import ordinal
from app.feeds import PART_SECONDS, Item, air_key, episode_title, new_token
from app.series import sort_key

KIND = "on_this_day"
TITLE = "On this day"
DESCRIPTION = "Broadcasts that first aired on this day in years past, from Airwave Archive."
DAYS = 7                  # the feed holds this many days
FULL_DATE = and_(Episode.air_year.is_not(None), Episode.air_month.is_not(None),
                 Episode.air_day.is_not(None))


# Its own picture (a tuning dial; the drawing is docs/artwork/on-this-day.svg),
# used until the owner uploads one. Kept with uploaded pictures: main sets
# where that folder is.
PICTURE = Path(__file__).parent / "static" / "on-this-day.jpg"
picture_folder: Callable[[], Path] | None = None
_saved: dict[Path, str | None] = {}


def picture() -> str | None:
    """The fingerprint of the feed's own picture (saved with the uploaded
    pictures the first time it's needed)."""
    if picture_folder is None:
        return None
    folder = picture_folder()
    if folder not in _saved:
        from app.artwork import save_picture
        try:
            _saved[folder] = save_picture(PICTURE.read_bytes(), folder)
        except OSError:
            return None
    return _saved[folder]


def the_feed(session: Session) -> Feed:
    """The "On this day" feed, made the first time it's needed (a draft,
    with no series ticked, newest first like a daily show)."""
    feed = session.scalar(select(Feed).where(Feed.kind == KIND).limit(1))
    if feed is None:
        now = utcnow()
        feed = Feed(kind=KIND, token=new_token(), title=TITLE, newest_first=True,
                    roll_time=rolling.DEFAULT_TIME, roll_zone=settings.time_zone(session),
                    created_at=now, updated_at=now)
        session.add(feed)
        session.flush()
    return feed


def is_it(feed: Feed | None) -> bool:
    return feed is not None and feed.kind == KIND


# ---------------------------------------------------------- the series

def chosen(session: Session, feed: Feed) -> dict[str, datetime]:
    """The ticked series, and when each was ticked."""
    return dict(session.execute(select(FeedSeries.series, FeedSeries.added_at)
                                .where(FeedSeries.feed_id == feed.id)).all())


def choose(session: Session, feed: Feed, names: list[str], now: datetime | None = None) -> None:
    """Tick these series (and untick the rest). Ones ticked already keep
    the day they were ticked."""
    now = now or utcnow()
    names = set(filter(None, names))
    for row in session.scalars(select(FeedSeries).where(FeedSeries.feed_id == feed.id)):
        if row.series in names:
            names.discard(row.series)
        else:
            session.delete(row)
    for name in names:
        session.add(FeedSeries(feed_id=feed.id, series=name, added_at=now))
    feed.updated_at = now


def follow_rename(session: Session, old: str, new: str) -> None:
    """A series was renamed: its tick follows it."""
    for row in session.scalars(select(FeedSeries).where(FeedSeries.series == old)):
        if session.get(FeedSeries, (row.feed_id, new)) is None:
            session.add(FeedSeries(feed_id=row.feed_id, series=new, added_at=row.added_at))
        session.delete(row)


def series_to_choose(session: Session) -> list[dict]:
    """Every series, with how many of its episodes have a full air date
    (only those can come out)."""
    rows = session.execute(
        select(Episode.series, func.count(Episode.id), func.count(Episode.id).filter(FULL_DATE))
        .where(Episode.series.is_not(None)).group_by(Episode.series)).all()
    return sorted(({"name": name, "episodes": total, "dated": dated} for name, total, dated in rows),
                  key=lambda s: sort_key(s["name"]))


# ------------------------------------------------------------- the days

def day_start(feed: Feed, day: date) -> datetime:
    """When a day's anniversaries come out (UTC)."""
    at = rolling._time_of_day(feed)
    return rolling._to_utc(datetime.combine(day, at, tzinfo=rolling._zone(feed)))


def today(feed: Feed, now: datetime) -> date:
    """The day whose anniversaries are out now: before the release time,
    still yesterday's."""
    day = rolling._local_day(feed, now)
    return day if day_start(feed, day) <= now else day - timedelta(days=1)


def next_turn(feed: Feed, now: datetime) -> datetime:
    return day_start(feed, today(feed, now) + timedelta(days=1))


def _dates_of(day: date) -> list[tuple[int, int]]:
    """The (month, day) pairs that come out on this day: February 28th
    also brings February 29th's, except in a leap year."""
    found = [(day.month, day.day)]
    if (day.month, day.day) == (2, 28) and not calendar.isleap(day.year):
        found.append((2, 29))
    return found


def _match(episode: Episode, day: date) -> bool:
    return (episode.air_month, episode.air_day) in _dates_of(day)


def items(session: Session, feed: Feed, now: datetime | None = None,
          days: int = DAYS) -> list[Item]:
    """The feed's recordings: the anniversaries of the last ``days`` days
    (each series' from the day it was ticked), each dated when its day
    came out. Within a day, the earliest broadcast comes first."""
    now = now or utcnow()
    ticked = chosen(session, feed)
    if not ticked:
        return []
    last = today(feed, now)
    window = [last - timedelta(days=back) for back in range(days)]
    pairs = {pair for day in window for pair in _dates_of(day)}
    episodes = session.scalars(
        select(Episode).where(Episode.series.in_(list(ticked)), FULL_DATE,
                              or_(*[and_(Episode.air_month == m, Episode.air_day == d)
                                    for m, d in pairs]))
        .options(selectinload(Episode.files))).all()
    found: list[Item] = []
    for day in window:
        start = day_start(feed, day)
        ends = day_start(feed, day + timedelta(days=1))
        todays = sorted((e for e in episodes if _match(e, day) and ticked[e.series] < ends),
                        key=air_key)
        for number, episode in enumerate(todays):
            parts = [f for f in episode.parts if not f.missing]
            if not parts:
                continue
            # Newest first: the later date is listed first, so the earliest
            # broadcast gets the latest time (and part 1 within it).
            at = start + timedelta(seconds=(len(todays) - number) * PART_SECONDS)
            title = item_title(episode, parts[0])
            for part, f in enumerate(parts, 1):
                name = title if len(parts) == 1 else f"{title} (part {part} of {len(parts)})"
                found.append(Item(f, episode, name, part if len(parts) > 1 else None, len(parts),
                                  at - timedelta(seconds=part - 1)))
    return sorted(found, key=lambda item: item.added, reverse=True)


def item_title(episode: Episode, f: AudioFile) -> str:
    """'1944 · Suspense: Donovan's Brain' (the series left out when the
    title starts with it already)."""
    title = episode_title(episode, f)
    series = episode.series or ""
    if series and not title.lower().startswith(series.lower()):
        title = f"{series}: {title}"
    return f"{episode.air_year} · {title}"


def in_feed(session: Session, feed: Feed, f: AudioFile) -> bool:
    """Whether a recording may be played from the feed: an episode of a
    ticked series with a full air date (a little more than what's listed,
    so one that just dropped off still plays)."""
    episode = session.get(Episode, f.episode_id) if f.episode_id else None
    return (episode is not None and episode.air_year is not None and episode.air_month is not None
            and episode.air_day is not None and episode.series in chosen(session, feed))


def _day_words(day: date) -> str:
    return f"{day.strftime('%A, %B')} {ordinal(day.day)}"


def status(session: Session, feed: Feed, now: datetime | None = None) -> dict:
    """For the Feeds page and the feed's page."""
    now = now or utcnow()
    ticked = chosen(session, feed)
    found = items(session, feed, now, days=1)
    return {"series": len(ticked), "today": len({item.episode.id for item in found}),
            "day": today(feed, now), "day_words": _day_words(today(feed, now)), "next_when": rolling.when_words(feed, next_turn(feed, now), now),
            "zone": rolling.TIME_ZONES.get(feed.roll_zone or "", rolling.TIME_ZONES[rolling.DEFAULT_ZONE])}
