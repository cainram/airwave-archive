"""Podcast feeds (RSS) of the library's episodes, for podcast apps.

An RSS feed is a web address that lists episodes in a standard form that
podcast apps read: the app shows them and plays the recordings straight
from this server. The feed follows a series: episodes added to the series
later appear in it too.

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
  they arrived in the library. The real air date goes in the description.
- An episode in several parts (Hour 1, Hour 2) is one item per part,
  since a podcast item holds one recording.

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

from app.db import AudioFile, Episode, Feed, utcnow
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


def feed_episodes(session: Session, feed: Feed) -> list[Episode]:
    """The feed's episodes, in air-date order, with their files."""
    if feed.series is None:
        return []
    return list(session.scalars(
        select(Episode).where(Episode.series == feed.series)
        .options(selectinload(Episode.files))
        .order_by(*AIR_ORDER, Episode.id)))


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


def items(feed: Feed, episodes: list[Episode]) -> list[Item]:
    """The feed's recordings, oldest air date first. Missing files are
    left out."""
    found: list[Item] = []
    already = 0     # items there when the feed was made: a second apart
    for episode in episodes:
        parts = [f for f in episode.parts if not f.missing]
        title = None
        for f in parts:
            title = title or episode_title(episode, f)
            number = parts.index(f) + 1
            name = title if len(parts) == 1 else f"{title} (part {number} of {len(parts)})"
            if f.first_seen <= feed.created_at:
                added = feed.created_at + timedelta(seconds=already)
                already += 1
            else:
                added = f.first_seen + timedelta(seconds=number - 1)
            found.append(Item(f, episode, name, number if len(parts) > 1 else None,
                              len(parts), added))
    return found


def newest_first(found: list[Item]) -> list[Item]:
    """The order podcast apps list them in: most recently added first."""
    return sorted(found, key=lambda item: item.added, reverse=True)


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
    episodes = feed_episodes(session, feed)
    found = items(feed, episodes)
    series = one_series(session, feed.series) if feed.series is not None else None
    picture = series["art"] if series else None
    it = f"{{{ITUNES}}}"

    root = ET.Element("rss", {"version": "2.0"})
    channel = _add(root, "channel")
    description = feed.description or (
        f"{feed.series}, from Airwave Archive." if feed.series else "From Airwave Archive.")
    _add(channel, "title", feed.title)
    _add(channel, "link", f"{base}/feeds/{feed.id}")
    _add(channel, "description", description)
    _add(channel, "language", "en-us")
    _add(channel, "generator", "Airwave Archive")
    _add(channel, "lastBuildDate", _when(utcnow()))
    _add(channel, it + "author", "Airwave Archive")
    _add(channel, it + "summary", description)
    _add(channel, it + "explicit", "false")
    _add(channel, it + "type", "episodic")
    _add(channel, it + "block", "Yes")    # never list it in Apple's directory
    if picture:
        url = f"{base}/art/{picture}.jpg"
        _add(channel, it + "image", href=url)
        image = _add(channel, "image")
        _add(image, "url", url)
        _add(image, "title", feed.title)
        _add(image, "link", f"{base}/feeds/{feed.id}")

    for item in newest_first(found):      # as podcast feeds usually are
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
        if episode.episode_number and episode.episode_number.isdigit():
            _add(entry, it + "episode", str(int(episode.episode_number)))
        if f.art_hash and f.art_hash != picture:      # a picture inside the file
            _add(entry, it + "image", href=f"{base}/art/{f.art_hash}.jpg")
    ET.indent(root)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def in_feed(session: Session, feed: Feed, f: AudioFile) -> bool:
    """Whether a recording belongs to this feed."""
    episode = session.get(Episode, f.episode_id) if f.episode_id else None
    return episode is not None and feed.series is not None and episode.series == feed.series


def qr_svg(text: str) -> str:
    """A QR code (for a phone's camera) as an SVG picture."""
    out = io.BytesIO()
    segno.make(text, error="m").save(out, kind="svg", scale=5, border=3, xmldecl=False,
                                     svgns=True, dark="#000000", light="#ffffff",
                                     svgclass="qr", title="QR code for this feed")
    return out.getvalue().decode()
