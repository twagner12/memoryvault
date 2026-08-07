"""Folder ingest, and the two things it must never get wrong.

The first is structural: the source tree is read-only. `ArchiveEntry.temp_path`
means "scratch I own", and the two functions acting on that promise MOVE and
UNLINK. Pointing them at a drive would empty it as a side effect of a read, so
`test_source_tree_is_untouched` is the guard that keeps this path honest.

The second is the invisible one: a collapsed duplicate must not take information
with it. Every rule that runs writes into `adopted` or into `declined`, so an
empty pair is a bug rather than an absence of news — the same invariant
test_metadata_outcomes enforces for metadata.
"""

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import piexif
import pytest

from memoryvault.folder import (
    CollapseOutcome, ingest_folder, iter_source_files, mtime_verdict,
)
from memoryvault.hasher import hash_full
from memoryvault.repair import TAKEOUT_INGEST_WINDOW
from tests.conftest import make_jpeg_bytes

CAPTURE_UTC = int(datetime(2021, 7, 4, 18, 0, 0, tzinfo=timezone.utc).timestamp())
CLOBBERED = TAKEOUT_INGEST_WINDOW[0] + 3600          # inside the Aug 2026 ingest run


def snapshot(root: Path) -> dict:
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            st = p.stat()
            out[str(p.relative_to(root))] = (
                st.st_size, hashlib.sha256(p.read_bytes()).hexdigest())
    return out


def jpeg(path: Path, colour="blue", dto=b"2021:07:04 13:00:00", offset=b"-05:00"):
    exif = {"0th": {}, "Exif": {piexif.ExifIFD.DateTimeOriginal: dto,
                                piexif.ExifIFD.OffsetTimeOriginal: offset},
            "GPS": {}, "1st": {}, "thumbnail": None}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(make_jpeg_bytes(color=colour, exif=exif))
    return path


@pytest.fixture
def vault(tmp_path):
    v = tmp_path / "vault"
    v.mkdir()
    return v


@pytest.fixture
def source(tmp_path):
    s = tmp_path / "drive"
    s.mkdir()
    return s


class TestSourceTreeIsReadOnly:
    def test_source_tree_is_untouched(self, tmp_path, db, vault, source):
        """The hazard guard. If this ever fails, a drive is being emptied."""
        jpeg(source / "a" / "one.jpg", "red")
        jpeg(source / "b" / "two.jpg", "green")
        jpeg(source / "three.jpg", "blue")
        before = snapshot(source)

        ingest_folder(source, vault, db, dry_run=False)

        assert snapshot(source) == before, "folder ingest modified the source tree"

    def test_source_survives_a_collapse(self, tmp_path, db, vault, source):
        """A duplicate is discarded from the vault's point of view — the file on
        the drive must still be there afterwards."""
        kept = jpeg(vault / "one.jpg", "red")
        db.upsert_file(path=str(kept), size=kept.stat().st_size,
                       blake3_full=hash_full(kept), mtime=CLOBBERED,
                       scan_time=datetime.now(timezone.utc).isoformat())
        dup = source / "copy.jpg"
        dup.write_bytes(kept.read_bytes())
        before = snapshot(source)

        stats = ingest_folder(source, vault, db, dry_run=False)

        assert stats["collapsed"] == 1
        assert dup.exists()
        assert snapshot(source) == before

    def test_destination_inside_source_is_refused(self, db, source):
        with pytest.raises(ValueError, match="inside the source"):
            ingest_folder(source, source / "vault", db, dry_run=False)


class TestDryRun:
    def test_dry_run_copies_nothing_and_writes_nothing(self, db, vault, source):
        jpeg(source / "one.jpg")

        stats = ingest_folder(source, vault, db, dry_run=True)

        assert stats["kept"] == 1
        assert list(vault.iterdir()) == []
        assert db.conn.execute("SELECT COUNT(*) c FROM files").fetchone()["c"] == 0
        assert db.conn.execute(
            "SELECT COUNT(*) c FROM dedup_collapse").fetchone()["c"] == 0


