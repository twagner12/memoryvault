"""Identify a file's real container from its magic bytes (#7).

The extension is not evidence. Measured across the 12,924-file shakedown vault,
4,771 files (37%) are not the format their name claims:

    .png    4,593 →  4,238 JPEG (92%),  355 PNG
    .heic   3,086 →  2,553 HEIF,        533 JPEG (17%)
    .mp4    1,288 →  1,243 MOV,          45 MP4
    .mov      469 →    289 MOV,         180 MP4

PNG-holding-JPEG is now the dominant case — 4,238 files, a third of the corpus,
where the original review only knew about `.heic`. Google transcodes on export
and keeps the original name, in both directions. Trusting `Path.suffix`
therefore fails twice over: it refuses EXIF writes that would have worked, and
it attempts writes into genuine HEIC that cannot work and were being swallowed
as no-ops.

`supports_exif` answers one narrow question — *can piexif embed EXIF into this
container* — and is deliberately conservative. TIFF is a real EXIF format, but
piexif cannot insert into it, so it answers False. Anything this module cannot
positively identify is treated as unwritable.
"""

import struct
from enum import Enum
from pathlib import Path

# Enough to walk a couple of leading ISO-BMFF atoms: a `wide` placeholder plus
# the header of whatever follows it needs 16, and padding atoms can push that
# further. Every fixed signature below needs at most 12.
_HEADER_BYTES = 64

# How many leading atoms to walk before giving up. Real files reach `ftyp` or
# `mdat` within one or two; a longer chain is a malformed file, not a format we
# want to guess at.
_MAX_ATOMS = 4

# Atoms carrying no identity — padding and placeholders. Walked past.
_SKIPPABLE_ATOMS = frozenset({b"wide", b"free", b"skip"})

# A file whose first meaningful atom is one of these, with no `ftyp` in front,
# is the pre-2004 QuickTime layout: 96 `.mp4` files in the shakedown vault open
# `[wide][mdat]` and carry no `ftyp` at all.
_QUICKTIME_ATOMS = frozenset({b"mdat", b"moov"})

# ISO-BMFF major brands that denote a still image rather than a video track.
_HEIF_BRANDS = frozenset({
    b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx",
    b"mif1", b"msf1", b"avif", b"avis",
})

# QuickTime — the .MOV half of an iPhone Live Photo.
_MOV_BRANDS = frozenset({b"qt  "})


class Container(Enum):
    JPEG = "jpeg"
    WEBP = "webp"
    TIFF = "tiff"
    HEIF = "heif"
    PNG = "png"
    GIF = "gif"
    MP4 = "mp4"
    MOV = "mov"
    UNKNOWN = "unknown"


# Containers piexif can actually write EXIF into. Kept as a frozenset rather
# than a method on the enum so the claim is stated in exactly one place.
_EXIF_WRITABLE = frozenset({Container.JPEG, Container.WEBP})

# Reading is a weaker requirement than writing, and conflating them cost us:
# `_merge_from_source_file` gated on the WRITE set, so a discarded HEIC
# duplicate was never read for the date it might carry — 18,676 files in the
# vault. piexif cannot reach HEIF, but pillow-heif can, and it is a declared
# dependency.
_EXIF_READABLE = frozenset({Container.JPEG, Container.WEBP, Container.HEIF})


def supports_exif_read(container: Container) -> bool:
    """True when this container's EXIF can be READ. Superset of supports_exif."""
    return container in _EXIF_READABLE


def supports_exif(container: Container) -> bool:
    """True only where piexif can embed EXIF.

    TIFF is absent on purpose: it carries EXIF natively, but `piexif.insert`
    raises `InvalidImageDataError` on it, and finding #7 is precisely about
    not claiming a capability we do not have.
    """
    return container in _EXIF_WRITABLE


def detect_container(path: Path) -> Container:
    """Return the container `path` actually is, ignoring its extension.

    Raises OSError if the file cannot be read — a missing file is a caller
    bug, and reporting it as UNKNOWN would let it be silently misrouted.
    """
    with open(path, "rb") as f:
        header = f.read(_HEADER_BYTES)

    return _classify(header)


def detect_container_from_bytes(header: bytes) -> Container:
    """Same classification against an in-memory prefix.

    Ingest already holds small entries in memory; this avoids a temp-file
    round-trip just to read twelve bytes.
    """
    return _classify(header)


def _classify(header: bytes) -> Container:
    if header[:3] == b"\xff\xd8\xff":
        return Container.JPEG
    if header[:8] == b"\x89PNG\r\n\x1a\n":
        return Container.PNG
    if header[:6] in (b"GIF87a", b"GIF89a"):
        return Container.GIF
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return Container.WEBP
    if header[:4] in (b"II\x2a\x00", b"MM\x00\x2a"):
        return Container.TIFF

    return _classify_atoms(header)


def _classify_atoms(header: bytes) -> Container:
    """Walk the leading ISO-BMFF / QuickTime atoms for a verdict.

    Every atom is `[4-byte big-endian size][4-byte type]`. Testing
    `header[4:8] == b"ftyp"` at a fixed offset only works when `ftyp` is
    literally first, which the modern layout guarantees and the older
    QuickTime one does not: 96 `.mp4` files in the shakedown vault open with an
    8-byte `wide` placeholder followed straight by `mdat` and carry no `ftyp`
    at all. All 96 were classified UNKNOWN, which is how a video ended up in
    the branch that decides whether EXIF can be embedded.

    The walk identifies; it never guesses. Anything unrecognised — an unknown
    atom type, a size too small to advance the cursor, or a chain that runs
    past the bytes we read — stays UNKNOWN, which is treated as unwritable.
    """
    offset = 0
    for _ in range(_MAX_ATOMS):
        if offset + 8 > len(header):
            return Container.UNKNOWN

        size = struct.unpack(">I", header[offset:offset + 4])[0]
        atom = header[offset + 4:offset + 8]

        if atom == b"ftyp":
            brand = header[offset + 8:offset + 12]
            if brand in _HEIF_BRANDS:
                return Container.HEIF
            if brand in _MOV_BRANDS:
                return Container.MOV
            return Container.MP4

        if atom in _QUICKTIME_ATOMS:
            return Container.MOV

        if atom not in _SKIPPABLE_ATOMS:
            return Container.UNKNOWN

        # A size below the 8-byte header would leave the cursor where it is,
        # or move it backwards. Either way the file is malformed and the walk
        # must not spin on it.
        if size < 8:
            return Container.UNKNOWN
        offset += size

    return Container.UNKNOWN
