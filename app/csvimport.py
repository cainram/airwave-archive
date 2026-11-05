"""Importing episode details from a spreadsheet (a CSV file).

A CSV ("comma-separated values") file is a plain-text spreadsheet: one
line per row, with the cells separated by commas (or semicolons or tabs).
Excel, Numbers and Google Sheets can all save one. The first row names
the columns.

How an import works:

1. The file is kept for a while in the data folder (``imports/``).
2. Each column is matched to an episode detail by its name ("Title",
   "Air date"...). The owner can change the matching.
3. Each row is matched to an episode: by file name if there's a File
   column, else by series and air date, else by series and episode number.
4. A preview shows what would change. Nothing is saved until the owner
   confirms, and then only the ticked rows. Empty cells change nothing.
"""

from __future__ import annotations

import calendar
import csv
import io
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import MANUAL, AudioFile, Episode, utcnow
from app.episodes import (
    EARLIEST_YEAR, LATEST_YEAR, MONTHS, TEXT_FIELDS, TIME_ZONES, _clean, format_time,
)
from app.filenames import MONTH_NAMES

MAX_ROWS = 5000
MAX_BYTES = 10_000_000
KEEP_SECONDS = 24 * 3600   # uploaded files older than this are tidied away
TOKEN_PATTERN = re.compile(r"^[0-9a-f]{16}$")

# The details a column can hold, in the order they're offered.
FIELDS = {
    "file": "File name",
    "series": "Series",
    "episode_number": "Episode number",
    "title": "Title",
    "date": "Air date",
    "time": "Start time",
    "timezone": "Time zone",
    "network": "Station / network",
    "host": "Host",
    "cast": "Cast",
    "notes": "Notes",
}
TEXT = ("series", "episode_number", "title", "network", "host", "cast", "notes")

# Column names recognised for each detail (compared without spaces,
# punctuation or capitals).
COLUMN_NAMES = {
    "file": ["file", "filename", "path", "filepath", "mp3", "recording"],
    "series": ["series", "show", "program", "programme", "seriestitle", "showname", "programname"],
    "episode_number": ["episode", "ep", "epno", "episodeno", "episodenumber", "number", "no",
                       "num", "lognumber"],
    "title": ["title", "episodetitle", "eptitle", "story", "storytitle"],
    "date": ["date", "airdate", "aired", "broadcastdate", "originalairdate", "airdates",
             "broadcast", "dateaired", "originaldate"],
    "time": ["time", "airtime", "starttime", "start", "timeaired"],
    "timezone": ["timezone", "zone", "tz"],
    "network": ["network", "station", "net", "broadcaster"],
    "host": ["host", "hosts", "presenter", "presenters", "announcer"],
    "cast": ["cast", "starring", "stars", "actors", "guest", "guests", "performers", "with"],
    "notes": ["notes", "note", "description", "summary", "comment", "comments", "synopsis",
              "plot", "details", "topic", "topics"],
}
_COLUMN_LOOKUP = {name: key for key, names in COLUMN_NAMES.items() for name in names}

# Time zones as people write them.
ZONE_NAMES = {
    "useastern": "America/New_York", "eastern": "America/New_York", "et": "America/New_York",
    "est": "America/New_York", "edt": "America/New_York",
    "uscentral": "America/Chicago", "central": "America/Chicago", "ct": "America/Chicago",
    "cst": "America/Chicago", "cdt": "America/Chicago",
    "usmountain": "America/Denver", "mountain": "America/Denver", "mt": "America/Denver",
    "mst": "America/Denver", "mdt": "America/Denver",
    "uspacific": "America/Los_Angeles", "pacific": "America/Los_Angeles",
    "pt": "America/Los_Angeles", "pst": "America/Los_Angeles", "pdt": "America/Los_Angeles",
    "uk": "Europe/London", "gmt": "Europe/London", "bst": "Europe/London",
    "utc": "UTC", "z": "UTC",
}
ZONE_NAMES.update({zone.lower().replace("/", "").replace("_", ""): zone for zone in TIME_ZONES})


def _key(text: str) -> str:
    """'Air Date' -> 'airdate'; '#' and 'Ep. #' -> 'number' / 'epno'."""
    text = text.lower().replace("#", "no")
    return "number" if text.strip() == "no" else re.sub(r"[^a-z0-9]", "", text)


# An example to start from: every column the app knows, and one row. Its
# series is made up, so importing it unchanged matches no real episode.
EXAMPLE_HEADERS = ["File", "Series", "Episode", "Title", "Air Date", "Time", "Time Zone",
                   "Network", "Host", "Cast", "Notes"]