class TestKeeping:
    def test_unique_file_is_copied_and_indexed(self, db, vault, source):
        src = jpeg(source / "sub" / "one.jpg")

        ingest_folder(source, vault, db, dry_run=False)

        dest = vault / "one.jpg"
        assert dest.exists()
        assert dest.read_bytes() == src.read_bytes()
        assert db.get_file_by_path(str(dest)) is not None

    def test_second_run_processes_nothing(self, db, vault, source):
        jpeg(source / "one.jpg")
        ingest_folder(source, vault, db, dry_run=False)

        again = ingest_folder(source, vault, db, dry_run=False)

        assert again["seen"] == 0
        assert len(list(vault.iterdir())) == 1


class TestCollapseIsRecorded:
    def _collapse(self, db, vault, source, src_mtime=CAPTURE_UTC,
                  survivor_mtime=CLOBBERED):
        kept = jpeg(vault / "one.jpg")
        os.utime(kept, (survivor_mtime, survivor_mtime))
        db.upsert_file(path=str(kept), size=kept.stat().st_size,
                       blake3_full=hash_full(kept), mtime=survivor_mtime,
                       scan_time=datetime.now(timezone.utc).isoformat())
        dup = source / "copy.jpg"
        dup.write_bytes(kept.read_bytes())
        os.utime(dup, (src_mtime, src_mtime))
        ingest_folder(source, vault, db, dry_run=False)
        return kept, db.conn.execute("SELECT * FROM dedup_collapse").fetchone()

    def test_a_row_is_written_naming_the_survivor(self, db, vault, source):
        kept, row = self._collapse(db, vault, source)
        assert row is not None
        assert row["survivor_path"] == str(kept)      # the link that used to be NULL
        assert row["entry_path"] == "copy.jpg"
        assert row["matched_on"] == "blake3_full"
        assert row["source_root"] == str(source.resolve())

    def test_adopted_or_declined_is_never_both_empty(self, db, vault, source):
        _, row = self._collapse(db, vault, source)
        assert json.loads(row["adopted"]) or json.loads(row["declined"])

    def test_provenance_is_always_recorded(self, db, vault, source):
        """Phase 7 erases the drive; that this file was also seen there is
        information worth keeping."""
        _, row = self._collapse(db, vault, source)
        assert any(a["what"] == "provenance" for a in json.loads(row["adopted"]))


