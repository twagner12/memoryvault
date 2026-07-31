"""Failure boundaries around sidecar application.

Three things are pinned here, all about a single bad sidecar not being able
to take down or corrupt a run:

1. An apply that raises — in the stream or in the post-pass — is recorded
   against that entry and the run continues. The post-pass in particular must
   still reach `update_archive_status("complete")`, because an archive stuck
   at `in_progress` is indistinguishable from an abandoned one.
2. A sidecar whose JSON cannot be read gets a `sidecars_unmatched` row rather
   than only an `archive_entries.skip_reason`.
3. A sidecar is applied exactly once, whichever order it and its media appear
   in the zip. Takeout interleaves both ways.
"""

from datetime import datetime, timezone

import pytest

import memoryvault.ingest as ingest_mod
from memoryvault.ingest import ingest_archive
from tests.conftest import make_jpeg_bytes, make_sidecar_bytes

TS = int(datetime(2021, 7, 4, 18, 0, 0, tzinfo=timezone.utc).timestamp())

# A stem Google truncated past its extension, so only the fuzzy tier in the
# post-pass can bind it — which is how we get an apply to happen *there*.
TRUNCATED_SIDECAR = "64122699523__7F4958E8-FB3A-4208-8990-7C62C7623.json"
TRUNCATED_MEDIA = "64122699523__7F4958E8-FB3A-4208-8990-7C62C7623FBA.JPG"


def _archive_status(db) -> str:
    return db.conn.execute("SELECT status FROM archives").fetchone()["status"]


def _entry(db, name: str) -> dict:
    row = db.conn.execute(
        "SELECT status, skip_reason FROM archive_entries "
        "WHERE entry_path LIKE ?", (f"%{name}",)).fetchone()
    return dict(row) if row else None


@pytest.fixture
def exploding_apply(monkeypatch):
    """Make `_apply_sidecar_to_file` raise for one chosen destination."""
    real = ingest_mod._apply_sidecar_to_file

    def _install(marker: str):
        def fake(dest_path, sidecar, source_desc, db, siblings=None):
            if marker in str(dest_path):
                raise OSError(f"simulated failure applying to {dest_path}")
            return real(dest_path, sidecar, source_desc, db, siblings=siblings)

        monkeypatch.setattr(ingest_mod, "_apply_sidecar_to_file", fake)

    return _install


class TestPostPassFailureIsContained:
    @pytest.fixture
    def ingested(self, tmp_path, db, make_zip, exploding_apply):
        exploding_apply("BOOM")
        zip_path = make_zip({
            # Fuzzy-bound in the post-pass, and rigged to raise there.
            "T/P/BOOM_7F4958E8-FB3A-4208-8990-7C62C7623FBA.JPG":
                make_jpeg_bytes(color="red"),
            "T/P/BOOM_7F4958E8-FB3A-4208-8990-7C62C7623.json":
                make_sidecar_bytes(TS, lat=41.8781, lon=-87.6298),
            # Also fuzzy-bound in the post-pass, and must still succeed.
            f"T/P/{TRUNCATED_MEDIA}": make_jpeg_bytes(color="green"),
            f"T/P/{TRUNCATED_SIDECAR}": make_sidecar_bytes(TS),
        })
        stats = ingest_archive(zip_path, tmp_path / "vault", db,
                               allow_unreachable_volumes=True)
        return stats, tmp_path / "vault"

    def test_archive_still_completes(self, ingested, db):
        """A single bad sidecar must not leave the archive in_progress."""
        assert _archive_status(db) == "complete"

    def test_failure_recorded_against_the_entry(self, ingested, db):
        entry = _entry(db, "BOOM_7F4958E8-FB3A-4208-8990-7C62C7623.json")
        assert entry["status"] == "error"
        assert "simulated failure" in entry["skip_reason"]

    def test_error_counted(self, ingested):
        stats, _ = ingested
        assert stats["errors"] == 1

    def test_tally_still_reconciles(self, ingested):
        """kept + skipped + errors must still equal processed_count."""
        stats, _ = ingested
        assert stats["verified"] is True

    def test_the_other_sidecar_still_applied(self, ingested, db):
        """The pass continues past the failure rather than aborting."""
        from memoryvault.metadata import get_exif_date
        _, vault = ingested
        assert get_exif_date(vault / TRUNCATED_MEDIA) is not None


