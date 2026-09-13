"""Tests for memoryvault.database."""

from pathlib import Path

from memoryvault.database import Database


class TestDatabase:
    def test_create_database(self, tmp_path):
        db = Database(tmp_path / "test.db")
        assert db.file_count() == 0
        db.close()

    def test_upsert_and_get_file(self, tmp_path):
        db = Database(tmp_path / "test.db")
        db.upsert_file(path="/photos/a.jpg", size=1000, blake3_full="abc123",
                       blake3_head="abc", blake3_tail="123")
        f = db.get_file_by_path("/photos/a.jpg")
        assert f is not None
        assert f["size"] == 1000
        assert f["blake3_full"] == "abc123"
        db.close()

    def test_upsert_updates_existing(self, tmp_path):
        db = Database(tmp_path / "test.db")
        db.upsert_file(path="/photos/a.jpg", size=1000, blake3_full="old")
        db.upsert_file(path="/photos/a.jpg", size=2000, blake3_full="new")
        f = db.get_file_by_path("/photos/a.jpg")
        assert f["size"] == 2000
        assert f["blake3_full"] == "new"
        db.close()

    def test_bulk_upsert(self, tmp_path):
        db = Database(tmp_path / "test.db")
        records = [
            {"path": f"/photos/{i}.jpg", "size": i * 100, "blake3_full": f"hash{i}",
             "blake3_head": f"head{i}", "blake3_tail": f"tail{i}"}
            for i in range(100)
        ]
        db.bulk_upsert_files(records)
        assert db.file_count() == 100
        db.close()

    def test_find_duplicate_groups(self, tmp_path):
        db = Database(tmp_path / "test.db")
        db.upsert_file(path="/a.jpg", size=100, blake3_full="samehash",
                       blake3_head="h", blake3_tail="t")
        db.upsert_file(path="/b.jpg", size=100, blake3_full="samehash",
                       blake3_head="h", blake3_tail="t")
        db.upsert_file(path="/c.jpg", size=200, blake3_full="unique",
                       blake3_head="h2", blake3_tail="t2")
        groups = db.find_duplicate_groups()
        assert len(groups) == 1
        assert len(groups[0]) == 2
        db.close()

    def test_get_files_by_size(self, tmp_path):
        db = Database(tmp_path / "test.db")
        db.upsert_file(path="/a.jpg", size=100, blake3_full="h1",
                       blake3_head="h", blake3_tail="t")
        db.upsert_file(path="/b.jpg", size=100, blake3_full="h2",
                       blake3_head="h", blake3_tail="t")
        db.upsert_file(path="/c.jpg", size=200, blake3_full="h3",
                       blake3_head="h", blake3_tail="t")
        matches = db.get_files_by_size(100)
        assert len(matches) == 2
        db.close()

    def test_archive_operations(self, tmp_path):
        db = Database(tmp_path / "test.db")
        aid = db.register_archive("/downloads/takeout.zip", entries_total=50)
        assert aid > 0

        archive = db.get_archive("/downloads/takeout.zip")
        assert archive["status"] == "pending"
        assert archive["entries_total"] == 50

        db.update_archive_status(aid, "in_progress")
        archive = db.get_archive("/downloads/takeout.zip")
        assert archive["status"] == "in_progress"

        db.log_archive_entry(aid, "photos/a.jpg", "kept", kept_path="/dest/a.jpg")
        db.log_archive_entry(aid, "photos/b.jpg", "skipped", skip_reason="duplicate")
        processed = db.get_processed_entries(aid)
        assert len(processed) == 2
        assert "photos/a.jpg" in processed
        db.close()

    def test_metadata_log(self, tmp_path):
        db = Database(tmp_path / "test.db")
        db.log_metadata_merge("/dest/a.jpg", "takeout json", "date", "2012-07-04")
        db.log_metadata_merge("/dest/a.jpg", "duplicate exif", "gps", "41.8,-87.6")
        rows = db.conn.execute(
            "SELECT * FROM metadata_log WHERE target_path = ?", ("/dest/a.jpg",)
        ).fetchall()
        assert len(rows) == 2
        db.close()


import sqlite3

import pytest


class TestBatch:
    """db.batch(): one commit for a unit of work, or none of it (2026-09-13)."""

    @staticmethod
    def _committed_log_rows(db):
        # A second connection sees only what has been committed.
        other = sqlite3.connect(str(db.db_path))
        try:
            return other.execute("SELECT COUNT(*) FROM metadata_log").fetchone()[0]
        finally:
            other.close()

    def test_writes_are_invisible_until_the_batch_ends(self, db):
        with db.batch():
            db.log_metadata_merge("/v/a.jpg", "t", "date", "x")
            db.log_metadata_merge("/v/a.jpg", "t", "gps", "y")
            assert self._committed_log_rows(db) == 0
        assert self._committed_log_rows(db) == 2

    def test_an_exception_rolls_every_write_back(self, db):
        with pytest.raises(RuntimeError):
            with db.batch():
                db.log_metadata_merge("/v/a.jpg", "t", "date", "x")
                raise RuntimeError("boom")
        assert self._committed_log_rows(db) == 0
        assert db.conn.execute("SELECT COUNT(*) FROM metadata_log").fetchone()[0] == 0

    def test_nested_batches_commit_once_at_the_outermost(self, db):
        with db.batch():
            with db.batch():
                db.log_metadata_merge("/v/a.jpg", "t", "date", "x")
            assert self._committed_log_rows(db) == 0
        assert self._committed_log_rows(db) == 1

    def test_outside_a_batch_each_write_still_commits(self, db):
        db.log_metadata_merge("/v/a.jpg", "t", "date", "x")
        assert self._committed_log_rows(db) == 1
