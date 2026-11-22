"""The owner's settings (the Settings page, behind the cog in the top bar).

For now there's one: **your time zone**, the starting choice for new
rolling feeds and the "On this day" feed. Feeds that exist keep their own
time zone, changed on their own pages. Settings are kept in the database
(the ``settings`` table), one row each.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.db import Setting
from app.episodes import TIME_ZONES
from app.rolling import DEFAULT_ZONE      # US Central until it's set (owner's choice)


def get(session: Session, key: str) -> str | None:
    row = session.get(Setting, key)
    return None if row is None else row.value


def put(session: Session, key: str, value: str | None) -> None:
    row = session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=value))
    else:
        row.value = value


def time_zone(session: Session) -> str:
    """Your time zone (US Central until it's set)."""
    zone = get(session, "time_zone")
    return zone if zone in TIME_ZONES else DEFAULT_ZONE


def read_form(form) -> tuple[dict, dict]:
    values = {"time_zone": str(form.get("time_zone", ""))}
    errors = {}
    if values["time_zone"] not in TIME_ZONES:
        errors["time_zone"] = "Pick a time zone from the list."
    return values, errors


def save(session: Session, values: dict) -> None:
    put(session, "time_zone", values["time_zone"])
