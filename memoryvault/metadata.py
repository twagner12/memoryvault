"""Read/write EXIF date and GPS metadata, and parse Google Takeout JSON sidecars.

Two rules hold throughout this module, both from findings #7 and #10:

- Capability is decided by the file's *bytes*, never its extension. See
  `memoryvault.containers`.
- A write that cannot be performed correctly raises. It never falls back to
  substituting an empty EXIF dict, because that silently deletes the metadata
  the write was called to preserve.
"""

import json
import os
from datetime import datetime
from pathlib import Path

import piexif

from memoryvault.containers import detect_container, supports_exif

# Extensions that *hint* at an EXIF-capable file. Kept only for cheap
# pre-filtering where no bytes are available; `can_have_exif` is the
# authority. `.heic/.heif/.tiff/.tif` were removed here as part of #7 —
# piexif cannot write any of them, and pretending otherwise produced 17,164
# HEIC files whose writes were verified no-ops.
EXIF_EXTENSIONS = {".jpg", ".jpeg", ".webp"}


class UnparseableExifError(Exception):
    """The file has an EXIF segment that piexif cannot parse.

    Raised instead of silently replacing it with an empty one. Malformed
    maker notes are common, and the old fallback turned "I cannot read this"
    into "I have deleted this."
    """


def can_have_exif(path: Path) -> bool:
    """True when EXIF can actually be embedded into this file's container.

    Reads the file's magic bytes. An unreadable or missing file answers
    False — there is genuinely nowhere to write — but the caller is expected
    to have established existence for anything it intends to modify.
    """
    try:
        return supports_exif(detect_container(path))
    except OSError:
        return False


def read_exif(path: Path) -> dict | None:
    """Read EXIF data from an image file. Returns None if not readable.

    This is the *read* path, where a missing or unparseable segment is a
    legitimate answer of "no metadata". Writes use `_load_exif_for_write`,
    which distinguishes the two.
    """
    if not can_have_exif(path):
        return None
    try:
        return piexif.load(str(path))
    except Exception:
        return None


def _load_exif_for_write(path: Path) -> dict:
    """Load EXIF ahead of modifying it, refusing to guess on failure.

    A file with no EXIF at all loads as empty IFDs and is safe to write. A
    file whose EXIF exists but cannot be parsed raises: overwriting it would
    discard tags we were never able to see.
    """
    if not can_have_exif(path):
        raise UnparseableExifError(
            f"{path.name}: {detect_container(path).value} cannot carry EXIF"
        )
    try:
        return piexif.load(str(path))
    except Exception as exc:
        raise UnparseableExifError(f"{path.name}: {exc}") from exc


def get_exif_date(path: Path) -> str | None:
    """Extract the date taken from EXIF. Returns ISO format string or None."""
    exif = read_exif(path)
    if not exif:
        return None

    # Try DateTimeOriginal first, then DateTimeDigitized, then DateTime
    for tag in (piexif.ExifIFD.DateTimeOriginal,
                piexif.ExifIFD.DateTimeDigitized):
        val = exif.get("Exif", {}).get(tag)
        if val:
            try:
                date_str = val.decode("utf-8") if isinstance(val, bytes) else val
                dt = datetime.strptime(date_str, "%Y:%m:%d %H:%M:%S")
                return dt.isoformat()
            except (ValueError, UnicodeDecodeError):
                continue

    # Fall back to 0th IFD DateTime
    val = exif.get("0th", {}).get(piexif.ImageIFD.DateTime)
    if val:
        try:
            date_str = val.decode("utf-8") if isinstance(val, bytes) else val
            dt = datetime.strptime(date_str, "%Y:%m:%d %H:%M:%S")
            return dt.isoformat()
        except (ValueError, UnicodeDecodeError):
            # A malformed DateTime is the same answer as no DateTime: this
            # file cannot tell us when it was taken.
            return None

    return None


def _dms_to_decimal(dms_tuple, ref: str) -> float:
    """Convert EXIF GPS DMS tuple ((deg, 1), (min, 1), (sec, 100)) to decimal degrees."""
    degrees = dms_tuple[0][0] / dms_tuple[0][1]
    minutes = dms_tuple[1][0] / dms_tuple[1][1]
    seconds = dms_tuple[2][0] / dms_tuple[2][1]
    decimal = degrees + minutes / 60 + seconds / 3600
    if ref in ("S", "W"):
        decimal = -decimal
    return decimal


