"""The database: where Airwave Archive remembers what it found.

The database is a single SQLite file (``airwave.db``) in the data folder.
It holds information *about* the recordings, never the audio itself, so it
stays small even for very large libraries.

Two tables so far:

- ``audio_files``: one row per audio file in the library.
- ``scan_runs``: one row per scan, recording what changed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import Engine, UniqueConstraint, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

DB_FILENAME = "airwave.db"

# Bumped whenever the table layout changes, so a future version of the app
# can tell an old database apart and update it.
SCHEMA_VERSION = 1


class Base(DeclarativeBase):
    pass


class AudioFile(Base):
    """One audio file in the library, as last seen by a scan."""

    __tablename__ = "audio_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Where the file is. ``folder`` is relative to the library root, with
    # "/" between folder names ("" means the top level).
    folder: Mapped[str]
    name: Mapped[str]

    # Size and last-modified time let a re-scan tell whether the file changed
    # without opening it.
    size_bytes: Mapped[int]
    mtime_ns: Mapped[int]

    duration_seconds: Mapped[float | None]   # None if the length couldn't be read
    bitrate: Mapped[int | None]              # bits per second, e.g. 64000

    # Common tags, read from the file's own labels (ID3 and similar).
    tag_title: Mapped[str | None]
    tag_artist: Mapped[str | None]
    tag_album: Mapped[str | None]
    tag_date: Mapped[str | None]
    tag_genre: Mapped[str | None]
    tag_track: Mapped[str | None]
    tag_comment: Mapped[str | None]
    # Every readable text tag, as a JSON list of [name, value] pairs.
    tags_json: Mapped[str | None]

    first_seen: Mapped[datetime]   # UTC
    last_read: Mapped[datetime]    # UTC: when the file's contents were last read

    # Each path appears once. This also creates an index (fast lookup by path).
    __table_args__ = (UniqueConstraint("folder", "name", name="uq_audio_files_path"),)


class ScanRun(Base):
    """A record of one scan: when it ran and what it found."""

    __tablename__ = "scan_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime]   # UTC
    seconds: Mapped[float]
    added: Mapped[int] = mapped_column(default=0)
    changed: Mapped[int] = mapped_column(default=0)
    removed: Mapped[int] = mapped_column(default=0)
    unchanged: Mapped[int] = mapped_column(default=0)
    # True if the library folder couldn't be found. Nothing is removed then.
    library_missing: Mapped[bool] = mapped_column(default=False)


def utcnow() -> datetime:
    """The current time in UTC, without time-zone info (how SQLite stores it)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def make_engine(db_path: Path) -> Engine:
    """Open (or create) the database file and make sure the tables exist."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{db_path}")

    @event.listens_for(engine, "connect")
    def _sqlite_settings(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        # WAL mode: pages can keep reading while a scan is saving changes.
        cursor.execute("PRAGMA journal_mode=WAL")
        # Safe with WAL, and much faster than the default for big scans.
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql(f"PRAGMA user_version={SCHEMA_VERSION}")
    return engine
