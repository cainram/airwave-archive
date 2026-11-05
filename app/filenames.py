"""Guess episode details from a file's name.

Many collections put the details in the file name, for example:

    Suspense 1943-05-13 - Donovan's Brain.mp3
    CampbellPlayhouse39-04-14ThePatriot.mp3
    Suspense_1943-xx-xx_Unknown.mp3          ("xx" = not known)
    1943-05-13 Suspense - Donovan's Brain.mp3
    Coast to Coast - Mar 01 2010 - Hour 1.mp3   (month names work too)

The reader looks for a date. What comes before it is taken as the series,
and what comes after it as the title. These are only guesses: they're shown
on the review page and nothing is saved until the owner confirms.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from pathlib import PurePath

from app.episodes import EARLIEST_YEAR, LATEST_YEAR, PART_PATTERN

# Month names and short forms: "Mar", "March", "Sept"...
MONTH_NAMES = {name: number for number, names in enumerate([
    (), ("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"),
    ("may",), ("jun", "june"), ("jul", "july"), ("aug", "august"),
    ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"),
    ("dec", "december")]) for name in names}
_MONTH = r"(?P<mon>" + "|".join(sorted(MONTH_NAMES, key=len, reverse=True)) + r")\.?"
_DAY = r"(?P<d>[0-3]?\d)(?:st|nd|rd|th)?"
_YEAR = r"(?P<y>(?:18|19|20)\d\d)"

# Dates in file names. "xx" (or "XX") marks an unknown month or day.
DATE_PATTERNS = [
    # 1943-05-13, 1943.05.13, 1943_05_13, 1943 05 13 (same mark both times)
    re.compile(r"(?<!\d)(?P<y>(?:18|19|20)\d\d)(?P<s>[-._ ])(?P<m>\d\d|xx)(?P=s)(?P<d>\d\d|xx)(?!\d)", re.I),
    # 19430513
    re.compile(r"(?<!\d)(?P<y>(?:18|19|20)\d\d)(?P<m>0[1-9]|1[0-2])(?P<d>0[1-9]|[12]\d|3[01])(?!\d)"),
    # 43-05-13: a two-digit year is read as 19xx (old-time radio)
    re.compile(r"(?<!\d)(?P<yy>\d\d)-(?P<m>\d\d|xx)-(?P<d>\d\d|xx)(?!\d)", re.I),
    # Mar 01 2010, March 1, 2010, Mar_1st_2010
    re.compile(r"(?<![a-z])" + _MONTH + r"[ _-]+" + _DAY + r",?[ _-]+" + _YEAR + r"(?!\d)", re.I),
    # 1 March 2010, 01-Mar-2010
    re.compile(r"(?<!\d)" + _DAY + r"[ _-]+" + _MONTH + r",?[ _-]+" + _YEAR + r"(?!\d)", re.I),
]
# An episode number, only when clearly marked: "#42", "Ep 42", "Episode 42", "e42".
EPISODE_PATTERN = re.compile(r"^(?:#\s*|ep(?:isode)?\.?\s*|e)(\d+)\b\s*", re.I)
EDGE_JUNK = " -_.,;:()[]{}"


@dataclass
class Guess:
    series: str | None = None
    episode_number: str | None = None
    title: str | None = None
    year: int | None = None
    month: int | None = None
    day: int | None = None

    @property
    def date_text(self) -> str:
        """1939-04-14, 1939-04, or 1939 (only the known parts)."""
        return date_text(self.year, self.month, self.day)


def date_text(year: int | None, month: int | None, day: int | None) -> str:
    if year is None:
        return ""
    if month is None:
        return str(year)
    return f"{year}-{month:02d}" + (f"-{day:02d}" if day else "")


def tidy(text: str) -> str | None:
    """Turn a piece of a file name into readable words.

    'CampbellPlayhouse' -> 'Campbell Playhouse'; 'Donovans_Brain' ->
    'Donovans Brain'; ' - Title ' -> 'Title'.
    """
    text = text.strip(EDGE_JUNK)
    if not text:
        return None
    text = text.replace("_", " ")
    if " " not in text:
        text = text.replace(".", " ")
    if " " not in text:
        # RunTogetherWords: put a space before each capital after a small letter.
        text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)
    text = re.sub(r"\s+", " ", text).strip(EDGE_JUNK)
    return text or None


def _find_date(stem: str):
    """Find the first sensible date in a name: (match, year, month, day)."""
    for pattern in DATE_PATTERNS:
        for match in pattern.finditer(stem):
            parts = match.groupdict()
            year = int(parts["y"]) if parts.get("y") else 1900 + int(parts["yy"])
            if parts.get("mon"):
                month = MONTH_NAMES[parts["mon"].lower()]
            else:
                month = None if parts["m"].lower() == "xx" else int(parts["m"])
            day = None if parts["d"].lower() == "xx" else int(parts["d"])
            if not EARLIEST_YEAR <= year <= LATEST_YEAR:
                continue
            if month is not None and not 1 <= month <= 12:
                continue
            if month is None:
                day = None   # a day without a month means nothing
            if day is not None and not 1 <= day <= calendar.monthrange(year, month)[1]:
                continue
            return match, year, month, day
    return None


def parse_filename(name: str) -> Guess | None:
    """Guess details from a file name, or None if it has no date in it."""
    stem = PurePath(name).stem
    found = _find_date(stem)
    if found is None:
        return None
    match, year, month, day = found
    before, after = stem[:match.start()], stem[match.end():]
    guess = Guess(year=year, month=month, day=day)

    # "Hour 1" names a part of a broadcast, not its title.
    after = PART_PATTERN.sub("", after).strip(EDGE_JUNK)
    number = EPISODE_PATTERN.match(after)
    if number:
        guess.episode_number = number[1]
        after = after[number.end():]

    if before.strip(EDGE_JUNK):
        guess.series = tidy(before)
        guess.title = tidy(after)
    elif " - " in after:
        # "1943-05-13 Suspense - Donovan's Brain"
        series, _, title = after.partition(" - ")
        guess.series, guess.title = tidy(series), tidy(title)
    else:
        guess.title = tidy(after)
    return guess
