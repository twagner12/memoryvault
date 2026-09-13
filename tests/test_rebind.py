"""Rebinding sidecars whose media arrived in a different archive (#6).

Google splits one album directory across zip parts, so a sidecar routinely
lands in part 9 while the photo it describes is in part 8. Ingest sees one part
at a time and cannot bind across that boundary, so the sidecar is recorded in
`sidecars_unmatched` with its payload. Every one of the 312 rebindable
sidecars in the shakedown corpus is exactly this shape — 27 part-9 sidecars
whose media came from part 8, and 285 the other way round.

The pass is deliberately not a second matcher. It reuses `resolve_media_name`,
so counter arithmetic, truncated suffixes, the fuzzy prefix tier and its
refusal on ambiguity all behave identically to ingest, and a bind outside the
sidecar's own archive directory remains impossible.
"""

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from memoryvault.ingest import ingest_archive
from memoryvault.metadata import get_exif_date, get_exif_gps
from memoryvault.rebind import rebind_sidecars
from tests.conftest import make_jpeg_bytes, make_sidecar_bytes

TS = int(datetime(2021, 7, 4, 18, 0, 0, tzinfo=timezone.utc).timestamp())
CHICAGO = {"lat": 41.8781, "lon": -87.6298}


@pytest.fixture
def vault(tmp_path) -> Path:
    return tmp_path / "vault"


@pytest.fixture
def ingest(db, vault, make_zip):
    """Ingest one archive's worth of entries and return the vault."""
    def _ingest(entries, name=None):
        ingest_archive(make_zip(entries, name=name), vault, db,
                       allow_unreachable_volumes=True)
        return vault
    return _ingest


class TestTheCrossArchiveBind:
    """The shape all 312 shakedown rebinds take."""

    @pytest.fixture
    def split_across_archives(self, db, ingest):
        # Part 1: the sidecar, with no media in its directory anywhere yet.
        ingest({"T/P/img.jpg.supplemental-metadata.json":
                make_sidecar_bytes(TS, **CHICAGO)})
        assert len(db.get_unmatched()) == 1, "setup: sidecar must go unmatched"

        # Part 2: the photo it describes, same archive directory.
        return ingest({"T/P/img.jpg": make_jpeg_bytes(color="red")})

    def test_it_binds(self, db, split_across_archives):
        stats = rebind_sidecars(db)

        assert stats["bound"] == 1
        assert stats["refused"] == {}

    def test_the_metadata_actually_reaches_the_file(self, db,
                                                    split_across_archives):
        rebind_sidecars(db)

        photo = split_across_archives / "img.jpg"
        assert get_exif_date(photo).startswith("2021-07-04")
        assert get_exif_gps(photo) is not None

    def test_the_row_is_marked_and_stops_being_unmatched(self, db,
                                                         split_across_archives):
        rebind_sidecars(db)

        assert db.get_unmatched() == []

    def test_provenance_records_where_it_bound(self, db,
                                               split_across_archives):
        rebind_sidecars(db)

        row = db.conn.execute(
            "SELECT rebound_at, rebound_path, rebound_outcome "
            "FROM sidecars_unmatched").fetchone()
        assert row["rebound_at"]
        assert row["rebound_path"] == str(split_across_archives / "img.jpg")
        assert json.loads(row["rebound_outcome"])["merged"] == ["date", "gps"]

    def test_binding_is_attributed_to_the_sidecars_archive(
            self, db, split_across_archives):
        """The 27 / 285 split in the acceptance is read off this."""
        stats = rebind_sidecars(db)

        assert stats["by_archive"] == {1: 1}


class TestSameDirectorySemantics:
    """A bind outside the sidecar's own archive directory must be impossible."""

    def test_same_stem_in_another_directory_is_refused(self, db, ingest):
        ingest({"T/P/img.jpg.supplemental-metadata.json":
                make_sidecar_bytes(TS, **CHICAGO)})
        vault = ingest({"T/Q/img.jpg": make_jpeg_bytes(color="red")})

        stats = rebind_sidecars(db)

        assert stats["bound"] == 0
        assert stats["refused"] == {"no_media_in_dir": 1}
        assert get_exif_date(vault / "img.jpg") is None

    def test_the_refused_row_is_left_for_a_later_run(self, db, ingest):
        ingest({"T/P/img.jpg.supplemental-metadata.json":
                make_sidecar_bytes(TS, **CHICAGO)})
        ingest({"T/Q/img.jpg": make_jpeg_bytes(color="red")})

        rebind_sidecars(db)

        assert len(db.get_unmatched()) == 1

    def test_media_known_in_the_directory_but_never_kept_is_refused(
            self, db, ingest):
        """The name is right and the directory is right — but we have no file.

        The entry was skipped as a duplicate of a copy kept from a *different*
        directory, so `archive_entries` has no `kept_path` for it. Binding by
        content instead would be a cross-directory bind through the back door.
        """
        shared = make_jpeg_bytes(color="blue")
        ingest({"T/Q/img.jpg": shared})                       # kept here
        ingest({"T/P/img.jpg.supplemental-metadata.json":
                make_sidecar_bytes(TS, **CHICAGO)})           # sidecar, no media
        ingest({"T/P/img.jpg": shared})                       # skipped: duplicate

        stats = rebind_sidecars(db)

        assert stats["bound"] == 0
        assert stats["refused"] == {"media_not_kept": 1}


