"""The mtime repair pass, and above all the direction of its safety.

`repair-mtime` walks 62,409 production files and rewrites their timestamps, so
the property worth pinning hardest is not that it repairs correctly but that
doing nothing is what happens by default. A bare invocation must be a report.

The other claim under test is where the value comes from. Files that arrived
carrying their own EXIF date got no write from ingest precisely because they
knew better than Google, and Google's photoTakenTime is documented as
unreliable — two vault files disagree with their camera by nearly two years.
So the file's own EXIF wins, and Google's value is only allowed to supply an
offset the file omitted, never to contradict it.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import piexif
import pytest
from click.testing import CliRunner

from memoryvault.cli import cli
from memoryvault.database import Database
from memoryvault.hasher import hash_full, hash_head, hash_tail
from memoryvault.repair import (
    INGEST_WINDOW, find_candidates, load_google_epochs, repair_mtimes,
    resolve_instant,
)
from tests.conftest import make_jpeg_bytes

# 2021-07-04 18:00:00 UTC == 13:00:00 in Chicago (CDT, -05:00).
CAPTURE_UTC = int(datetime(2021, 7, 4, 18, 0, 0, tzinfo=timezone.utc).timestamp())
IN_WINDOW = INGEST_WINDOW[0] + 3600          # inside the Aug 2026 ingest run


def jpeg_with(tmp_path, name, dto="2021:07:04 13:00:00", offset=b"-05:00"):
    exif = {"0th": {}, "Exif": {piexif.ExifIFD.DateTimeOriginal: dto.encode()},
            "GPS": {}, "1st": {}, "thumbnail": None}
    if offset is not None:
        exif["Exif"][piexif.ExifIFD.OffsetTimeOriginal] = offset
    p = tmp_path / name
    p.write_bytes(make_jpeg_bytes(exif=exif))
    return p


def index(db, path, mtime=IN_WINDOW, blake3=None):
    size = path.stat().st_size
    db.upsert_file(path=str(path), size=size,
                   blake3_full=blake3 or hash_full(path),
                   blake3_head=hash_head(path),
                   blake3_tail=hash_tail(path) if size > 4096 else hash_head(path),
                   mtime=mtime,
                   scan_time=datetime.now(timezone.utc).isoformat())
    os.utime(path, (mtime, mtime))


class TestDryRunIsTheDefault:
    def test_bare_invocation_writes_nothing(self, tmp_path, db):
        p = jpeg_with(tmp_path, "a.jpg")
        index(db, p)
        db.close()

        before = p.stat().st_mtime
        result = CliRunner().invoke(
            cli, ["--db", str(tmp_path / "test.db"), "repair-mtime"])

        assert result.exit_code == 0, result.output
        assert p.stat().st_mtime == before
        assert "Dry run" in result.output

    def test_bare_invocation_opens_the_database_read_only(self, tmp_path, db,
                                                          monkeypatch):
        """Structurally incapable of writing, not merely choosing not to."""
        p = jpeg_with(tmp_path, "a.jpg")
        index(db, p)
        db.close()

        seen = {}
        original = Database.__init__

        def spy(self, path, read_only=False, **kw):
            seen["read_only"] = read_only
            return original(self, path, read_only=read_only, **kw)

        monkeypatch.setattr(Database, "__init__", spy)
        CliRunner().invoke(cli, ["--db", str(tmp_path / "test.db"),
                                 "repair-mtime"])
        assert seen["read_only"] is True

    def test_apply_is_what_writes(self, tmp_path, db):
        p = jpeg_with(tmp_path, "a.jpg")
        index(db, p)
        db.close()

        result = CliRunner().invoke(
            cli, ["--db", str(tmp_path / "test.db"), "repair-mtime", "--apply"])

        assert result.exit_code == 0, result.output
        assert int(p.stat().st_mtime) == CAPTURE_UTC


class TestValueSource:
    def test_exif_offset_is_preferred_over_google(self, tmp_path, db):
        """The file describes a complete instant; Google is not consulted."""
        p = jpeg_with(tmp_path, "a.jpg")
        epoch, source, _ = resolve_instant(p, google_epoch=CAPTURE_UTC + 999_999)

        assert source == "exif_offset"
        assert epoch == CAPTURE_UTC

    def test_google_supplies_a_missing_offset(self, tmp_path, db):
        """No offset in the file, but Google agrees about the moment."""
        p = jpeg_with(tmp_path, "a.jpg", offset=None)
        epoch, source, _ = resolve_instant(p, google_epoch=CAPTURE_UTC)

        assert source == "google_corroborated"
        assert epoch == CAPTURE_UTC

    def test_google_contradicting_the_camera_is_refused(self, tmp_path, db):
        """The DSC_0179 case: Google's import date, two years out."""
        p = jpeg_with(tmp_path, "a.jpg", offset=None)
        epoch, reason, detail = resolve_instant(
            p, google_epoch=CAPTURE_UTC + 704 * 86400)

        assert epoch is None
        assert reason == "google_contradicts_exif"
        assert "apart" in detail

    def test_gap_that_is_not_a_legal_utc_offset_is_refused(self, tmp_path, db):
        """The 272-file case: near enough to look corroborated, wrong anyway.

        A sidecar that really describes the photo differs from its local wall
        clock by exactly the zone offset. +10.425h is inside the plausible
        range but is not any offset that exists, so the sidecar belongs to a
        neighbouring photo — Google numbers its exports in a way that produces
        exactly this, on DSC_0108(4)(1).JPG among 271 others.
        """
        p = jpeg_with(tmp_path, "a.jpg", offset=None)
        naive = int(datetime(2021, 7, 4, 13, 0, 0,
                             tzinfo=timezone.utc).timestamp())
        epoch, reason, detail = resolve_instant(
            p, google_epoch=naive + int(10.425 * 3600))

        assert epoch is None
        assert reason == "google_offset_not_quantised"
        assert "15-minute" in detail

    def test_a_real_offset_at_fifteen_minute_granularity_is_accepted(
            self, tmp_path, db):
        """Some zones really are :30 or :45 from UTC (India, Nepal, Chatham)."""
        p = jpeg_with(tmp_path, "a.jpg", offset=None)
        naive = int(datetime(2021, 7, 4, 13, 0, 0,
                             tzinfo=timezone.utc).timestamp())
        epoch, source, _ = resolve_instant(
            p, google_epoch=naive - int(5.75 * 3600))       # UTC+05:45

        assert source == "google_corroborated"
        assert epoch == naive - int(5.75 * 3600)

    def test_no_offset_and_no_google_value_is_refused(self, tmp_path, db):
        """A local wall clock alone is not an instant; guessing is how the
        tz_unknown_assumed_utc rows happened in the first place."""
        p = jpeg_with(tmp_path, "a.jpg", offset=None)
        epoch, reason, _ = resolve_instant(p, google_epoch=None)

        assert epoch is None
        assert reason == "no_offset_and_no_google_value"


