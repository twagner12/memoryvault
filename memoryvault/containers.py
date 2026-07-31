"""Identify a file's real container from its magic bytes (#7).

The extension is not evidence. Sampling 4,000 `.heic` files from the corpus
found 471 (12%) that are actually JPEG — Google transcoded them on export and
kept the original name. Trusting `Path.suffix` therefore fails twice over: it
refuses EXIF writes that would have worked, and it attempts writes into genuine
HEIC that cannot work and were being swallowed as no-ops.

`supports_exif` answers one narrow question — *can piexif embed EXIF into this
container* — and is deliberately conservative. TIFF is a real EXIF format, but
piexif cannot insert into it, so it answers False. Anything this module cannot
positively identify is treated as unwritable.
"""

from enum import Enum
from pathlib import Path

# Enough for every signature below: ISO-BMFF needs 12 bytes, RIFF/WEBP needs 12.
_HEADER_BYTES = 32

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
    MP4 = "mp4"
    MOV = "mov"
    UNKNOWN = "unknown"


# Containers piexif can actually write EXIF into. Kept as a frozenset rather
# than a method on the enum so the claim is stated in exactly one place.
_EXIF_WRITABLE = frozenset({Container.JPEG, Container.WEBP})


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
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return Container.WEBP
    if header[:4] in (b"II\x2a\x00", b"MM\x00\x2a"):
        return Container.TIFF

    # ISO base media format: [4-byte size]["ftyp"][4-byte major brand]
    if header[4:8] == b"ftyp":
        brand = header[8:12]
        if brand in _HEIF_BRANDS:
            return Container.HEIF
        if brand in _MOV_BRANDS:
            return Container.MOV
        return Container.MP4

    return Container.UNKNOWN
