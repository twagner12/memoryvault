"""Shared fixtures: a real-schema database and real on-disk image files.

Both fixtures deliberately avoid hand-written SQL and synthetic byte blobs —
the database is built by memoryvault.database.Database so schema changes and
migrations are exercised, and the images are real JPEGs written by Pillow so
piexif/EXIF round-trips behave as they do in production.

The container fixtures go further: a genuine HEIC and an `.mp4` are copied
from the real corpus rather than synthesised, because the whole point of
finding #7 is that these formats behave differently from what their extension
claims. A hand-built stand-in would agree with the code under test for the
wrong reason.
"""

import io
import json
import shutil
import zipfile
from pathlib import Path

import piexif
import pytest
from PIL import Image

from memoryvault.database import Database

# Real files from Tim's corpus, chosen as the smallest of their kind so the
# suite stays fast. Verified by magic bytes, not by extension:
#   lp_image(2).heic  20 KB  ftyp:heic  — a true HEIC; piexif cannot read it
#   IMG_1671.heic     38 KB  jpeg       — Google transcoded it, kept the name
#   …752.mp4         195 KB  ftyp:mp42  — no EXIF container at all
CORPUS = Path("/home/tim/Pictures/Test_folder_files")
GENUINE_HEIC = CORPUS / "lp_image(2).heic"
JPEG_NAMED_HEIC = CORPUS / "IMG_1671.heic"
SMALL_MP4 = CORPUS / "92532505-c63f-4376-96b6-a5872a7ef752.mp4"


def _corpus_copy(src: Path, dest_dir: Path, name: str | None = None) -> Path:
    """Copy a real corpus file into a tmp dir, skipping the test if it is gone.

    The corpus is Tim's own photo library, not a checked-in fixture set, so a
    missing file is an environment fact rather than a failure to report.
    """
    if not src.exists():
        pytest.skip(f"corpus fixture missing: {src}")
    dest = dest_dir / (name or src.name)
    shutil.copy2(src, dest)
    return dest


def make_jpeg_bytes(color: str = "blue", size: tuple[int, int] = (16, 16),
                    exif: dict | None = None) -> bytes:
    """Build a real JPEG, optionally carrying EXIF."""
    img = Image.new("RGB", size, color=color)
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    data = buf.getvalue()

    if exif is not None:
        # piexif.insert writes to its third argument rather than returning the
        # bytes, so a BytesIO stands in for a temp file round-trip.
        out = io.BytesIO()
        piexif.insert(piexif.dump(exif), data, out)
        data = out.getvalue()
    return data


def make_sidecar_bytes(timestamp: int, lat: float | None = None,
                       lon: float | None = None) -> bytes:
    """Build a Google Takeout supplemental-metadata sidecar."""
    payload = {"photoTakenTime": {"timestamp": str(timestamp)}}
    if lat is not None:
        payload["geoData"] = {"latitude": lat, "longitude": lon}
    return json.dumps(payload).encode("utf-8")


@pytest.fixture
def db(tmp_path) -> Database:
    """A database created through the real schema/migration code path."""
    database = Database(tmp_path / "test.db")
    yield database
    database.close()


@pytest.fixture
def photos(tmp_path) -> Path:
    """A directory of real, distinct small JPEGs."""
    folder = tmp_path / "photos"
    folder.mkdir()
    for i, color in enumerate(["red", "green", "blue", "yellow"]):
        (folder / f"img_{i}.jpg").write_bytes(make_jpeg_bytes(color=color))
    return folder


def make_full_gps_ifd(lat_dms=((41, 1), (52, 1), (4128, 100)),
                      lon_dms=((87, 1), (37, 1), (7272, 100))) -> dict:
    """A GPS IFD carrying far more than lat/lon.

    The extra six tags are the ones finding #10 showed being destroyed by
    `exif["GPS"] = gps_ifd`. They exist here so a merge can be asserted to
    leave them alone.
    """
    return {
        piexif.GPSIFD.GPSLatitudeRef: b"N",
        piexif.GPSIFD.GPSLatitude: lat_dms,
        piexif.GPSIFD.GPSLongitudeRef: b"W",
        piexif.GPSIFD.GPSLongitude: lon_dms,
        piexif.GPSIFD.GPSAltitudeRef: 0,
        piexif.GPSIFD.GPSAltitude: (18150, 100),
        piexif.GPSIFD.GPSTimeStamp: ((14, 1), (23, 1), (11, 1)),
        piexif.GPSIFD.GPSDateStamp: b"2021:06:05",
        piexif.GPSIFD.GPSImgDirection: (12345, 100),
        piexif.GPSIFD.GPSImgDirectionRef: b"T",
    }


