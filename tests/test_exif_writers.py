"""EXIF writers must never destroy what they cannot read (#10).

Two distinct destructive behaviours are pinned here:

1. `piexif.load` failure used to fall back to `{"0th": {}, ...}`, so a file
   with unparseable EXIF had its APP1 segment *replaced with an empty one* —
   a write that reported success while deleting the metadata it was called to
   preserve.
2. `write_exif_gps` assigned `exif["GPS"] = gps_ifd` wholesale, discarding
   GPSAltitude, GPSTimeStamp, GPSDateStamp and GPSImgDirection on every merge.
"""

from datetime import datetime

import piexif
import pytest

from memoryvault.metadata import (
    UnparseableExifError, get_exif_gps, read_exif,
    write_exif_date, write_exif_gps,
)
from tests.conftest import make_jpeg_bytes

# The GPS tags that must survive a lat/lon merge untouched.
PRESERVED_GPS_TAGS = [
    piexif.GPSIFD.GPSAltitude,
    piexif.GPSIFD.GPSAltitudeRef,
    piexif.GPSIFD.GPSTimeStamp,
    piexif.GPSIFD.GPSDateStamp,
    piexif.GPSIFD.GPSImgDirection,
    piexif.GPSIFD.GPSImgDirectionRef,
]


class TestGpsMergePreservesIfd:
    def test_each_extra_tag_survives_key_by_key(self, jpeg_with_full_gps):
        before = read_exif(jpeg_with_full_gps)["GPS"]

        write_exif_gps(jpeg_with_full_gps, 48.8584, 2.2945)

        after = read_exif(jpeg_with_full_gps)["GPS"]
        for tag in PRESERVED_GPS_TAGS:
            assert tag in after, f"GPS tag {tag} was dropped by the merge"
            assert after[tag] == before[tag], f"GPS tag {tag} was altered"

    def test_lat_lon_actually_updated(self, jpeg_with_full_gps):
        write_exif_gps(jpeg_with_full_gps, 48.8584, 2.2945)

        lat, lon = get_exif_gps(jpeg_with_full_gps)
        assert lat == pytest.approx(48.8584, abs=1e-4)
        assert lon == pytest.approx(2.2945, abs=1e-4)

    def test_hemisphere_refs_follow_the_sign(self, jpeg_with_full_gps):
        """The fixture starts N/W; a southern-eastern point must flip both."""
        write_exif_gps(jpeg_with_full_gps, -33.8688, 151.2093)

        gps = read_exif(jpeg_with_full_gps)["GPS"]
        assert gps[piexif.GPSIFD.GPSLatitudeRef] == b"S"
        assert gps[piexif.GPSIFD.GPSLongitudeRef] == b"E"

    def test_writing_gps_into_a_file_with_none_still_works(self, tmp_path):
        path = tmp_path / "no_gps.jpg"
        path.write_bytes(make_jpeg_bytes())

        write_exif_gps(path, 41.8781, -87.6298)

        lat, lon = get_exif_gps(path)
        assert lat == pytest.approx(41.8781, abs=1e-4)
        assert lon == pytest.approx(-87.6298, abs=1e-4)


class TestUnparseableExifIsRefused:
    def test_date_write_raises(self, corrupt_exif_jpeg):
        with pytest.raises(UnparseableExifError):
            write_exif_date(corrupt_exif_jpeg, datetime(2021, 6, 5, 19, 30, 46),
                            offset="-05:00")

    def test_gps_write_raises(self, corrupt_exif_jpeg):
        with pytest.raises(UnparseableExifError):
            write_exif_gps(corrupt_exif_jpeg, 41.8781, -87.6298)

    def test_file_is_byte_identical_after_refused_date_write(self, corrupt_exif_jpeg):
        before = corrupt_exif_jpeg.read_bytes()

        with pytest.raises(UnparseableExifError):
            write_exif_date(corrupt_exif_jpeg, datetime(2021, 6, 5, 19, 30, 46),
                            offset="-05:00")

        assert corrupt_exif_jpeg.read_bytes() == before

    def test_file_is_byte_identical_after_refused_gps_write(self, corrupt_exif_jpeg):
        before = corrupt_exif_jpeg.read_bytes()

        with pytest.raises(UnparseableExifError):
            write_exif_gps(corrupt_exif_jpeg, 41.8781, -87.6298)

        assert corrupt_exif_jpeg.read_bytes() == before


