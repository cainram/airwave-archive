"""Podcast feeds (RSS) of the library's episodes, for podcast apps.

An RSS feed is a web address that lists episodes in a standard form that
podcast apps read: the app shows them and plays the recordings straight
from this server. Two kinds:

- A series feed follows a series: episodes added to it later appear too.
- A hand-picked feed holds the episodes ticked for it (more can be added,
  and some removed, later). Episodes deleted from the library drop out.

- Each feed has a private address, with a long random "token" in it
  (podcast apps can't log in, so the address itself is the key).
- A feed starts as a draft: its address only works once it's published.
- The recordings are sent as they are in the library; the feed supplies
  the names and details. Nothing in the library is changed.
- Dates: podcast apps need a full date and time for every item, and sort
  by it. Air dates are never invented to fill that in (a ground rule), so
  an item's date is when it was added to the feed: episodes already in
  the series when the feed was made count as added then, a second apart
  in air-date order (so apps list them in that order); later ones, when
  they arrived in the library. Episodes picked together for a hand-picked
  feed are likewise a second apart in air-date order. The real air date goes in the description.
- An episode in several parts (Hour 1, Hour 2) is one item per part,
  since a podcast item holds one recording.
- Feeds are "serial" shows: podcast apps list them oldest first, in
  air-date order (and each episode's parts in order). A feed can be
  switched to newest first (an "episodic" show); parts still read 1, 2, 3. Episode numbers
  aren't sent as Apple's episode numbers (apps would sort serial shows by
  them, and many episodes have none); they're in the description.

The feed is written like the ones the owner's iPhone already reads
(standard RSS 2.0 plus Apple's "itunes:" tags).
"""

from __future__ import annotations

import io
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import PurePosixPath
from xml.etree import ElementTree as ET

import segno
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db import AudioFile, Episode, Feed, FeedItem, utcnow
from app.episodes import format_air_date
from app.series import AIR_ORDER, one_series

ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"
ET.register_namespace("itunes", ITUNES)

# File types Apple's Podcasts app is documented to play. Others (WAV,
# FLAC...) may or may not play there, so the feed page points them out.
APPLE_PLAYS = {".mp3", ".m4a", ".aac"}
AUDIO_TYPES = {
    ".mp3": "audio/mpeg", ".m4a": "audio/x-m4a", ".m4b": "audio/x-m4a",
    ".aac": "audio/aac", ".flac": "audio/flac", ".ogg": "audio/ogg",
    ".oga": "audio/ogg", ".opus": "audio/ogg", ".wav": "audio/wav",
    ".wma": "audio/x-ms-wma", ".aiff": "audio/aiff", ".aif": "audio/aiff",
}


def new_token() -> str:
    return secrets.token_urlsafe(24)    # 32 letters, digits, - and _


def make_series_feed(session: Session, series: str, title: str,
                     description: str | None) -> Feed:
    now = utcnow()
    feed = Feed(token=new_token(), title=title, description=description, series=series,
                published=False, created_at=now, updated_at=now)
    session.add(feed)
    session.flush()
    return feed


def make_picked_feed(session: Session, episodes: list[Episode], title: str,
                     description: str | None) -> Feed:
    """A hand-picked feed of these episodes."""
    now = utcnow()
    feed = Feed(token=new_token(), title=title, description=description, series=None,
                published=False, created_at=now, updated_at=now)
    session.add(feed)
    session.flush()
    add_episodes(session, feed, episodes)
    return feed


def add_episodes(session: Session, feed: Feed, episodes: list[Episode]) -> int:
    """Add episodes to a hand-picked feed (ones already in it are skipped),
    after the ones there, in air-date order: each part a second after the
    one before (podcast apps sort by dates to the second). Returns how
    many episodes were added."""
    have = {pick.episode_id for pick in feed.picks}
    last = [pick.added_at + timedelta(seconds=PART_SECONDS) for pick in feed.picks]
    when = max([utcnow(), *last])
    new = sorted((e for e in {e.id: e for e in episodes}.values() if e.id not in have),
                 key=air_key)
    for episode in new:
        feed.picks.append(FeedItem(episode_id=episode.id, added_at=when))
        when += timedelta(seconds=max(len(episode.files), 1))
    if new:
        feed.updated_at = utcnow()
    return len(new)


# Room left after an episode's date for its parts (each a second apart).
PART_SECONDS = 100


def remove_episodes(feed: Feed, episode_ids: set[int]) -> int:
    """Take episodes out of a hand-picked feed (never out of the library)."""
    gone = [pick for pick in feed.picks if pick.episode_id in episode_ids]
    for pick in gone:
        feed.picks.remove(pick)
    if gone:
        feed.updated_at = utcnow()
    return len(gone)


