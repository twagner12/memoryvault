"""Tests for memoryvault.metadata."""

import json
import struct
from datetime import datetime, timezone
from pathlib import Path

import piexif

from memoryvault.metadata import (
    get_exif_date, get_exif_gps, write_exif_date, write_exif_gps,
    has_metadata, can_have_exif,
    parse_takeout_sidecar, find_takeout_sidecar, merge_metadata_from_sidecar,
)


def _make_jpeg(path: Path, exif_dict: dict = None) -> Path:
    """Create a minimal valid JPEG file with optional EXIF data."""
    if exif_dict:
        exif_bytes = piexif.dump(exif_dict)
    else:
        exif_bytes = piexif.dump({"0th": {}, "Exif": {}, "GPS": {}, "1st": {}})

    # Minimal JPEG: SOI + APP1(EXIF) + minimal image data + EOI
    # We need a valid JPEG that piexif can read/write
    import io
    from PIL import Image
    img = Image.new("RGB", (10, 10), color="red")
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    jpeg_bytes = buf.getvalue()
    path.write_bytes(jpeg_bytes)

    if exif_dict:
        piexif.insert(exif_bytes, str(path))

    return path


def _make_exif_with_date(date_str: str) -> dict:
    return {
        "0th": {},
        "Exif": {piexif.ExifIFD.DateTimeOriginal: date_str.encode("utf-8")},
        "GPS": {},
        "1st": {},
    }


def _make_exif_with_gps(lat: float, lon: float) -> dict:
    def to_dms(val):
        val = abs(val)
        d = int(val)
        m = int((val - d) * 60)
        s = int(((val - d) * 60 - m) * 60 * 10000)
        return ((d, 1), (m, 1), (s, 10000))

    return {
        "0th": {},
        "Exif": {},
        "GPS": {
            piexif.GPSIFD.GPSLatitudeRef: b"N" if lat >= 0 else b"S",
            piexif.GPSIFD.GPSLatitude: to_dms(lat),
            piexif.GPSIFD.GPSLongitudeRef: b"E" if lon >= 0 else b"W",
            piexif.GPSIFD.GPSLongitude: to_dms(lon),
        },
        "1st": {},
    }


class TestCanHaveExif:
    def test_jpeg(self):
        assert can_have_exif(Path("photo.jpg"))
        assert can_have_exif(Path("photo.JPEG"))

    def test_non_exif(self):
        assert not can_have_exif(Path("photo.png"))
        assert not can_have_exif(Path("video.mp4"))
        assert not can_have_exif(Path("song.mp3"))


class TestExifDate:
    def test_read_date(self, tmp_path):
        exif = _make_exif_with_date("2012:07:04 14:30:00")
        path = _make_jpeg(tmp_path / "photo.jpg", exif)
        date = get_exif_date(path)
        assert date is not None
        assert "2012-07-04" in date

    def test_no_date(self, tmp_path):
        path = _make_jpeg(tmp_path / "photo.jpg")
        date = get_exif_date(path)
        assert date is None

    def test_non_jpeg_returns_none(self, tmp_path):
        path = tmp_path / "photo.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n")
        assert get_exif_date(path) is None

    def test_write_date(self, tmp_path):
        path = _make_jpeg(tmp_path / "photo.jpg")
        assert get_exif_date(path) is None
        write_exif_date(path, "2012-07-04T14:30:00")
        date = get_exif_date(path)
        assert date is not None
        assert "2012-07-04" in date


class TestExifGps:
    def test_read_gps(self, tmp_path):
        exif = _make_exif_with_gps(41.8781, -87.6298)
        path = _make_jpeg(tmp_path / "photo.jpg", exif)
        gps = get_exif_gps(path)
        assert gps is not None
        lat, lon = gps
        assert abs(lat - 41.8781) < 0.001
        assert abs(lon - (-87.6298)) < 0.001

    def test_no_gps(self, tmp_path):
        path = _make_jpeg(tmp_path / "photo.jpg")
        assert get_exif_gps(path) is None

    def test_write_gps(self, tmp_path):
        path = _make_jpeg(tmp_path / "photo.jpg")
        assert get_exif_gps(path) is None
        write_exif_gps(path, 41.8781, -87.6298)
        gps = get_exif_gps(path)
        assert gps is not None
        assert abs(gps[0] - 41.8781) < 0.001
        assert abs(gps[1] - (-87.6298)) < 0.001


