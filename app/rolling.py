"""Rolling feeds: episodes come out a few at a time on a schedule (like a
station rerunning a series), instead of all at once.

- The schedule: one episode at a set time of day, in a chosen time zone,
  every day. The time stays the same local time when the clocks change.
- When the feed is published, the first few (how many is chosen) come
  out straight away; the clock starts then.
- Nothing runs in the background. Whenever the feed (or its page) is
  opened, `catch_up` works out which release times have passed since it
  last looked, and lets out one waiting episode for each, dated with that
  release time. So if nobody looks for three days, three come out, each
  with its own day's time.
- Each release is written down (FeedRelease), so what's out never changes.
  Episodes come out in the feed's own order (oldest first, in air-date
  order); a new episode added to a series feed takes its air-date place
  among those still waiting. An episode in several parts comes out whole.
- Podcast apps see only what's out. Dates are release times, never air
  dates (a ground rule: air dates are never invented).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import Episode, Feed, FeedRelease, utcnow
from app.episodes import TIME_PATTERN, TIME_ZONES, ordinal

DEFAULT_TIME = "06:00"
DEFAULT_ZONE = "America/New_York"
DEFAULT_START = 1
MAX_START = 999
MAX_AT_ONCE = 999        # "Release more now"


@dataclass
class Schedule:
    every: str = "day"
    time: str = DEFAULT_TIME
    zone: str = DEFAULT_ZONE
    start: int = DEFAULT_START


def read_form(form) -> tuple[dict, dict]:
    """The "How episodes come out" part of the feed form: the values (as
    typed) and any problems."""
    values = {"release": "rolling" if form.get("release") == "rolling" else "all",
              "roll_time": str(form.get("roll_time", "")).strip() or DEFAULT_TIME,
              "roll_zone": str(form.get("roll_zone", "")) or DEFAULT_ZONE,
              "roll_start": str(form.get("roll_start", "")).strip()}
    errors = {}
    if values["release"] == "rolling":
        if not TIME_PATTERN.match(values["roll_time"]):
            errors["roll_time"] = "Give a time of day, like 6:00 AM."
        if values["roll_zone"] not in TIME_ZONES:
            errors["roll_zone"] = "Pick a time zone from the list."
        if values["roll_start"] and not (values["roll_start"].isdigit()
                                         and int(values["roll_start"]) <= MAX_START):
            errors["roll_start"] = f"A number from 0 to {MAX_START}."
    return values, errors


def form_values(feed: Feed | None, zone: str = DEFAULT_ZONE) -> dict:
    """The form's starting values: the feed's, or the usual ones for a new
    feed (with the time zone used last)."""
    if feed is None or not feed.rolling:
        return {"release": "all", "roll_time": DEFAULT_TIME, "roll_zone": zone,
                "roll_start": str(DEFAULT_START)}
    return {"release": "rolling", "roll_time": feed.roll_time or DEFAULT_TIME,
            "roll_zone": feed.roll_zone or zone,
            "roll_start": str(feed.roll_start if feed.roll_start is not None else DEFAULT_START)}


def last_zone(session: Session) -> str:
    """The time zone of the rolling feed made last (to suggest it again)."""
    return session.scalar(select(Feed.roll_zone).where(Feed.roll_zone.is_not(None))
                          .order_by(Feed.created_at.desc()).limit(1)) or DEFAULT_ZONE


def can_switch(feed: Feed | None) -> bool:
    """Whether a feed can still be made rolling (or not): only before it's
    first published, so episodes never vanish from podcast apps."""
    return feed is None or feed.published_at is None


def apply_form(feed: Feed, values: dict) -> None:
    """Save the form's choices on the feed."""
    if can_switch(feed):
        feed.rolling = values["release"] == "rolling"
    if not feed.rolling:
        return
    feed.roll_every = feed.roll_every or "day"
    feed.roll_time = values["roll_time"]
    feed.roll_zone = values["roll_zone"]
    if feed.roll_checked_at is None and values["roll_start"]:
        feed.roll_start = int(values["roll_start"])
    elif feed.roll_start is None:
        feed.roll_start = DEFAULT_START


# ---------------------------------------------------------------- the clock

def _zone(feed: Feed) -> ZoneInfo:
    return ZoneInfo(feed.roll_zone if feed.roll_zone in TIME_ZONES else DEFAULT_ZONE)


def _time_of_day(feed: Feed) -> time:
    hours, minutes = (feed.roll_time or DEFAULT_TIME).split(":")
    return time(int(hours), int(minutes))


def _to_utc(moment: datetime) -> datetime:
    """A local time (with its zone) as UTC without zone info, like the database keeps."""
    return moment.astimezone(timezone.utc).replace(tzinfo=None)


def release_times(feed: Feed, after: datetime, until: datetime) -> list[datetime]:
    """The release times (UTC) after `after`, up to and including `until`."""
    zone = _zone(feed)
    at = _time_of_day(feed)
    day = after.replace(tzinfo=timezone.utc).astimezone(zone).date()
    last = until.replace(tzinfo=timezone.utc).astimezone(zone).date()
    found = []
    while day <= last:
        moment = _to_utc(datetime.combine(day, at, tzinfo=zone))
        if after < moment <= until:
            found.append(moment)
        day += timedelta(days=1)
    return found


def next_release(feed: Feed, now: datetime) -> datetime | None:
    """When the next episode comes out (UTC), if the clock is running."""
    if not feed.rolling or feed.roll_paused:
        return None
    found = release_times(feed, now, now + timedelta(days=2))
    return found[0] if found else None


