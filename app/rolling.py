"""Rolling feeds: episodes come out a few at a time on a schedule (like a
station rerunning a series), instead of all at once.

- The schedule: one episode at a set time of day, in a chosen time zone,
  every day, every weekday (Monday to Friday), every Friday, Saturday and
  Sunday ("Weekend Warrior"), once a week on a chosen
  day, or every few days (counted from the day it was first published).
  The time stays the same local time when the clocks change.
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
- The first few episodes can be skipped (already heard somewhere else):
  they never come out in this feed (FeedSkip), even if everything is let
  out later. Changing how many only moves episodes not out yet.
- Podcast apps see only what's out. Dates are release times, never air
  dates (a ground rule: air dates are never invented).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import Episode, Feed, FeedRelease, FeedSkip, Listening, utcnow
from app.episodes import TIME_PATTERN, TIME_ZONES, ordinal

# 3 AM: podcast apps fetch new episodes overnight, so they're on the
# phone by morning (owner's choice, 2026-11-22). Also clear of the hour
# the clocks change (2 AM in the US).
DEFAULT_TIME = "03:00"
DEFAULT_ZONE = "America/Chicago"      # US Central (owner's choice, 2026-11-22)
DEFAULT_START = 1
MAX_START = 999
MAX_AT_ONCE = 999        # "Release more now"
MAX_SKIP = 99999
EVERY = ("day", "weekdays", "weekend", "week", "days")
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
DEFAULT_WEEKDAY = 5      # Saturday
DEFAULT_DAYS = 2
MAX_DAYS = 365


def read_form(form) -> tuple[dict, dict]:
    """The "How episodes come out" part of the feed form: the values (as
    typed) and any problems."""
    values = {"release": "rolling" if form.get("release") == "rolling" else "all",
              "roll_time": str(form.get("roll_time", "")).strip() or DEFAULT_TIME,
              "roll_zone": str(form.get("roll_zone", "")) or DEFAULT_ZONE,
              "roll_start": str(form.get("roll_start", "")).strip(),
              "roll_skip": str(form.get("roll_skip", "")).strip() or "0",
              "roll_every": str(form.get("roll_every", "")) or "day",
              "roll_weekday": str(form.get("roll_weekday", "")) or str(DEFAULT_WEEKDAY),
              "roll_days": str(form.get("roll_days", "")).strip() or str(DEFAULT_DAYS)}
    errors = {}
    if values["release"] == "rolling":
        if values["roll_every"] not in EVERY:
            errors["roll_every"] = "Pick how often from the list."
        if values["roll_every"] == "week" and values["roll_weekday"] not in "0123456":
            errors["roll_every"] = "Pick a day of the week."
        if values["roll_every"] == "days" and not (
                values["roll_days"].isdigit() and 2 <= int(values["roll_days"]) <= MAX_DAYS):
            errors["roll_days"] = f"A number of days from 2 to {MAX_DAYS}."
        if not TIME_PATTERN.match(values["roll_time"]):
            errors["roll_time"] = "Give a time of day, like 6:00 AM."
        if values["roll_zone"] not in TIME_ZONES:
            errors["roll_zone"] = "Pick a time zone from the list."
        if values["roll_start"] and not (values["roll_start"].isdigit()
                                         and int(values["roll_start"]) <= MAX_START):
            errors["roll_start"] = f"A number from 0 to {MAX_START}."
        if not (values["roll_skip"].isdigit() and int(values["roll_skip"]) <= MAX_SKIP):
            errors["roll_skip"] = f"A number from 0 to {MAX_SKIP}."
    return values, errors


def form_values(feed: Feed | None, zone: str = DEFAULT_ZONE) -> dict:
    """The form's starting values: the feed's, or the usual ones for a new
    feed: rolling (owner's choice, 2026-11-22), in your time zone (Settings)."""
    if feed is None or not feed.rolling:
        return {"release": "rolling" if feed is None else "all", "roll_time": DEFAULT_TIME, "roll_zone": zone,
                "roll_start": str(DEFAULT_START), "roll_skip": "0", "roll_every": "day",
                "roll_weekday": str(DEFAULT_WEEKDAY), "roll_days": str(DEFAULT_DAYS)}
    return {"release": "rolling", "roll_time": feed.roll_time or DEFAULT_TIME,
            "roll_zone": feed.roll_zone or zone,
            "roll_start": str(feed.roll_start if feed.roll_start is not None else DEFAULT_START),
            "roll_skip": str(feed.roll_skip or 0),
            "roll_every": feed.roll_every or "day",
            "roll_weekday": str(feed.roll_weekday if feed.roll_weekday is not None else DEFAULT_WEEKDAY),
            "roll_days": str(feed.roll_days or DEFAULT_DAYS)}


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
    feed.roll_every = values["roll_every"]
    feed.roll_weekday = int(values["roll_weekday"]) if feed.roll_every == "week" else None
    feed.roll_days = int(values["roll_days"]) if feed.roll_every == "days" else None
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


def _local_day(feed: Feed, moment: datetime) -> date:
    return moment.replace(tzinfo=timezone.utc).astimezone(_zone(feed)).date()


def _is_release_day(feed: Feed, day: date) -> bool:
    every = feed.roll_every or "day"
    if every == "weekdays":
        return day.weekday() < 5
    if every == "weekend":          # "Weekend Warrior" (owner's idea, 2026-11-22)
        return day.weekday() >= 4
    if every == "week":
        return day.weekday() == (feed.roll_weekday if feed.roll_weekday is not None
                                 else DEFAULT_WEEKDAY)
    if every == "days":
        first = _local_day(feed, feed.roll_since) if feed.roll_since else day
        return (day - first).days % (feed.roll_days or DEFAULT_DAYS) == 0
    return True


def release_times(feed: Feed, after: datetime, until: datetime) -> list[datetime]:
    """The release times (UTC) after `after`, up to and including `until`."""
    zone = _zone(feed)
    at = _time_of_day(feed)
    day = _local_day(feed, after)
    last = _local_day(feed, until)
    found = []
    while day <= last:
        if _is_release_day(feed, day):
            moment = _to_utc(datetime.combine(day, at, tzinfo=zone))
            if after < moment <= until:
                found.append(moment)
        day += timedelta(days=1)
    return found


def next_release(feed: Feed, now: datetime) -> datetime | None:
    """When the next episode comes out (UTC), if the clock is running."""
    if not feed.rolling or feed.roll_paused:
        return None
    ahead = (feed.roll_days or 0) + 8      # far enough for any schedule
    found = release_times(feed, now, now + timedelta(days=ahead))
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
    """'One episode every day at 6:00 AM (US Eastern)', or every weekday
    (Monday to Friday), every Saturday, every 3 days."""
    at = _time_of_day(feed).strftime("%I:%M %p").lstrip("0")
    every = feed.roll_every or "day"
    if every == "weekdays":
        often = "every weekday (Monday to Friday)"
    elif every == "weekend":
        often = "every Friday, Saturday and Sunday"
    elif every == "week":
        often = f"every {WEEKDAYS[feed.roll_weekday if feed.roll_weekday is not None else DEFAULT_WEEKDAY]}"
    elif every == "days":
        often = f"every {feed.roll_days or DEFAULT_DAYS} days"
    else:
        often = "every day"
    return f"One episode {often} at {at} ({TIME_ZONES.get(feed.roll_zone or '', TIME_ZONES[DEFAULT_ZONE])})"


# ------------------------------------------------------------ releasing

def released(session: Session, feed: Feed) -> dict[int, datetime]:
    """The feed's episodes that are out: episode id -> when."""
    return dict(session.execute(select(FeedRelease.episode_id, FeedRelease.released_at)
                                .where(FeedRelease.feed_id == feed.id)).all())


def skipped(session: Session, feed: Feed) -> set[int]:
    """The episodes this feed skips (they never come out in it)."""
    if feed.id is None:
        return set()
    return set(session.scalars(select(FeedSkip.episode_id).where(FeedSkip.feed_id == feed.id)))


def playable(episodes: list[Episode]) -> list[Episode]:
    """The episodes with a recording in the library: the ones counted when
    skipping, in the feed's order."""
    return [e for e in episodes if any(not f.missing for f in e.files)]


def waiting(session: Session, feed: Feed, episodes: list[Episode]) -> list[Episode]:
    """The feed's episodes still to come, in the order they'll come out
    (only those with a recording in the library, and not skipped)."""
    out = released(session, feed)
    skip = skipped(session, feed)
    return [e for e in playable(episodes) if e.id not in out and e.id not in skip]


def skip(session: Session, feed: Feed, how_many: int, episodes: list[Episode]) -> None:
    """Skip the feed's first `how_many` episodes (counting only those with
    a recording). Ones already out stay out; ones skipped before but not
    among the first `how_many` any more go back in the queue."""
    first = {e.id for e in playable(episodes)[:how_many]}
    out = released(session, feed)
    before = skipped(session, feed)
    for row in session.scalars(select(FeedSkip).where(FeedSkip.feed_id == feed.id)):
        if row.episode_id not in first:
            session.delete(row)
    for episode_id in first - before - set(out):
        session.add(FeedSkip(feed_id=feed.id, episode_id=episode_id))
    feed.roll_skip = how_many
    feed.updated_at = utcnow()


def skip_preview(session: Session, feed: Feed | None, episodes: list[Episode],
                 how_many: int) -> dict:
    """For the form, as the number is typed: which episode the feed would
    start with (or, once it's rolling, which comes out next), and how far
    the owner has listened here (the last one marked played)."""
    candidates = playable(episodes)
    out = released(session, feed) if feed is not None and feed.id is not None else {}
    first = {e.id for e in candidates[:how_many]}
    start = next(((number, e) for number, e in enumerate(candidates, 1)
                  if e.id not in out and e.id not in first), None)
    ids = [e.id for e in candidates]
    played = set(session.scalars(select(Listening.episode_id).where(
        Listening.played, Listening.episode_id.in_(ids)))) if ids else set()
    last_played = max((number for number, e in enumerate(candidates, 1) if e.id in played),
                      default=0)
    return {"total": len(candidates), "skip": how_many, "started": bool(out),
            "number": start[0] if start else None, "episode": start[1] if start else None,
            "played": last_played,
            "played_episode": candidates[last_played - 1] if last_played else None}


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
        feed.roll_since = now
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


def pause(session: Session, feed: Feed, now: datetime | None = None) -> None:
    """Stop letting episodes out on schedule (what was due comes out first)."""
    catch_up(session, feed, now)
    feed.roll_paused = True
    feed.updated_at = utcnow()


def resume(feed: Feed, now: datetime | None = None) -> None:
    """Carry on from now: the next episode at the next release time (the
    ones missed while paused don't all come out at once)."""
    feed.roll_paused = False
    if feed.roll_checked_at is not None:
        feed.roll_checked_at = now or utcnow()
    feed.updated_at = utcnow()


def all_at_once(session: Session, feed: Feed, now: datetime | None = None) -> int:
    """Let every waiting episode out now and make it an ordinary feed. What
    came out keeps its date (podcast apps keep their order); episodes
    added later appear straight away, as in any feed. Returns how many
    came out now."""
    from app.feeds import feed_episodes     # (here, to avoid a circular import)
    now = now or utcnow()
    catch_up(session, feed, now)
    episodes = feed_episodes(session, feed)
    count = _release(session, feed, episodes, [now] * len(waiting(session, feed, episodes)))
    feed.rolling = False
    feed.roll_paused = False
    feed.updated_at = utcnow()
    return count


def forget(session: Session, feed: Feed, episode_ids: set[int]) -> None:
    """Episodes taken out of a hand-picked feed: forget they were out (or
    skipped)."""
    for table in (FeedRelease, FeedSkip):
        for row in session.scalars(select(table).where(
                table.feed_id == feed.id, table.episode_id.in_(episode_ids))):
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
            "paused": feed.roll_paused, "skipped": len(skipped(session, feed)),
            "next_episode": queue[0] if queue else None,
            "next_when": when_words(feed, nxt, now) if nxt and queue else None,
            "schedule": schedule_words(feed),
            "started": feed.roll_checked_at is not None}
