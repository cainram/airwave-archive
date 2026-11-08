"""The database: where Airwave Archive remembers what it found.

The database is a single SQLite file (``airwave.db``) in the data folder.
It holds information *about* the recordings, never the audio itself, so it
stays small even for very large libraries.

Tables:

- ``episodes``: one row per broadcast, with its radio details (series,
  title, air date, cast...). Like an index card for each broadcast.
- ``audio_files``: one row per audio file in the library. Each file belongs
  to an episode. One episode can have several files ("parts"), such as
  a long show saved as Hour 1, Hour 2, Hour 3.
- ``folder_art``: picture files found in library folders (cover.jpg...).
- ``series_art``: pictures the owner uploaded for a series.
- ``inbox_items``: uploaded files waiting in the inbox, with their details.
- ``feeds``: podcast feeds (RSS) the owner made, for podcast apps.
- ``feed_items``: the episodes in each hand-picked feed.
- ``scan_runs``: one row per scan, recording what changed.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import Engine, ForeignKey, Index, UniqueConstraint, create_engine, event, or_
from sqlalchemy.orm import (
    DeclarativeBase, Mapped, joinedload, mapped_column, relationship, selectinload,
)

log = logging.getLogger(__name__)

DB_FILENAME = "airwave.db"

# Bumped whenever the table layout changes, so the app can tell an older
# database apart and update it (see ``_migrate``).
#   1: Phase 2, audio files and scans
#   2: Phase 4a, episodes, and files can be marked missing
#   3: Phase 4b, episode details filled in from tags (no layout change)
#   4: Phase 4e, files can be parts of one episode (part_number)
#   5: Phase 4e, a group's notes gather all parts' descriptions (no layout change)
#   6: Phase 5b, pictures (art_hash on files, folder_art table)
#   7: Phase 5c, uploaded series pictures (series_art table)
#   8: Phase 6b, search index (episode_search, a full-text search table)
#   9: Phase 7a, the upload inbox (inbox_items table)
#  10: Phase 8, where you stopped listening, and what's played (listening table)
#  11: Phase 11, a permanent ID for each episode (episodes.uid), for exports
#  12: Phase 11b, inbox files from an export zip keep its details (inbox_items.extra_json)
#  13: an episode can be taken off "Continue listening" (listening.dismissed)
#  14: Phase 12a, podcast feeds (feeds table)
#  15: Phase 12b, hand-picked feeds (feed_items table)
#  16: Phase 12c, a feed's author, own picture and order (feeds.author, art_hash, newest_first)
#  17: suggested groups the owner dismissed (group_dismissals table)
#  18: suggested groups the owner has seen, for the Library's notice (group_seen table)
SCHEMA_VERSION = 18

# Where a value came from. Manual edits always win over automatic ones.
MANUAL = "manual"
TAGS = "tags"   # read from the file's own tags


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

    # A permanent ID that stays the same on any Airwave server (given the
    # first time the episode is exported; kept when it's imported), so a
    # server can tell it already has an episode.
    uid: Mapped[str | None]   # (unique: see ix_episodes_uid in _migrate)

    created_at: Mapped[datetime]   # UTC
    updated_at: Mapped[datetime]   # UTC

    files: Mapped[list[AudioFile]] = relationship(back_populates="episode")
    # Where listening stopped, and whether it's been played (None: never started).
    listening: Mapped[Listening | None] = relationship(
        lazy="selectin", cascade="all, delete-orphan", passive_deletes=True)

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

    @property
    def parts(self) -> list[AudioFile]:
        """This episode's files in playing order (just one for most)."""
        return sorted(self.files, key=lambda f: (f.part_number or 0, natural_key(f.name)))

    @property
    def length(self) -> float | None:
        """Total length of all parts, in seconds; None if any is unknown."""
        lengths = [f.duration_seconds for f in self.files]
        return None if None in lengths or not lengths else sum(lengths)


def natural_key(name: str) -> list:
    """Sort file names the way people expect: 'Hour 2' before 'Hour 10'."""
    return [int(piece) if piece.isdigit() else piece.lower()
            for piece in re.split(r"(\d+)", name)]


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
    # For an episode made of several files: 1, 2, 3... in playing order.
    # None for a file that is a whole episode by itself.
    part_number: Mapped[int | None]

    # The picture stored inside the file, if any: the fingerprint of the
    # app's copy in the data folder (see app/artwork.py).
    art_hash: Mapped[str | None]

    # True if the file wasn't found by the last scan but is kept because its
    # episode has edits.
    missing: Mapped[bool] = mapped_column(default=False)
    missing_since: Mapped[datetime | None]   # UTC

    # Each path appears once. This also creates an index (fast lookup by path).
    __table_args__ = (UniqueConstraint("folder", "name", name="uq_audio_files_path"),)


