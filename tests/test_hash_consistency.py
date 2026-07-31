"""Finding #11 — indexed hashes must describe the bytes actually on disk.

Ingest writes the file, then writes EXIF into it, then records hashes. The
recorded `blake3_full` was computed from the pre-write source bytes while
head/tail came from the post-write file, so a row described up to three
different byte streams. A later rescan recomputes a different hash and the
same photo is re-ingested as unique.
"""

from pathlib import Path

from memoryvault.database import Database
from memoryvault.hasher import hash_bytes, hash_full, hash_head, hash_tail
from memoryvault.ingest import ingest_archive

from .conftest import make_jpeg_bytes, make_sidecar_bytes

TAKEN_AT = 1_600_000_000


def assert_row_matches_disk(row: dict):
    path = Path(row["path"])
    assert path.exists(), f"indexed file missing: {path}"
    assert row["size"] == path.stat().st_size, "size drifted from disk"
    assert row["blake3_full"] == hash_full(path), "blake3_full drifted from disk"
    assert row["blake3_head"] == hash_head(path), "blake3_head drifted from disk"
    expected_tail = hash_tail(path) if path.stat().st_size > 4096 else hash_head(path)
    assert row["blake3_tail"] == expected_tail, "blake3_tail drifted from disk"


class TestHashesMatchDisk:
    def test_sidecar_before_media(self, db, tmp_path, make_zip):
        photo = make_jpeg_bytes(color="red")
        zp = make_zip({
            "Takeout/photo.jpg.supplemental-metadata.json":
                make_sidecar_bytes(TAKEN_AT, lat=42.02, lon=-87.70),
            "Takeout/photo.jpg": photo,
        })

        stats = ingest_archive(zp, tmp_path / "vault", db)
        assert stats["kept"] == 1
        assert stats["merged_metadata"] > 0, "EXIF was not written; test proves nothing"

        row = db.conn.execute("SELECT * FROM files").fetchone()
        assert_row_matches_disk(dict(row))

    def test_sidecar_after_media(self, db, tmp_path, make_zip):
        """Late-arriving sidecars mutate an already-indexed file."""
        photo = make_jpeg_bytes(color="green")
        zp = make_zip({
            "Takeout/photo.jpg": photo,
            "Takeout/photo.jpg.supplemental-metadata.json":
                make_sidecar_bytes(TAKEN_AT, lat=10.0, lon=20.0),
        })

        stats = ingest_archive(zp, tmp_path / "vault", db)
        assert stats["merged_metadata"] > 0

        row = db.conn.execute("SELECT * FROM files").fetchone()
        assert_row_matches_disk(dict(row))

    def test_rescan_finds_no_new_content(self, db, tmp_path, make_zip):
        """The real symptom: a rescan of the vault must agree with the ingest."""
        from memoryvault.scanner import scan_folder

        photo = make_jpeg_bytes(color="blue")
        vault = tmp_path / "vault"
        zp = make_zip({
            "Takeout/photo.jpg.supplemental-metadata.json": make_sidecar_bytes(TAKEN_AT),
            "Takeout/photo.jpg": photo,
        })
        ingest_archive(zp, vault, db)
        before = db.conn.execute("SELECT blake3_full FROM files").fetchone()["blake3_full"]

        scan_folder(vault, db, source="local")
        after = db.conn.execute("SELECT blake3_full FROM files").fetchone()["blake3_full"]

        assert before == after, "rescan disagreed with ingest about the same file"
        assert db.file_count() == 1


class TestSourceProvenance:
    def test_source_blake3_records_pre_write_bytes(self, db, tmp_path, make_zip):
        photo = make_jpeg_bytes(color="yellow")
        zp = make_zip({
            "Takeout/photo.jpg.supplemental-metadata.json": make_sidecar_bytes(TAKEN_AT),
            "Takeout/photo.jpg": photo,
        })
        ingest_archive(zp, tmp_path / "vault", db)

        row = dict(db.conn.execute("SELECT * FROM files").fetchone())
        assert row["source_blake3"] == hash_bytes(photo), \
            "source_blake3 must hold the bytes as they came out of the archive"
        assert row["blake3_full"] != row["source_blake3"], \
            "EXIF write should have changed the file; otherwise test proves nothing"

    def test_incoming_duplicate_matches_on_source_hash(self, db, tmp_path, make_zip):
        """A second archive carrying the same original bytes must still dedupe."""
        photo = make_jpeg_bytes(color="red")
        sidecar = make_sidecar_bytes(TAKEN_AT)

        first = make_zip({
            "Takeout/photo.jpg.supplemental-metadata.json": sidecar,
            "Takeout/photo.jpg": photo,
        }, name="a.zip")
        ingest_archive(first, tmp_path / "vault", db)

        second = make_zip({"Takeout/other_name.jpg": photo}, name="b.zip")
        stats = ingest_archive(second, tmp_path / "vault", db)

        assert stats["kept"] == 0, "same source bytes were kept twice"
        assert stats["skipped"] == 1


class TestMigrationIdempotency:
    def test_reopening_database_does_not_error(self, tmp_path):
        path = tmp_path / "test.db"
        first = Database(path)
        first.close()
        second = Database(path)  # must not raise
        cols = {r[1] for r in second.conn.execute("PRAGMA table_info(files)")}
        assert "source_blake3" in cols
        second.close()

    def test_migration_adds_column_to_legacy_database(self, tmp_path):
        """Simulate a pre-existing DB that lacks the new columns."""
        import sqlite3

        path = tmp_path / "legacy.db"
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE files (id INTEGER PRIMARY KEY, path TEXT NOT NULL, "
            "size INTEGER NOT NULL, blake3_full TEXT, scan_time TEXT NOT NULL, UNIQUE(path))"
        )
        conn.execute(
            "CREATE TABLE resolutions (id INTEGER PRIMARY KEY, blake3_full TEXT NOT NULL, "
            "winner_path TEXT NOT NULL, action TEXT NOT NULL, resolved_at TEXT NOT NULL, "
            "auto_resolved BOOLEAN DEFAULT FALSE)"
        )
        conn.commit()
        conn.close()

        db = Database(path)
        file_cols = {r[1] for r in db.conn.execute("PRAGMA table_info(files)")}
        res_cols = {r[1] for r in db.conn.execute("PRAGMA table_info(resolutions)")}
        assert "source_blake3" in file_cols
        assert "stale" in res_cols
        db.close()

        Database(path).close()  # second open must also succeed