def air_key(e: Episode):
    """Sorts episodes in air-date order, like AIR_ORDER does in the database."""
    number = e.episode_number or ""
    return (e.air_year is None, e.air_year or 0, e.air_month is None, e.air_month or 0,
            e.air_day is None, e.air_day or 0, e.air_time is None, e.air_time or "",
            not number, int(number) if number.isdigit() else 0, number, e.title or "", e.id)


def feed_episodes(session: Session, feed: Feed) -> list[Episode]:
    """The feed's episodes, in air-date order (a hand-picked feed's: in the
    order they were added, each batch in air-date order), with their files."""
    if feed.series is not None:
        return list(session.scalars(
            select(Episode).where(Episode.series == feed.series)
            .options(selectinload(Episode.files))
            .order_by(*AIR_ORDER, Episode.id)))
    return list(session.scalars(
        select(Episode).join(FeedItem, FeedItem.episode_id == Episode.id)
        .where(FeedItem.feed_id == feed.id)
        .options(selectinload(Episode.files))
        .order_by(FeedItem.added_at, *AIR_ORDER, Episode.id)))


def feed_items(session: Session, feed: Feed) -> list[Item]:
    """The feed's recordings, in the order podcast apps list them."""
    added = None
    if feed.series is None:
        added = {pick.episode_id: pick.added_at for pick in session.scalars(
            select(FeedItem).where(FeedItem.feed_id == feed.id))}
    return in_order(items(feed, feed_episodes(session, feed), added), feed.newest_first)


def feed_picture(session: Session, feed: Feed, found: list[Item]) -> str | None:
    """The podcast's picture: one uploaded for it, else its series' (for a
    hand-picked feed, that of the first episode's series that has one)."""
    if feed.art_hash:
        return feed.art_hash
    names = [feed.series] if feed.series is not None else list(dict.fromkeys(
        item.episode.series for item in found if item.episode.series))
    for name in names:
        series = one_series(session, name)
        if series and series["art"]:
            return series["art"]
    return None


@dataclass
class Item:
    """One recording in the feed."""
    file: AudioFile
    episode: Episode
    title: str
    part: int | None      # with `parts`, for episodes in several parts
    parts: int
    added: datetime       # UTC: when it counts as added to the feed

    @property
    def suffix(self) -> str:
        return PurePosixPath(self.file.name).suffix.lower()


def episode_title(episode: Episode, f: AudioFile) -> str:
    """The episode's title in Airwave Archive (else its file's)."""
    return episode.title or f.tag_title or PurePosixPath(f.name).stem


def items(feed: Feed, episodes: list[Episode],
          added_at: dict[int, datetime] | None = None) -> list[Item]:
    """The feed's recordings, in the episodes' order. Missing files are
    left out. `added_at`: when each episode was added (hand-picked feeds).

    Each episode's parts are a second apart, so they keep their order in
    podcast apps whichever way the feed is listed: part 1 first."""
    found: list[Item] = []
    already = 0     # seconds used by recordings there when the feed was made
    for episode in episodes:
        parts = [f for f in episode.parts if not f.missing]
        if not parts:
            continue
        title = episode_title(episode, parts[0])
        if added_at is not None:
            start = added_at[episode.id]
        elif min(f.first_seen for f in parts) <= feed.created_at:
            start = feed.created_at + timedelta(seconds=already)
            already += len(parts)
        else:
            start = min(f.first_seen for f in parts)
        for number, f in enumerate(parts, 1):
            name = title if len(parts) == 1 else f"{title} (part {number} of {len(parts)})"
            # Newest first lists later dates first, so part 1 gets the latest.
            offset = len(parts) - number if feed.newest_first else number - 1
            found.append(Item(f, episode, name, number if len(parts) > 1 else None,
                              len(parts), start + timedelta(seconds=offset)))
    return found


def in_order(found: list[Item], newest_first: bool = False) -> list[Item]:
    """The order podcast apps list them in: oldest first (in air-date
    order, each episode's parts 1, 2, 3...), then ones added later; or
    all that backwards, but each episode's parts still 1, 2, 3."""
    return sorted(found, key=lambda item: item.added, reverse=newest_first)


def odd_formats(found: list[Item]) -> dict[str, int]:
    """File types in the feed that Apple's Podcasts app may not play: how many of each."""
    counts: dict[str, int] = {}
    for item in found:
        if item.suffix not in APPLE_PLAYS:
            kind = item.suffix.lstrip(".").upper() or "no extension"
            counts[kind] = counts.get(kind, 0) + 1
    return counts


