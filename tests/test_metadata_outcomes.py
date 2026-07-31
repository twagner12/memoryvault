"""Every metadata outcome is recorded somewhere (#7, #8, and the invariant).

The branch's central claim is that a sidecar's date or GPS can no longer
vanish. `apply_sidecar` is the only function allowed to decide, and each
(file, field) pair it touches must leave a trace.

The invariant is stated **per (file, field)**, not per sidecar, because the
choke point in the design deliberately writes to two tables at once for two
specific cases. A sidecar carrying both a date and GPS produces two
independent outcomes, and a video's date legitimately produces both a
`metadata_log` row and a `metadata_pending` row.

Legal states for one (file, field):

  already_present  the file already had it; nothing to merge
  merged           metadata_log only
  failed           metadata_pending(state='failed') only
  deferred         metadata_pending(state='deferred') only
  merged_mtime_only + deferred    date onto a container that cannot hold EXIF
  merged + tz_unknown_assumed_utc date written under an assumed UTC offset

Anything else — above all a silent zero, present in neither table — is a bug.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import piexif
import pytest

from memoryvault.containers import Container, detect_container
from memoryvault.ingest import apply_sidecar, ingest_archive
from memoryvault.metadata import get_exif_date, get_exif_gps
from tests.conftest import make_jpeg_bytes, make_sidecar_bytes

CHICAGO_UTC = int(datetime(2021, 7, 4, 18, 0, 0, tzinfo=timezone.utc).timestamp())
CHICAGO = {"lat": 41.8781, "lon": -87.6298}


def sidecar(utc_epoch=CHICAGO_UTC, lat=None, lon=None):
    return {"utc_epoch": utc_epoch, "lat": lat, "lon": lon}


class TestGenuineHeic:
    def test_date_is_deferred_not_written(self, tmp_path, db, genuine_heic):
        outcome = apply_sidecar(genuine_heic, sidecar(), db, "takeout:x.heic")

        assert "date" in outcome.deferred
        pending = db.get_pending(str(genuine_heic))
        assert [p["field"] for p in pending] == ["date"]
        assert pending[0]["state"] == "deferred"

    def test_no_exif_merge_is_claimed(self, tmp_path, db, genuine_heic):
        apply_sidecar(genuine_heic, sidecar(), db, "takeout:x.heic")

        rows = db.conn.execute(
            "SELECT field FROM metadata_log WHERE target_path = ?",
            (str(genuine_heic),)).fetchall()
        # Only the mtime fallback may be claimed — never an EXIF date.
        assert all(r["field"] == "merged_mtime_only" for r in rows)

    def test_pending_row_is_drainable(self, tmp_path, db, genuine_heic):
        apply_sidecar(genuine_heic, sidecar(lat=41.8781, lon=-87.6298), db,
                      "takeout:x.heic")

        for row in db.get_pending(str(genuine_heic)):
            assert row["file_path"] == str(genuine_heic)
            assert row["file_blake3"]
            payload = json.loads(row["value"])
            if row["field"] == "date":
                assert payload["offset"]
                assert payload["tz_source"]
                assert payload["utc"]
            else:
                assert payload["lat"] == pytest.approx(41.8781)


class TestJpegNamedHeic:
    def test_existing_date_on_the_real_file_is_respected(self, tmp_path, db,
                                                         jpeg_named_heic):
        """The corpus original already has a date; a sidecar must not clobber it."""
        outcome = apply_sidecar(jpeg_named_heic, sidecar(**CHICAGO), db,
                                "takeout:x.heic")

        assert "date" in outcome.already_present

    def test_gps_is_written_despite_the_extension(self, tmp_path, db,
                                                  jpeg_named_heic):
        """The 12% case — extension trust was refusing a write that works."""
        outcome = apply_sidecar(jpeg_named_heic, sidecar(**CHICAGO), db,
                                "takeout:x.heic")

        assert "gps" in outcome.merged
        assert get_exif_gps(jpeg_named_heic) is not None

    def test_date_really_written_despite_extension(self, tmp_path, db,
                                                   jpeg_named_heic_undated):
        """With no date of its own, the sidecar's date must reach the file."""
        outcome = apply_sidecar(jpeg_named_heic_undated, sidecar(**CHICAGO), db,
                                "takeout:x.heic")

        assert "date" in outcome.merged
        assert get_exif_date(jpeg_named_heic_undated) is not None

    def test_written_date_is_local_wall_clock(self, tmp_path, db,
                                              jpeg_named_heic_undated):
        apply_sidecar(jpeg_named_heic_undated, sidecar(**CHICAGO), db,
                      "takeout:x.heic")

        exif = piexif.load(str(jpeg_named_heic_undated))
        assert exif["Exif"][piexif.ExifIFD.DateTimeOriginal] == b"2021:07:04 13:00:00"
        assert exif["Exif"][piexif.ExifIFD.OffsetTimeOriginal] == b"-05:00"


