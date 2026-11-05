"""Episode details: which fields there are, checking what's typed into the
edit form, and showing air dates in plain words.

Air dates follow the plan's rule: never invent missing parts. A date can be
just a year, a year and month, a full date, or a full date with a time and
time zone.

Details can also be filled in automatically from a file's own tags. Those
are marked as coming from tags, and never replace anything typed by hand.
"""

from __future__ import annotations

import calendar
import re
from datetime import date

from app.db import MANUAL, TAGS, AudioFile, Episode, utcnow

# The plain text fields, in the order they appear on the form.
TEXT_FIELDS = {
    "series": "Series",
    "episode_number": "Episode number",
    "title": "Title",
    "network": "Station / network",
    "host": "Host",
    "cast": "Cast",
    "notes": "Notes",
}
LONG_FIELDS = {"cast", "notes"}   # shown as big boxes

# The air date parts are edited together and recorded as one "air_date".
DATE_PARTS = ("air_year", "air_month", "air_day", "air_time", "air_timezone")

# Time zones offered for air times. US Eastern was the usual reference for
# network radio; Pacific is where Coast to Coast AM was produced.
TIME_ZONES = {
    "America/New_York": "US Eastern",
    "America/Chicago": "US Central",
    "America/Denver": "US Mountain",
    "America/Los_Angeles": "US Pacific",
    "Europe/London": "UK",
    "UTC": "UTC",
}

