"""Read/write EXIF date and GPS metadata, and parse Google Takeout JSON sidecars."""

import json
import struct
from datetime import datetime, timezone
from pathlib import Path

import piexif


# EXIF-capable image extensions
EXIF_EXTENSIONS = {".jpg", ".jpeg", ".tiff", ".tif", ".heic", ".heif", ".webp"}


def can_have_exif(path: Path) -> bool:
    return path.suffix.lower() in EXIF_EXTENSIONS


def read_exif(path: Path) -> dict | None:
    """Read EXIF data from an image file. Returns None if not readable."""
    if not can_have_exif(path):
        return None
    try:
        return piexif.load(str(path))
    except Exception:
        return None


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
            pass

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


def write_exif_date(path: Path, date_iso: str):
    """Write a date into the EXIF DateTimeOriginal field."""
    dt = datetime.fromisoformat(date_iso)
    date_str = dt.strftime("%Y:%m:%d %H:%M:%S").encode("utf-8")

    try:
        exif = piexif.load(str(path))
    except Exception:
        exif = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}}

    exif.setdefault("Exif", {})[piexif.ExifIFD.DateTimeOriginal] = date_str
    exif.setdefault("0th", {})[piexif.ImageIFD.DateTime] = date_str

    exif_bytes = piexif.dump(exif)
    piexif.insert(exif_bytes, str(path))


def write_exif_gps(path: Path, lat: float, lon: float):
    """Write GPS coordinates into EXIF."""
    try:
        exif = piexif.load(str(path))
    except Exception:
        exif = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}}

    lat_dms, lat_neg = _decimal_to_dms(lat)
    lon_dms, lon_neg = _decimal_to_dms(lon)

    gps_ifd = {
        piexif.GPSIFD.GPSLatitudeRef: b"S" if lat_neg else b"N",
        piexif.GPSIFD.GPSLatitude: lat_dms,
        piexif.GPSIFD.GPSLongitudeRef: b"W" if lon_neg else b"E",
        piexif.GPSIFD.GPSLongitude: lon_dms,
    }
    exif["GPS"] = gps_ifd

    exif_bytes = piexif.dump(exif)
    piexif.insert(exif_bytes, str(path))


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


def parse_takeout_sidecar(json_path: Path) -> dict:
    """Parse a Google Takeout JSON sidecar and extract date and GPS.

    Returns dict with keys:
      - date: ISO format string or None
      - lat: float or None
      - lon: float or None
    """
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"date": None, "lat": None, "lon": None}

    result = {"date": None, "lat": None, "lon": None}

    # Date: photoTakenTime or creationTime
    for time_key in ("photoTakenTime", "creationTime"):
        time_data = data.get(time_key)
        if time_data and "timestamp" in time_data:
            try:
                ts = int(time_data["timestamp"])
                if ts > 0:
                    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                    result["date"] = dt.isoformat()
                    break
            except (ValueError, OSError):
                continue

    # GPS: geoData or geoDataExif
    for geo_key in ("geoData", "geoDataExif"):
        geo = data.get(geo_key)
        if geo:
            lat = geo.get("latitude", 0)
            lon = geo.get("longitude", 0)
            # Google uses 0,0 as "no data"
            if lat != 0 or lon != 0:
                result["lat"] = lat
                result["lon"] = lon
                break

    return result


def merge_metadata_from_sidecar(media_path: Path, sidecar_path: Path) -> list[str]:
    """Merge metadata from a Takeout sidecar into the media file's EXIF.

    Only writes fields that are missing from the file's EXIF.
    Returns a list of fields that were merged.
    """
    if not can_have_exif(media_path):
        return []

    sidecar = parse_takeout_sidecar(sidecar_path)
    merged = []

    # Merge date if file doesn't have one
    if sidecar["date"] and not get_exif_date(media_path):
        write_exif_date(media_path, sidecar["date"])
        merged.append("date")

    # Merge GPS if file doesn't have it
    if sidecar["lat"] is not None and not get_exif_gps(media_path):
        write_exif_gps(media_path, sidecar["lat"], sidecar["lon"])
        merged.append("gps")

    return merged


def merge_metadata_from_file(target: Path, source: Path, db=None) -> list[str]:
    """Merge metadata from a source file into a target file.

    If the target is missing date or GPS but the source has it,
    copy it over. This handles the case where a duplicate has better
    metadata than the original that was kept.

    Returns a list of fields that were merged.
    """
    if not can_have_exif(target) or not can_have_exif(source):
        return []
    if not target.exists() or not source.exists():
        return []

    merged = []

    # Merge date if target doesn't have one but source does
    target_date = get_exif_date(target)
    if not target_date:
        source_date = get_exif_date(source)
        if source_date:
            try:
                write_exif_date(target, source_date)
                merged.append("date")
                if db:
                    db.log_metadata_merge(str(target), str(source), "date", source_date)
            except Exception:
                pass

    # Merge GPS if target doesn't have it but source does
    target_gps = get_exif_gps(target)
    if not target_gps:
        source_gps = get_exif_gps(source)
        if source_gps:
            try:
                write_exif_gps(target, source_gps[0], source_gps[1])
                merged.append("gps")
                if db:
                    db.log_metadata_merge(str(target), str(source), "gps",
                                          f"{source_gps[0]},{source_gps[1]}")
            except Exception:
                pass

    return merged