class TestMatcherRulesAreInherited:
    def test_counter_arithmetic_still_works(self, db, ingest):
        """`DSC_0109.JPG` + `(9)` names `DSC_0109(9).JPG`, truncation and all."""
        ingest({"T/P/DSC_0109.JPG.supplemental-meta(9).json":
                make_sidecar_bytes(TS, **CHICAGO)})
        vault = ingest({"T/P/DSC_0109(9).JPG": make_jpeg_bytes(color="red")})

        stats = rebind_sidecars(db)

        assert stats["bound"] == 1
        assert get_exif_date(vault / "DSC_0109(9).JPG") is not None

    def test_a_counter_never_lands_on_the_counterless_file(self, db, ingest):
        """`DSC_1570(1).JPG` and `DSC_1570.JPG` are two different photos."""
        ingest({"T/P/DSC_1570.JPG.supplemental-metadata(1).json":
                make_sidecar_bytes(TS, **CHICAGO)})
        vault = ingest({"T/P/DSC_1570.JPG": make_jpeg_bytes(color="red")})

        stats = rebind_sidecars(db)

        assert stats["bound"] == 0
        assert get_exif_date(vault / "DSC_1570.JPG") is None

    def test_two_prefix_candidates_refuse_rather_than_pick(self, db, ingest):
        """The length cap ate the extension; two files match. Refuse."""
        ingest({"T/P/64122699523__7F4958E8.json":
                make_sidecar_bytes(TS, **CHICAGO)})
        vault = ingest({
            "T/P/64122699523__7F4958E8A.JPG": make_jpeg_bytes(color="red"),
            "T/P/64122699523__7F4958E8B.JPG": make_jpeg_bytes(color="green"),
        })

        stats = rebind_sidecars(db)

        assert stats["bound"] == 0
        assert stats["refused"] == {"ambiguous": 1}
        assert get_exif_date(vault / "64122699523__7F4958E8A.JPG") is None
        assert get_exif_date(vault / "64122699523__7F4958E8B.JPG") is None

    def test_an_unambiguous_truncated_stem_does_bind(self, db, ingest):
        """The fuzzy tier is inherited too, not just its refusals."""
        ingest({"T/P/64122699523__7F4958E8.json":
                make_sidecar_bytes(TS, **CHICAGO)})
        vault = ingest({
            "T/P/64122699523__7F4958E8A.JPG": make_jpeg_bytes(color="red")})

        stats = rebind_sidecars(db)

        assert stats["bound"] == 1
        assert get_exif_date(vault / "64122699523__7F4958E8A.JPG") is not None


class TestDryRun:
    @pytest.fixture
    def ready(self, db, ingest):
        ingest({"T/P/img.jpg.supplemental-metadata.json":
                make_sidecar_bytes(TS, **CHICAGO)})
        return ingest({"T/P/img.jpg": make_jpeg_bytes(color="red")})

    def test_it_reports_what_would_bind(self, db, ready):
        stats = rebind_sidecars(db, dry_run=True)

        assert stats["bound"] == 1
        assert stats["dry_run"] is True

    def test_it_touches_neither_the_file_nor_the_tables(self, db, ready):
        photo = ready / "img.jpg"
        before_bytes = photo.read_bytes()
        before_mtime = photo.stat().st_mtime
        before_log = db.conn.execute(
            "SELECT COUNT(*) c FROM metadata_log").fetchone()["c"]

        rebind_sidecars(db, dry_run=True)

        assert photo.read_bytes() == before_bytes
        assert photo.stat().st_mtime == before_mtime
        assert db.conn.execute(
            "SELECT COUNT(*) c FROM metadata_log").fetchone()["c"] == before_log
        assert db.get_pending(str(photo)) == []

    def test_it_leaves_the_row_unmatched(self, db, ready):
        rebind_sidecars(db, dry_run=True)

        assert len(db.get_unmatched()) == 1

    def test_a_real_run_afterwards_still_binds(self, db, ready):
        """A dry run must not consume the work it was only inspecting."""
        rebind_sidecars(db, dry_run=True)

        assert rebind_sidecars(db)["bound"] == 1