def description_lines(feed: Feed, episode: Episode) -> list[str]:
    """What goes in an item's description: the real air date, and the
    episode's details (its series too, unless the feed is that series)."""
    lines = []
    aired = format_air_date(episode)
    lines.append(f"Aired: {aired}" if aired else "Air date unknown")
    series = episode.series if episode.series != feed.series else None
    for label, value in (("Series", series), ("Episode", episode.episode_number),
                         ("Station", episode.network), ("Host", episode.host),
                         ("Cast", episode.cast)):
        if value:
            lines.append(f"{label}: {value}")
    if episode.notes:
        lines.append(episode.notes)
    return lines


def _when(utc: datetime) -> str:
    return format_datetime(utc.replace(tzinfo=timezone.utc))


def _html(lines: list[str]) -> str:
    """Lines as simple HTML paragraphs (podcast apps show descriptions as HTML)."""
    from html import escape
    return "".join(f"<p>{escape(line).replace(chr(10), '<br>')}</p>" for line in lines)


def _add(parent: ET.Element, tag: str, text: str | None = None, **attrs) -> ET.Element:
    element = ET.SubElement(parent, tag, {k.replace("_", "-"): v for k, v in attrs.items()})
    if text is not None:
        element.text = text
    return element


def rss(session: Session, feed: Feed, base: str) -> bytes:
    """The feed, as RSS (XML). `base`: this server's address as the podcast
    app reached it, like http://192.168.1.20:8000."""
    found = feed_items(session, feed)
    picture = feed_picture(session, feed, found)
    it = f"{{{ITUNES}}}"

    root = ET.Element("rss", {"version": "2.0"})
    channel = _add(root, "channel")
    description = feed.description or (
        f"{feed.series}, from Airwave Archive." if feed.series else "Episodes from Airwave Archive.")
    _add(channel, "title", feed.title)
    _add(channel, "link", f"{base}/feeds/{feed.id}")
    _add(channel, "description", description)
    _add(channel, "language", "en-us")
    _add(channel, "generator", "Airwave Archive")
    _add(channel, "lastBuildDate", _when(utcnow()))
    _add(channel, it + "author", feed.author or "Airwave Archive")
    _add(channel, it + "summary", description)
    _add(channel, it + "explicit", "false")
    # "serial": a show to hear in order, so Podcasts lists the oldest first
    # and plays on to the next (owner's choice, 2026-11-07); "episodic":
    # newest first.
    _add(channel, it + "type", "episodic" if feed.newest_first else "serial")
    _add(channel, it + "block", "Yes")    # never list it in Apple's directory
    if picture:
        url = f"{base}/art/{picture}.jpg"
        _add(channel, it + "image", href=url)
        image = _add(channel, "image")
        _add(image, "url", url)
        _add(image, "title", feed.title)
        _add(image, "link", f"{base}/feeds/{feed.id}")

    for item in found:
        f, episode = item.file, item.episode
        entry = _add(channel, "item")
        _add(entry, "title", item.title)
        lines = description_lines(feed, episode)
        _add(entry, "description", _html(lines))
        _add(entry, it + "summary", "\n".join(lines))
        _add(entry, "enclosure", url=f"{base}/feeds/{feed.token}/audio/{f.id}{item.suffix}",
             length=str(f.size_bytes or 0),
             type=AUDIO_TYPES.get(item.suffix, "application/octet-stream"))
        _add(entry, "guid", f"airwave-file-{f.id}", isPermaLink="false")
        _add(entry, "pubDate", _when(item.added))
        if f.duration_seconds:
            _add(entry, it + "duration", str(round(f.duration_seconds)))
        if f.art_hash and f.art_hash != picture:      # a picture inside the file
            _add(entry, it + "image", href=f"{base}/art/{f.art_hash}.jpg")
    ET.indent(root)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def in_feed(session: Session, feed: Feed, f: AudioFile) -> bool:
    """Whether a recording belongs to this feed."""
    if f.episode_id is None:
        return False
    if feed.series is None:
        return session.get(FeedItem, (feed.id, f.episode_id)) is not None
    episode = session.get(Episode, f.episode_id)
    return episode is not None and episode.series == feed.series


def qr_svg(text: str) -> str:
    """A QR code (for a phone's camera) as an SVG picture."""
    out = io.BytesIO()
    segno.make(text, error="m").save(out, kind="svg", scale=5, border=3, xmldecl=False,
                                     svgns=True, dark="#000000", light="#ffffff",
                                     svgclass="qr", title="QR code for this feed")
    return out.getvalue().decode()
