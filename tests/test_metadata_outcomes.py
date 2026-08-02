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

  already_present  metadata_log(field='already_present') only — the file
                   already carried the offered value
  merged           metadata_log only
  failed           metadata_pending(state='failed') only
  deferred         metadata_pending(state='deferred') only
  merged_mtime_only + deferred    date onto a container that cannot hold EXIF
  merged + tz_unknown_assumed_utc date written under an assumed UTC offset

Anything else — above all a silent zero, present in neither table — is a bug.

`already_present` is the one state whose cardinality may legitimately exceed
one: two different sidecars can offer two different dates to the same
already-dated file, and each refusal is a separate fact worth recording. What
is forbidden is the *identical* offer recorded twice, which is what a re-ingest
would produce, so repeats are counted separately and rejected on their own.
"""

import json
import os
from collections import Counter
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
    """The 648 cases that left no row anywhere.

    A bound sidecar whose target already carries the offered value was, until
    now, indistinguishable from a sidecar that was never bound at all. Both
    produced silence.
    """

    @pytest.fixture
    def dated_jpeg(self, tmp_path) -> Path:
        path = tmp_path / "p.jpg"
        path.write_bytes(make_jpeg_bytes(exif={
            "0th": {}, "Exif": {
                piexif.ExifIFD.DateTimeOriginal: b"1999:12:31 23:59:59"},
            "GPS": {}, "1st": {}}))
        return path

    def test_existing_date_is_not_overwritten(self, db, dated_jpeg):
        outcome = apply_sidecar(dated_jpeg, sidecar(**CHICAGO), db,
                                "takeout:p.jpg")

        assert "date" in outcome.already_present
        assert get_exif_date(dated_jpeg).startswith("1999-12-31")

    def test_already_present_writes_no_pending_row(self, db, dated_jpeg):
        """Nothing is outstanding: the file has the data, just not ours."""
        apply_sidecar(dated_jpeg, sidecar(), db, "takeout:p.jpg")

        assert db.get_pending(str(dated_jpeg)) == []

    def test_the_refusal_is_recorded(self, db, dated_jpeg):
        apply_sidecar(dated_jpeg, sidecar(), db, "takeout:p.jpg")

        rows = db.conn.execute(
            "SELECT field, value, source_desc FROM metadata_log "
            "WHERE target_path = ?", (str(dated_jpeg),)).fetchall()
        assert [r["field"] for r in rows] == ["already_present"]
        assert rows[0]["source_desc"] == "takeout:p.jpg"

    def test_the_row_says_what_was_offered_and_what_won(self, db, dated_jpeg):
        """Enough to audit the decision without the sidecar or the file."""
        apply_sidecar(dated_jpeg, sidecar(), db, "takeout:p.jpg")

        value = json.loads(db.conn.execute(
            "SELECT value FROM metadata_log WHERE target_path = ?",
            (str(dated_jpeg),)).fetchone()["value"])
        assert value["field"] == "date"
        assert value["offered"] == {"utc_epoch": CHICAGO_UTC}
        assert value["satisfied_by"] == "exif_date"

    def test_gps_already_present_names_exif_gps(self, db, jpeg_with_full_gps):
        apply_sidecar(jpeg_with_full_gps, sidecar(utc_epoch=None, **CHICAGO),
                      db, "takeout:p.jpg")

        value = json.loads(db.conn.execute(
            "SELECT value FROM metadata_log WHERE target_path = ?",
            (str(jpeg_with_full_gps),)).fetchone()["value"])
        assert value["field"] == "gps"
        assert value["satisfied_by"] == "exif_gps"
        assert value["offered"]["lat"] == pytest.approx(CHICAGO["lat"])

    def test_mtime_satisfaction_is_named_as_such(self, db, small_mp4):
        """A video's date lives in its mtime, so that is what satisfied it."""
        apply_sidecar(small_mp4, sidecar(), db, "takeout:v.mp4")
        apply_sidecar(small_mp4, sidecar(), db, "takeout:v.mp4")

        rows = db.conn.execute(
            "SELECT value FROM metadata_log WHERE target_path = ? "
            "AND field = 'already_present'", (str(small_mp4),)).fetchall()
        assert len(rows) == 1
        assert json.loads(rows[0]["value"])["satisfied_by"] == "mtime"

    def test_re_offering_records_nothing_further(self, db, dated_jpeg):
        """The re-ingest guard: identical offer, one row."""
        for _ in range(5):
            apply_sidecar(dated_jpeg, sidecar(), db, "takeout:p.jpg")

        counts = _outcome_states(db, str(dated_jpeg), "date")

        assert counts == Counter({("log", "already_present"): 1})
        assert_legal(counts, str(dated_jpeg), "date", already_present=True)

    def test_a_different_offer_is_recorded_separately(self, db, dated_jpeg):
        """Two sidecars, two different dates, two distinct refusals."""
        apply_sidecar(dated_jpeg, sidecar(), db, "takeout:a.jpg")
        apply_sidecar(dated_jpeg, sidecar(utc_epoch=CHICAGO_UTC + 86_400), db,
                      "takeout:b.jpg")

        counts = _outcome_states(db, str(dated_jpeg), "date")

        assert counts == Counter({("log", "already_present"): 2})
        assert_legal(counts, str(dated_jpeg), "date", already_present=True)

    def test_the_invariant_rejects_an_identical_repeat(self, db, dated_jpeg):
        """Guard the guard: plant the row the suppressed write would have made."""
        apply_sidecar(dated_jpeg, sidecar(), db, "takeout:p.jpg")
        planted = db.conn.execute(
            "SELECT value FROM metadata_log WHERE target_path = ?",
            (str(dated_jpeg),)).fetchone()["value"]
        db.log_metadata_merge(str(dated_jpeg), "takeout:p.jpg",
                              "already_present", planted)

        counts = _outcome_states(db, str(dated_jpeg), "date")

        with pytest.raises(AssertionError, match="already_present"):
            assert_legal(counts, str(dated_jpeg), "date", already_present=True)


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