class FolderArt(Base):
    """A picture file in a library folder (cover.jpg, folder.png...), as
    last seen by a scan. Only the best one in each folder is kept."""

    __tablename__ = "folder_art"

    folder: Mapped[str] = mapped_column(primary_key=True)   # "" = the top level
    name: Mapped[str]
    size_bytes: Mapped[int]
    mtime_ns: Mapped[int]
    art_hash: Mapped[str | None]   # None if it couldn't be read as a picture


class SeriesArt(Base):
    """A picture the owner uploaded for a series. It's kept (in the data
    folder) under the series name; renaming every episode of the series
    moves it to the new name."""

    __tablename__ = "series_art"

    series: Mapped[str] = mapped_column(primary_key=True)
    art_hash: Mapped[str]
    uploaded_at: Mapped[datetime]   # UTC


class Listening(Base):
    """Where the owner stopped listening to an episode, and whether it's
    been played to the end. Removed along with its episode."""

    __tablename__ = "listening"

    episode_id: Mapped[int] = mapped_column(
        ForeignKey("episodes.id", ondelete="CASCADE"), primary_key=True)
    # The part playing when listening stopped, and how far into it (seconds).
    file_id: Mapped[int | None]
    position: Mapped[float] = mapped_column(default=0.0)
    played: Mapped[bool] = mapped_column(default=False)
    played_at: Mapped[datetime | None]   # UTC
    updated_at: Mapped[datetime] = mapped_column(index=True)   # UTC
    # Taken off the home page's "Continue listening" row (the place is
    # kept). Listening again puts it back.
    dismissed: Mapped[bool] = mapped_column(default=False)


class InboxItem(Base):
    """A file waiting in the inbox (uploaded, or copied there), with the
    details worked out for it and any fixes the owner made. It goes into
    the library only when the owner approves it."""

    __tablename__ = "inbox_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(unique=True)   # the file's name in the inbox
    size_bytes: Mapped[int]
    duration_seconds: Mapped[float | None]
    added_at: Mapped[datetime]   # UTC
    series: Mapped[str | None]
    episode_number: Mapped[str | None]
    title: Mapped[str | None]
    air_year: Mapped[int | None]
    air_month: Mapped[int | None]
    air_day: Mapped[int | None]
    # More details that came with the file (from an export zip or Airwave
    # tags): ID, part, air time, station, cast, notes... as JSON. Saved on
    # its episode when it's added.
    extra_json: Mapped[str | None]

    @property
    def extra(self) -> dict:
        return json.loads(self.extra_json) if self.extra_json else {}

    @extra.setter
    def extra(self, value: dict) -> None:
        self.extra_json = json.dumps(value, sort_keys=True) if value else None


class Feed(Base):
    """A podcast feed (RSS) for podcast apps: a series' episodes, or
    hand-picked ones, at a private address. Feeds start as drafts; the address only works once
    the feed is published. Deleting a feed never touches the recordings."""

    __tablename__ = "feeds"

    id: Mapped[int] = mapped_column(primary_key=True)
    # The secret part of the feed's address (long and unguessable, since
    # podcast apps can't log in).
    token: Mapped[str] = mapped_column(unique=True)
    title: Mapped[str]                   # the podcast's name in the app
    description: Mapped[str | None]
    author: Mapped[str | None]           # None: "Airwave Archive"
    # A picture uploaded for this feed (kept with the series pictures);
    # None: its series' picture.
    art_hash: Mapped[str | None]
    # Podcast apps list it newest first (an "episodic" show) instead of
    # oldest first (a "serial" one, the default).
    newest_first: Mapped[bool] = mapped_column(default=False)
    series: Mapped[str | None]           # a series feed: every episode of it
    #                                      (None: hand-picked, see FeedItem)
    published: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime]   # UTC; episodes already there count as added then
    updated_at: Mapped[datetime]   # UTC
    published_at: Mapped[datetime | None]   # UTC

    picks: Mapped[list[FeedItem]] = relationship(
        cascade="all, delete-orphan", passive_deletes=True)


