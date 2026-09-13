"""Tests for memoryvault.ingest."""

import io
import json
import zipfile
from pathlib import Path

import piexif
from PIL import Image

from memoryvault.database import Database
from memoryvault.ingest import ingest_archive, is_media_file, is_sidecar_json
from memoryvault.metadata import get_exif_date, get_exif_gps


def _make_jpeg_bytes(exif_dict: dict = None) -> bytes:
    """Create minimal JPEG bytes with optional EXIF."""
    img = Image.new("RGB", (10, 10), color="blue")
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    jpeg_bytes = buf.getvalue()

    if exif_dict:
        exif_bytes = piexif.dump(exif_dict)
        # Insert EXIF into JPEG bytes
        output = io.BytesIO()
        img.save(output, format="JPEG")
        output.seek(0)
        output_bytes = output.getvalue()
        # Use piexif to insert
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            f.write(output_bytes)
            f.flush()
            piexif.insert(exif_bytes, f.name)
            return Path(f.name).read_bytes()

    return jpeg_bytes


def _make_takeout_zip(path: Path, files: dict[str, bytes]) -> Path:
    """Create a zip mimicking Google Takeout structure."""
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return path


class TestHelpers:
    def test_is_media_file(self):
        assert is_media_file("photo.jpg")
        assert is_media_file("VIDEO.MP4")
        assert is_media_file("song.mp3")
        assert not is_media_file("data.json")
        assert not is_media_file("readme.txt")

    def test_is_sidecar_json(self):
        assert is_sidecar_json("photo.jpg.supplemental-metadata.json")
        assert is_sidecar_json("photo.jpg.json")
        assert not is_sidecar_json("photo.jpg")


class TestIngestArchive:
    def test_ingest_unique_files(self, tmp_path):
        jpeg1 = _make_jpeg_bytes()
        jpeg2 = Image.new("RGB", (20, 20), color="red")
        buf = io.BytesIO()
        jpeg2.save(buf, format="JPEG")
        jpeg2_bytes = buf.getvalue()

        zp = _make_takeout_zip(tmp_path / "takeout.zip", {
            "Google Photos/001/photo1.jpg": jpeg1,
            "Google Photos/001/photo2.jpg": jpeg2_bytes,
        })

        dest = tmp_path / "output"
        db = Database(tmp_path / "test.db")
        stats = ingest_archive(zp, dest, db)

        assert stats["kept"] == 2
        assert stats["skipped"] == 0
        assert stats["errors"] == 0
        assert db.file_count() == 2
        db.close()

    def test_ingest_skips_duplicates(self, tmp_path):
        jpeg = _make_jpeg_bytes()

        zp = _make_takeout_zip(tmp_path / "takeout.zip", {
            "Google Photos/001/photo.jpg": jpeg,
            "Google Photos/002/photo.jpg": jpeg,  # exact duplicate
        })

        dest = tmp_path / "output"
        db = Database(tmp_path / "test.db")
        stats = ingest_archive(zp, dest, db)

        assert stats["kept"] == 1
        assert stats["skipped"] == 1
        assert db.file_count() == 1
        db.close()

    def test_ingest_skips_already_in_db(self, tmp_path):
        """Files already scanned locally should be skipped.

        The previously-scanned copy must exist on disk: a skip is only safe if
        the file being kept is really there (finding #1).
        """
        jpeg = _make_jpeg_bytes()

        # Pre-populate DB with this file, and put it where the row claims it is.
        from memoryvault.hasher import hash_bytes
        existing = tmp_path / "existing" / "photo.jpg"
        existing.parent.mkdir(parents=True)
        existing.write_bytes(jpeg)

        db = Database(tmp_path / "test.db")
        full_hash = hash_bytes(jpeg)
        head_hash = hash_bytes(jpeg[:65_536])
        db.upsert_file(
            path=str(existing), size=len(jpeg),
            blake3_full=full_hash, blake3_head=head_hash, blake3_tail=head_hash,
        )

        zp = _make_takeout_zip(tmp_path / "takeout.zip", {
            "Google Photos/001/photo.jpg": jpeg,
        })

        dest = tmp_path / "output"
        stats = ingest_archive(zp, dest, db)

        assert stats["kept"] == 0
        assert stats["skipped"] == 1
        assert db.file_count() == 1  # no new file added
        db.close()

    def test_ingest_applies_sidecar_metadata(self, tmp_path):
        jpeg = _make_jpeg_bytes()
        sidecar = json.dumps({
            "photoTakenTime": {"timestamp": "1341415800"},
            "geoData": {"latitude": 41.8781, "longitude": -87.6298, "altitude": 0},
        }).encode()

        zp = _make_takeout_zip(tmp_path / "takeout.zip", {
            "Google Photos/001/photo.jpg": jpeg,
            "Google Photos/001/photo.jpg.supplemental-metadata.json": sidecar,
        })

        dest = tmp_path / "output"
        db = Database(tmp_path / "test.db")
        stats = ingest_archive(zp, dest, db)

        assert stats["kept"] == 1
        assert stats["merged_metadata"] > 0

        # Check that the saved file has the metadata
        saved = dest / "photo.jpg"
        assert saved.exists()
        date = get_exif_date(saved)
        assert date is not None
        db.close()

    def test_ingest_skips_non_media(self, tmp_path):
        zp = _make_takeout_zip(tmp_path / "takeout.zip", {
            "Google Photos/001/metadata.json": b'{"album": "test"}',
            "Google Photos/001/readme.txt": b"not a media file",
        })

        dest = tmp_path / "output"
        db = Database(tmp_path / "test.db")
        stats = ingest_archive(zp, dest, db)

        assert stats["kept"] == 0
        assert db.file_count() == 0
        db.close()

    def test_resume_skips_processed(self, tmp_path):
        jpeg1 = _make_jpeg_bytes()
        jpeg2 = Image.new("RGB", (15, 15), color="green")
        buf = io.BytesIO()
        jpeg2.save(buf, format="JPEG")
        jpeg2_bytes = buf.getvalue()

        zp = _make_takeout_zip(tmp_path / "takeout.zip", {
            "Google Photos/001/photo1.jpg": jpeg1,
            "Google Photos/001/photo2.jpg": jpeg2_bytes,
        })

        dest = tmp_path / "output"
        db = Database(tmp_path / "test.db")

        # First run
        stats1 = ingest_archive(zp, dest, db)
        assert stats1["kept"] == 2

        # Second run — should skip everything (archive marked complete)
        stats2 = ingest_archive(zp, dest, db)
        assert stats2["kept"] == 0
        assert stats2["skipped"] == 0
        db.close()

    def test_progress_callback(self, tmp_path):
        jpeg = _make_jpeg_bytes()
        zp = _make_takeout_zip(tmp_path / "takeout.zip", {
            "Google Photos/001/photo.jpg": jpeg,
        })

        dest = tmp_path / "output"
        db = Database(tmp_path / "test.db")

        calls = []
        def cb(stage, **kwargs):
            calls.append(stage)

        ingest_archive(zp, dest, db, progress_callback=cb)
        assert "done" in calls
        db.close()

    def test_unique_filenames(self, tmp_path):
        """Two different files with the same name should both be kept."""
        jpeg1 = _make_jpeg_bytes()
        jpeg2 = Image.new("RGB", (30, 30), color="yellow")
        buf = io.BytesIO()
        jpeg2.save(buf, format="JPEG")
        jpeg2_bytes = buf.getvalue()

        zp = _make_takeout_zip(tmp_path / "takeout.zip", {
            "Google Photos/001/photo.jpg": jpeg1,
            "Google Photos/002/photo.jpg": jpeg2_bytes,  # different content, same name
        })

        dest = tmp_path / "output"
        db = Database(tmp_path / "test.db")
        stats = ingest_archive(zp, dest, db)

        assert stats["kept"] == 2
        # Should have photo.jpg and photo(1).jpg
        files = list(dest.iterdir())
        assert len(files) == 2
        db.close()