class TestVideoMtimeFallback:
    def test_mp4_gets_phototakentime_as_mtime(self, tmp_path, db, small_mp4):
        """4,741 videos landed with no date at all. One utime recovers them."""
        apply_sidecar(small_mp4, sidecar(), db, "takeout:v.mp4")

        assert os.stat(small_mp4).st_mtime == pytest.approx(CHICAGO_UTC, abs=1)

    def test_merged_mtime_only_is_logged(self, tmp_path, db, small_mp4):
        apply_sidecar(small_mp4, sidecar(), db, "takeout:v.mp4")

        fields = [r["field"] for r in db.conn.execute(
            "SELECT field FROM metadata_log WHERE target_path = ?",
            (str(small_mp4),)).fetchall()]
        assert "merged_mtime_only" in fields

    def test_the_unembeddable_value_is_also_deferred(self, tmp_path, db,
                                                     small_mp4):
        """Both tables, on purpose: mtime is a fallback, not the real thing."""
        outcome = apply_sidecar(small_mp4, sidecar(), db, "takeout:v.mp4")

        assert "date" in outcome.merged_mtime_only
        assert "date" in outcome.deferred

    def test_gps_on_a_video_is_deferred_with_no_mtime_claim(self, tmp_path, db,
                                                            small_mp4):
        outcome = apply_sidecar(small_mp4, sidecar(utc_epoch=None, lat=41.9,
                                                  lon=-87.6),
                                db, "takeout:v.mp4")

        assert "gps" in outcome.deferred
        assert outcome.merged_mtime_only == []


class TestUtcFallbackDualState:
    def test_date_is_written_and_flagged(self, tmp_path, db):
        """No GPS, no sibling: write UTC but record that it was assumed."""
        path = tmp_path / "p.jpg"
        path.write_bytes(make_jpeg_bytes())

        outcome = apply_sidecar(path, sidecar(), db, "takeout:p.jpg")

        assert "date" in outcome.merged
        pending = db.get_pending(str(path))
        assert [p["reason"] for p in pending] == ["tz_unknown_assumed_utc"]
        assert pending[0]["state"] == "deferred"

    def test_offset_written_is_plus_zero(self, tmp_path, db):
        path = tmp_path / "p.jpg"
        path.write_bytes(make_jpeg_bytes())

        apply_sidecar(path, sidecar(), db, "takeout:p.jpg")

        exif = piexif.load(str(path))
        assert exif["Exif"][piexif.ExifIFD.OffsetTimeOriginal] == b"+00:00"

    def test_known_offset_produces_no_pending_row(self, tmp_path, db):
        """The GPS tier resolved it, so there is nothing left outstanding."""
        path = tmp_path / "p.jpg"
        path.write_bytes(make_jpeg_bytes())

        apply_sidecar(path, sidecar(**CHICAGO), db, "takeout:p.jpg")

        assert db.get_pending(str(path)) == []


class TestFailedWrites:
    def test_unparseable_exif_records_failed(self, tmp_path, db,
                                             corrupt_exif_jpeg):
        outcome = apply_sidecar(corrupt_exif_jpeg, sidecar(**CHICAGO), db,
                                "takeout:c.jpg")

        assert "date" in outcome.failed
        pending = db.get_pending(str(corrupt_exif_jpeg))
        assert pending[0]["state"] == "failed"
        assert pending[0]["reason"] == "failed_unparseable"

    def test_file_untouched_after_a_failed_write(self, tmp_path, db,
                                                 corrupt_exif_jpeg):
        before = corrupt_exif_jpeg.read_bytes()

        apply_sidecar(corrupt_exif_jpeg, sidecar(**CHICAGO), db, "takeout:c.jpg")

        assert corrupt_exif_jpeg.read_bytes() == before

    def test_nothing_is_claimed_as_merged(self, tmp_path, db,
                                          corrupt_exif_jpeg):
        outcome = apply_sidecar(corrupt_exif_jpeg, sidecar(**CHICAGO), db,
                                "takeout:c.jpg")

        assert outcome.merged == []