class TestStreamFailureIsContained:
    @pytest.fixture
    def ingested(self, tmp_path, db, make_zip, exploding_apply):
        exploding_apply("BOOM")
        zip_path = make_zip({
            # Media first, so the sidecar binds during the stream — and blows
            # up there rather than in the post-pass.
            "T/P/BOOM.jpg": make_jpeg_bytes(color="red"),
            "T/P/BOOM.jpg.supplemental-metadata.json": make_sidecar_bytes(TS),
            "T/P/fine.jpg": make_jpeg_bytes(color="blue"),
            "T/P/fine.jpg.supplemental-metadata.json": make_sidecar_bytes(TS),
        })
        stats = ingest_archive(zip_path, tmp_path / "vault", db,
                               allow_unreachable_volumes=True)
        return stats, tmp_path / "vault"

    def test_archive_still_completes(self, ingested, db):
        assert _archive_status(db) == "complete"

    def test_failure_recorded_against_the_entry(self, ingested, db):
        entry = _entry(db, "BOOM.jpg.supplemental-metadata.json")
        assert entry["status"] == "error"
        assert "simulated failure" in entry["skip_reason"]

    def test_error_counted_not_skipped(self, ingested):
        stats, _ = ingested
        assert stats["errors"] == 1

    def test_tally_still_reconciles(self, ingested):
        stats, _ = ingested
        assert stats["verified"] is True

    def test_later_sidecar_unaffected(self, ingested, db):
        from memoryvault.metadata import get_exif_date
        _, vault = ingested
        assert get_exif_date(vault / "fine.jpg") is not None

    def test_failed_sidecar_is_not_retried_as_unmatched(self, ingested, db):
        """It is already recorded as an error; it must not double-report."""
        names = [u["entry_name"] for u in db.get_unmatched()]
        assert "BOOM.jpg.supplemental-metadata.json" not in names


class TestUnreadableSidecarIsRecorded:
    @pytest.fixture
    def ingested(self, tmp_path, db, make_zip):
        zip_path = make_zip({
            "T/P/keeper.jpg": make_jpeg_bytes(color="blue"),
            # Truncated JSON — json.loads raises.
            "T/P/broken.jpg.supplemental-metadata.json":
                b'{"photoTakenTime": {"timestamp": "162542',
            # Valid JSON bytes, but not decodable as UTF-8.
            "T/P/badbytes.jpg.supplemental-metadata.json":
                b'\xff\xfe{"photoTakenTime": {}}',
            # Zero-length.
            "T/P/empty.jpg.supplemental-metadata.json": b"",
        })
        stats = ingest_archive(zip_path, tmp_path / "vault", db,
                               allow_unreachable_volumes=True)
        return stats, tmp_path / "vault"

    def test_all_three_land_in_sidecars_unmatched(self, ingested, db):
        names = {u["entry_name"] for u in db.get_unmatched()}
        assert names == {
            "broken.jpg.supplemental-metadata.json",
            "badbytes.jpg.supplemental-metadata.json",
            "empty.jpg.supplemental-metadata.json",
        }

    def test_reason_is_unparseable(self, ingested, db):
        assert {u["reason"] for u in db.get_unmatched()} == {"unparseable"}

    def test_payload_is_null(self, ingested, db):
        """There is nothing to rebind from — the JSON never parsed."""
        assert all(u["payload"] is None for u in db.get_unmatched())

    def test_name_fields_populated_as_far_as_parsing_got(self, ingested, db):
        rows = {u["entry_name"]: u for u in db.get_unmatched()}
        row = rows["broken.jpg.supplemental-metadata.json"]
        assert row["media_stem"] == "broken.jpg"
        assert row["archive_dir"] == "T/P"
        assert row["sidecar_path"] == \
            "T/P/broken.jpg.supplemental-metadata.json"

    def test_counted_in_stats(self, ingested):
        stats, _ = ingested
        assert stats["sidecars_unmatched"] == 3

    def test_run_still_completes(self, ingested, db):
        assert _archive_status(db) == "complete"

    def test_good_file_unaffected(self, ingested, db):
        _, vault = ingested
        assert (vault / "keeper.jpg").exists()