import pytest


class TestOneCommitPerEntryArchive:
    """Takeout path: one commit per entry, and resume adopts a crash's leftover copy (2026-09-13)."""

    def test_each_entry_costs_one_commit(self, tmp_path):
        counts = []
        for n in (1, 3):
            files = {}
            for i in range(n):
                im = Image.new("RGB", (10 + i, 10), color=("red", "green", "blue")[i])
                buf = io.BytesIO(); im.save(buf, format="JPEG")
                files[f"Google Photos/001/p{i}.jpg"] = buf.getvalue()
            zp = _make_takeout_zip(tmp_path / f"t{n}.zip", files)
            db = Database(tmp_path / f"db{n}.db")
            real = db.conn

            class Counting:
                commits = 0

                def __getattr__(self, name):
                    return getattr(real, name)

                def commit(self):
                    Counting.commits += 1
                    real.commit()

            db.conn = Counting()
            try:
                ingest_archive(zp, tmp_path / f"out{n}", db)
            finally:
                db.conn = real
                db.close()
            counts.append(Counting.commits)
        assert counts[1] - counts[0] == 2, counts

    def test_resume_adopts_the_orphan_instead_of_copying_again(self, tmp_path, monkeypatch):
        zp = _make_takeout_zip(tmp_path / "takeout.zip",
                               {"Google Photos/001/photo1.jpg": _make_jpeg_bytes()})
        dest = tmp_path / "output"
        db = Database(tmp_path / "test.db")
        real_log = db.log_archive_entry

        def killed(archive_id, entry_path, status, **kw):
            if status == "kept":
                raise KeyboardInterrupt("power cut")   # not caught per entry, like a kill
            return real_log(archive_id, entry_path, status, **kw)

        monkeypatch.setattr(db, "log_archive_entry", killed)
        with pytest.raises(KeyboardInterrupt):
            ingest_archive(zp, dest, db)
        assert (dest / "photo1.jpg").exists(), "setup: the copy reached disk"
        assert db.file_count() == 0, "setup: its row rolled back"
        monkeypatch.setattr(db, "log_archive_entry", real_log)

        stats = ingest_archive(zp, dest, db)

        assert stats["kept"] == 1
        assert sorted(p.name for p in dest.iterdir() if p.is_file()) == ["photo1.jpg"]
        assert db.file_count() == 1
        row = db.conn.execute("SELECT skip_reason FROM archive_entries "
                              "WHERE entry_path LIKE '%photo1.jpg'").fetchone()
        assert row[0] == "orphan_adopted"
        db.close()
