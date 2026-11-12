"""Guess episode details from a file's name.

Many collections put the details in the file name, for example:

    Suspense 1943-05-13 - Donovan's Brain.mp3
    CampbellPlayhouse39-04-14ThePatriot.mp3
    Suspense_1943-xx-xx_Unknown.mp3          ("xx" = not known)
    1943-05-13 Suspense - Donovan's Brain.mp3
    Coast to Coast - Mar 01 2010 - Hour 1.mp3   (month names work too)
    The Lone Ranger, Old Time Radio, 380411   Flaming Buildings.mp3
                                         (380411 = 1938-04-11, run together)
    Dragnet 49-06-03 (001) Production 1.mp3     (episode 1)
    X Minus One - 001 - No Contact.mp3          (no date: episode 1)

The reader looks for a date. What comes before it is taken as the series,
and what comes after it as the title. Without a date, a clear episode
number splits the name the same way. These are only guesses: they're shown
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
    # 380411: year, month and day run together, the year in two digits
    # (read as 19xx, like 43-05-13 above). Common in old-time radio
    # collections. Tried last, and only six digits that make a real date.
    re.compile(r"(?<!\d)(?P<yy>\d\d)(?P<m>0[1-9]|1[0-2])(?P<d>0[1-9]|[12]\d|3[01])(?!\d)"),
]
# Labels some collections put in names that aren't part of the series:
# "The Lone Ranger, Old Time Radio, 380411 ..."
LABELS = re.compile(r"^(?:old[- ]?time[- ]radio|otr|otrr)$", re.I)
# Episode numbers. Numbers are only taken where collections put them, so a
# series like "Box 13" or a title like "The 39 Steps" is left alone.
# Marked: "#42", "Ep 42", "Ep. 42", "Episode 42", "No. 42", "e042".
_MARKED = r"(?<![a-z0-9])(?:#|(?:episode|epi|ep|no|nr)\.?\s*#?)\s*(?P<n>\d{1,5})(?!\d)"
_MARKED_E = r"(?<![a-z0-9])e(?P<n>\d{1,5})(?!\d)"
# In brackets: "(042)", "[42]".
_BRACKETS = r"[(\[]\s*(?P<n>\d{1,5})\s*[)\]]"
# Bare, right after the date: "Gunsmoke 1952-04-26 001 Billy the Kid".
# Up to three digits (or four starting with 0), so "1984" stays a title.
_BARE = r"(?P<n>\d{1,3}|0\d{3})(?![\d)\]])(?=[\s_.-]|$)"
MARKED = [re.compile(_MARKED, re.I), re.compile(_MARKED_E, re.I)]
AFTER_DATE = [re.compile(r"^(?:" + p + r")", re.I) for p in (_MARKED, _MARKED_E, _BRACKETS, _BARE)]
BEFORE_DATE = [re.compile(r"(?:" + p + r")$", re.I) for p in (_MARKED, _MARKED_E, _BRACKETS)]
AFTER_TITLE = [re.compile(r"(?:" + p + r")\)?$", re.I) for p in (_MARKED, _MARKED_E)]
# Names without a date: a marked number anywhere; brackets with a title
# after them; or a bare number between " - " marks, between "_" marks, or
# written with a leading 0 ("X Minus One 001 No Contact").
NO_DATE = [
    *MARKED,
    re.compile(_BRACKETS + r"(?=.*[a-z])", re.I),
    re.compile(r"(?<= - )(?P<n>\d{1,4})(?= - )"),
    re.compile(r"(?<![^\s_])(?P<n>0\d{1,3})(?=[\s_]|$)"),
    re.compile(r"(?<=_)(?P<n>\d{1,3})(?=_)"),
]
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
    closed = text.rstrip(" -_.,;:").endswith(")")
    text = text.strip(EDGE_JUNK)
    if not text:
        return None
    text = text.replace("_", " ")
    if " " not in text:
        text = text.replace(".", " ")
    if " " not in text:
        # RunTogetherWords: put a space before each capital after a small letter.
        # And before a capitalised word after a capital: "XMinusOne".
        text = re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", text)
    text = re.sub(r"\s+", " ", text).strip(EDGE_JUNK)
    if text and closed and text.count("(") > text.count(")"):
        text += ")"   # keep the bracket that closes "Title (1)"
    return text or None


def _read_date(match: re.Match):
    """The (year, month, day) a date match stands for, or None if it isn't
    a sensible date."""
    parts = match.groupdict()
    year = int(parts["y"]) if parts.get("y") else 1900 + int(parts["yy"])
    if parts.get("mon"):
        month = MONTH_NAMES[parts["mon"].lower()]
    else:
        month = None if parts["m"].lower() == "xx" else int(parts["m"])
    day = None if parts["d"].lower() == "xx" else int(parts["d"])
    if not EARLIEST_YEAR <= year <= LATEST_YEAR:
        return None
    if month is not None and not 1 <= month <= 12:
        return None
    if month is None:
        day = None   # a day without a month means nothing
    if day is not None and not 1 <= day <= calendar.monthrange(year, month)[1]:
        return None
    return year, month, day


def _find_date(stem: str):
    """Find the first sensible date in a name: (match, year, month, day)."""
    for pattern in DATE_PATTERNS:
        for match in pattern.finditer(stem):
            date = _read_date(match)
            if date:
                return match, *date
    return None


def _without_labels(text: str) -> str:
    """Drop comma-separated labels such as "Old Time Radio" or "OTR"."""
    pieces = [piece for piece in text.split(",") if not LABELS.match(piece.strip(EDGE_JUNK))]
    return ",".join(pieces) if pieces else text


def _number(match: re.Match) -> str | None:
    """The episode number from a match, without leading zeros ("007" -> "7").
    None for 0, which is never an episode."""
    number = match["n"].lstrip("0")
    return number or None


def _take_number(text: str, patterns) -> tuple[str | None, str]:
    """Look for an episode number in ``text`` with the first pattern that
    finds one: (the number, the text without it), or (None, text)."""
    for pattern in patterns:
        for match in pattern.finditer(text):
            number = _number(match)
            if number:
                return number, text[:match.start()] + " " + text[match.end():]
    return None, text


def marked_number(text: str | None) -> str | None:
    """A clearly marked episode number anywhere in some text, such as a
    tag ("xx/xx/1952 episode 09" -> "9"), or None."""
    number, _ = _take_number(text or "", MARKED)
    return number


# Marks between the parts of a title tag: "1943-05-13 - Donovan's Brain".
TITLE_JUNK = " -_.,;:|~/"
TITLE_END_JUNK = " -_,;:|~/"   # a full stop at the end may be "Inc."


def _without_series(title: str, names) -> str:
    """``title`` without a series name at its start ("Suspense - Donovan's
    Brain" -> "Donovan's Brain"). Only the whole name, followed by a mark
    or a date: "The Shadow Knows" keeps its words when the series is "The
    Shadow". Never the whole title."""
    for name in names:
        name = (name or "").strip()
        if not name or not title.lower().startswith(name.lower()):
            continue
        rest = title[len(name):]
        if not rest or rest[0].isalnum() or rest[0] in "'’":
            continue   # "The Shadows...", "Suspense's..."
        after_space = rest.lstrip(" ")
        if after_space and after_space[0] not in TITLE_JUNK \
                and _date_at(after_space, start=True) is None:
            continue   # only a space, then words: may be part of the title
        rest = rest.lstrip(TITLE_JUNK)
        if rest:
            return rest
    return title


def _date_at(text: str, start: bool):
    """A sensible date right at the start (or end) of ``text``, give or
    take marks and brackets: (the text without it, (year, month, day)), or
    None. At the end, six digits run together aren't taken: "Case 380411"
    is more likely a title than April 11th, 1938."""
    patterns = DATE_PATTERNS if start else DATE_PATTERNS[:-1]
    for pattern in patterns:
        for match in pattern.finditer(text):
            before, after = text[:match.start()], text[match.end():]
            if start:
                if before.strip(TITLE_JUNK + "(["):
                    continue
                if before.rstrip()[-1:] in ("(", "["):
                    after = after.lstrip(" ")
                    after = after[1:] if after[:1] in (")", "]") else after
                rest = after.lstrip(TITLE_JUNK)
            else:
                if after.strip(TITLE_JUNK + ")]"):
                    continue
                rest = before.rstrip(TITLE_END_JUNK)
                if rest[-1:] in ("(", "["):
                    rest = rest[:-1].rstrip(TITLE_END_JUNK)
            date = _read_date(match)
            if date:
                return rest, date
    return None


def clean_title(title: str | None, series=()) -> tuple[str | None, tuple]:
    """Take a date and the series name off a title tag, so the title is
    just the episode's own name: (the title, (year, month, day) of the date
    taken off, or three Nones).

    "1943-05-13 - Donovan's Brain" -> ("Donovan's Brain", (1943, 5, 13))
    "Suspense - 1943-05-13 - Donovan's Brain" (series "Suspense") -> the same
    "Donovan's Brain (May 13, 1943)" -> the same

    Only a date at the very start or end is taken off: one in the middle
    may belong to the title. If nothing would be left, the title is None
    (the date is still given).
    """
    none = (None, None, None)
    if not title:
        return title, none
    text = _without_series(title.strip(), series)
    found = _date_at(text, start=True) or _date_at(text, start=False)
    if found is None:
        return text, none
    text, date = found
    text = _without_series(text, series).strip() or None
    return text, date


def _parse_without_date(stem: str) -> Guess | None:
    """Names with no date: a clear episode number splits the series (before
    it) from the title (after it). None if there's no such number."""
    stem = PART_PATTERN.sub("", stem)
    for pattern in NO_DATE:
        for match in pattern.finditer(stem):
            number = _number(match)
            if number:
                before, after = stem[:match.start()], stem[match.end():]
                return Guess(series=tidy(_without_labels(before)), episode_number=number,
                             title=tidy(after))
    return None


def parse_filename(name: str) -> Guess | None:
    """Guess details from a file name, or None if it has neither a date nor
    a clear episode number in it."""
    stem = PurePath(name).stem
    found = _find_date(stem)
    if found is None:
        return _parse_without_date(stem)
    match, year, month, day = found
    before, after = stem[:match.start()], stem[match.end():]
    guess = Guess(year=year, month=month, day=day)

    # "Hour 1" names a part of a broadcast, not its title.
    after = PART_PATTERN.sub("", after).strip(" -_.,")
    # The episode number: just after the date ("(001)", "001", "#42"), else
    # just before it ("Dragnet #001 49-06-03"), else at the end of the title
    # ("... - Episode 270").
    number, after = _take_number(after, AFTER_DATE)
    if number is None:
        number, kept = _take_number(before.rstrip(" -_.,"), BEFORE_DATE)
        if number:
            before = kept
    if number is None:
        number, after = _take_number(after.rstrip(" -_.,"), AFTER_TITLE)
    guess.episode_number = number

    if before.strip(EDGE_JUNK):
        guess.series = tidy(_without_labels(before))
        guess.title = tidy(after)
    elif " - " in after:
        # "1943-05-13 Suspense - Donovan's Brain"
        series, _, title = after.partition(" - ")
        guess.series, guess.title = tidy(series), tidy(title)
    else:
        guess.title = tidy(after)
    return guess