EXAMPLE_ROW = ["", "Example Series", "1", "Example Title", "4/14/1939", "8:00 PM", "US Eastern",
               "CBS", "Host Name", "First Actor, Second Actor",
               "Replace this row with your own. Leave a cell empty to keep what's there."]


def example_csv() -> bytes:
    """The example spreadsheet, as Excel likes it (with a mark at the start
    so accented letters come out right)."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(EXAMPLE_HEADERS)
    writer.writerow(EXAMPLE_ROW)
    return ("\ufeff" + out.getvalue()).encode("utf-8")


# ------------------------------------------------------------- the file


def save_upload(data: bytes, import_dir: Path) -> str:
    """Keep an uploaded file for the preview. Returns its token (a random
    name, so nobody can guess another import's address)."""
    import_dir.mkdir(parents=True, exist_ok=True)
    for old in import_dir.glob("*.csv"):   # tidy away forgotten imports
        if old.stat().st_mtime < time.time() - KEEP_SECONDS:
            old.unlink(missing_ok=True)
    token = secrets.token_hex(8)
    (import_dir / f"{token}.csv").write_bytes(data)
    return token


def upload_path(import_dir: Path, token: str) -> Path | None:
    path = import_dir / f"{token}.csv"
    return path if TOKEN_PATTERN.match(token) and path.is_file() else None


def read_table(data: bytes) -> tuple[list[str], list[list[str]]]:
    """The column names and rows of a CSV file. Accepts commas, semicolons
    or tabs, and the text encodings Excel uses."""
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    sample = text[:20000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = [row for row in csv.reader(io.StringIO(text), dialect) if any(c.strip() for c in row)]
    if not rows:
        return [], []
    headers = [h.strip() for h in rows[0]]
    return headers, rows[1:MAX_ROWS + 1]


def guess_columns(headers: list[str]) -> list[str]:
    """Which detail each column holds ("" for none), by its name. Each
    detail is given to one column at most."""
    used: set[str] = set()
    guesses = []
    for header in headers:
        guess = _COLUMN_LOOKUP.get(_key(header), "")
        if guess in used:
            guess = ""
        used.add(guess)
        guesses.append(guess)
    return guesses


# ----------------------------------------------------------- reading cells

_NUMERIC_DATE = re.compile(r"^(\d{1,2})[/.-](\d{1,2})[/.-](\d{2}|\d{4})$")
_ISO_DATE = re.compile(r"^(\d{4})(?:[-/.](\d{1,2}|xx)(?:[-/.](\d{1,2}|xx))?)?$", re.I)
_MONTH_WORD = "(" + "|".join(sorted(MONTH_NAMES, key=len, reverse=True)) + r")\.?"
_WORD_DATES = [
    # April 14, 1939 · Apr 14th 1939 · April 1939
    re.compile(r"^" + _MONTH_WORD + r"(?:\s+(\d{1,2})(?:st|nd|rd|th)?)?,?\s+(\d{4})$", re.I),
    # 14 April 1939 · 14th of April, 1939
    re.compile(r"^(\d{1,2})(?:st|nd|rd|th)?(?:\s+of)?\s+" + _MONTH_WORD + r",?\s+(\d{4})$", re.I),
]
_WEEKDAY = re.compile(r"^(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?,?\s+", re.I)


def read_date(text: str) -> tuple[tuple | None, str | None]:
    """Read a date as people write them. Returns ((year, month, day), None)
    with None for unknown parts, or (None, what's wrong). Numbers with
    slashes are read the American way: 4/14/1939 is April 14th. A two-digit
    year is read as 19xx (4/14/39 is 1939)."""
    text = _WEEKDAY.sub("", text.strip())
    year = month = day = None
    if match := _ISO_DATE.match(text):
        year = int(match[1])
        month = int(match[2]) if match[2] and match[2].lower() != "xx" else None
        day = int(match[3]) if match[3] and match[3].lower() != "xx" and month else None
    elif match := _NUMERIC_DATE.match(text):
        month, day = int(match[1]), int(match[2])
        year = int(match[3]) if len(match[3]) == 4 else 1900 + int(match[3])
    elif match := _WORD_DATES[0].match(text):
        month, day, year = MONTH_NAMES[match[1].lower()], match[2] and int(match[2]), int(match[3])
    elif match := _WORD_DATES[1].match(text):
        day, month, year = int(match[1]), MONTH_NAMES[match[2].lower()], int(match[3])
    else:
        return None, f"Can't read the date “{text}”. Write it like 1939-04-14 or 4/14/1939."
    if not EARLIEST_YEAR <= year <= LATEST_YEAR:
        return None, f"The year in “{text}” should be between {EARLIEST_YEAR} and {LATEST_YEAR}."
    if month is not None and not 1 <= month <= 12:
        return None, f"“{text}” has no month {month}. (Dates with slashes are read month first.)"
    if day is not None and not 1 <= day <= calendar.monthrange(year, month)[1]:
        return None, f"{MONTHS[month - 1]} {year} doesn't have a day {day}."
    return (year, month, day or None), None


_TIME = re.compile(r"^(\d{1,2})(?:[:.](\d{2}))?\s*([ap])?\.?\s*m?\.?$", re.I)


def read_time(text: str) -> tuple[str | None, str | None]:
    """'22:00', '10:00 PM', '10pm', '10 p.m.' -> ('22:00', None)."""
    match = _TIME.match(text.strip())
    if not match:
        return None, f"Can't read the time “{text}”. Write it like 22:00 or 10:00 PM."
    hours, minutes, half = int(match[1]), int(match[2] or 0), (match[3] or "").lower()
    if half:
        if not 1 <= hours <= 12:
            return None, f"“{text}” isn't a time."
        hours = hours % 12 + (12 if half == "p" else 0)
    elif not match[2]:
        return None, f"Is “{text}” in the morning or evening? Write it like 22:00 or 10 PM."
    if hours > 23 or minutes > 59:
        return None, f"“{text}” isn't a time."
    return f"{hours:02d}:{minutes:02d}", None


def read_zone(text: str) -> tuple[str | None, str | None]:
    zone = ZONE_NAMES.get(re.sub(r"[^a-z]", "", text.lower()))
    if zone is None:
        return None, f"Unknown time zone “{text}”. Use US Eastern, Central, Mountain, Pacific, UK or UTC."
    return zone, None


# --------------------------------------------------------------- matching


@dataclass
class Row:
    """One spreadsheet row, as read and matched."""
    number: int                 # its row number in the spreadsheet (2 = first under the names)
    values: dict = field(default_factory=dict)   # detail -> value read from the row
    errors: list[str] = field(default_factory=list)
    episode_id: int | None = None
    matched_by: str = ""        # "file name", "series and date", "series and episode number"
    problem: str = ""           # why it wasn't matched
    changes: list[dict] = field(default_factory=list)   # what would change

    @property
    def checked(self) -> bool:
        return not self.errors and not any(c["conflict"] for c in self.changes)


def read_rows(rows: list[list[str]], columns: list[str], series_for_all: str | None) -> list[Row]:
    """Read every row's cells according to the column matching."""
    found = []
    for index, cells in enumerate(rows):
        row = Row(number=index + 2)
        for column, key in enumerate(columns):
            value = _clean(cells[column]) if key and column < len(cells) else None
            if value is None:
                continue
            if key == "date":
                value, error = read_date(value)
            elif key == "time":
                value, error = read_time(value)
            elif key == "timezone":
                value, error = read_zone(value)
            else:
                error = None
            if error:
                row.errors.append(error)
            elif value is not None:
                row.values[key] = value
        if "series" not in row.values and series_for_all:
            row.values["series"] = series_for_all
        found.append(row)
    return found


def _lower(text: str | None) -> str:
    return (text or "").strip().lower()


def match_rows(session: Session, rows: list[Row]) -> None:
    """Find each row's episode: by file name, else by series and air date,
    else by series and episode number. A row that fits several episodes
    isn't matched (better unmatched than wrong)."""
    by_file: dict[str, set[int]] = {}
    for name, episode_id in session.execute(
            select(AudioFile.name, AudioFile.episode_id).where(AudioFile.episode_id.is_not(None))):
        for key in (_lower(name), _lower(Path(name).stem)):
            by_file.setdefault(key, set()).add(episode_id)
    by_date: dict[tuple, set[int]] = {}
    by_number: dict[tuple, set[int]] = {}
    for ep_id, series, number, y, m, d in session.execute(
            select(Episode.id, Episode.series, Episode.episode_number,
                   Episode.air_year, Episode.air_month, Episode.air_day)):
        if series and y and m and d:
            by_date.setdefault((_lower(series), y, m, d), set()).add(ep_id)
        if series and number:
            by_number.setdefault((_lower(series), _lower(number)), set()).add(ep_id)

    taken: dict[int, int] = {}   # episode -> the row that has it
    for row in rows:
        v = row.values
        tries = []
        if v.get("file"):
            tries.append(("file name", by_file.get(_lower(v["file"]))
                          or by_file.get(_lower(Path(v["file"]).name))
                          or by_file.get(_lower(Path(v["file"]).stem))))
        else:
            date_value = v.get("date")
            if v.get("series") and date_value and date_value[2]:
                tries.append(("series and date", by_date.get((_lower(v["series"]), *date_value))))
            if v.get("series") and v.get("episode_number"):
                tries.append(("series and episode number",
                              by_number.get((_lower(v["series"]), _lower(v["episode_number"])))))
        if not tries:
            row.problem = ("Needs a file name, or a series with a full air date or an "
                           "episode number, to find its episode.")
            continue
        for how, ids in tries:
            if ids and len(ids) == 1:
                episode_id = next(iter(ids))
                if episode_id in taken:
                    row.problem = f"The same episode as row {taken[episode_id]}."
                else:
                    row.episode_id, row.matched_by = episode_id, how
                    taken[episode_id] = row.number
                break
            if ids:
                row.problem = f"Fits {len(ids)} episodes (by {how}); can't tell which."
                break
        else:
            row.problem = "No episode in the library matches."


def _date_tuple(ep: Episode) -> tuple:
    return (ep.air_year, ep.air_month, ep.air_day)


def _show_date(value: tuple | None) -> str:
    if not value or value[0] is None:
        return ""
    year, month, day = value
    if month is None:
        return str(year)
    name = calendar.month_name[month]
    return f"{name} {day}, {year}" if day else f"{name} {year}"


def work_out_changes(session: Session, rows: list[Row]) -> None:
    """For each matched row, list what would change on its episode."""
    for row in rows:
        if row.episode_id is None:
            continue
        ep = session.get(Episode, row.episode_id)
        sources = ep.sources
        v = row.values
        for key in TEXT:
            if key in v and v[key] != getattr(ep, key):
                _add(row, key, getattr(ep, key), v[key], sources.get(key))
        if "date" in v and v["date"] != _date_tuple(ep):
            _add(row, "date", _show_date(_date_tuple(ep)), _show_date(v["date"]),
                 sources.get("air_date"))
        day_known = (v.get("date") or _date_tuple(ep))[2]
        if "time" in v:
            if not day_known:
                row.errors.append("A start time needs a full air date (year, month and day).")
            elif v["time"] != ep.air_time:
                _add(row, "time", format_time(ep.air_time) if ep.air_time else "",
                     format_time(v["time"]), sources.get("air_date"))
        if "timezone" in v and v["timezone"] != ep.air_timezone:
            if not (v.get("time") or ep.air_time):
                row.errors.append("A time zone needs a start time.")
            else:
                _add(row, "timezone", TIME_ZONES.get(ep.air_timezone or "", ""),
                     TIME_ZONES[v["timezone"]], sources.get("air_date"))


def _add(row: Row, key: str, now, new, source) -> None:
    row.changes.append({"key": key, "label": FIELDS[key], "now": now or "", "new": new,
                        # It would replace something the owner typed.
                        "conflict": bool(now) and source == MANUAL})


def apply_row(ep: Episode, values: dict) -> bool:
    """Save one row's values onto its episode. Empty cells change nothing;
    everything saved counts as typed by hand."""
    sources = ep.sources
    changed = False
    for key in TEXT:
        if key in values:
            changed |= getattr(ep, key) != values[key]
            setattr(ep, key, values[key])
            sources[key] = MANUAL
    date_value = values.get("date")
    if date_value is not None and date_value != _date_tuple(ep):
        ep.air_year, ep.air_month, ep.air_day = date_value
        ep.air_time = ep.air_timezone = None   # a time belongs to the old day
        changed = True
    if values.get("time") and ep.air_day:
        changed |= (ep.air_time, ep.air_timezone) != (values["time"], values.get("timezone", ep.air_timezone))
        ep.air_time = values["time"]
        ep.air_timezone = values.get("timezone", ep.air_timezone)
    elif values.get("timezone") and ep.air_time:
        changed |= ep.air_timezone != values["timezone"]
        ep.air_timezone = values["timezone"]
    if any(k in values for k in ("date", "time", "timezone")):
        sources["air_date"] = MANUAL
    ep.sources = sources
    if changed:
        ep.updated_at = utcnow()
    return changed


def prepare(session: Session, data: bytes, columns: list[str] | None,
            series_for_all: str | None):
    """Read, match and compare a whole file. Returns (headers, columns,
    rows): ``columns`` is the matching used (guessed if not given)."""
    headers, table = read_table(data)
    if columns is None or len(columns) != len(headers):
        columns = guess_columns(headers)
    rows = read_rows(table, columns, series_for_all)
    match_rows(session, rows)
    work_out_changes(session, rows)
    return headers, columns, rows
