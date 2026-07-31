"""Timezone resolution for Takeout timestamps (#9).

Takeout gives a UTC epoch. EXIF DateTimeOriginal is camera-local wall-clock
with no zone. Writing one into the other shifts every recovered date by the
shooting offset — up to ±14 h, so photos land on the wrong day, for exactly
the files that had no EXIF to begin with.

The DST pair is the load-bearing test: the same coordinates must produce
different offsets in summer and winter, which is only true if the zone lookup
is cached but the *offset* is recomputed per photo.
"""

from datetime import datetime, timezone

import piexif
import pytest

from memoryvault.phototime import TzSource, resolve_capture_time
from tests.conftest import make_jpeg_bytes

CHICAGO = {"lat": 41.8781, "lon": -87.6298}
SYDNEY = {"lat": -33.8688, "lon": 151.2093}

# 2021-07-04 18:00 UTC and 2021-01-04 18:00 UTC.
SUMMER_UTC = int(datetime(2021, 7, 4, 18, 0, 0, tzinfo=timezone.utc).timestamp())
WINTER_UTC = int(datetime(2021, 1, 4, 18, 0, 0, tzinfo=timezone.utc).timestamp())


def _jpeg_with_offset(tmp_path, name, offset: bytes, when: bytes = b"2019:03:02 08:15:00"):
    path = tmp_path / name
    path.write_bytes(make_jpeg_bytes(exif={
        "0th": {},
        "Exif": {
            piexif.ExifIFD.DateTimeOriginal: when,
            piexif.ExifIFD.OffsetTimeOriginal: offset,
        },
        "GPS": {}, "1st": {},
    }))
    return path


class TestGpsTier:
    def test_summer_resolves_to_cdt(self, tmp_path):
        path = tmp_path / "p.jpg"
        path.write_bytes(make_jpeg_bytes())

        result = resolve_capture_time(SUMMER_UTC, CHICAGO, path, siblings=[])

        assert result.offset == "-05:00"
        assert result.tz_name == "America/Chicago"
        assert result.tz_source is TzSource.GPS

    def test_winter_resolves_to_cst_from_identical_coordinates(self, tmp_path):
        """Same lat/lon as the summer case — only the instant differs."""
        path = tmp_path / "p.jpg"
        path.write_bytes(make_jpeg_bytes())

        result = resolve_capture_time(WINTER_UTC, CHICAGO, path, siblings=[])

        assert result.offset == "-06:00"
        assert result.tz_name == "America/Chicago"
        assert result.tz_source is TzSource.GPS

    def test_dst_pair_produces_different_wall_clocks(self, tmp_path):
        """Guards against a cached fixed offset per location."""
        path = tmp_path / "p.jpg"
        path.write_bytes(make_jpeg_bytes())

        summer = resolve_capture_time(SUMMER_UTC, CHICAGO, path, siblings=[])
        winter = resolve_capture_time(WINTER_UTC, CHICAGO, path, siblings=[])

        assert summer.local_dt.hour == 13   # 18:00 UTC − 5
        assert winter.local_dt.hour == 12   # 18:00 UTC − 6

    def test_southern_hemisphere_dst_runs_the_other_way(self, tmp_path):
        path = tmp_path / "p.jpg"
        path.write_bytes(make_jpeg_bytes())

        jul = resolve_capture_time(SUMMER_UTC, SYDNEY, path, siblings=[])
        jan = resolve_capture_time(WINTER_UTC, SYDNEY, path, siblings=[])

        assert jul.offset == "+10:00"   # AEST, southern winter
        assert jan.offset == "+11:00"   # AEDT, southern summer

    def test_local_datetime_is_naive(self, tmp_path):
        """It is a wall clock; carrying a tzinfo would re-introduce #9."""
        path = tmp_path / "p.jpg"
        path.write_bytes(make_jpeg_bytes())

        result = resolve_capture_time(SUMMER_UTC, CHICAGO, path, siblings=[])

        assert result.local_dt.tzinfo is None


class TestExifTier:
    def test_existing_offset_wins_over_gps(self, tmp_path):
        """The camera's own declaration outranks a coordinate lookup."""
        path = _jpeg_with_offset(tmp_path, "p.jpg", b"+09:00")

        result = resolve_capture_time(SUMMER_UTC, CHICAGO, path, siblings=[])

        assert result.offset == "+09:00"
        assert result.tz_source is TzSource.EXIF

    def test_exif_offset_shifts_the_wall_clock(self, tmp_path):
        path = _jpeg_with_offset(tmp_path, "p.jpg", b"+09:00")

        result = resolve_capture_time(SUMMER_UTC, CHICAGO, path, siblings=[])

        assert result.local_dt.hour == 3          # 18:00 UTC + 9 → next day 03:00
        assert result.local_dt.day == 5