def _outcome_states(db, file_path: str, field: str) -> Counter:
    """How many times each table state was recorded for one (file, field).

    A Counter, not a set. Folding these into a set is what let the
    double-apply bug through: a sidecar applied twice wrote two identical
    `merged_mtime_only` rows and two identical `deferred` rows, which
    collapsed to exactly the same one-of-each shape a correct single apply
    produces. Presence was never the whole invariant — cardinality is.

    `already_present` rows carry which field they refer to inside their value,
    because `metadata_log.field` is spent naming the outcome. Distinct offers
    are counted under `already_present`; byte-identical repeats — the
    re-ingest failure — are counted under `already_present_repeat`, so the two
    can be judged separately.
    """
    counts: Counter = Counter()
    already_present: Counter = Counter()

    for row in db.conn.execute(
        "SELECT field, value FROM metadata_log WHERE target_path = ?",
        (file_path,)
    ).fetchall():
        if row["field"] == "already_present":
            if json.loads(row["value"])["field"] == field:
                already_present[row["value"]] += 1
        elif row["field"] == field:
            counts[("log", "merged")] += 1
        elif row["field"] == "merged_mtime_only" and field == "date":
            counts[("log", "merged_mtime_only")] += 1

    if already_present:
        counts[("log", "already_present")] = len(already_present)
        repeats = sum(n - 1 for n in already_present.values())
        if repeats:
            counts[("log", "already_present_repeat")] = repeats

    for row in db.conn.execute(
        "SELECT field, state, reason FROM metadata_pending WHERE file_path = ?",
        (file_path,)
    ).fetchall():
        if row["field"] != field:
            continue
        if row["reason"] == "tz_unknown_assumed_utc":
            counts[("pending", "tz_unknown_assumed_utc")] += 1
        else:
            counts[("pending", row["state"])] += 1

    return counts


