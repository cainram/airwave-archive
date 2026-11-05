"""Continue listening: where the owner stopped in each episode, and which
episodes have been played to the end.

The player bar reports its place every few seconds (and when paused). For
an episode made of several files, the place is a part plus a time within
it. Pressing Play again picks up from there. Hearing the last part to its
end marks the episode as played, and its place is forgotten.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import AudioFile, Episode, Listening, utcnow

# Less than this, at the very start, doesn't count as having started.
STARTED = 10.0
REWIND = 3.0   # resume a little before where it stopped, to catch the thread


@dataclass
class Progress:
    heard: float          # seconds heard, across all parts
    left: float | None    # seconds still to go (None if a length is unknown)
    percent: int | None


def _record(session: Session, episode_id: int) -> Listening:
    row = session.get(Listening, episode_id)
    if row is None:
        row = Listening(episode_id=episode_id, updated_at=utcnow())
        session.add(row)
    return row


def save_place(session: Session, episode: Episode, file_id: int | None,
               seconds: float, finished: bool = False) -> None:
    """Remember where listening stopped, or that it reached the end."""
    row = _record(session, episode.id)
    row.updated_at = utcnow()
    if finished:
        row.played, row.played_at = True, utcnow()
        row.file_id, row.position = None, 0.0
        return
    if file_id not in {f.id for f in episode.files}:
        return
    row.file_id, row.position = file_id, max(0.0, float(seconds))
    if row.played and (row.position >= STARTED or file_id != episode.parts[0].id):
        row.played = False      # listening again


def mark(session: Session, episodes: list[Episode], played: bool) -> None:
    """Mark episodes as played, or as not played (which also forgets where
    listening stopped, so they start from the beginning)."""
    for episode in episodes:
        if played:
            row = _record(session, episode.id)
            row.played, row.played_at, row.updated_at = True, utcnow(), utcnow()
            row.file_id, row.position = None, 0.0
        else:
            row = _record(session, episode.id)
            row.played, row.played_at, row.updated_at = False, None, utcnow()
            row.file_id, row.position = None, 0.0


def progress(episode: Episode | None) -> Progress | None:
    """How far into an unfinished episode the owner got (None if it isn't
    started, or is played)."""
    row = episode.listening if episode is not None else None
    if row is None or row.played or row.file_id is None:
        return None
    heard = 0.0
    for part in episode.parts:
        if part.id == row.file_id:
            heard += row.position
            break
        heard += part.duration_seconds or 0
    else:
        return None          # that part isn't in this episode any more
    if heard < STARTED:
        return None
    length = episode.length
    if not length:
        return Progress(heard, None, None)
    return Progress(heard, max(0.0, length - heard), min(99, int(heard / length * 100)))


def listened_at(f: AudioFile) -> int | None:
    """When this file's episode was last listened to or marked, in
    milliseconds since 1970 (UTC), for the player to tell whether the page
    or its own memory is newer."""
    row = f.episode.listening if f.episode is not None else None
    if row is None:
        return None
    return int(row.updated_at.replace(tzinfo=timezone.utc).timestamp() * 1000)


def resume_point(f: AudioFile) -> tuple[int, float] | None:
    """Where a Play button for file ``f`` should start: (file id, seconds),
    or None for the beginning. The button for the first part resumes
    anywhere in the episode; the button for a later part only within it
    (or a part after it)."""
    episode = f.episode
    if progress(episode) is None:
        return None
    row = episode.listening
    ids = [p.id for p in episode.parts]
    if f.id not in ids or row.file_id not in ids[ids.index(f.id):]:
        return None
    return row.file_id, max(0.0, row.position - REWIND)


def continue_listening(session: Session, limit: int = 8) -> list[tuple[AudioFile, Progress]]:
    """Unfinished episodes, most recently played first: (first part, progress)."""
    found = []
    rows = session.scalars(
        select(Episode).join(Listening).where(Listening.played.is_(False))
        .order_by(Listening.updated_at.desc()).limit(limit * 3)).all()
    for episode in rows:
        done = progress(episode)
        first = next((p for p in episode.parts if not p.missing), None)
        if done is not None and first is not None:
            found.append((first, done))
        if len(found) == limit:
            break
    return found


def time_left(done: Progress | None) -> str:
    """'23 min left', '1 hr 5 min left'..."""
    if done is None:
        return ""
    if done.left is None:
        return "started"
    minutes = round(done.left / 60)
    if minutes < 1:
        return "less than a minute left"
    hours, minutes = divmod(minutes, 60)
    if not hours:
        return f"{minutes} min left"
    return f"{hours} hr {minutes} min left" if minutes else f"{hours} hr left"
