"""The database: where Airwave Archive remembers what it found.

The database is a single SQLite file (``airwave.db``) in the data folder.
It holds information *about* the recordings, never the audio itself, so it
stays small even for very large libraries.

Tables:

- ``episodes``: one row per broadcast, with its radio details (series,
  title, air date, cast...). Like an index card for each broadcast.
- ``audio_files``: one row per audio file in the library. Each file belongs
  to an episode. (Later, one episode can have several files.)
- ``scan_runs``: one row per scan, recording what changed.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import Engine, ForeignKey, Index, UniqueConstraint, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

log = logging.getLogger(__name__)

DB_FILENAME = "airwave.db"

# Bumped whenever the table layout changes, so the app can tell an older
# database apart and update it (see ``_migrate``).
#   1: Phase 2, audio files and scans
#   2: Phase 4a, episodes, and files can be marked missing
SCHEMA_VERSION = 2

# Where a value came from. Manual edits always win over automatic ones.
MANUAL = "manual"


class Base(DeclarativeBase):
    pass


class Episode(Base):
    """One broadcast and its radio details. Every detail is optional."""

    __tablename__ = "episodes"

    id: Mapped[int] = mapped_column(primary_key=True)
    series: Mapped[str | None]
    episode_number: Mapped[str | None]   # text, since some are like "A-12"
    title: Mapped[str | None]
    network: Mapped[str | None]          # station or network, e.g. "CBS"
    host: Mapped[str | None]
    cast: Mapped[str | None]
    notes: Mapped[str | None]

    # The original air date, stored in parts so a partly known date is never
    # filled in with made-up parts. A day needs a month; a month needs a year.
    air_year: Mapped[int | None]
    air_month: Mapped[int | None]
    air_day: Mapped[int | None]
    air_time: Mapped[str | None]       # "20:00" (24-hour clock); needs a full date
    air_timezone: Mapped[str | None]   # e.g. "America/New_York"; None = not known

    # Where each detail came from, as JSON: {"cast": "manual", ...}.
    sources_json: Mapped[str | None]

    created_at: Mapped[datetime]   # UTC
    updated_at: Mapped[datetime]   # UTC

    files: Mapped[list[AudioFile]] = relationship(back_populates="episode")

    __table_args__ = (
        Index("ix_episodes_series", "series"),
        Index("ix_episodes_air_date", "air_year", "air_month", "air_day"),
    )

    @property
    def sources(self) -> dict[str, str]:
        return json.loads(self.sources_json) if self.sources_json else {}

    @sources.setter
    def sources(self, value: dict[str, str]) -> None:
        self.sources_json = json.dumps(value, sort_keys=True) if value else None

    @property
    def has_manual_edits(self) -> bool:
        return MANUAL in self.sources.values()


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

    # The episode this file belongs to.
    episode_id: Mapped[int | None] = mapped_column(ForeignKey("episodes.id"), index=True)
    episode: Mapped[Episode | None] = relationship(back_populates="files", lazy="joined")

    # True if the file wasn't found by the last scan but is kept because its
    # episode has edits.
    missing: Mapped[bool] = mapped_column(default=False)
    missing_since: Mapped[datetime | None]   # UTC

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


def _columns(conn, table: str) -> set[str]:
    return {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}


def _migrate(conn) -> None:
    """Bring an older database up to the current layout. Each step checks
    first, so it's safe to run more than once."""
    columns = _columns(conn, "audio_files")
    # Version 2: files belong to episodes, and can be marked missing.
    if "episode_id" not in columns:
        conn.exec_driver_sql(
            "ALTER TABLE audio_files ADD COLUMN episode_id INTEGER REFERENCES episodes(id)")
    if "missing" not in columns:
        conn.exec_driver_sql(
            "ALTER TABLE audio_files ADD COLUMN missing BOOLEAN NOT NULL DEFAULT 0")
    if "missing_since" not in columns:
        conn.exec_driver_sql("ALTER TABLE audio_files ADD COLUMN missing_since DATETIME")
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_audio_files_episode_id ON audio_files (episode_id)")
    # (Files without an episode get one at the end of the next scan.)


def make_engine(db_path: Path) -> Engine:
    """Open (or create) the database file and make sure it's up to date."""
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

    with engine.connect() as conn:
        version = conn.exec_driver_sql("PRAGMA user_version").scalar()
        if 0 < version < SCHEMA_VERSION:
            # Keep a copy of the old database before changing its layout.
            backup = db_path.with_name(f"{db_path.name}.before-v{SCHEMA_VERSION}")
            if not backup.exists():
                conn.exec_driver_sql("VACUUM INTO ?", (str(backup),))
                log.warning("Updating database to version %s; backup saved as %s",
                            SCHEMA_VERSION, backup)

    Base.metadata.create_all(engine)   # creates any tables that don't exist yet
    with engine.begin() as conn:
        _migrate(conn)
        conn.exec_driver_sql(f"PRAGMA user_version={SCHEMA_VERSION}")
    return engine
