"""Re-offering the same sidecar records nothing new and re-reads nothing.

The duplicate-merge path offers a sidecar to a surviving copy every time a
byte-identical file arrives, which the corpus does constantly. Every branch of
`apply_sidecar` is guarded against that except the GPS deferral, which had two
costs per re-offer: a second identical `metadata_pending` row — enough on its
own to make a drain pass apply the location twice — and a full `hash_full()`
re-read of the file, because the hash was computed as a call argument and so
ran before anything could decide not to insert.

The hash counter here is the load-bearing half. A guard that skips the INSERT
but still hashes would pass a row-count assertion while leaving the per-file
cost of a re-offer unchanged.
"""

import json

import pytest

from memoryvault.hasher import hash_full
from memoryvault.ingest import apply_sidecar
from tests.conftest import make_jpeg_bytes

CHICAGO_UTC = 1625421600
GPS = {"lat": 41.8781, "lon": -87.6298}


def gps_sidecar(lat=GPS["lat"], lon=GPS["lon"]):
    """A sidecar carrying only a location — no date, so only the GPS branch runs."""
    return {"utc_epoch": None, "lat": lat, "lon": lon}


@pytest.fixture
def count_hashes(monkeypatch):
    """Count `hash_full` calls made from within ingest.

    Patched on `memoryvault.ingest`, not on `memoryvault.hasher`: ingest does
    `from memoryvault.hasher import hash_full`, so the name it actually calls
    lives in its own module namespace.
    """
    calls = []

    def counting_hash_full(path, *args, **kwargs):
        calls.append(str(path))
        return hash_full(path, *args, **kwargs)

    monkeypatch.setattr("memoryvault.ingest.hash_full", counting_hash_full)
    return calls


class TestGpsDeferralIsIdempotent:
    """A genuine HEIC cannot hold EXIF, so GPS can only ever be deferred."""

    def test_second_offer_records_no_second_row(self, db, genuine_heic):
        apply_sidecar(genuine_heic, gps_sidecar(), db, "takeout:x.heic")
        apply_sidecar(genuine_heic, gps_sidecar(), db, "takeout:x.heic")

        pending = db.get_pending(str(genuine_heic))
        assert [p["field"] for p in pending] == ["gps"]

    def test_second_offer_still_reports_the_value_as_deferred(self, db,
                                                              genuine_heic):
        """Suppressing the row must not turn into a silent zero.

        The value is outstanding either way; the caller has to hear that, or a
        re-offer would look like the sidecar carried nothing.
        """
        apply_sidecar(genuine_heic, gps_sidecar(), db, "takeout:x.heic")
        outcome = apply_sidecar(genuine_heic, gps_sidecar(), db, "takeout:x.heic")

        assert outcome.deferred == ["gps"]

    def test_second_offer_does_not_re_hash_the_file(self, db, genuine_heic,
                                                    count_hashes):
        apply_sidecar(genuine_heic, gps_sidecar(), db, "takeout:x.heic")
        assert count_hashes == [str(genuine_heic)], "first offer must hash once"

        apply_sidecar(genuine_heic, gps_sidecar(), db, "takeout:x.heic")

        assert count_hashes == [str(genuine_heic)], (
            "the re-offer re-hashed the file; the guard has to run before the "
            "hash, not just before the INSERT")

    def test_ten_offers_cost_one_row_and_one_hash(self, db, genuine_heic,
                                                  count_hashes):
        """The duplicate-merge path can offer the same sidecar many times."""
        for _ in range(10):
            apply_sidecar(genuine_heic, gps_sidecar(), db, "takeout:x.heic")

        assert len(db.get_pending(str(genuine_heic))) == 1
        assert len(count_hashes) == 1

    def test_a_different_location_is_still_recorded(self, db, genuine_heic):
        """The guard keys on the value, so a genuinely new offer gets through."""
        apply_sidecar(genuine_heic, gps_sidecar(), db, "takeout:x.heic")
        apply_sidecar(genuine_heic, gps_sidecar(lat=48.8584, lon=2.2945), db,
                      "takeout:x.heic")

        values = [json.loads(p["value"])["lat"]
                  for p in db.get_pending(str(genuine_heic))]
        assert sorted(values) == pytest.approx([41.8781, 48.8584])

    def test_an_applied_row_does_not_suppress_a_new_one(self, db, genuine_heic):
        """Only *outstanding* rows count.

        Once a drain pass has applied a value and stamped `applied_at`, the
        file's location is no longer outstanding — a later offer of the same
        value is new information about the file's current state, not a repeat.
        """
        apply_sidecar(genuine_heic, gps_sidecar(), db, "takeout:x.heic")
        db.conn.execute(
            "UPDATE metadata_pending SET applied_at = '2026-01-01T00:00:00Z'")
        db.conn.commit()

        apply_sidecar(genuine_heic, gps_sidecar(), db, "takeout:x.heic")

        assert len(db.get_pending(str(genuine_heic))) == 1
        assert len(db.get_pending(str(genuine_heic), outstanding_only=False)) == 2


class TestFailedWriteIsIdempotent:
    """The same guard, on the sibling branch that records a refused write.

    A corrupt-EXIF JPEG re-offered from the duplicate-merge path used to write
    a second identical `failed` row — the same duplicate-outcome shape the
    invariant test rejects, reached by a different door.
    """

    def test_second_offer_records_no_second_failed_row(self, db,
                                                       corrupt_exif_jpeg):
        sidecar = {"utc_epoch": CHICAGO_UTC, **GPS}
        apply_sidecar(corrupt_exif_jpeg, sidecar, db, "takeout:c.jpg")
        apply_sidecar(corrupt_exif_jpeg, sidecar, db, "takeout:c.jpg")

        pending = db.get_pending(str(corrupt_exif_jpeg))
        assert sorted(p["field"] for p in pending) == ["date", "gps"]
        assert all(p["state"] == "failed" for p in pending)

    def test_second_offer_does_not_re_hash(self, db, corrupt_exif_jpeg,
                                           count_hashes):
        sidecar = {"utc_epoch": CHICAGO_UTC, **GPS}
        apply_sidecar(corrupt_exif_jpeg, sidecar, db, "takeout:c.jpg")
        first = len(count_hashes)

        apply_sidecar(corrupt_exif_jpeg, sidecar, db, "takeout:c.jpg")

        assert len(count_hashes) == first


class TestGuardDoesNotAffectFirstOffer:
    """No behaviour change on the path that was already correct."""

    def test_writable_file_still_merges_normally(self, db, tmp_path):
        path = tmp_path / "p.jpg"
        path.write_bytes(make_jpeg_bytes())

        outcome = apply_sidecar(path, {"utc_epoch": CHICAGO_UTC, **GPS}, db,
                                "takeout:p.jpg")

        assert outcome.merged == ["date", "gps"]
        assert db.get_pending(str(path)) == []

    def test_first_deferral_is_recorded_in_full(self, db, genuine_heic):
        apply_sidecar(genuine_heic, gps_sidecar(), db, "takeout:x.heic")

        row = db.get_pending(str(genuine_heic))[0]
        assert row["state"] == "deferred"
        assert row["reason"] == "unsupported_heif"
        assert row["file_blake3"], "a recorded row must still carry its hash"
        assert row["source_desc"] == "takeout:x.heic"
