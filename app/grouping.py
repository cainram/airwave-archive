"""Suggested groups: files that are probably parts of one broadcast.

A long show is often saved as several files whose names are the same
except for a part marker: "Hour 1", "Hr 2", "Part 3", "Pt 4", "(1 of 2)",
"Side A". This finds such sets so the owner can group them in one go:
in the library ("Suggested groups" page, and a notice on the Library page
when a scan turns up new ones), and among files waiting in the inbox
(shown together there, so they go into the library as one episode).
Nothing is ever grouped without the owner's approval, and a suggestion
the owner dismisses isn't offered again.

A set is suggested only if:

- its files are in the same folder (uploads are filed into their series'
  folder, so one upload's parts end up together; the inbox counts as one
  folder), with the same name apart from the part number, and the same
  kind of file;
- the numbers run 1, 2, 3... with none missing or doubled (and match the
  "of N" total, if the names have one);
- the files' dates (date tags in the library; the dates worked out in the
  inbox), where they have one, agree;
- none of the files is already part of a group, or missing.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import PurePath

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import AudioFile, GroupDismissal, GroupSeen, natural_key, utcnow
from app.parts import set_parts

# A part marker anywhere in a name (not inside a longer word):
#   "Hour 1", "Hr. 2", "Part 3", "pt_4", "Part 1 of 3"
#   "(1 of 2)", "1 of 2"
#   "Side A", "Side 2"
MARKER = re.compile(r"""
    (?<![a-z0-9])
    (?:
        (?P<word>hour|hr|part|pt)\.?[\s_-]*(?P<n>\d{1,2})(?:[\s_]*of[\s_]*(?P<of>\d{1,2}))?
      | \(?(?P<n2>\d{1,2})[\s_]+of[\s_]+(?P<of2>\d{1,2})\)?
      | side[\s_-]*(?P<side>[a-d]|\d)
    )
    (?![a-z0-9])
""", re.I | re.X)

MOST_PARTS = 24        # more than this in a set isn't one broadcast
SHOWN = 100            # suggestions on the page at once
INBOX = "\x00inbox"    # the inbox's "folder" in fingerprints (no real folder is named that)


@dataclass
class Marker:
    before: str        # the name before the marker
    after: str         # and after it (without the file's extension)
    kind: str          # "hour", "part", "of", "side"...
    number: int
    total: int | None  # from "1 of 3"


def find_marker(name: str) -> Marker | None:
    """The part marker in a file name, if it has exactly one."""
    stem = PurePath(name).stem
    found = list(MARKER.finditer(stem))
    if len(found) != 1:
        return None
    m = found[0]
    if m["word"]:
        kind, number, total = m["word"].lower(), int(m["n"]), m["of"]
    elif m["n2"]:
        kind, number, total = "of", int(m["n2"]), m["of2"]
    else:
        side = m["side"].lower()
        kind, number, total = "side", int(side) if side.isdigit() else ord(side) - ord("a") + 1, None
    return Marker(stem[:m.start()], stem[m.end():], kind, number,
                  int(total) if total else None)


def label_for(marker: Marker) -> str:
    """A name for the whole broadcast: the file name without its marker.
    "Coast to Coast - Mar 01 2010 - Hour 1" -> "Coast to Coast - Mar 01 2010"."""
    text = f"{marker.before} {marker.after}"
    text = re.sub(r"\(\s*\)|\[\s*\]", " ", text)
    text = re.sub(r"\s*[-_]\s*[-_]\s*", " - ", text)
    text = re.sub(r"\s+", " ", text).strip(" -_,.")
    return text or "(no name)"


def signature(folder: str, names: list[str]) -> str:
    """A short fingerprint of a set of files, for remembering dismissals.
    If a file is added to the set later, it's a new suggestion."""
    text = folder + "\n" + "\n".join(sorted(name.lower() for name in names))
    return hashlib.sha256(text.encode()).hexdigest()[:32]


@dataclass
class Suggestion:
    signature: str
    folder: str
    label: str
    file_ids: list[int]          # in part order
    files: list[AudioFile] = field(default_factory=list)   # filled in to show

    @property
    def length(self) -> float | None:
        lengths = [f.duration_seconds for f in self.files]
        return None if not lengths or None in lengths else sum(lengths)

    @property
    def typed_details(self) -> list[AudioFile]:
        """Files after the first whose episodes have details the owner
        typed: those details go when they join the group."""
        return [f for f in self.files[1:] if f.episode is not None and f.episode.has_manual_edits]


