"""Changing many episodes at once ("bulk editing"), and renaming a series.

The bulk edit form only offers details that are usually the same across
many episodes: series, station/network, host, cast, notes, and a start
time with its time zone. For each one the owner can leave it as it is
(the default), set it to a new value, or empty it. Whatever is changed
counts as a manual edit, like any other.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import MANUAL, Episode, utcnow
from app.episodes import TEXT_FIELDS, TIME_PATTERN, TIME_ZONES, _clean

BULK_FIELDS = {name: TEXT_FIELDS[name] for name in ("series", "network", "host", "cast", "notes")}
KEEP, SET, CLEAR = "keep", "set", "clear"


def read_bulk_form(form) -> tuple[dict, dict, dict[str, str]]:
    """Read the bulk edit form. Returns (changes, time, errors):

    - ``changes``: field -> new text, or None to empty it (fields to leave
      alone aren't included);
    - ``time``: {} to leave times alone, {"time": None} to remove them, or
      {"time": "22:00", "zone": "America/Los_Angeles"};
    - ``errors``: field -> what's wrong, in plain words.
    """
    changes: dict = {}
    errors: dict[str, str] = {}
    for name in BULK_FIELDS:
        action = form.get(f"do-{name}", KEEP)
        value = _clean(form.get(name))
        if action == CLEAR:
            changes[name] = None
        elif action == SET:
            if value is None:
                errors[name] = "Type the new value, or choose “leave as it is”."
            else:
                changes[name] = value

    time: dict = {}
    action = form.get("do-time", KEEP)
    if action == CLEAR:
        time = {"time": None}
    elif action == SET:
        text, zone = _clean(form.get("air_time")), _clean(form.get("air_timezone"))
        match = TIME_PATTERN.match(text or "")
        if not match:
            errors["time"] = "The time should look like 22:00 (24-hour clock)."
        elif zone and zone not in TIME_ZONES:
            errors["time"] = "Please pick a time zone from the list."
        else:
            time = {"time": f"{int(match[1]):02d}:{match[2]}", "zone": zone}
    return changes, time, errors


def apply_bulk(episode: Episode, changes: dict, time: dict) -> tuple[bool, bool]:
    """Apply bulk changes to one episode. Returns (changed, time_skipped):
    a time is only given to episodes whose full air date is known (a time
    without a day would be meaningless)."""
    sources = episode.sources
    changed = skipped = False
    for name, value in changes.items():
        if getattr(episode, name) != value or (value is not None and sources.get(name) != MANUAL):
            setattr(episode, name, value)
            changed = True
        if value is None:
            sources.pop(name, None)
        else:
            sources[name] = MANUAL
    if time:
        if time["time"] is not None and not episode.air_day:
            skipped = True
        else:
            new_time = time["time"]
            new_zone = time.get("zone") if new_time else None
            if (episode.air_time, episode.air_timezone) != (new_time, new_zone):
                episode.air_time, episode.air_timezone = new_time, new_zone
                changed = True
            if episode.air_year is not None:
                sources["air_date"] = MANUAL
    if changed or sources != episode.sources:
        episode.sources = sources
        episode.updated_at = utcnow()
    return changed, skipped


def rename_series(session: Session, old: str, new: str) -> int:
    """Give every episode of series ``old`` the name ``new``. Returns how
    many episodes were renamed. (If ``new`` already exists, the two
    series become one.)"""
    episodes = session.scalars(select(Episode).where(Episode.series == old)).all()
    for episode in episodes:
        apply_bulk(episode, {"series": new}, {})
    return len(episodes)