class TestMtimeRule:
    def _outcome(self, src, survivor):
        o = CollapseOutcome()
        return mtime_verdict(src, survivor, o), o

    def test_adopts_a_plausible_source_mtime(self, tmp_path):
        src, dst = jpeg(tmp_path / "s.jpg"), jpeg(tmp_path / "d.jpg")
        os.utime(src, (CAPTURE_UTC, CAPTURE_UTC))
        os.utime(dst, (CLOBBERED, CLOBBERED))

        epoch, o = self._outcome(src, dst)

        assert int(epoch) == CAPTURE_UTC
        assert any(a["what"] == "mtime" for a in o.adopted)

    def test_refuses_when_the_survivor_agrees_with_its_own_exif(self, tmp_path):
        """The primary test, and it needs no window: a survivor whose mtime sits
        a legal UTC offset from its own DateTimeOriginal is describing the
        capture, so it is not ours to overwrite — whatever the date."""
        src, dst = jpeg(tmp_path / "s.jpg"), jpeg(tmp_path / "d.jpg")
        os.utime(src, (CAPTURE_UTC, CAPTURE_UTC))
        os.utime(dst, (CAPTURE_UTC, CAPTURE_UTC))     # exactly -05:00 from its EXIF

        epoch, o = self._outcome(src, dst)

        assert epoch is None
        assert o.declined[0]["reason"] == "survivor_mtime_not_clobbered"
        assert "own" in o.declined[0]["detail"]

    def test_a_survivor_whose_mtime_contradicts_its_exif_is_clobbered(self, tmp_path):
        """Recognised without reference to any ingest window — this is what
        makes the rule outlive the August 2026 run. 99 seconds is not an
        offset that exists."""
        src, dst = jpeg(tmp_path / "s.jpg"), jpeg(tmp_path / "d.jpg")
        os.utime(src, (CAPTURE_UTC, CAPTURE_UTC))
        os.utime(dst, (CAPTURE_UTC - 99, CAPTURE_UTC - 99))

        epoch, o = self._outcome(src, dst)

        assert int(epoch) == CAPTURE_UTC
        assert any(a["what"] == "mtime" for a in o.adopted)

    def test_no_exif_falls_back_to_the_takeout_window(self, tmp_path):
        """With no date of its own, the historical window is all there is."""
        src = tmp_path / "s.jpg"; src.write_bytes(make_jpeg_bytes(color="red"))
        dst = tmp_path / "d.jpg"; dst.write_bytes(make_jpeg_bytes(color="red"))
        os.utime(src, (CAPTURE_UTC, CAPTURE_UTC))
        os.utime(dst, (CLOBBERED, CLOBBERED))

        epoch, o = self._outcome(src, dst)

        assert int(epoch) == CAPTURE_UTC

    def test_no_exif_outside_the_window_is_left_alone(self, tmp_path):
        src = tmp_path / "s.jpg"; src.write_bytes(make_jpeg_bytes(color="red"))
        dst = tmp_path / "d.jpg"; dst.write_bytes(make_jpeg_bytes(color="red"))
        os.utime(src, (CAPTURE_UTC, CAPTURE_UTC))
        os.utime(dst, (CAPTURE_UTC - 500, CAPTURE_UTC - 500))

        epoch, o = self._outcome(src, dst)

        assert epoch is None
        assert o.declined[0]["reason"] == "survivor_mtime_not_clobbered"
        assert "no EXIF" in o.declined[0]["detail"]

    def test_refuses_an_implausible_source_mtime(self, tmp_path):
        src, dst = jpeg(tmp_path / "s.jpg"), jpeg(tmp_path / "d.jpg")
        os.utime(src, (100_000, 100_000))            # 1970
        os.utime(dst, (CLOBBERED, CLOBBERED))

        epoch, o = self._outcome(src, dst)

        assert epoch is None
        assert o.declined[0]["reason"] == "source_mtime_implausible"

    def test_refuses_when_source_mtime_contradicts_its_own_exif(self, tmp_path):
        """Reuses the quantisation rule that caught 110 misaligned files in the
        mtime repair: a real gap is a whole 15-minute offset."""
        src, dst = jpeg(tmp_path / "s.jpg"), jpeg(tmp_path / "d.jpg")
        os.utime(src, (CAPTURE_UTC + int(10.425 * 3600),) * 2)
        os.utime(dst, (CLOBBERED, CLOBBERED))

        epoch, o = self._outcome(src, dst)

        assert epoch is None
        assert o.declined[0]["reason"] == "source_mtime_disagrees_with_its_own_exif"

    def test_survivor_mtime_actually_changes_on_adopt(self, db, vault, source):
        kept = jpeg(vault / "one.jpg")
        os.utime(kept, (CLOBBERED, CLOBBERED))
        db.upsert_file(path=str(kept), size=kept.stat().st_size,
                       blake3_full=hash_full(kept), mtime=CLOBBERED,
                       scan_time=datetime.now(timezone.utc).isoformat())
        digest_before = hash_full(kept)
        dup = source / "copy.jpg"
        dup.write_bytes(kept.read_bytes())
        os.utime(dup, (CAPTURE_UTC, CAPTURE_UTC))

        ingest_folder(source, vault, db, dry_run=False)

        assert int(kept.stat().st_mtime) == CAPTURE_UTC
        assert int(db.get_file_by_path(str(kept))["mtime"]) == CAPTURE_UTC
        assert hash_full(kept) == digest_before        # content never touched


class TestWalk:
    def test_destination_is_excluded_from_the_walk(self, tmp_path, db):
        """A vault nested beside the source must not be ingested into itself."""
        root = tmp_path / "root"
        (root / "src").mkdir(parents=True)
        jpeg(root / "src" / "one.jpg")
        found = iter_source_files(root / "src", exclude=set())
        assert [f.rel for f in found] == ["one.jpg"]

    def test_walk_is_sorted_and_relative(self, tmp_path):
        root = tmp_path / "r"
        jpeg(root / "b" / "2.jpg")
        jpeg(root / "a" / "1.jpg")
        rels = [f.rel for f in iter_source_files(root, exclude=set())]
        assert rels == [str(Path("a/1.jpg")), str(Path("b/2.jpg"))]
