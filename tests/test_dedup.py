"""Tests for memoryvault.dedup."""

from memoryvault.database import Database
from memoryvault.dedup import score_file, find_duplicates


class TestScoreFile:
    def test_larger_file_scores_higher(self):
        small = {"path": "/a.jpg", "size": 100_000}
        large = {"path": "/b.jpg", "size": 5_000_000}
        assert score_file(large) > score_file(small)

    def test_raw_scores_higher_than_jpeg(self):
        raw = {"path": "/photo.cr2", "size": 1_000_000}
        jpg = {"path": "/photo.jpg", "size": 1_000_000}
        assert score_file(raw) > score_file(jpg)

    def test_exif_metadata_bonus(self):
        no_meta = {"path": "/a.jpg", "size": 1_000_000}
        with_date = {"path": "/b.jpg", "size": 1_000_000, "has_exif_date": True}
        with_both = {"path": "/c.jpg", "size": 1_000_000, "has_exif_date": True, "has_exif_gps": True}
        assert score_file(with_date) > score_file(no_meta)
        assert score_file(with_both) > score_file(with_date)

    def test_resolution_bonus(self):
        low_res = {"path": "/a.jpg", "size": 1_000_000, "width": 640, "height": 480}
        high_res = {"path": "/b.jpg", "size": 1_000_000, "width": 4000, "height": 3000}
        assert score_file(high_res) > score_file(low_res)


class TestFindDuplicates:
    def test_no_duplicates(self, tmp_path):
        db = Database(tmp_path / "test.db")
        db.upsert_file(path="/a.jpg", size=100, blake3_full="hash1",
                       blake3_head="h1", blake3_tail="t1")
        db.upsert_file(path="/b.jpg", size=200, blake3_full="hash2",
                       blake3_head="h2", blake3_tail="t2")
        results = find_duplicates(db)
        assert results == []
        db.close()

    def test_picks_higher_quality_winner(self, tmp_path):
        db = Database(tmp_path / "test.db")
        db.upsert_file(path="/small.jpg", size=100_000, blake3_full="samehash",
                       blake3_head="h", blake3_tail="t")
        db.upsert_file(path="/large.jpg", size=5_000_000, blake3_full="samehash",
                       blake3_head="h", blake3_tail="t")
        results = find_duplicates(db)
        assert len(results) == 1
        assert results[0]["winner"]["path"] == "/large.jpg"
        assert len(results[0]["losers"]) == 1
        db.close()

    def test_picks_raw_over_jpeg(self, tmp_path):
        db = Database(tmp_path / "test.db")
        db.upsert_file(path="/photo.jpg", size=1_000_000, blake3_full="samehash",
                       blake3_head="h", blake3_tail="t")
        db.upsert_file(path="/photo.cr2", size=1_000_000, blake3_full="samehash",
                       blake3_head="h", blake3_tail="t")
        results = find_duplicates(db)
        assert results[0]["winner"]["path"] == "/photo.cr2"
        db.close()

    def test_multiple_groups(self, tmp_path):
        db = Database(tmp_path / "test.db")
        db.upsert_file(path="/a1.jpg", size=100, blake3_full="group_a",
                       blake3_head="h", blake3_tail="t")
        db.upsert_file(path="/a2.jpg", size=200, blake3_full="group_a",
                       blake3_head="h", blake3_tail="t")
        db.upsert_file(path="/b1.jpg", size=300, blake3_full="group_b",
                       blake3_head="h", blake3_tail="t")
        db.upsert_file(path="/b2.jpg", size=400, blake3_full="group_b",
                       blake3_head="h", blake3_tail="t")
        results = find_duplicates(db)
        assert len(results) == 2
        db.close()