class TestAlreadyPresent:
    def test_existing_date_is_not_overwritten(self, tmp_path, db):
        path = tmp_path / "p.jpg"
        path.write_bytes(make_jpeg_bytes(exif={
            "0th": {}, "Exif": {
                piexif.ExifIFD.DateTimeOriginal: b"1999:12:31 23:59:59"},
            "GPS": {}, "1st": {}}))

        outcome = apply_sidecar(path, sidecar(**CHICAGO), db, "takeout:p.jpg")

        assert "date" in outcome.already_present
        assert get_exif_date(path).startswith("1999-12-31")

    def test_already_present_writes_no_rows(self, tmp_path, db):
        path = tmp_path / "p.jpg"
        path.write_bytes(make_jpeg_bytes(exif={
            "0th": {}, "Exif": {
                piexif.ExifIFD.DateTimeOriginal: b"1999:12:31 23:59:59"},
            "GPS": {}, "1st": {}}))

        apply_sidecar(path, sidecar(), db, "takeout:p.jpg")

        assert db.get_pending(str(path)) == []


class TestGpsMerge:
    def test_gps_written_and_logged(self, tmp_path, db):
        path = tmp_path / "p.jpg"
        path.write_bytes(make_jpeg_bytes())

        outcome = apply_sidecar(path, sidecar(utc_epoch=None, lat=41.8781,
                                              lon=-87.6298),
                                db, "takeout:p.jpg")

        assert "gps" in outcome.merged
        lat, lon = get_exif_gps(path)
        assert lat == pytest.approx(41.8781, abs=1e-4)


# --- The invariant ---

LEGAL_DUAL = {
    frozenset({("log", "merged_mtime_only"), ("pending", "deferred")}),
    frozenset({("log", "merged"), ("pending", "tz_unknown_assumed_utc")}),
}


def _outcome_states(db, file_path: str, field: str) -> set:
    """Every table state recorded for one (file, field) pair."""
    states = set()

    for row in db.conn.execute(
        "SELECT field FROM metadata_log WHERE target_path = ?", (file_path,)
    ).fetchall():
        if row["field"] == field:
            states.add(("log", "merged"))
        elif row["field"] == "merged_mtime_only" and field == "date":
            states.add(("log", "merged_mtime_only"))

    for row in db.conn.execute(
        "SELECT field, state, reason FROM metadata_pending WHERE file_path = ?",
        (file_path,)
    ).fetchall():
        if row["field"] != field:
            continue
        if row["reason"] == "tz_unknown_assumed_utc":
            states.add(("pending", "tz_unknown_assumed_utc"))
        else:
            states.add(("pending", row["state"]))

    return states


def assert_legal(states: set, file_path: str, field: str, already_present: bool):
    """The whole invariant, in one place."""
    if already_present:
        assert not states, (
            f"{Path(file_path).name}/{field}: already had this field, so "
            f"nothing should have been recorded, got {states}")
        return

    assert states, (
        f"{Path(file_path).name}/{field}: SILENT ZERO — the sidecar carried "
        f"this field and no row records what happened to it")

    if len(states) == 1:
        return

    assert frozenset(states) in LEGAL_DUAL, (
        f"{Path(file_path).name}/{field}: illegal dual-table combination "
        f"{states}; only {LEGAL_DUAL} are permitted")


