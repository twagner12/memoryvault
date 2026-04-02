"""Tests for memoryvault.scanner."""

from pathlib import Path

from memoryvault.database import Database
from memoryvault.scanner import scan_folder


class TestScanner:
    def test_scan_empty_folder(self, tmp_path):
        db = Database(tmp_path / "test.db")
        count = scan_folder(tmp_path / "empty", db)
        # Folder doesn't exist yet, should handle gracefully or scan 0
        # Actually, let's create it
        (tmp_path / "empty").mkdir()
        count = scan_folder(tmp_path / "empty", db)
        assert count == 0
        db.close()

    def test_scan_folder_indexes_files(self, tmp_path):
        data_dir = tmp_path / "photos"
        data_dir.mkdir()
        (data_dir / "a.jpg").write_bytes(b"photo data A")
        (data_dir / "b.jpg").write_bytes(b"photo data B")
        (data_dir / "c.jpg").write_bytes(b"photo data C")

        db = Database(tmp_path / "test.db")
        count = scan_folder(data_dir, db)
        assert count == 3
        assert db.file_count() == 3

        f = db.get_file_by_path(str((data_dir / "a.jpg").resolve()))
        assert f is not None
        assert f["blake3_full"] is not None
        assert f["source"] == "local"
        db.close()

    def test_scan_nested_folders(self, tmp_path):
        data_dir = tmp_path / "photos"
        sub = data_dir / "2012" / "july"
        sub.mkdir(parents=True)
        (sub / "img.jpg").write_bytes(b"nested photo")

        db = Database(tmp_path / "test.db")
        count = scan_folder(data_dir, db)
        assert count == 1
        db.close()

    def test_scan_with_source_label(self, tmp_path):
        data_dir = tmp_path / "photos"
        data_dir.mkdir()
        (data_dir / "a.jpg").write_bytes(b"data")

        db = Database(tmp_path / "test.db")
        scan_folder(data_dir, db, source="takeout")
        f = db.get_file_by_path(str((data_dir / "a.jpg").resolve()))
        assert f["source"] == "takeout"
        db.close()

    def test_scan_progress_callback(self, tmp_path):
        data_dir = tmp_path / "photos"
        data_dir.mkdir()
        (data_dir / "a.jpg").write_bytes(b"data")

        calls = []
        def cb(stage, total, current, error=None):
            calls.append(stage)

        db = Database(tmp_path / "test.db")
        scan_folder(data_dir, db, progress_callback=cb)
        assert "start" in calls
        assert "done" in calls
        db.close()
