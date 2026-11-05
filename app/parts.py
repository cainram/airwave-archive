"""Episodes made of several files ("parts").

A long broadcast is often saved as several files: "Hour 1", "Hour 2", and
so on. Grouping them makes one episode with one set of details, which plays
straight through. Each file is still remembered on its own, and the group
can be split up again at any time.

Grouping is something the owner does by hand, so it counts as a manual
edit: if a part goes missing from the library, the group is kept.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import MANUAL, AudioFile, Episode, natural_key, utcnow
from app.episodes import fill_from_tags
from app.scanner import _new_episode

NEARBY = 15   # files offered on each side of the current one


def nearby_files(session: Session, f: AudioFile) -> list[AudioFile]:
    """Files that could be parts of the same broadcast as ``f``: its
    neighbours in the same folder (by name), plus its current parts."""
    same_folder = select(AudioFile).where(AudioFile.folder == f.folder)
    before = session.scalars(same_folder.where(AudioFile.name < f.name)
                             .order_by(AudioFile.name.desc()).limit(NEARBY)).all()
    after = session.scalars(same_folder.where(AudioFile.name >= f.name)
                            .order_by(AudioFile.name).limit(NEARBY + 1)).all()
    found = {row.id: row for row in [*before, *after]}
    if f.episode is not None:
        found.update({row.id: row for row in f.episode.files})
    return sorted(found.values(), key=lambda row: (row.folder, natural_key(row.name)))


def renumber(episode: Episode) -> None:
    """Number an episode's files 1, 2, 3... in file-name order. A single
    file needs no number, and is no longer a group."""
    files = sorted(episode.files, key=lambda row: natural_key(row.name))
    sources = episode.sources
    if len(files) > 1:
        for number, row in enumerate(files, start=1):
            row.part_number = number
        sources["parts"] = MANUAL
    else:
        for row in files:
            row.part_number = None
        sources.pop("parts", None)
    episode.sources = sources
    episode.updated_at = utcnow()


def set_parts(session: Session, episode: Episode, chosen: list[AudioFile]) -> None:
    """Make ``chosen`` the parts of ``episode``, in file-name order.

    - The episode keeps its own details.
    - Files that were parts but aren't chosen now become episodes of their
      own again, filled in from their tags.
    - Chosen files that belonged to other episodes leave them. An episode
      left with no files is deleted (its details with it: the page that
      offers the choice warns about this).
    """
    chosen_ids = {row.id for row in chosen}
    for row in list(episode.files):
        if row.id not in chosen_ids:
            row.part_number = None
            row.episode = _new_episode()
            fill_from_tags(row.episode, row)

    emptied: list[Episode] = []
    regrouped: list[Episode] = []
    for row in chosen:
        old = row.episode
        if old is not episode:
            row.episode = episode
            if old is None:
                continue
            if old.files:
                regrouped.append(old)
            else:
                emptied.append(old)
    for old in regrouped:
        renumber(old)
    for old in emptied:
        session.delete(old)

    renumber(episode)
    # Details from tags come from the first part (the others are often
    # just "Hour 2" and so on).
    fill_from_tags(episode, episode.parts[0])