def assert_legal(counts: Counter, file_path: str, field: str,
                 already_present: bool):
    """The whole invariant, in one place.

    Four separate claims, each with its own failure message:
      - an already-present field records exactly that, and nothing else
      - no offer is recorded twice byte-for-byte
      - a field the sidecar carried records *something* (no silent zero)
      - each state is recorded exactly once, and the combination is legal
    """
    name = Path(file_path).name

    assert ("log", "already_present_repeat") not in counts, (
        f"{name}/{field}: the identical offer was recorded as already_present "
        f"{counts[('log', 'already_present_repeat')] + 1}×. Re-offering a "
        f"sidecar must be a no-op, not another row.")

    if already_present:
        assert set(counts) == {("log", "already_present")}, (
            f"{name}/{field}: the file already carried this field, so the only "
            f"legal record is an already_present log row, got {dict(counts)}")
        return

    assert counts, (
        f"{name}/{field}: SILENT ZERO — the sidecar carried this field and no "
        f"row records what happened to it")

    # already_present is exempt: two sidecars may offer two different values to
    # the same already-populated file, and both refusals deserve a row. An
    # identical repeat is caught above, which is the failure that matters.
    repeated = {state: n for state, n in counts.items()
                if n > 1 and state != ("log", "already_present")}
    assert not repeated, (
        f"{name}/{field}: DUPLICATE OUTCOME — "
        + ", ".join(f"{state} recorded {n}×" for state, n in repeated.items())
        + ". Each state must be recorded exactly once; a repeat means the "
          "value was applied more than once and a drain pass would re-apply it."
    )

    if len(counts) == 1:
        return

    assert frozenset(counts) in LEGAL_DUAL, (
        f"{name}/{field}: illegal dual-table combination "
        f"{set(counts)}; only {LEGAL_DUAL} are permitted")


def _legacy_set_view(counts: Counter) -> set:
    """The old set-based collapse, kept only to prove the blind spot was real.

    This is exactly what `_outcome_states` used to return. It exists so the
    tests below can demonstrate that the previous invariant accepted a
    double-applied value, rather than merely asserting that it did.
    """
    return set(counts)


class TestInvariantCatchesDuplicateRows:
    """The tightened check must reject what the set-based one waved through.

    Models the real double-apply bug: a sidecar that arrived before its media
    was consumed by the media entry *and* re-applied by the rebind post-pass,
    so a video logged two `merged_mtime_only` rows and two `deferred` rows.
    """

    @pytest.fixture
    def double_applied_video(self, tmp_path, db, small_mp4):
        """One correct apply, plus the extra rows a double apply left behind.

        The rows are planted directly rather than by calling `apply_sidecar`
        twice, because the pipeline no longer permits a double apply. The
        checker has to be demonstrable on its own — a guard test that depends
        on the bug still existing stops working the moment it is fixed.
        """
        apply_sidecar(small_mp4, sidecar(), db, "takeout:v.mp4")

        payload = json.dumps({"utc_epoch": CHICAGO_UTC, "offset": "+00:00",
                              "tz_source": "utc_fallback"})
        db.log_metadata_merge(str(small_mp4), "takeout:v.mp4",
                              "merged_mtime_only", payload)
        db.record_pending(str(small_mp4), "date", payload, "unsupported_mp4",
                          "deferred", source_desc="takeout:v.mp4")
        return small_mp4

    def test_setup_really_produced_duplicates(self, double_applied_video, db):
        """Guard the fixture: without duplicates the rest proves nothing."""
        counts = _outcome_states(db, str(double_applied_video), "date")

        assert counts[("log", "merged_mtime_only")] == 2
        assert counts[("pending", "deferred")] == 2

    def test_tightened_check_rejects_it(self, double_applied_video, db):
        counts = _outcome_states(db, str(double_applied_video), "date")

        with pytest.raises(AssertionError, match="DUPLICATE OUTCOME"):
            assert_legal(counts, str(double_applied_video), "date",
                         already_present=False)

    def test_the_old_set_based_check_would_have_passed(self,
                                                       double_applied_video, db):
        """The blind spot, demonstrated rather than asserted.

        Collapsed to a set, a double apply is indistinguishable from the
        legal `merged_mtime_only + deferred` pair — which is precisely why
        the bug survived the original invariant test.
        """
        counts = _outcome_states(db, str(double_applied_video), "date")
        legacy = _legacy_set_view(counts)

        assert frozenset(legacy) in LEGAL_DUAL
        assert len(legacy) == 2

    def test_duplicate_merged_row_alone_is_rejected(self, tmp_path, db):
        """A plain JPEG date written twice — one table, still a duplicate."""
        path = tmp_path / "p.jpg"
        path.write_bytes(make_jpeg_bytes())
        apply_sidecar(path, sidecar(**CHICAGO), db, "takeout:p.jpg")
        # A second identical log row, as a re-apply would leave behind.
        db.log_metadata_merge(str(path), "takeout:p.jpg", "date", "{}")

        counts = _outcome_states(db, str(path), "date")

        assert counts[("log", "merged")] == 2
        with pytest.raises(AssertionError, match="DUPLICATE OUTCOME"):
            assert_legal(counts, str(path), "date", already_present=False)

    def test_duplicate_pending_row_alone_is_rejected(self, tmp_path, db,
                                                     genuine_heic):
        """Two deferred rows would make a drain pass apply the value twice."""
        apply_sidecar(genuine_heic, sidecar(utc_epoch=None, lat=41.9, lon=-87.6),
                      db, "takeout:x.heic")
        db.record_pending(str(genuine_heic), "gps", "{}", "unsupported_heif",
                          "deferred", source_desc="takeout:x.heic")

        counts = _outcome_states(db, str(genuine_heic), "gps")

        assert counts[("pending", "deferred")] == 2
        with pytest.raises(AssertionError, match="DUPLICATE OUTCOME"):
            assert_legal(counts, str(genuine_heic), "gps",
                         already_present=False)

    def test_a_correct_single_apply_still_passes(self, tmp_path, db, small_mp4):
        """No false positive: the legal dual state is one of each."""
        apply_sidecar(small_mp4, sidecar(), db, "takeout:v.mp4")

        counts = _outcome_states(db, str(small_mp4), "date")

        assert counts[("log", "merged_mtime_only")] == 1
        assert counts[("pending", "deferred")] == 1
        assert_legal(counts, str(small_mp4), "date", already_present=False)