class TestIdempotency:
    @pytest.fixture
    def rebound(self, db, ingest):
        ingest({"T/P/clip.mp4.supplemental-metadata.json":
                make_sidecar_bytes(TS)})
        vault = ingest({"T/P/img.jpg": make_jpeg_bytes(color="red"),
                        "T/P/clip.mp4.supplemental-metadata.json":
                            make_sidecar_bytes(TS)})
        return vault

    def test_a_second_run_binds_nothing(self, db, ingest):
        ingest({"T/P/img.jpg.supplemental-metadata.json":
                make_sidecar_bytes(TS, **CHICAGO)})
        ingest({"T/P/img.jpg": make_jpeg_bytes(color="red")})
        assert rebind_sidecars(db)["bound"] == 1

        second = rebind_sidecars(db)

        assert second["bound"] == 0
        assert second["refused"] == {}

    def test_a_second_run_adds_no_rows(self, db, ingest):
        ingest({"T/P/img.jpg.supplemental-metadata.json":
                make_sidecar_bytes(TS, **CHICAGO)})
        ingest({"T/P/img.jpg": make_jpeg_bytes(color="red")})
        rebind_sidecars(db)
        log = db.conn.execute("SELECT COUNT(*) c FROM metadata_log").fetchone()["c"]
        pending = db.conn.execute(
            "SELECT COUNT(*) c FROM metadata_pending").fetchone()["c"]

        rebind_sidecars(db)

        assert db.conn.execute(
            "SELECT COUNT(*) c FROM metadata_log").fetchone()["c"] == log
        assert db.conn.execute(
            "SELECT COUNT(*) c FROM metadata_pending").fetchone()["c"] == pending

    def test_identical_rows_from_two_archives_bind_once(self, db, ingest):
        """The duplicate-path shape: the same zip ingested under two names.

        Both archives record the same sidecar, with the same directory, name
        and payload. Binding both would apply the value twice; this is what
        turns 339 candidate rows into 312 binds in the shakedown.
        """
        sidecar = {"T/P/img.jpg.supplemental-metadata.json":
                   make_sidecar_bytes(TS, **CHICAGO)}
        ingest(sidecar, name="part_a.zip")
        ingest(sidecar, name="part_b.zip")
        ingest({"T/P/img.jpg": make_jpeg_bytes(color="red")})
        assert len(db.get_unmatched()) == 2

        stats = rebind_sidecars(db)

        assert stats["bound"] == 1
        assert stats["duplicate_rows"] == 1

    def test_both_duplicate_rows_are_marked(self, db, ingest):
        """Otherwise the next run would offer the collapsed row all over again."""
        sidecar = {"T/P/img.jpg.supplemental-metadata.json":
                   make_sidecar_bytes(TS, **CHICAGO)}
        ingest(sidecar, name="part_a.zip")
        ingest(sidecar, name="part_b.zip")
        ingest({"T/P/img.jpg": make_jpeg_bytes(color="red")})

        rebind_sidecars(db)

        assert db.get_unmatched() == []
        assert rebind_sidecars(db)["bound"] == 0


class TestRelocatedTargets:
    """`kept_path` is where the file was, not a promise of where it is."""

    def _relocate(self, db, src: Path, dest: Path):
        row = db.get_file_by_path(str(src))
        shutil.move(str(src), str(dest))
        db.upsert_file(path=str(dest), size=row["size"],
                       blake3_full=row["blake3_full"],
                       source_blake3=row["source_blake3"],
                       mtime=dest.stat().st_mtime)

    def test_it_follows_the_file_by_content_hash(self, db, ingest):
        ingest({"T/P/img.jpg.supplemental-metadata.json":
                make_sidecar_bytes(TS, **CHICAGO)})
        vault = ingest({"T/P/img.jpg": make_jpeg_bytes(color="red")})
        self._relocate(db, vault / "img.jpg", vault / "moved.jpg")

        stats = rebind_sidecars(db)

        assert stats["bound"] == 1
        assert get_exif_date(vault / "moved.jpg") is not None

    def test_a_vanished_target_is_refused_not_guessed(self, db, ingest):
        ingest({"T/P/img.jpg.supplemental-metadata.json":
                make_sidecar_bytes(TS, **CHICAGO)})
        vault = ingest({"T/P/img.jpg": make_jpeg_bytes(color="red")})
        (vault / "img.jpg").unlink()

        stats = rebind_sidecars(db)

        assert stats["bound"] == 0
        assert stats["refused"] == {"target_missing": 1}

    def test_two_surviving_twins_are_refused(self, db, ingest):
        """Two candidates is a choice, and this pass does not make choices."""
        ingest({"T/P/img.jpg.supplemental-metadata.json":
                make_sidecar_bytes(TS, **CHICAGO)})
        vault = ingest({"T/P/img.jpg": make_jpeg_bytes(color="red")})
        original = vault / "img.jpg"
        row = db.get_file_by_path(str(original))
        for name in ("twin_a.jpg", "twin_b.jpg"):
            shutil.copy2(original, vault / name)
            db.upsert_file(path=str(vault / name), size=row["size"],
                           blake3_full=row["blake3_full"],
                           source_blake3=row["source_blake3"],
                           mtime=(vault / name).stat().st_mtime)
        original.unlink()

        stats = rebind_sidecars(db)

        assert stats["bound"] == 0
        assert stats["refused"] == {"ambiguous_target": 1}