def _decimal_to_dms(decimal: float) -> tuple:
    """Convert decimal degrees to EXIF GPS DMS format."""
    is_negative = decimal < 0
    decimal = abs(decimal)
    degrees = int(decimal)
    minutes = int((decimal - degrees) * 60)
    seconds = int(((decimal - degrees) * 60 - minutes) * 60 * 10000)
    return ((degrees, 1), (minutes, 1), (seconds, 10000)), is_negative


def get_exif_gps(path: Path) -> tuple[float, float] | None:
    """Extract GPS coordinates from EXIF. Returns (latitude, longitude) or None."""
    exif = read_exif(path)
    if not exif:
        return None

    gps = exif.get("GPS", {})
    if not gps:
        return None

    lat_data = gps.get(piexif.GPSIFD.GPSLatitude)
    lat_ref = gps.get(piexif.GPSIFD.GPSLatitudeRef)
    lon_data = gps.get(piexif.GPSIFD.GPSLongitude)
    lon_ref = gps.get(piexif.GPSIFD.GPSLongitudeRef)

    if not all([lat_data, lat_ref, lon_data, lon_ref]):
        return None

    try:
        if isinstance(lat_ref, bytes):
            lat_ref = lat_ref.decode("ascii")
        if isinstance(lon_ref, bytes):
            lon_ref = lon_ref.decode("ascii")
        lat = _dms_to_decimal(lat_data, lat_ref)
        lon = _dms_to_decimal(lon_data, lon_ref)
        return (lat, lon)
    except (ValueError, ZeroDivisionError, IndexError):
        return None


def get_exif_offset(path: Path) -> str | None:
    """Read OffsetTimeOriginal — the zone EXIF's wall clock is relative to."""
    exif = read_exif(path)
    if not exif:
        return None

    for tag in (piexif.ExifIFD.OffsetTimeOriginal,
                piexif.ExifIFD.OffsetTimeDigitized):
        val = exif.get("Exif", {}).get(tag)
        if val:
            try:
                return val.decode("ascii") if isinstance(val, bytes) else val
            except UnicodeDecodeError:
                continue
    return None


def write_exif_date(path: Path, local_dt: datetime, offset: str | None = None):
    """Write a local wall-clock capture time, plus the zone it belongs to.

    `local_dt` must be naive. EXIF DateTimeOriginal carries no zone, so an
    aware datetime would be silently rendered in whatever zone it happened to
    hold — the ±14 h error of finding #9. The zone is written separately as
    OffsetTimeOriginal, where it is unambiguous and recoverable.

    Raises UnparseableExifError rather than overwriting EXIF it cannot read.
    """
    if local_dt.tzinfo is not None:
        raise ValueError(
            "write_exif_date requires a naive local datetime; "
            "pass the wall clock and give the zone via `offset`"
        )

    date_str = local_dt.strftime("%Y:%m:%d %H:%M:%S").encode("utf-8")
    exif = _load_exif_for_write(path)

    exif.setdefault("Exif", {})[piexif.ExifIFD.DateTimeOriginal] = date_str
    exif.setdefault("Exif", {})[piexif.ExifIFD.DateTimeDigitized] = date_str
    exif.setdefault("0th", {})[piexif.ImageIFD.DateTime] = date_str

    if offset:
        encoded = offset.encode("ascii")
        exif["Exif"][piexif.ExifIFD.OffsetTimeOriginal] = encoded
        exif["Exif"][piexif.ExifIFD.OffsetTimeDigitized] = encoded

    _insert(exif, path)


