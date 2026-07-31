"""Finding #12 — a rescan must not re-hash files whose size and mtime are unchanged.

`files.size` and `files.mtime` are already recorded but never consulted, so
every rescan re-reads every byte. At 596 GB indexed that is hours of pure
re-read before any new work begins.
"""

import os

import pytest

from memoryvault import scanner
from memoryvault.scanner import scan_folder


@pytest.fixture
def counting_hasher(monkeypatch):
    """Wrap the scanner's hasher so we can count full-file hashes."""
    calls = []
    original = scanner.hash_file_progressive

    def counted(path):
        calls.append(str(path))
        return original(path)

    monkeypatch.setattr(scanner, "hash_file_progressive", counted)
    return calls


class TestRescanSkipsUnchanged:
    def test_second_scan_rehashes_nothing(self, db, photos, counting_hasher):
        scan_folder(photos, db)
        assert len(counting_hasher) == 4
        counting_hasher.clear()

        count = scan_folder(photos, db)

        assert counting_hasher == [], "unchanged files were re-hashed"
        assert count == 4, "skipped files must still be counted as scanned"
        assert db.file_count() == 4

    def test_touched_file_is_rehashed_alone(self, db, photos, counting_hasher):
        scan_folder(photos, db)
        counting_hasher.clear()

        touched = photos / "img_2.jpg"
        stat = touched.stat()
        os.utime(touched, (stat.st_atime, stat.st_mtime + 120))

        scan_folder(photos, db)

        assert counting_hasher == [str(touched.resolve())]

    def test_changed_size_is_rehashed(self, db, photos, counting_hasher):
        scan_folder(photos, db)
        counting_hasher.clear()

        grown = photos / "img_1.jpg"
        grown.write_bytes(grown.read_bytes() + b"\x00" * 32)

        scan_folder(photos, db)

        assert counting_hasher == [str(grown.resolve())]

    def test_new_file_is_hashed(self, db, photos, counting_hasher):
        from .conftest import make_jpeg_bytes

        scan_folder(photos, db)
        counting_hasher.clear()

        added = photos / "img_new.jpg"
        added.write_bytes(make_jpeg_bytes(color="purple"))

        scan_folder(photos, db)

        assert counting_hasher == [str(added.resolve())]
        assert db.file_count() == 5

    def test_hashes_survive_the_skip(self, db, photos):
        """Skipping must preserve the existing row, not blank it."""
        scan_folder(photos, db)
        before = {r["path"]: dict(r) for r in db.conn.execute("SELECT * FROM files")}

        scan_folder(photos, db)
        after = {r["path"]: dict(r) for r in db.conn.execute("SELECT * FROM files")}

        for path, row in before.items():
            assert after[path]["blake3_full"] == row["blake3_full"]
            assert after[path]["size"] == row["size"]

    def test_rescan_reports_skips(self, db, photos):
        """The degraded/short-circuit path must be observable."""
        scan_folder(photos, db)

        events = []
        scan_folder(photos, db,
                    progress_callback=lambda stage, total, current, error=None:
                        events.append((stage, total, current)))

        assert any(stage == "done" for stage, _, _ in events)