class TestSidecarAppliedExactlyOnce:
    """Takeout interleaves; neither order may apply a sidecar twice.

    Caught by probing the real pipeline: with the sidecar ahead of its media,
    the media consumed it during the stream *and* the post-pass re-resolved
    and re-applied it. For a JPEG that is a harmless no-op, because the date
    is then already present — but a video has no such guard, so it logged two
    `merged_mtime_only` rows and two `deferred` pending rows, inflating both
    tables and making a future drain pass apply the same value twice.
    """

    def _ingest(self, tmp_path, db, make_zip, entries):
        ingest_archive(make_zip(entries), tmp_path / "vault", db,
                       allow_unreachable_volumes=True)
        return tmp_path / "vault"

    def _rows(self, db, path):
        logs = db.conn.execute(
            "SELECT field FROM metadata_log WHERE target_path = ?",
            (str(path),)).fetchall()
        return [r["field"] for r in logs], db.get_pending(str(path))

    def test_video_sidecar_before_media(self, tmp_path, db, make_zip, small_mp4):
        vault = self._ingest(tmp_path, db, make_zip, {
            "T/P/clip.mp4.supplemental-metadata.json": make_sidecar_bytes(TS),
            "T/P/clip.mp4": small_mp4.read_bytes(),
        })
        logs, pending = self._rows(db, vault / "clip.mp4")

        assert logs == ["merged_mtime_only"]
        assert len(pending) == 1

    def test_video_media_before_sidecar(self, tmp_path, db, make_zip, small_mp4):
        vault = self._ingest(tmp_path, db, make_zip, {
            "T/P/clip.mp4": small_mp4.read_bytes(),
            "T/P/clip.mp4.supplemental-metadata.json": make_sidecar_bytes(TS),
        })
        logs, pending = self._rows(db, vault / "clip.mp4")

        assert logs == ["merged_mtime_only"]
        assert len(pending) == 1

    def test_jpeg_sidecar_before_media(self, tmp_path, db, make_zip):
        vault = self._ingest(tmp_path, db, make_zip, {
            "T/P/p.jpg.supplemental-metadata.json":
                make_sidecar_bytes(TS, lat=41.8781, lon=-87.6298),
            "T/P/p.jpg": make_jpeg_bytes(color="red"),
        })
        logs, _ = self._rows(db, vault / "p.jpg")

        assert sorted(logs) == ["date", "gps"]

    def test_duplicate_entry_does_not_re_date_the_survivor(self, tmp_path, db,
                                                           make_zip, small_mp4):
        """A deduped copy contributing its own sidecar must not re-apply.

        Two byte-identical videos, each with its own sidecar. The second is
        skipped as a duplicate, and `_try_merge_from_duplicate` offers its
        sidecar to the surviving copy — which already has the date. The EXIF
        paths are guarded by `get_exif_date`; the mtime path had no
        equivalent, so the survivor collected a second `merged_mtime_only`
        row and a second deferred value.

        Live-DB relevance: 51,863 entries are already skipped as duplicates,
        and archives 2 and 3 are literally the same zip.
        """
        ingest_archive(make_zip({
            "T/P/clip.mp4": small_mp4.read_bytes(),
            "T/P/clip.mp4.supplemental-metadata.json": make_sidecar_bytes(TS),
            "T/P/dupe.mp4.supplemental-metadata.json": make_sidecar_bytes(TS),
            "T/P/dupe.mp4": small_mp4.read_bytes(),
        }), tmp_path / "vault", db, allow_unreachable_volumes=True)

        survivor = str(tmp_path / "vault" / "clip.mp4")
        logs = [r["field"] for r in db.conn.execute(
            "SELECT field FROM metadata_log WHERE target_path = ?",
            (survivor,)).fetchall()]

        assert logs == ["merged_mtime_only"]
        assert len(db.get_pending(survivor)) == 1

    def test_duplicate_entry_with_a_jpeg_survivor(self, tmp_path, db, make_zip):
        """The EXIF path already had this guard; pin it so it stays."""
        ingest_archive(make_zip({
            "T/P/a.jpg": make_jpeg_bytes(color="red"),
            "T/P/a.jpg.supplemental-metadata.json": make_sidecar_bytes(TS),
            "T/P/b.jpg.supplemental-metadata.json": make_sidecar_bytes(TS),
            "T/P/b.jpg": make_jpeg_bytes(color="red"),
        }), tmp_path / "vault", db, allow_unreachable_volumes=True)

        survivor = str(tmp_path / "vault" / "a.jpg")
        logs = [r["field"] for r in db.conn.execute(
            "SELECT field FROM metadata_log WHERE target_path = ?",
            (survivor,)).fetchall()]

        assert logs.count("date") == 1

    def test_sidecar_counted_once_in_stats(self, tmp_path, db, make_zip,
                                           small_mp4):
        stats = ingest_archive(
            make_zip({
                "T/P/clip.mp4.supplemental-metadata.json": make_sidecar_bytes(TS),
                "T/P/clip.mp4": small_mp4.read_bytes(),
            }),
            tmp_path / "vault", db, allow_unreachable_volumes=True)

        assert stats["sidecars_matched"] == 1
        assert stats["sidecars_unmatched"] == 0
        assert stats["metadata_deferred"] == 1