def when_words(feed: Feed, moment: datetime, now: datetime) -> str:
    """'today at 6:00 AM', 'tomorrow at 6:00 AM', or 'Tuesday, November
    24th at 6:00 AM', in the feed's time zone."""
    zone = _zone(feed)
    local = moment.replace(tzinfo=timezone.utc).astimezone(zone)
    today = now.replace(tzinfo=timezone.utc).astimezone(zone).date()
    clock = local.strftime("%I:%M %p").lstrip("0")
    if local.date() == today:
        day = "today"
    elif local.date() == today + timedelta(days=1):
        day = "tomorrow"
    else:
        day = f"{local.strftime('%A, %B')} {ordinal(local.day)}"
    return f"{day} at {clock}"


def day_words(feed: Feed, moment: datetime, now: datetime) -> str:
    """When an episode came out, as a day: 'today', 'yesterday', or
    'Sunday, November 22nd' (in the feed's time zone)."""
    zone = _zone(feed)
    day = moment.replace(tzinfo=timezone.utc).astimezone(zone).date()
    today = now.replace(tzinfo=timezone.utc).astimezone(zone).date()
    if day == today:
        return "today"
    if day == today - timedelta(days=1):
        return "yesterday"
    return f"{day.strftime('%A, %B')} {ordinal(day.day)}"


def schedule_words(feed: Feed) -> str:
    """'One episode every day at 6:00 AM (US Eastern)'."""
    at = _time_of_day(feed).strftime("%I:%M %p").lstrip("0")
    return f"One episode every day at {at} ({TIME_ZONES.get(feed.roll_zone or '', 'US Eastern')})"


# ------------------------------------------------------------ releasing

def released(session: Session, feed: Feed) -> dict[int, datetime]:
    """The feed's episodes that are out: episode id -> when."""
    return dict(session.execute(select(FeedRelease.episode_id, FeedRelease.released_at)
                                .where(FeedRelease.feed_id == feed.id)).all())


def waiting(session: Session, feed: Feed, episodes: list[Episode]) -> list[Episode]:
    """The feed's episodes still to come, in the order they'll come out
    (only those with a recording in the library)."""
    out = released(session, feed)
    return [e for e in episodes if e.id not in out and any(not f.missing for f in e.files)]


def _release(session: Session, feed: Feed, episodes: list[Episode],
             when: list[datetime]) -> int:
    """Let out the next waiting episodes, one at each of the times given.
    Each episode's parts get a second each after its time, so a later one
    never starts before an earlier one's parts."""
    from app.feeds import feed_episodes     # (here, to avoid a circular import)
    queue = waiting(session, feed, episodes or feed_episodes(session, feed))
    latest = session.scalar(select(FeedRelease.released_at)
                            .where(FeedRelease.feed_id == feed.id)
                            .order_by(FeedRelease.released_at.desc()).limit(1))
    count = 0
    for moment, episode in zip(when, queue):
        if latest is not None:
            moment = max(moment, latest + timedelta(seconds=1))
        session.add(FeedRelease(feed_id=feed.id, episode_id=episode.id, released_at=moment))
        latest = moment + timedelta(seconds=max(len(episode.files), 1) - 1)
        count += 1
    if count:
        feed.updated_at = utcnow()
    return count


def release_now(session: Session, feed: Feed, how_many: int,
                now: datetime | None = None, episodes: list[Episode] | None = None) -> int:
    """Let out the next few waiting episodes now ("Release more now")."""
    now = now or utcnow()
    return _release(session, feed, episodes or [], [now] * max(0, min(how_many, MAX_AT_ONCE)))


def start(session: Session, feed: Feed, now: datetime | None = None) -> None:
    """The feed has just been published. The first time, the first few
    come out and the clock starts; after being a draft again, the clock
    carries on from now (the releases missed meanwhile don't all come out)."""
    if not feed.rolling:
        return
    now = now or utcnow()
    if feed.roll_checked_at is None:
        release_now(session, feed, feed.roll_start or 0, now)
    feed.roll_checked_at = now


def catch_up(session: Session, feed: Feed, now: datetime | None = None) -> int:
    """Let out an episode for each release time that has passed since the
    feed was last looked at. Returns how many came out."""
    if not feed.rolling or not feed.published or feed.roll_paused or feed.roll_checked_at is None:
        return 0
    now = now or utcnow()
    due = release_times(feed, feed.roll_checked_at, now)
    count = _release(session, feed, [], due) if due else 0
    feed.roll_checked_at = now
    return count


def bring_up_to_date(session: Session, feed: Feed) -> None:
    """catch_up, saved. Two readers at once (the phone and this page) could
    both try to let out the same episode: the second simply finds it out."""
    if not feed.rolling:
        return
    try:
        catch_up(session, feed)
        session.commit()
    except IntegrityError:
        session.rollback()


def forget(session: Session, feed: Feed, episode_ids: set[int]) -> None:
    """Episodes taken out of a hand-picked feed: forget they were out."""
    for row in session.scalars(select(FeedRelease).where(
            FeedRelease.feed_id == feed.id, FeedRelease.episode_id.in_(episode_ids))):
        session.delete(row)


def status(session: Session, feed: Feed, episodes: list[Episode],
           now: datetime | None = None) -> dict:
    """For the feed's page: how many are out of how many, and what comes
    out next and when."""
    now = now or utcnow()
    queue = waiting(session, feed, episodes)
    out = len(released(session, feed))
    nxt = next_release(feed, now) if feed.roll_checked_at is not None else None
    return {"out": out, "total": out + len(queue), "waiting": len(queue),
            "next_episode": queue[0] if queue else None,
            "next_when": when_words(feed, nxt, now) if nxt and queue else None,
            "schedule": schedule_words(feed),
            "started": feed.roll_checked_at is not None}