class TestHasMetadata:
    def test_with_date_and_gps(self, tmp_path):
        exif = _make_exif_with_date("2012:07:04 14:30:00")
        exif["GPS"] = _make_exif_with_gps(41.0, -87.0)["GPS"]
        path = _make_jpeg(tmp_path / "photo.jpg", exif)
        meta = has_metadata(path)
        assert meta["has_exif_date"] is True
        assert meta["has_exif_gps"] is True

    def test_no_metadata(self, tmp_path):
        path = _make_jpeg(tmp_path / "photo.jpg")
        meta = has_metadata(path)
        assert meta["has_exif_date"] is False
        assert meta["has_exif_gps"] is False


class TestTakeoutSidecar:
    def test_parse_sidecar_with_date_and_gps(self, tmp_path):
        sidecar = tmp_path / "photo.jpg.supplemental-metadata.json"
        sidecar.write_text(json.dumps({
            "photoTakenTime": {"timestamp": "1341415800"},
            "geoData": {"latitude": 41.8781, "longitude": -87.6298, "altitude": 0},
        }))
        result = parse_takeout_sidecar(sidecar)
        assert result["date"] is not None
        assert result["lat"] is not None
        assert abs(result["lat"] - 41.8781) < 0.001

    def test_parse_sidecar_zero_gps_is_no_data(self, tmp_path):
        sidecar = tmp_path / "photo.jpg.json"
        sidecar.write_text(json.dumps({
            "photoTakenTime": {"timestamp": "1341415800"},
            "geoData": {"latitude": 0, "longitude": 0, "altitude": 0},
        }))
        result = parse_takeout_sidecar(sidecar)
        assert result["lat"] is None

    def test_parse_sidecar_creation_time_fallback(self, tmp_path):
        sidecar = tmp_path / "photo.jpg.json"
        sidecar.write_text(json.dumps({
            "creationTime": {"timestamp": "1341415800"},
        }))
        result = parse_takeout_sidecar(sidecar)
        assert result["date"] is not None

    def test_find_sidecar_supplemental(self, tmp_path):
        media = tmp_path / "photo.jpg"
        media.write_bytes(b"fake")
        sidecar = tmp_path / "photo.jpg.supplemental-metadata.json"
        sidecar.write_text("{}")
        found = find_takeout_sidecar(media)
        assert found == sidecar

    def test_find_sidecar_old_format(self, tmp_path):
        media = tmp_path / "photo.jpg"
        media.write_bytes(b"fake")
        sidecar = tmp_path / "photo.jpg.json"
        sidecar.write_text("{}")
        found = find_takeout_sidecar(media)
        assert found == sidecar

    def test_find_sidecar_none(self, tmp_path):
        media = tmp_path / "photo.jpg"
        media.write_bytes(b"fake")
        assert find_takeout_sidecar(media) is None


class TestMergeFromSidecar:
    def test_merge_date_from_sidecar(self, tmp_path):
        path = _make_jpeg(tmp_path / "photo.jpg")
        sidecar = tmp_path / "photo.jpg.supplemental-metadata.json"
        sidecar.write_text(json.dumps({
            "photoTakenTime": {"timestamp": "1341415800"},
        }))
        merged = merge_metadata_from_sidecar(path, sidecar)
        assert "date" in merged
        assert get_exif_date(path) is not None

    def test_no_overwrite_existing_date(self, tmp_path):
        exif = _make_exif_with_date("2012:07:04 14:30:00")
        path = _make_jpeg(tmp_path / "photo.jpg", exif)
        sidecar = tmp_path / "photo.jpg.json"
        sidecar.write_text(json.dumps({
            "photoTakenTime": {"timestamp": "0"},
        }))
        merged = merge_metadata_from_sidecar(path, sidecar)
        assert "date" not in merged
        # Original date preserved
        assert "2012-07-04" in get_exif_date(path)

    def test_skip_non_exif_files(self, tmp_path):
        path = tmp_path / "video.mp4"
        path.write_bytes(b"fake video")
        sidecar = tmp_path / "video.mp4.json"
        sidecar.write_text(json.dumps({"photoTakenTime": {"timestamp": "1341415800"}}))
        merged = merge_metadata_from_sidecar(path, sidecar)
        assert merged == []
