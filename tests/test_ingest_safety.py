"""Finding #1 — a skip decision must be backed by a file that actually exists.

Two failure modes are covered:

  1. A row in `files` whose path is gone (deleted, or on a detached volume)
     must not cause an incoming identical file to be discarded.
  2. When indexed rows point at an unreachable volume, an ingest must refuse
     to start rather than silently dedupe against files nobody can see.
"""

import pytest

from memoryvault.hasher import hash_bytes
from memoryvault.ingest import ingest_archive
from memoryvault.volumes import UnreachableVolumeError

from .conftest import make_jpeg_bytes

DETACHED = "/run/media/tim/Seagate Backup Plus Drive/cloud_imports"


class TestSkipRequiresSurvivingCopy:
    def test_missing_kept_file_is_re_ingested_not_skipped(self, db, tmp_path, make_zip):
        """A hash match against a vanished file must not discard the incoming bytes."""
        photo = make_jpeg_bytes(color="red")
        # Index a row claiming we already own this content, at a path that is gone.
        db.upsert_file(
            path=str(tmp_path / "vanished" / "original.jpg"),
            size=len(photo),
            blake3_full=hash_bytes(photo),
            blake3_head=hash_bytes(photo),
            blake3_tail=hash_bytes(photo),
            source="local",
        )

        zp = make_zip({"Takeout/Google Photos/2021/original.jpg": photo})
        dest = tmp_path / "vault"

        stats = ingest_archive(zp, dest, db)

        assert stats["kept"] == 1, "incoming file was discarded against a missing copy"
        assert stats["skipped"] == 0
        written = list(dest.glob("*.jpg"))
        assert len(written) == 1
        assert written[0].read_bytes() == photo

    def test_re_ingest_records_why_it_did_not_skip(self, db, tmp_path, make_zip):
        """The decision must be queryable afterwards, not silent."""
        photo = make_jpeg_bytes(color="green")
        db.upsert_file(
            path=str(tmp_path / "vanished" / "original.jpg"),
            size=len(photo),
            blake3_full=hash_bytes(photo),
            source="local",
        )

        zp = make_zip({"Takeout/original.jpg": photo})
        ingest_archive(zp, tmp_path / "vault", db)

        row = db.conn.execute(
            "SELECT status, skip_reason FROM archive_entries "
            "WHERE entry_path = 'Takeout/original.jpg'"
        ).fetchone()
        assert row["status"] == "kept"
        assert row["skip_reason"] == "prior_copy_missing"

    def test_present_duplicate_is_still_skipped(self, db, tmp_path, make_zip):
        """The guard must not defeat ordinary deduplication."""
        photo = make_jpeg_bytes(color="blue")
        existing = tmp_path / "vault" / "already_here.jpg"
        existing.parent.mkdir(parents=True)
        existing.write_bytes(photo)
        db.upsert_file(path=str(existing), size=len(photo),
                       blake3_full=hash_bytes(photo), source="local")

        zp = make_zip({"Takeout/copy.jpg": photo})
        stats = ingest_archive(zp, tmp_path / "vault", db)

        assert stats["kept"] == 0
        assert stats["skipped"] == 1


class TestUnreachableVolumeGuard:
    def test_ingest_aborts_when_indexed_volume_is_unreachable(self, db, tmp_path, make_zip):
        for i in range(5):
            db.upsert_file(path=f"{DETACHED}/img_{i}.jpg", size=100 + i,
                           blake3_full=f"hash{i}", source="local")

        zp = make_zip({"Takeout/new.jpg": make_jpeg_bytes()})

        with pytest.raises(UnreachableVolumeError) as exc:
            ingest_archive(zp, tmp_path / "vault", db)

        message = str(exc.value)
        assert "Seagate Backup Plus Drive" in message
        assert "5" in message, "error must state how many rows are affected"

    def test_explicit_override_allows_ingest(self, db, tmp_path, make_zip):
        for i in range(3):
            db.upsert_file(path=f"{DETACHED}/img_{i}.jpg", size=100 + i,
                           blake3_full=f"hash{i}", source="local")

        zp = make_zip({"Takeout/new.jpg": make_jpeg_bytes()})
        stats = ingest_archive(zp, tmp_path / "vault", db,
                               allow_unreachable_volumes=True)
        assert stats["kept"] == 1

    def test_no_abort_when_all_volumes_reachable(self, db, tmp_path, make_zip):
        present = tmp_path / "vault" / "here.jpg"
        present.parent.mkdir(parents=True)
        present.write_bytes(make_jpeg_bytes())
        db.upsert_file(path=str(present), size=10, blake3_full="h", source="local")

        zp = make_zip({"Takeout/new.jpg": make_jpeg_bytes(color="yellow")})
        stats = ingest_archive(zp, tmp_path / "vault", db)
        assert stats["kept"] == 1

    def test_guard_reports_volume_and_row_count(self, db):
        from memoryvault.volumes import find_unreachable_volumes

        for i in range(7):
            db.upsert_file(path=f"{DETACHED}/img_{i}.jpg", size=1,
                           blake3_full=f"h{i}", source="local")

        unreachable = find_unreachable_volumes(db)
        assert len(unreachable) == 1
        assert unreachable[0]["volume"] == "/run/media/tim/Seagate Backup Plus Drive"
        assert unreachable[0]["row_count"] == 7