class TestInvariantAcrossAMixedArchive:
    """Ingest a mixed archive, then check every (file, field) outcome."""

    @pytest.fixture
    def ingested(self, tmp_path, db, make_zip, genuine_heic,
                 jpeg_named_heic_undated, small_mp4):
        entries = {
            # plain JPEG with GPS in the sidecar → both fields merge normally
            "Takeout/Photos/plain.jpg": make_jpeg_bytes(color="red"),
            "Takeout/Photos/plain.jpg.supplemental-metadata.json":
                make_sidecar_bytes(CHICAGO_UTC, lat=41.8781, lon=-87.6298),

            # no GPS anywhere → date merges under an assumed UTC offset
            "Takeout/Photos/noloc.jpg": make_jpeg_bytes(color="green"),
            "Takeout/Photos/noloc.jpg.supplemental-metadata.json":
                make_sidecar_bytes(CHICAGO_UTC),

            # genuine HEIC → date cannot be embedded at all
            "Takeout/Photos/real.heic": genuine_heic.read_bytes(),
            "Takeout/Photos/real.heic.supplemental-metadata.json":
                make_sidecar_bytes(CHICAGO_UTC, lat=41.8781, lon=-87.6298),

            # JPEG wearing a .heic name → writes succeed
            "Takeout/Photos/fake.heic": jpeg_named_heic_undated.read_bytes(),
            "Takeout/Photos/fake.heic.supplemental-metadata.json":
                make_sidecar_bytes(CHICAGO_UTC, lat=41.8781, lon=-87.6298),

            # video → mtime fallback plus a deferred row
            "Takeout/Photos/clip.mp4": small_mp4.read_bytes(),
            "Takeout/Photos/clip.mp4.supplemental-metadata.json":
                make_sidecar_bytes(CHICAGO_UTC),

            # truncated suffix + counter → exercises the new matcher
            "Takeout/Photos/DSC_0109(9).JPG": make_jpeg_bytes(color="yellow"),
            "Takeout/Photos/DSC_0109.JPG.supplemental-meta(9).json":
                make_sidecar_bytes(CHICAGO_UTC, lat=41.8781, lon=-87.6298),
        }
        zip_path = make_zip(entries)
        dest = tmp_path / "vault"
        stats = ingest_archive(zip_path, dest, db, allow_unreachable_volumes=True)
        return stats, dest

    def test_every_field_outcome_is_legal(self, ingested, db):
        stats, dest = ingested

        # Rebuild what each kept file was offered, from the archive entries.
        offered = {
            "plain.jpg": {"date", "gps"},
            "noloc.jpg": {"date"},
            "real.heic": {"date", "gps"},
            "fake.heic": {"date", "gps"},
            "clip.mp4": {"date"},
            "DSC_0109(9).JPG": {"date", "gps"},
        }

        checked = 0
        for name, fields in offered.items():
            path = dest / name
            assert path.exists(), f"{name} was not kept by ingest"
            for field in fields:
                states = _outcome_states(db, str(path), field)
                assert_legal(states, str(path), field, already_present=False)
                checked += 1

        # 2 + 1 + 2 + 2 + 1 + 2 — pinned so a fixture that silently stops
        # being ingested cannot quietly shrink the invariant's coverage.
        assert checked == 10

    def test_no_sidecar_went_unmatched(self, ingested, db):
        """Every sidecar in this archive is nameable by the new matcher."""
        assert db.get_unmatched() == []

    def test_video_date_landed_as_mtime(self, ingested, db):
        _, dest = ingested
        assert os.stat(dest / "clip.mp4").st_mtime == pytest.approx(
            CHICAGO_UTC, abs=1)

    def test_genuine_heic_has_a_deferred_date(self, ingested, db):
        _, dest = ingested
        pending = db.get_pending(str(dest / "real.heic"))
        assert {p["field"] for p in pending} == {"date", "gps"}
        assert all(p["state"] == "deferred" for p in pending)

    def test_fake_heic_actually_got_exif(self, ingested, db):
        _, dest = ingested
        assert get_exif_date(dest / "fake.heic") is not None
        assert get_exif_gps(dest / "fake.heic") is not None

    def test_truncated_counter_sidecar_bound(self, ingested, db):
        _, dest = ingested
        assert get_exif_date(dest / "DSC_0109(9).JPG") is not None

    def test_containers_were_identified_by_bytes(self, ingested):
        _, dest = ingested
        assert detect_container(dest / "real.heic") is Container.HEIF
        assert detect_container(dest / "fake.heic") is Container.JPEG


class TestUnmatchedSidecarIsRecorded:
    def test_orphan_sidecar_lands_in_the_table(self, tmp_path, db, make_zip):
        """No media to bind to — the payload must survive for a rebind pass."""
        zip_path = make_zip({
            "Takeout/Photos/keeper.jpg": make_jpeg_bytes(color="blue"),
            "Takeout/Photos/ghost.jpg.supplemental-metadata.json":
                make_sidecar_bytes(CHICAGO_UTC, lat=41.8781, lon=-87.6298),
        })
        ingest_archive(zip_path, tmp_path / "vault", db,
                       allow_unreachable_volumes=True)

        unmatched = db.get_unmatched()
        assert len(unmatched) == 1
        assert unmatched[0]["entry_name"] == \
            "ghost.jpg.supplemental-metadata.json"
        assert unmatched[0]["media_stem"] == "ghost.jpg"

    def test_payload_is_preserved_for_rebinding(self, tmp_path, db, make_zip):
        """The zips are gone after ingest; the table is the only record left."""
        zip_path = make_zip({
            "Takeout/Photos/keeper.jpg": make_jpeg_bytes(color="blue"),
            "Takeout/Photos/ghost.jpg.supplemental-metadata.json":
                make_sidecar_bytes(CHICAGO_UTC, lat=41.8781, lon=-87.6298),
        })
        ingest_archive(zip_path, tmp_path / "vault", db,
                       allow_unreachable_volumes=True)

        payload = json.loads(db.get_unmatched()[0]["payload"])
        assert payload["utc_epoch"] == CHICAGO_UTC
        assert payload["lat"] == pytest.approx(41.8781)

    def test_unmatched_is_counted_in_stats(self, tmp_path, db, make_zip):
        zip_path = make_zip({
            "Takeout/Photos/keeper.jpg": make_jpeg_bytes(color="blue"),
            "Takeout/Photos/ghost.jpg.supplemental-metadata.json":
                make_sidecar_bytes(CHICAGO_UTC),
        })
        stats = ingest_archive(zip_path, tmp_path / "vault", db,
                               allow_unreachable_volumes=True)

        assert stats["sidecars_unmatched"] == 1