class FeedItem(Base):
    """An episode in a hand-picked feed. Removed along with its feed or its
    episode (so episodes deleted from the library drop out of feeds)."""

    __tablename__ = "feed_items"

    feed_id: Mapped[int] = mapped_column(
        ForeignKey("feeds.id", ondelete="CASCADE"), primary_key=True)
    episode_id: Mapped[int] = mapped_column(
        ForeignKey("episodes.id", ondelete="CASCADE"), primary_key=True, index=True)
    added_at: Mapped[datetime]   # UTC: its date in podcast apps (never an air date)


# Load a file's episode and all that episode's parts along with it.
WITH_PARTS = joinedload(AudioFile.episode).selectinload(Episode.files)
# Files shown in lists: whole episodes, and the first part of grouped ones.
FIRST_PARTS = or_(AudioFile.part_number.is_(None), AudioFile.part_number == 1)


class GroupDismissal(Base):
    """A suggested group (files that looked like parts of one broadcast)
    that the owner dismissed, so it isn't suggested again. Kept by a
    fingerprint of the folder and file names (see ``app/grouping.py``)."""

    __tablename__ = "group_dismissals"

    signature: Mapped[str] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(default="")   # what it was called, for reference
    dismissed_at: Mapped[datetime]   # UTC


class GroupSeen(Base):
    """A suggested group the owner has seen on the Suggested groups page,
    so the Library page only mentions new ones."""

    __tablename__ = "group_seen"

    signature: Mapped[str] = mapped_column(primary_key=True)
    seen_at: Mapped[datetime]   # UTC


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
    # Version 4: files can be parts of one episode.
    if "part_number" not in columns:
        conn.exec_driver_sql("ALTER TABLE audio_files ADD COLUMN part_number INTEGER")
    # Version 6: pictures.
    if "art_hash" not in columns:
        conn.exec_driver_sql("ALTER TABLE audio_files ADD COLUMN art_hash VARCHAR")
        # Files were read before pictures were looked for: the next scan
        # reads every file again, once.
        conn.exec_driver_sql("UPDATE audio_files SET mtime_ns = 0")
    # Version 11: a permanent ID for each episode.
    if "uid" not in _columns(conn, "episodes"):
        conn.exec_driver_sql("ALTER TABLE episodes ADD COLUMN uid VARCHAR")
    conn.exec_driver_sql("CREATE UNIQUE INDEX IF NOT EXISTS ix_episodes_uid ON episodes (uid)")
    # Version 12: inbox files can bring more details with them.
    if "extra_json" not in _columns(conn, "inbox_items"):
        conn.exec_driver_sql("ALTER TABLE inbox_items ADD COLUMN extra_json VARCHAR")
    # Version 13: episodes can be taken off "Continue listening".
    if "dismissed" not in _columns(conn, "listening"):
        conn.exec_driver_sql(
            "ALTER TABLE listening ADD COLUMN dismissed BOOLEAN NOT NULL DEFAULT 0")
    # Version 16: a feed's author, own picture and order.
    feed_columns = _columns(conn, "feeds")
    if "author" not in feed_columns:
        conn.exec_driver_sql("ALTER TABLE feeds ADD COLUMN author VARCHAR")
    if "art_hash" not in feed_columns:
        conn.exec_driver_sql("ALTER TABLE feeds ADD COLUMN art_hash VARCHAR")
    if "newest_first" not in feed_columns:
        conn.exec_driver_sql(
            "ALTER TABLE feeds ADD COLUMN newest_first BOOLEAN NOT NULL DEFAULT 0")


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

    from app import search   # here, to avoid a circular import
    search.keep_index_up_to_date()

    Base.metadata.create_all(engine)   # creates any tables that don't exist yet
    with engine.begin() as conn:
        _migrate(conn)
        conn.exec_driver_sql(search.CREATE)
        if version < 8:
            search.rebuild_index(conn)   # version 8: index what's there already
    if 0 < version < 5:
        _fill_all_from_tags(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql(f"PRAGMA user_version={SCHEMA_VERSION}")
    return engine


def _fill_all_from_tags(engine: Engine) -> None:
    """Fill in details from tags for files already in the database, when
    the way that's done has changed (versions 3 and 5). New and changed
    files are filled in as they're scanned. Typed details are never
    touched."""
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from app.episodes import fill_from_tags   # here, to avoid a circular import

    with Session(engine) as session:
        for f in session.scalars(select(AudioFile).where(AudioFile.episode_id.is_not(None))):
            fill_from_tags(f.episode, f)
        session.commit()