@pytest.fixture
def genuine_heic(tmp_path) -> Path:
    """A real HEIC — piexif raises InvalidImageDataError on it."""
    return _corpus_copy(GENUINE_HEIC, tmp_path)


@pytest.fixture
def jpeg_named_heic(tmp_path) -> Path:
    """A JPEG that Google renamed to .heic — writable despite its extension."""
    return _corpus_copy(JPEG_NAMED_HEIC, tmp_path)


@pytest.fixture
def jpeg_named_heic_undated(tmp_path) -> Path:
    """The same JPEG-as-`.heic`, with its capture date stripped.

    The corpus original already carries a DateTimeOriginal, so it exercises
    the "already present, leave it alone" path rather than the write path.
    Removing just the date tags — the bytes are otherwise untouched, and it
    is still a JPEG wearing a `.heic` name — lets a sidecar date actually be
    written, which is the behaviour finding #7 restores.
    """
    path = _corpus_copy(JPEG_NAMED_HEIC, tmp_path, name="undated.heic")
    exif = piexif.load(str(path))
    for tag in (piexif.ExifIFD.DateTimeOriginal,
                piexif.ExifIFD.DateTimeDigitized):
        exif["Exif"].pop(tag, None)
    exif["0th"].pop(piexif.ImageIFD.DateTime, None)
    piexif.insert(piexif.dump(exif), str(path))
    return path


@pytest.fixture
def small_mp4(tmp_path) -> Path:
    """A real mp4 — no EXIF container, so date can only reach it via mtime."""
    return _corpus_copy(SMALL_MP4, tmp_path)


@pytest.fixture
def jpeg_with_full_gps(tmp_path) -> Path:
    """A JPEG whose GPS IFD carries altitude, timestamp, date and direction."""
    path = tmp_path / "with_gps.jpg"
    path.write_bytes(make_jpeg_bytes(
        color="teal",
        exif={"0th": {}, "Exif": {}, "GPS": make_full_gps_ifd(), "1st": {}},
    ))
    return path


@pytest.fixture
def corrupt_exif_jpeg(tmp_path) -> Path:
    """A JPEG with a structurally valid APP1 whose TIFF payload is garbage.

    Built by writing real EXIF and then pointing the TIFF header's first-IFD
    offset far past the end of the segment. The segment length and the
    `Exif\\0\\0` signature stay intact, so piexif commits to parsing it and
    then dies on a short read — the exact shape of the malformed files that
    finding #10 showed being silently emptied.

    Milder corruptions were tried first and rejected: zeroing the byte-order
    marker, a bogus BOM, and a wrong TIFF magic number are all tolerated by
    piexif, which returns a partial dict without raising.
    """
    data = bytearray(make_jpeg_bytes(
        color="orange",
        exif={"0th": {piexif.ImageIFD.Make: b"TestCam"},
              "Exif": {}, "GPS": {}, "1st": {}},
    ))
    marker = data.find(b"Exif\x00\x00")
    assert marker != -1, "fixture setup: no APP1 Exif segment was written"
    # Layout after "Exif\0\0": BOM(2) magic(2) first_IFD_offset(4).
    data[marker + 10:marker + 14] = b"\xff\xff\xff\xff"

    path = tmp_path / "corrupt_exif.jpg"
    path.write_bytes(bytes(data))
    return path


@pytest.fixture
def make_zip(tmp_path):
    """Factory building a Takeout-shaped zip from {entry_path: bytes}."""
    counter = {"n": 0}

    def _make(files: dict[str, bytes], name: str | None = None) -> Path:
        counter["n"] += 1
        path = tmp_path / (name or f"takeout_{counter['n']}.zip")
        with zipfile.ZipFile(path, "w") as zf:
            for entry_path, data in files.items():
                zf.writestr(entry_path, data)
        return path

    return _make
