"""Searching episodes by their words: titles, series, cast, host, station,
notes, episode numbers, and file names.

It uses SQLite's built-in full-text search (FTS5): a separate "index"
table, like the index at the back of a book, that lists every word and
which episodes it's in. That keeps searching quick even with tens of
thousands of episodes.

The index keeps itself up to date: whenever episodes or files are saved,
the episodes involved are indexed again (see ``keep_index_up_to_date``).
"""

from __future__ import annotations

import html
import re

from markupsafe import Markup
from sqlalchemy import event, inspect, text
from sqlalchemy.orm import Session

from app.db import AudioFile, Episode

TABLE = "episode_search"
# The columns of the index, and how much a match in each one counts.
COLUMNS = {"title": 10.0, "series": 5.0, "people": 3.0, "network": 2.0,
           "numbers": 3.0, "notes": 1.0, "files": 1.0}

CREATE = (
    f"CREATE VIRTUAL TABLE IF NOT EXISTS {TABLE} USING fts5("
    + ", ".join(COLUMNS)
    # unicode61: words are split the usual way, capitals don't matter, and
    # remove_diacritics: "Pépé" is found by "pepe". prefix: quick partial words.
    + ", tokenize='unicode61 remove_diacritics 2', prefix='2 3')"
)

# One episode's words: its details, and the names of its files.
_ROW = f"""
INSERT INTO {TABLE} (rowid, {", ".join(COLUMNS)})
SELECT e.id, e.title, e.series,
       coalesce(e.host, '') || ' ' || coalesce(e."cast", ''), e.network, e.episode_number, e.notes,
       (SELECT group_concat(f.name, ' ') FROM audio_files f WHERE f.episode_id = e.id)
FROM episodes e
"""


def rebuild_index(conn) -> None:
    """Make the whole index again from scratch."""
    conn.exec_driver_sql(f"DELETE FROM {TABLE}")
    conn.exec_driver_sql(_ROW)


def reindex(conn, episode_ids) -> None:
    """Index these episodes again (and drop ones that no longer exist)."""
    ids = sorted({i for i in episode_ids if i is not None})
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        marks = ", ".join("?" * len(chunk))
        conn.exec_driver_sql(f"DELETE FROM {TABLE} WHERE rowid IN ({marks})", tuple(chunk))
        conn.exec_driver_sql(f"{_ROW} WHERE e.id IN ({marks})", tuple(chunk))


def drop_forgotten(conn) -> None:
    """Remove index entries for episodes that were deleted."""
    conn.exec_driver_sql(
        f"DELETE FROM {TABLE} WHERE rowid NOT IN (SELECT id FROM episodes)")


def keep_index_up_to_date() -> None:
    """After every save, index the episodes that were touched again.
    (Safe to call more than once.)"""
    if event.contains(Session, "after_flush", _after_flush):
        return
    event.listen(Session, "after_flush", _after_flush)


def _after_flush(session, _context):
    touched: set[int] = set()
    for obj in [*session.new, *session.dirty, *session.deleted]:
        if isinstance(obj, Episode):
            touched.add(obj.id)
        elif isinstance(obj, AudioFile):
            touched.add(obj.episode_id)
            # A file that moved to another episode changes both.
            touched.update(inspect(obj).attrs.episode_id.history.deleted or ())
    touched.discard(None)
    if touched:
        reindex(session.connection(), touched)


# ------------------------------------------------------------------ searching

_WORD = re.compile(r'"([^"]+)"|(\S+)')
START, END = "\x02", "\x03"   # mark matches in snippets (never in real text)


def to_query(typed: str) -> str | None:
    """Turn what was typed into a full-text search query. Every word must
    match, in any order; the end of each word may be cut short ("welle"
    finds "Welles"); words "in quotes" must be together."""
    parts = []
    for phrase, word in _WORD.findall(typed or ""):
        words = re.findall(r"\w+", phrase or word)
        if words:
            parts.append('"' + " ".join(words) + '"*')
    return " ".join(parts) or None


def words_of(typed: str) -> list[str]:
    """The words typed, in small letters (for matching series names)."""
    return [w.lower() for w in re.findall(r"\w+", typed or "")]


def search(session: Session, typed: str, limit: int, offset: int = 0):
    """Episodes matching what was typed, best first.

    Returns (results, total). Each result is (episode id, snippet), where
    the snippet is a short piece of the matching text with the matched
    words highlighted (safe to put on a page)."""
    query = to_query(typed)
    if query is None:
        return [], 0
    conn = session.connection()
    total = conn.execute(text(f"SELECT count(*) FROM {TABLE} JOIN episodes e ON e.id = {TABLE}.rowid "
                              f"WHERE {TABLE} MATCH :q"), {"q": query}).scalar()
    weights = ", ".join(str(w) for w in COLUMNS.values())
    rows = conn.execute(text(
        f"SELECT {TABLE}.rowid, snippet({TABLE}, -1, :start, :end, '…', 14) "
        f"FROM {TABLE} JOIN episodes e ON e.id = {TABLE}.rowid "
        f"WHERE {TABLE} MATCH :q ORDER BY bm25({TABLE}, {weights}) "
        f"LIMIT :limit OFFSET :offset"),
        {"q": query, "start": START, "end": END, "limit": limit, "offset": offset}).all()
    return [(row_id, highlight(snippet)) for row_id, snippet in rows], total


def highlight(snippet: str | None) -> Markup:
    """Make a snippet safe for a page, with matched words in <mark>."""
    safe = html.escape(snippet or "")
    return Markup(safe.replace(START, "<mark>").replace(END, "</mark>"))