class TestOutcomesGoThroughTheChokePoint:
    def test_a_video_gets_its_mtime_and_exactly_one_deferred_row(
            self, db, ingest, small_mp4):
        ingest({"T/P/clip.mp4.supplemental-metadata.json":
                make_sidecar_bytes(TS)})
        vault = ingest({"T/P/clip.mp4": small_mp4.read_bytes()})

        rebind_sidecars(db)

        clip = vault / "clip.mp4"
        assert os.stat(clip).st_mtime == pytest.approx(TS, abs=1)
        assert len(db.get_pending(str(clip))) == 1

    def test_a_target_that_already_has_the_date_records_already_present(
            self, db, ingest):
        import piexif
        ingest({"T/P/img.jpg.supplemental-metadata.json":
                make_sidecar_bytes(TS)})
        vault = ingest({"T/P/img.jpg": make_jpeg_bytes(color="red", exif={
            "0th": {}, "Exif": {
                piexif.ExifIFD.DateTimeOriginal: b"1999:12:31 23:59:59"},
            "GPS": {}, "1st": {}})})

        stats = rebind_sidecars(db)

        assert stats["bound"] == 1
        fields = [r["field"] for r in db.conn.execute(
            "SELECT field FROM metadata_log WHERE target_path = ?",
            (str(vault / "img.jpg"),)).fetchall()]
        assert fields == ["already_present"]

    def test_the_source_names_the_sidecar_entry(self, db, ingest):
        """Distinguishable from an ingest-time apply, which names the media."""
        ingest({"T/P/img.jpg.supplemental-metadata.json":
                make_sidecar_bytes(TS, **CHICAGO)})
        vault = ingest({"T/P/img.jpg": make_jpeg_bytes(color="red")})

        rebind_sidecars(db)

        sources = {r["source_desc"] for r in db.conn.execute(
            "SELECT source_desc FROM metadata_log WHERE target_path = ?",
            (str(vault / "img.jpg"),)).fetchall()}
        assert sources == {"takeout:T/P/img.jpg.supplemental-metadata.json"}


class TestNothingToDo:
    def test_an_empty_table_is_not_an_error(self, db):
        stats = rebind_sidecars(db)

        assert stats == {"bound": 0, "refused": {}, "duplicate_rows": 0,
                         "by_archive": {}, "dry_run": False}

    def test_a_payloadless_row_is_refused(self, db, ingest):
        """An unreadable sidecar has nothing to re-apply, and never will."""
        ingest({"T/P/img.jpg": make_jpeg_bytes(color="red")})
        db.record_unmatched_sidecar(
            archive_id=1, sidecar_path="T/P/img.jpg.supplemental-metadata.json",
            archive_dir="T/P", entry_name="img.jpg.supplemental-metadata.json",
            reason="unparseable", payload=None)

        stats = rebind_sidecars(db)

        assert stats["bound"] == 0
        assert stats["refused"] == {"no_payload": 1}


class TestOneCommitPerBind:
    """Each bind's rows land in a single commit, or not at all (2026-09-13).

    On the vault's USB drive every commit is an fsync, and a bind used to make
    about four. Batching them is the speed-up; all-or-nothing is the safety.
    """

    @staticmethod
    def _split(ingest):
        ingest({"T/P/img.jpg.supplemental-metadata.json":
                make_sidecar_bytes(TS, **CHICAGO)})
        ingest({"T/P/img.jpg": make_jpeg_bytes(color="red")})

    def test_a_failure_while_marking_rolls_back_the_whole_bind(
            self, db, ingest, monkeypatch):
        self._split(ingest)
        logs_before = db.conn.execute("SELECT COUNT(*) FROM metadata_log").fetchone()[0]

        def disk_gone(*args, **kwargs):
            raise RuntimeError("disk gone")
        monkeypatch.setattr(db, "mark_sidecar_rebound", disk_gone)

        assert rebind_sidecars(db)["bound"] == 0
        assert db.conn.execute("SELECT COUNT(*) FROM metadata_log").fetchone()[0] == logs_before
        assert len(db.get_unmatched()) == 1, "the row must be re-offered next run"

    def test_a_bind_makes_exactly_one_commit(self, db, ingest):
        self._split(ingest)
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
            assert rebind_sidecars(db)["bound"] == 1
        finally:
            db.conn = real
        assert Counting.commits == 1