class TestGoogleEpochIsFoundAtBothDepths:
    """metadata_log stores the epoch at two depths and the shallower read
    silently loses the larger population.

    A merged row is the payload itself. An `already_present` row records a
    refusal and nests what was offered under "offered". Reading only the top
    level finds 30,150 paths instead of the full set — and the ones it misses
    are exactly the files that arrived with their own EXIF, i.e. the bulk of
    what this pass exists to repair. The failure is silent: nothing is
    corrupted, the run just quietly declines to fix most of the vault.
    """

    def test_merged_row_shape(self, tmp_path, db):
        db.log_metadata_merge("/v/a.jpg", "takeout:a", "date",
                              json.dumps({"utc_epoch": CAPTURE_UTC}))
        assert load_google_epochs(db)["/v/a.jpg"] == CAPTURE_UTC

    def test_already_present_row_nests_the_offer(self, tmp_path, db):
        db.log_metadata_merge(
            "/v/b.jpg", "takeout:b", "already_present",
            json.dumps({"field": "date",
                        "offered": {"utc_epoch": CAPTURE_UTC},
                        "satisfied_by": "exif_date"}))
        assert load_google_epochs(db)["/v/b.jpg"] == CAPTURE_UTC

    def test_gps_refusal_carries_no_epoch_and_is_ignored(self, tmp_path, db):
        db.log_metadata_merge(
            "/v/c.jpg", "takeout:c", "already_present",
            json.dumps({"field": "gps", "offered": {"lat": 41.8, "lon": -87.6},
                        "satisfied_by": "exif_gps"}))
        assert "/v/c.jpg" not in load_google_epochs(db)