MONTHS = list(calendar.month_name)[1:]   # "January" ... "December"
EARLIEST_YEAR, LATEST_YEAR = 1890, 2100
TIME_PATTERN = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def _clean(text: str | None) -> str | None:
    """Trim spaces and use one kind of line ending; empty means "not known"."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    return text or None


def read_form(form) -> tuple[dict, dict[str, str]]:
    """Check what was typed into the edit form.

    Returns (values, errors). ``errors`` maps a field name to a message
    explaining what's wrong, in plain words.
    """
    values: dict = {name: _clean(form.get(name)) for name in TEXT_FIELDS}
    errors: dict[str, str] = {}

    year_text = _clean(form.get("air_year"))
    month_text = _clean(form.get("air_month"))
    day_text = _clean(form.get("air_day"))
    time_text = _clean(form.get("air_time"))
    zone = _clean(form.get("air_timezone"))

    year = month = day = None
    if year_text:
        if year_text.isdigit() and EARLIEST_YEAR <= int(year_text) <= LATEST_YEAR:
            year = int(year_text)
        else:
            errors["air_year"] = f"The year should be a number like 1943 (between {EARLIEST_YEAR} and {LATEST_YEAR})."
    if month_text:
        if month_text.isdigit() and 1 <= int(month_text) <= 12:
            month = int(month_text)
        else:
            errors["air_month"] = "Please pick a month from the list."
        if not year_text:
            errors["air_month"] = "A month needs a year. Fill in the year, or set the month to unknown."
    if day_text:
        if not (year and month):
            errors["air_day"] = "A day needs a year and a month."
        elif day_text.isdigit() and 1 <= int(day_text) <= calendar.monthrange(year, month)[1]:
            day = int(day_text)
        else:
            errors["air_day"] = f"{MONTHS[month - 1]} {year} doesn't have a day {day_text}."
    if time_text:
        match = TIME_PATTERN.match(time_text)
        if not match:
            errors["air_time"] = "The time should look like 20:00 (24-hour clock)."
        elif not day_text:
            errors["air_time"] = "A time needs a full date (year, month, and day)."
        else:
            time_text = f"{int(match[1]):02d}:{match[2]}"
    if zone and zone not in TIME_ZONES:
        errors["air_timezone"] = "Please pick a time zone from the list."
    if zone and not time_text:
        errors["air_timezone"] = "A time zone only goes with a time. Fill in the time, or set the zone to unknown."

    values.update(air_year=year, air_month=month, air_day=day,
                  air_time=time_text if not errors.get("air_time") else None,
                  air_timezone=zone)
    return values, errors


def apply_edits(episode: Episode, values: dict) -> list[str]:
    """Save edited values onto an episode. Only fields that actually changed
    are recorded as manual edits. Returns the names of changed fields."""
    sources = episode.sources
    changed = []
    for name in TEXT_FIELDS:
        if getattr(episode, name) != values[name]:
            setattr(episode, name, values[name])
            sources[name] = MANUAL
            changed.append(name)
    if any(getattr(episode, part) != values[part] for part in DATE_PARTS):
        for part in DATE_PARTS:
            setattr(episode, part, values[part])
        sources["air_date"] = MANUAL
        changed.append("air_date")
    if changed:
        episode.sources = sources
        episode.updated_at = utcnow()
    return changed


# Dates in tags: "1939", "1939-04", or "1939-04-14" (anything after the
# day, such as a time, is ignored because it has no time zone).
TAG_DATE_PATTERN = re.compile(r"^(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?(?:[T ].*)?$")


def date_from_tag(text: str | None) -> tuple[int | None, int | None, int | None]:
    """'1939' -> (1939, None, None); '1939-04-14' -> (1939, 4, 14).
    Anything unclear gives (None, None, None): better unknown than wrong."""
    match = TAG_DATE_PATTERN.match((text or "").strip())
    if not match:
        return None, None, None
    year, month, day = (int(part) if part else None for part in match.groups())
    if not EARLIEST_YEAR <= year <= LATEST_YEAR:
        return None, None, None
    if month is not None and not 1 <= month <= 12:
        return year, None, None
    if day is not None and not 1 <= day <= calendar.monthrange(year, month)[1]:
        return year, month, None
    return year, month, day


def fill_from_tags(episode: Episode, f: AudioFile) -> bool:
    """Fill in episode details from a file's tags.

    Anything typed by hand is left alone. Values that came from tags before
    are updated (or cleared) to match the tags now. Returns True if
    anything changed.
    """
    sources = episode.sources
    changed = False
    found = {
        "series": _clean(f.tag_artist) or _clean(f.tag_album),
        "title": _clean(f.tag_title),
        "notes": _clean(f.tag_comment),
    }
    for name, value in found.items():
        source = sources.get(name)
        if source == MANUAL or (value is None and source != TAGS):
            continue
        if getattr(episode, name) != value:
            setattr(episode, name, value)
            changed = True
        if value is None:
            sources.pop(name, None)
        else:
            sources[name] = TAGS

    source = sources.get("air_date")
    year, month, day = date_from_tag(f.tag_date)
    if source != MANUAL and (year is not None or source == TAGS):
        new = {"air_year": year, "air_month": month, "air_day": day,
               "air_time": None, "air_timezone": None}
        if any(getattr(episode, part) != value for part, value in new.items()):
            for part, value in new.items():
                setattr(episode, part, value)
            changed = True
        if year is None:
            sources.pop("air_date", None)
        else:
            sources["air_date"] = TAGS

    if sources != episode.sources:
        episode.sources = sources
        changed = True
    if changed:
        episode.updated_at = utcnow()
    return changed


def format_time(hhmm: str) -> str:
    """'20:00' -> '8:00 PM'."""
    hours, minutes = (int(part) for part in hhmm.split(":"))
    suffix = "AM" if hours < 12 else "PM"
    return f"{hours % 12 or 12}:{minutes:02d} {suffix}"


def format_air_date(ep: Episode | None, short: bool = False) -> str:
    """Show an air date with only the parts that are known.

    Long:  'Friday, 14 April 1939, 8:00 PM US Eastern'
           '14 April 1939, time unknown' · 'April 1939, day unknown'
           '1939, date unknown'
    Short: '14 Apr 1939' · 'Apr 1939' · '1939'
    Returns '' if nothing is known.
    """
    if ep is None or ep.air_year is None:
        return ""
    year, month, day = ep.air_year, ep.air_month, ep.air_day
    if short:
        if month is None:
            return str(year)
        name = calendar.month_abbr[month]
        return f"{day} {name} {year}" if day else f"{name} {year}"
    if month is None:
        return f"{year}, date unknown"
    if day is None:
        return f"{calendar.month_name[month]} {year}, day unknown"
    weekday = calendar.day_name[date(year, month, day).weekday()]
    text = f"{weekday}, {day} {calendar.month_name[month]} {year}"
    if not ep.air_time:
        return f"{text}, time unknown"
    zone = TIME_ZONES.get(ep.air_timezone or "", "time zone unknown")
    return f"{text}, {format_time(ep.air_time)} {zone}"