class TestDateWrite:
    def test_writes_all_five_fields(self, tmp_path):
        path = tmp_path / "photo.jpg"
        path.write_bytes(make_jpeg_bytes())

        write_exif_date(path, datetime(2021, 6, 5, 19, 30, 46), offset="-05:00")

        exif = read_exif(path)
        assert exif["Exif"][piexif.ExifIFD.DateTimeOriginal] == b"2021:06:05 19:30:46"
        assert exif["Exif"][piexif.ExifIFD.DateTimeDigitized] == b"2021:06:05 19:30:46"
        assert exif["0th"][piexif.ImageIFD.DateTime] == b"2021:06:05 19:30:46"
        assert exif["Exif"][piexif.ExifIFD.OffsetTimeOriginal] == b"-05:00"
        assert exif["Exif"][piexif.ExifIFD.OffsetTimeDigitized] == b"-05:00"

    def test_offset_is_optional(self, tmp_path):
        """No offset known → write the wall clock, claim no zone."""
        path = tmp_path / "photo.jpg"
        path.write_bytes(make_jpeg_bytes())

        write_exif_date(path, datetime(2021, 6, 5, 19, 30, 46), offset=None)

        exif = read_exif(path)
        assert exif["Exif"][piexif.ExifIFD.DateTimeOriginal] == b"2021:06:05 19:30:46"
        assert piexif.ExifIFD.OffsetTimeOriginal not in exif["Exif"]

    def test_naive_datetime_required(self, tmp_path):
        """A tz-aware datetime would silently write the wrong wall clock.

        EXIF DateTimeOriginal is local wall-clock with no zone; the offset
        travels in its own tag. Accepting an aware datetime here is how
        finding #9 happened, so it is rejected rather than coerced.
        """
        from datetime import timezone
        path = tmp_path / "photo.jpg"
        path.write_bytes(make_jpeg_bytes())

        with pytest.raises(ValueError):
            write_exif_date(path, datetime(2021, 6, 5, 19, 30, 46, tzinfo=timezone.utc),
                            offset="+00:00")

    def test_existing_unrelated_exif_is_preserved(self, tmp_path):
        path = tmp_path / "photo.jpg"
        path.write_bytes(make_jpeg_bytes(
            exif={"0th": {piexif.ImageIFD.Make: b"Canon",
                          piexif.ImageIFD.Model: b"EOS 5D"},
                  "Exif": {}, "GPS": {}, "1st": {}}))

        write_exif_date(path, datetime(2021, 6, 5, 19, 30, 46), offset="-05:00")

        exif = read_exif(path)
        assert exif["0th"][piexif.ImageIFD.Make] == b"Canon"
        assert exif["0th"][piexif.ImageIFD.Model] == b"EOS 5D"

    def test_gps_ifd_untouched_by_a_date_write(self, jpeg_with_full_gps):
        before = read_exif(jpeg_with_full_gps)["GPS"]

        write_exif_date(jpeg_with_full_gps, datetime(2021, 6, 5, 19, 30, 46),
                        offset="-05:00")

        assert read_exif(jpeg_with_full_gps)["GPS"] == before


class TestGenuineHeicIsRefused:
    def test_date_write_on_heic_raises(self, genuine_heic):
        """Not an EXIF container at all — this must fail loudly, not no-op."""
        with pytest.raises(Exception):
            write_exif_date(genuine_heic, datetime(2021, 6, 5, 19, 30, 46),
                            offset="-05:00")

    def test_heic_is_byte_identical_after_refused_write(self, genuine_heic):
        before = genuine_heic.read_bytes()
        with pytest.raises(Exception):
            write_exif_date(genuine_heic, datetime(2021, 6, 5, 19, 30, 46),
                            offset="-05:00")
        assert genuine_heic.read_bytes() == before