class TestSiblingTier:
    def test_sibling_offset_used_when_no_gps(self, tmp_path):
        sibling = _jpeg_with_offset(
            tmp_path, "sibling.jpg", b"+02:00",
            when=datetime.fromtimestamp(SUMMER_UTC, tz=timezone.utc)
            .strftime("%Y:%m:%d %H:%M:%S").encode(),
        )
        target = tmp_path / "target.jpg"
        target.write_bytes(make_jpeg_bytes())

        result = resolve_capture_time(SUMMER_UTC, None, target, siblings=[sibling])

        assert result.offset == "+02:00"
        assert result.tz_source is TzSource.SIBLING

    def test_conflicting_siblings_are_refused(self, tmp_path):
        """Ambiguous evidence must fall through, not pick one at random."""
        when = (datetime.fromtimestamp(SUMMER_UTC, tz=timezone.utc)
                .strftime("%Y:%m:%d %H:%M:%S").encode())
        a = _jpeg_with_offset(tmp_path, "a.jpg", b"+02:00", when=when)
        b = _jpeg_with_offset(tmp_path, "b.jpg", b"-07:00", when=when)
        target = tmp_path / "target.jpg"
        target.write_bytes(make_jpeg_bytes())

        result = resolve_capture_time(SUMMER_UTC, None, target, siblings=[a, b])

        assert result.tz_source is TzSource.UTC_FALLBACK

    def test_sibling_outside_time_window_is_ignored(self, tmp_path):
        """±12 h — a photo from another trip proves nothing about this one."""
        far = _jpeg_with_offset(tmp_path, "far.jpg", b"+02:00",
                                when=b"2015:01:01 00:00:00")
        target = tmp_path / "target.jpg"
        target.write_bytes(make_jpeg_bytes())

        result = resolve_capture_time(SUMMER_UTC, None, target, siblings=[far])

        assert result.tz_source is TzSource.UTC_FALLBACK

    def test_gps_outranks_sibling(self, tmp_path):
        when = (datetime.fromtimestamp(SUMMER_UTC, tz=timezone.utc)
                .strftime("%Y:%m:%d %H:%M:%S").encode())
        sibling = _jpeg_with_offset(tmp_path, "sibling.jpg", b"+02:00", when=when)
        target = tmp_path / "target.jpg"
        target.write_bytes(make_jpeg_bytes())

        result = resolve_capture_time(SUMMER_UTC, CHICAGO, target, siblings=[sibling])

        assert result.tz_source is TzSource.GPS


class TestUtcFallback:
    def test_no_evidence_falls_back_to_utc(self, tmp_path):
        path = tmp_path / "p.jpg"
        path.write_bytes(make_jpeg_bytes())

        result = resolve_capture_time(SUMMER_UTC, None, path, siblings=[])

        assert result.offset == "+00:00"
        assert result.tz_source is TzSource.UTC_FALLBACK
        assert result.local_dt.hour == 18

    def test_fallback_is_flagged_for_deferral(self, tmp_path):
        """The ambiguity must be recorded, not silently written as fact."""
        path = tmp_path / "p.jpg"
        path.write_bytes(make_jpeg_bytes())

        result = resolve_capture_time(SUMMER_UTC, None, path, siblings=[])

        assert result.is_assumed is True

    def test_resolved_tiers_are_not_flagged(self, tmp_path):
        path = tmp_path / "p.jpg"
        path.write_bytes(make_jpeg_bytes())

        result = resolve_capture_time(SUMMER_UTC, CHICAGO, path, siblings=[])

        assert result.is_assumed is False

    def test_zero_zero_coordinates_are_not_a_location(self, tmp_path):
        """Google writes 0,0 for 'no data'; it is in the Gulf of Guinea."""
        path = tmp_path / "p.jpg"
        path.write_bytes(make_jpeg_bytes())

        result = resolve_capture_time(SUMMER_UTC, {"lat": 0, "lon": 0}, path,
                                      siblings=[])

        assert result.tz_source is TzSource.UTC_FALLBACK


class TestOffsetCaching:
    def test_repeated_lookups_agree(self, tmp_path):
        """Memoisation must key on coordinates only, never on the instant."""
        path = tmp_path / "p.jpg"
        path.write_bytes(make_jpeg_bytes())

        first = resolve_capture_time(SUMMER_UTC, CHICAGO, path, siblings=[])
        _ = resolve_capture_time(WINTER_UTC, CHICAGO, path, siblings=[])
        again = resolve_capture_time(SUMMER_UTC, CHICAGO, path, siblings=[])

        assert again.offset == first.offset
        assert again.local_dt == first.local_dt


class TestOffsetFormatting:
    @pytest.mark.parametrize("lat,lon,expected", [
        (28.6139, 77.2090, "+05:30"),    # Kolkata — half-hour offset
        (27.7172, 85.3240, "+05:45"),    # Kathmandu — quarter-hour offset
    ])
    def test_sub_hour_offsets(self, tmp_path, lat, lon, expected):
        path = tmp_path / "p.jpg"
        path.write_bytes(make_jpeg_bytes())

        result = resolve_capture_time(SUMMER_UTC, {"lat": lat, "lon": lon},
                                      path, siblings=[])

        assert result.offset == expected