def _same_date(dates: list[str | None]) -> bool:
    found = {d for d in dates if d}
    return len(found) <= 1


def find_sets(entries, dismissed: set[str] = frozenset()) -> list[Suggestion]:
    """Likely groups among ``entries``: (id, folder, name, date or None)
    for files that are on their own. In folder and name order."""
    sets: dict[tuple, list[tuple[Marker, tuple]]] = {}
    for entry in entries:
        marker = find_marker(entry[2])
        if marker is None:
            continue
        key = (entry[1], marker.before.lower().strip(), marker.kind,
               marker.after.lower().strip(), PurePath(entry[2]).suffix.lower(), marker.total)
        sets.setdefault(key, []).append((marker, entry))

    found: list[Suggestion] = []
    for key, members in sets.items():
        numbers = sorted(marker.number for marker, _ in members)
        total = key[5]
        if not 2 <= len(members) <= MOST_PARTS or numbers != list(range(1, len(members) + 1)):
            continue
        if total is not None and total != len(members):
            continue
        if not _same_date([entry[3] for _, entry in members]):
            continue
        members.sort(key=lambda pair: pair[0].number)
        folder = key[0]
        names = [entry[2] for _, entry in members]
        sig = signature(folder, names)
        # Dismissed here, or already in the inbox before they were added.
        if sig in dismissed or signature(INBOX, names) in dismissed:
            continue
        found.append(Suggestion(sig, folder, label_for(members[0][0]),
                                [entry[0] for _, entry in members]))
    found.sort(key=lambda s: (natural_key(s.folder), natural_key(s.label)))
    return found


def dismissed_signatures(session: Session) -> set[str]:
    return set(session.scalars(select(GroupDismissal.signature)))


def suggestions(session: Session, include_dismissed: bool = False) -> list[Suggestion]:
    """Every likely group in the library, in folder and name order."""
    # Files that are on their own: not missing, and not already in a group.
    rows = session.execute(
        select(AudioFile.id, AudioFile.folder, AudioFile.name, AudioFile.tag_date)
        .where(AudioFile.missing.is_(False), AudioFile.part_number.is_(None),
               AudioFile.episode_id.is_not(None))
    ).all()
    entries = [(r.id, r.folder, r.name, (r.tag_date or "").strip()[:10] or None) for r in rows]
    return find_sets(entries, set() if include_dismissed else dismissed_signatures(session))


def unseen(session: Session) -> int:
    """How many library suggestions the owner hasn't looked at yet (for
    the notice on the Library page after a scan)."""
    seen = set(session.scalars(select(GroupSeen.signature)))
    return sum(1 for s in suggestions(session) if s.signature not in seen)


def mark_seen(session: Session, shown: list[Suggestion]) -> None:
    """The owner has seen these suggestions (on the Suggested groups page)."""
    seen = set(session.scalars(select(GroupSeen.signature)))
    for s in shown:
        if s.signature not in seen:
            session.add(GroupSeen(signature=s.signature, seen_at=utcnow()))


def load_files(session: Session, chosen: list[Suggestion]) -> None:
    """Fetch the files of suggestions about to be shown or grouped."""
    ids = [i for s in chosen for i in s.file_ids]
    by_id = {f.id: f for f in session.scalars(select(AudioFile).where(AudioFile.id.in_(ids)))}
    for s in chosen:
        s.files = [by_id[i] for i in s.file_ids]


def approve(session: Session, signatures: set[str]) -> int:
    """Group the suggestions the owner ticked (if they're still
    suggested: nothing that changed since the page was shown is grouped).
    Each group keeps the first part's episode and its details. Returns
    how many groups were made."""
    chosen = [s for s in suggestions(session) if s.signature in signatures]
    load_files(session, chosen)
    for s in chosen:
        set_parts(session, s.files[0].episode, s.files)
        session.flush()
    return len(chosen)


def dismiss(session: Session, sig: str, label: str = "") -> None:
    """Don't suggest this set of files again."""
    if session.get(GroupDismissal, sig) is None:
        session.add(GroupDismissal(signature=sig, label=label[:300], dismissed_at=utcnow()))