class TestInvariantAcrossAMixedArchive:
    """Ingest a mixed archive, then check every (file, field) outcome."""

    @pytest.fixture
    def ingested(self, tmp_path, db, make_zip, genuine_heic,
                 jpeg_named_heic_undated, small_mp4, second_mp4):
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

            # The same shape with the sidecar AHEAD of its media — the
            # ordering that produced the double-apply. Every other pair here
            # is media-first, so without this the tightened cardinality check
            # would never be pointed at the path that had the bug.
            "Takeout/Photos/early.mp4.supplemental-metadata.json":
                make_sidecar_bytes(CHICAGO_UTC),
            "Takeout/Photos/early.mp4": second_mp4.read_bytes(),

            # truncated suffix + counter → exercises the new matcher
            "Takeout/Photos/DSC_0109(9).JPG": make_jpeg_bytes(color="yellow"),
            "Takeout/Photos/DSC_0109.JPG.supplemental-meta(9).json":
                make_sidecar_bytes(CHICAGO_UTC, lat=41.8781, lon=-87.6298),

            # already carries its own date → the sidecar's is refused, and
            # that refusal is the outcome 648 sidecars used to leave unrecorded
            "Takeout/Photos/dated.jpg": make_jpeg_bytes(color="purple", exif={
                "0th": {}, "Exif": {
                    piexif.ExifIFD.DateTimeOriginal: b"1999:12:31 23:59:59"},
                "GPS": {}, "1st": {}}),
            "Takeout/Photos/dated.jpg.supplemental-metadata.json":
                make_sidecar_bytes(CHICAGO_UTC),
        }
        zip_path = make_zip(entries)
        dest = tmp_path / "vault"
        stats = ingest_archive(zip_path, dest, db, allow_unreachable_volumes=True)
        return stats, dest

    def test_every_field_outcome_is_legal(self, ingested, db):
        stats, dest = ingested

        # Rebuild what each kept file was offered, from the archive entries.
        # The bool is whether the file already carried that field.
        offered = {
            "plain.jpg": {"date": False, "gps": False},
            "noloc.jpg": {"date": False},
            "real.heic": {"date": False, "gps": False},
            "fake.heic": {"date": False, "gps": False},
            "clip.mp4": {"date": False},
            "early.mp4": {"date": False},
            "DSC_0109(9).JPG": {"date": False, "gps": False},
            "dated.jpg": {"date": True},
        }

        checked = 0
        for name, fields in offered.items():
            path = dest / name
            assert path.exists(), f"{name} was not kept by ingest"
            for field, present in fields.items():
                states = _outcome_states(db, str(path), field)
                assert_legal(states, str(path), field, already_present=present)
                checked += 1

        # 2 + 1 + 2 + 2 + 1 + 1 + 2 + 1 — pinned so a fixture that silently
        # stops being ingested cannot quietly shrink the invariant's coverage.
        assert checked == 12

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