class TestGuards:
    def test_size_mismatch_is_skipped_and_reported(self, tmp_path, db):
        """The cheapest half of the guard: the DB's size must still hold."""
        p = jpeg_with(tmp_path, "a.jpg")
        index(db, p)
        db.conn.execute("UPDATE files SET size = 999999 WHERE path = ?",
                        (str(p),))
        db.conn.commit()

        before = p.stat().st_mtime
        stats = repair_mtimes(db, dry_run=False, verify_hash=True)

        assert stats["repaired"] == 0
        assert stats["by_skip"]["hash_mismatch"] == 1
        assert p.stat().st_mtime == before
        assert any("size" in s[2] for s in stats["skips"])

    def test_head_hash_mismatch_is_skipped(self, tmp_path, db):
        """Head+tail is the default guard; a wrong head must still stop it."""
        p = jpeg_with(tmp_path, "a.jpg")
        index(db, p)
        db.conn.execute("UPDATE files SET blake3_head = ? WHERE path = ?",
                        ("0" * 64, str(p)))
        db.conn.commit()

        before = p.stat().st_mtime
        stats = repair_mtimes(db, dry_run=False, verify_hash=True)

        assert stats["repaired"] == 0
        assert stats["by_skip"]["hash_mismatch"] == 1
        assert p.stat().st_mtime == before

    def test_full_hash_mismatch_is_skipped_and_reported(self, tmp_path, db):
        p = jpeg_with(tmp_path, "a.jpg")
        index(db, p, blake3="0" * 64)          # database disagrees with disk

        before = p.stat().st_mtime
        stats = repair_mtimes(db, dry_run=False, verify_hash=True,
                              full_hash=True)

        assert stats["repaired"] == 0
        assert stats["by_skip"]["hash_mismatch"] == 1
        assert p.stat().st_mtime == before
        assert any(s[1] == "hash_mismatch" for s in stats["skips"])

    def test_mtime_outside_the_ingest_window_is_never_a_candidate(self, tmp_path,
                                                                  db):
        """Not clobbered by ingest, so it may hold a legitimate value."""
        p = jpeg_with(tmp_path, "a.jpg")
        index(db, p, mtime=CAPTURE_UTC)

        assert find_candidates(db) == []

    def test_file_missing_from_disk_is_skipped(self, tmp_path, db):
        p = jpeg_with(tmp_path, "a.jpg")
        index(db, p)
        p.unlink()

        stats = repair_mtimes(db, dry_run=False)

        assert stats["repaired"] == 0
        assert stats["by_skip"]["file_missing"] == 1

    def test_limit_bounds_the_run(self, tmp_path, db):
        for i in range(5):
            p = jpeg_with(tmp_path, f"a{i}.jpg")
            index(db, p)

        stats = repair_mtimes(db, dry_run=False, limit=2)

        assert stats["repaired"] == 2


class TestContentIsNeverAltered:
    def test_hash_still_matches_after_repair(self, tmp_path, db):
        """os.utime does not touch bytes, so the stored hash stays valid."""
        p = jpeg_with(tmp_path, "a.jpg")
        index(db, p)
        digest = hash_full(p)

        repair_mtimes(db, dry_run=False)

        assert hash_full(p) == digest
        assert int(p.stat().st_mtime) == CAPTURE_UTC

    def test_database_mtime_is_updated_to_match(self, tmp_path, db):
        """Otherwise the next scan re-hashes every repaired file."""
        p = jpeg_with(tmp_path, "a.jpg")
        index(db, p)

        repair_mtimes(db, dry_run=False)

        row = db.get_file_by_path(str(p))
        assert int(row["mtime"]) == CAPTURE_UTC


class TestLogIsAuditable:
    def test_log_records_old_new_and_source(self, tmp_path, db):
        p = jpeg_with(tmp_path, "a.jpg")
        index(db, p)
        log = tmp_path / "repair.log"

        repair_mtimes(db, dry_run=False, log_path=str(log))

        text = log.read_text()
        assert "old_mtime" in text and "new_mtime" in text
        assert "exif_offset" in text
        assert str(p) in text
        assert str(CAPTURE_UTC) in text      # reversible from the log alone
