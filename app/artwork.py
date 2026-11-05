"""Pictures for episodes and series ("artwork").

Pictures come from two places in the library, which are only ever read:

- a picture stored inside an audio file (most tag formats can hold one);
- a picture file in a folder, such as ``cover.jpg`` or ``folder.jpg``.

The app keeps its own smaller copy of each picture in the data folder
(``artwork/``), so pages load quickly, even on a phone. Each copy is named
after a fingerprint of the picture (a "hash"), so a picture shared by many
files, like a show's logo, is stored only once.

Pictures can also be uploaded for a series. Those are kept in the data
folder (``uploaded-artwork/``), never in the library, and can't be made
again by a scan, so they belong in backups.

Which picture is shown:

- for a series: an uploaded one, else its folder's, else one from inside
  its files;
- for an episode: the one inside its (first) file, else its series'
  uploaded one, else its folder's.
"""

from __future__ import annotations

import base64
import hashlib
import io
import logging
import re
from pathlib import Path, PurePosixPath

from PIL import Image, ImageOps

log = logging.getLogger(__name__)

# Picture files recognised in a folder, best first ("cover.jpg" beats
# "folder.png"). Any capitals are fine.
FOLDER_IMAGE_NAMES = ("cover", "folder", "front", "poster", "album", "artwork")
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")

ART_SIZE = 600                     # longest side of the saved copy, in pixels
MAX_PICTURE_BYTES = 30_000_000     # bigger "pictures" are ignored
HASH_PATTERN = re.compile(r"^[0-9a-f]{20}$")

# Refuse absurdly large images (a damaged or hostile file could claim to be
# enormous and use up all the memory).
Image.MAX_IMAGE_PIXELS = 100_000_000


def folder_image_rank(filename: str) -> int | None:
    """How good a folder picture this file name is (0 = best), or None if
    it isn't one."""
    path = PurePosixPath(filename.lower())
    if path.suffix not in IMAGE_EXTENSIONS or path.stem not in FOLDER_IMAGE_NAMES:
        return None
    return FOLDER_IMAGE_NAMES.index(path.stem) * 10 + IMAGE_EXTENSIONS.index(path.suffix)


def embedded_picture(audio) -> bytes | None:
    """The picture stored inside an audio file (opened with mutagen), if
    any. The front cover is preferred when there are several."""
    tags = getattr(audio, "tags", None)
    try:
        # MP3, WAV, AIFF (ID3 tags)
        if tags is not None and hasattr(tags, "getall"):
            pictures = tags.getall("APIC")
            if pictures:
                best = next((p for p in pictures if p.type == 3), pictures[0])   # 3 = front cover
                return bytes(best.data)
        # FLAC
        pictures = getattr(audio, "pictures", None)
        if pictures:
            best = next((p for p in pictures if p.type == 3), pictures[0])
            return bytes(best.data)
        if tags is None:
            return None
        # M4A / M4B
        if "covr" in tags and tags["covr"]:
            return bytes(tags["covr"][0])
        # Ogg and Opus keep FLAC-style pictures, written as text
        if "metadata_block_picture" in tags:
            from mutagen.flac import Picture
            return bytes(Picture(base64.b64decode(tags["metadata_block_picture"][0])).data)
    except Exception:
        pass   # a damaged picture just means no picture
    return None


def art_path(art_dir: Path, art_hash: str) -> Path:
    """Where the saved copy of a picture lives (in folders of 256, so no
    folder gets too full)."""
    return art_dir / art_hash[:2] / f"{art_hash}.jpg"


def save_picture(data: bytes | None, art_dir: Path) -> str | None:
    """Save a smaller copy of a picture, unless one is saved already.
    Returns its fingerprint, or None if it isn't a usable picture."""
    if not data or len(data) > MAX_PICTURE_BYTES:
        return None
    art_hash = hashlib.sha1(data).hexdigest()[:20]
    target = art_path(art_dir, art_hash)
    if target.exists():
        return art_hash
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.draft("RGB", (ART_SIZE, ART_SIZE))   # quicker for big JPEGs
            # Phone photos are often stored sideways, with a note saying
            # which way is up: turn them the right way round.
            image = ImageOps.exif_transpose(image).convert("RGBA")
            image.thumbnail((ART_SIZE, ART_SIZE))
            # JPEG has no see-through parts: put the picture on white.
            flat = Image.new("RGB", image.size, "white")
            flat.paste(image, mask=image.getchannel("A"))
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(".part")
            flat.save(temporary, "JPEG", quality=85, optimize=True)
            temporary.replace(target)   # appears all at once, never half-written
    except Exception as error:
        log.info("Not a usable picture (%s)", error)
        return None
    return art_hash


def folder_and_parents(folder: str) -> list[str]:
    """'Shows/Suspense/1943' -> ['Shows/Suspense/1943', 'Shows/Suspense', 'Shows'].
    The top of the library only counts for files that are right there: a
    picture there isn't taken as the picture of every series."""
    found = [folder]
    while "/" in folder:
        folder = folder.rpartition("/")[0]
        found.append(folder)
    return found


def folder_art(folder_pictures: dict[str, str], folder: str) -> str | None:
    """The picture for a folder: its own, or the nearest folder above it
    that has one. ``folder_pictures`` maps folder -> picture fingerprint."""
    for candidate in folder_and_parents(folder):
        if candidate in folder_pictures:
            return folder_pictures[candidate]
    return None