def write_exif_gps(path: Path, lat: float, lon: float):
    """Merge GPS coordinates into EXIF, preserving the rest of the GPS IFD.

    Only the four lat/lon keys are touched. The previous implementation
    assigned `exif["GPS"] = gps_ifd`, which discarded GPSAltitude,
    GPSTimeStamp, GPSDateStamp and GPSImgDirection on every merge (#10).

    Raises UnparseableExifError rather than overwriting EXIF it cannot read.
    """
    exif = _load_exif_for_write(path)

    lat_dms, lat_neg = _decimal_to_dms(lat)
    lon_dms, lon_neg = _decimal_to_dms(lon)

    gps = exif.setdefault("GPS", {})
    gps[piexif.GPSIFD.GPSLatitudeRef] = b"S" if lat_neg else b"N"
    gps[piexif.GPSIFD.GPSLatitude] = lat_dms
    gps[piexif.GPSIFD.GPSLongitudeRef] = b"W" if lon_neg else b"E"
    gps[piexif.GPSIFD.GPSLongitude] = lon_dms

    _insert(exif, path)


def _insert(exif: dict, path: Path):
    """Serialise and embed, leaving the file untouched if either step fails.

    `piexif.dump` can reject an IFD that loaded cleanly — an out-of-range
    value, or a tag whose type it will not re-encode. Doing the dump before
    opening the file for write keeps a failure from truncating the original.

    The rewrite preserves mtime. `piexif.insert` replaces the file in place,
    so without this every EXIF write restamps it with the moment of the write:
    a photo whose own EXIF reads 2018 would report as modified today, and
    anything that sorts or groups by mtime would file it under the ingest date.
    Callers that know the capture instant overwrite the mtime afterwards; the
    rest keep whatever the file already carried. Preserving here rather than in
    the callers matters because a sidecar with both a date and GPS writes
    twice, and the second write would otherwise undo the first one's mtime.
    """
    exif_bytes = piexif.dump(exif)
    before = path.stat()
    piexif.insert(exif_bytes, str(path))
    os.utime(path, (before.st_atime, before.st_mtime))


def has_metadata(path: Path) -> dict:
    """Check what metadata a file has. Returns dict with has_exif_date and has_exif_gps."""
    return {
        "has_exif_date": get_exif_date(path) is not None,
        "has_exif_gps": get_exif_gps(path) is not None,
    }


# --- Google Takeout JSON sidecar parsing ---

def find_takeout_sidecar(media_path: Path) -> Path | None:
    """Find the Google Takeout JSON sidecar for a media file.

    Google Takeout uses several naming patterns:
      - photo.jpg.supplemental-metadata.json
      - photo.jpg.json (older format)
    """
    candidates = [
        media_path.parent / f"{media_path.name}.supplemental-metadata.json",
        media_path.parent / f"{media_path.name}.json",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def parse_sidecar_json(data: dict) -> dict:
    """Extract the raw UTC epoch and GPS from decoded sidecar JSON.

    The timestamp stays an epoch. Takeout records UTC; EXIF wants camera-local
    wall-clock time, and converting here — with no location in hand — is what
    produced the ±14 h drift of finding #9. `memoryvault.phototime` does the
    conversion later, when the file and its siblings are available.
    """
    result = {"utc_epoch": None, "lat": None, "lon": None}

    for time_key in ("photoTakenTime", "creationTime"):
        time_data = data.get(time_key)
        if time_data and "timestamp" in time_data:
            try:
                ts = int(time_data["timestamp"])
            except (TypeError, ValueError):
                continue
            if ts > 0:
                result["utc_epoch"] = ts
                break

    for geo_key in ("geoData", "geoDataExif"):
        geo = data.get(geo_key)
        if geo:
            lat = geo.get("latitude", 0)
            lon = geo.get("longitude", 0)
            # Google writes 0,0 to mean "no data".
            if lat != 0 or lon != 0:
                result["lat"] = lat
                result["lon"] = lon
                break

    return result


def parse_takeout_sidecar(json_path: Path) -> dict:
    """Parse a Google Takeout JSON sidecar from disk.

    Returns dict with keys `utc_epoch`, `lat` and `lon`. An unreadable file
    yields all-None — the caller records that as an unmatched sidecar rather
    than treating it as absence of metadata.
    """
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return {"utc_epoch": None, "lat": None, "lon": None}

    return parse_sidecar_json(data)


# NOTE: `merge_metadata_from_sidecar` and `merge_metadata_from_file` used to
# live here. Both wrote EXIF directly and swallowed every failure, which made
# them a second, unaccountable path around the outcome tables. Metadata now
# flows through `memoryvault.ingest.apply_sidecar`, which is the only place
# allowed to decide — see `merge_metadata_between_files` for the file-to-file
# entry point.
