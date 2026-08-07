"""Ingest a folder tree into the vault, deduplicating against what is there.

Phase 5 brings four sources on drives. `ingest_archive` takes an archive and
`scan` indexes in place without copying, so neither fits. This is the missing
path, and it owns the moment a duplicate is collapsed — which is why the
collapse record lives here rather than being bolted on later.

WHY THIS DOES NOT REUSE extract_entry_to_path
---------------------------------------------
`ArchiveEntry.temp_path` means "a scratch file I own". Two functions act on that
promise: `extract_entry_to_path` MOVES it into the vault, and
`ArchiveEntry.cleanup` UNLINKS it. A source file on a drive is not scratch. The
obvious implementation — yield ArchiveEntry with temp_path set to the source
file — would therefore move source files into the vault and delete the rest,
emptying the drive as a side effect of what is supposed to be a read.

So this module copies, never moves, and never unlinks anything under the source
root. `test_source_tree_is_untouched` exists to keep it that way.

Everything else is shared with the archive path: content-hash dedup, the
sidecar/EXIF merge choke point, per-entry resume, and the archives ledger.
"""

import json
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from memoryvault.containers import detect_container
from memoryvault.database import Database
from memoryvault.hasher import hash_full
from memoryvault.ingest import (
    _index_saved_file, _try_merge_from_duplicate, _unique_dest_path,
    is_metadata_json, is_sidecar_json,
)
from memoryvault.metadata import can_read_exif, get_exif_date
from memoryvault.repair import (
    TAKEOUT_INGEST_WINDOW, mtime_matches_own_exif,
)
from memoryvault.volumes import assert_volumes_reachable

# An mtime older than this is not a capture time, it is a broken clock.
EARLIEST_PLAUSIBLE_MTIME = datetime(1990, 1, 1, tzinfo=timezone.utc).timestamp()


@dataclass
class SourceFile:
    """A file on the source drive. Deliberately NOT an ArchiveEntry — it carries
    no temp_path, so nothing can mistake it for scratch and move it."""
    rel: str          # path relative to the source root; the entry_path
    abs: Path
    size: int


@dataclass
class CollapseOutcome:
    """What a collapse took and what it refused. Never both empty."""
    adopted: list = field(default_factory=list)
    declined: list = field(default_factory=list)

    def take(self, what, source, detail=""):
        self.adopted.append({"what": what, "from": source, "detail": detail})

    def refuse(self, what, reason, detail=""):
        self.declined.append({"what": what, "reason": reason, "detail": detail})


def iter_source_files(root: Path, exclude: set) -> list:
    """Every file under root, sorted, skipping excluded trees.

    Sorted so a run is deterministic and a resumed run walks the same order.
    """
    out = []
    root = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath).resolve()
        if any(here == e or e in here.parents for e in exclude):
            dirnames[:] = []
            continue
        dirnames.sort()
        for name in sorted(filenames):
            p = here / name
            try:
                st = p.stat()
            except OSError:
                continue
            if not p.is_file():
                continue
            out.append(SourceFile(rel=str(p.relative_to(root)), abs=p, size=st.st_size))
    return out


def mtime_verdict(src: Path, survivor: Path, outcome: CollapseOutcome) -> float | None:
    """Should the survivor adopt the duplicate's mtime? Returns the epoch or None.

    This is the rule that closes the hole: for a byte-identical duplicate the
    EXIF is identical by construction, so mtime is the only thing that can
    legitimately be better. 16,957 vault files still carry an ingest-run
    timestamp; a drive copy may carry the real one.

    Every branch records a reason. Silence is not an outcome.

    DEFERRED, deliberately: there is no registry of ingest runs. One is not
    needed while no write path clobbers an mtime — folder ingest preserves it
    via shutil.copy2, and `_insert` restores it across an EXIF rewrite since
    commit eb06812. Build the registry (started_at/finished_at on `archives`,
    resolved per file through archive_entries.kept_path) the day some path
    starts stamping files with its own run time; until then the file's own EXIF
    answers the question better than any window could, and TAKEOUT_INGEST_WINDOW
    covers only the files that carry no date of their own.
    """
    try:
        src_m = src.stat().st_mtime
        dst_m = survivor.stat().st_mtime
    except OSError as exc:
        outcome.refuse("mtime", "stat_failed", str(exc))
        return None

    lo, hi = TAKEOUT_INGEST_WINDOW

    # Is the survivor's mtime an artefact? Ask the FILE first: a date that does
    # not sit a legal UTC offset from its own EXIF is an artefact whenever it
    # was written, by whatever wrote it. The historical Takeout window is only
    # the fallback, for files carrying no date to check against.
    surv_matches = mtime_matches_own_exif(survivor)
    if surv_matches is True:
        outcome.refuse("mtime", "survivor_mtime_not_clobbered",
                       "survivor mtime sits a legal UTC offset from its own "
                       "EXIF, so it describes the capture")
        return None
    if surv_matches is None and not (lo <= dst_m <= hi):
        outcome.refuse("mtime", "survivor_mtime_not_clobbered",
                       f"no EXIF to check against and "
                       f"{datetime.fromtimestamp(dst_m):%Y-%m-%d} is outside the "
                       f"Takeout ingest window; it may be legitimate")
        return None

    # Cheapest unambiguous rejection first, so an absurd timestamp is reported
    # as absurd rather than as an EXIF disagreement.
    now = datetime.now(timezone.utc).timestamp()
    if not (EARLIEST_PLAUSIBLE_MTIME < src_m < now):
        outcome.refuse("mtime", "source_mtime_implausible",
                       f"{datetime.fromtimestamp(src_m).isoformat()}")
        return None

    # The same question, asked of the source. Reuses the quantisation rule that
    # rejected 110 misaligned files during the mtime repair.
    src_matches = mtime_matches_own_exif(src)
    if src_matches is False:
        outcome.refuse("mtime", "source_mtime_disagrees_with_its_own_exif",
                       "gap is not a whole 15-minute offset")
        return None
    if src_matches is None and lo <= src_m <= hi:
        outcome.refuse("mtime", "source_mtime_also_from_ingest_window", "")
        return None

    outcome.take("mtime", "source_file",
                 f"{datetime.fromtimestamp(dst_m):%Y-%m-%d} (ingest) -> "
                 f"{datetime.fromtimestamp(src_m):%Y-%m-%d} (source)")
    return src_m


def collapse_duplicate(sf: SourceFile, survivor: dict, matched_on: str,
                       db: Database, archive_id: int, source_root: Path,
                       dry_run: bool) -> CollapseOutcome:
    """Merge what the duplicate holds into the survivor, then record all of it."""
    outcome = CollapseOutcome()
    survivor_path = Path(survivor["path"])

    if not dry_run:
        # Shared with the archive path, so the read-gate fix benefits both:
        # sidecar first, then the duplicate's own EXIF for anything missing.
        merged = _try_merge_from_duplicate(
            _AsEntry(sf), survivor, {}, db)
        for f in merged.merged:
            outcome.take(f, "duplicate_exif")
        for f in merged.merged_mtime_only:
            outcome.take(f, "duplicate_exif", "written as mtime; container "
                                              "cannot hold EXIF")
        for f in merged.already_present:
            outcome.refuse(f, "survivor_already_has_it")
        for f in merged.failed:
            outcome.refuse(f, "write_failed")
        for f in merged.deferred:
            outcome.refuse(f, "deferred_container_cannot_hold_it")
    else:
        outcome.refuse("exif_merge", "dry_run", "not attempted")

    new_mtime = mtime_verdict(sf.abs, survivor_path, outcome)
    if new_mtime is not None and not dry_run:
        os.utime(survivor_path, (new_mtime, new_mtime))
        db.conn.execute("UPDATE files SET mtime = ? WHERE path = ?",
                        (float(new_mtime), str(survivor_path)))

    outcome.take("provenance", "source_root",
                 f"also present at {source_root}/{sf.rel}")

    if not dry_run:
        db.conn.execute(
            "INSERT INTO dedup_collapse (archive_id, entry_path, source_root,"
            " survivor_path, survivor_blake3, matched_on, adopted, declined,"
            " collapsed_at) VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(archive_id, entry_path) DO UPDATE SET "
            "adopted=excluded.adopted, declined=excluded.declined,"
            " collapsed_at=excluded.collapsed_at",
            (archive_id, sf.rel, str(source_root), str(survivor_path),
             survivor.get("blake3_full"), matched_on,
             json.dumps(outcome.adopted), json.dumps(outcome.declined),
             datetime.now(timezone.utc).isoformat()))
        db.conn.commit()
    return outcome


class _AsEntry:
    """Adapts a SourceFile to what `_try_merge_from_duplicate` reads.

    It looks at `.path`, `.is_large` and `.temp_path`/`.data`. temp_path is
    deliberately left None and the bytes are handed over as `data`, so neither
    `extract_entry_to_path` nor `cleanup` can ever be pointed at the source
    file. Nothing in the merge path writes to the source; this only guarantees
    that nothing *could*.
    """
    def __init__(self, sf: SourceFile):
        self.path = sf.rel
        self.temp_path = None
        self.is_large = False
        self._abs = sf.abs

    @property
    def data(self):
        try:
            return self._abs.read_bytes()
        except OSError:
            return None

    def cleanup(self):
        pass          # there is nothing of ours to clean up


def ingest_folder(src_root: Path, dest: Path, db: Database, *, dry_run=True,
                  limit=0, progress=None, allow_unreachable_volumes=False) -> dict:
    """Copy every unique file under src_root into dest, recording every collapse."""
    src_root = Path(src_root).resolve()
    dest = Path(dest).resolve()
    if dest == src_root or src_root in dest.parents:
        raise ValueError("destination is inside the source tree")

    if not allow_unreachable_volumes:
        assert_volumes_reachable(db, override_hint="--allow-unreachable-volumes")

    stats = {"seen": 0, "kept": 0, "collapsed": 0, "skipped": 0, "errors": 0,
             "adopted_mtime": 0, "collapses": [], "kept_examples": []}

    archive_id = None
    if not dry_run:
        dest.mkdir(parents=True, exist_ok=True)
        archive_id = db.register_archive(
            str(src_root), blake3=None, entries_total=0,
            title=f"folder:{src_root.name}")
        db.update_archive_status(archive_id, "in_progress")
        already = db.get_processed_entries(archive_id)
    else:
        row = db.conn.execute("SELECT id FROM archives WHERE path = ?",
                              (str(src_root),)).fetchone()
        already = db.get_processed_entries(row["id"]) if row else set()

    files = iter_source_files(src_root, exclude={dest, dest.parent / (dest.name + ".mvtmp")})
    for i, sf in enumerate(files, 1):
        if limit and stats["seen"] >= limit:
            break
        if sf.rel in already:
            continue
        stats["seen"] += 1

        if is_metadata_json(sf.rel) or is_sidecar_json(sf.rel):
            stats["skipped"] += 1
            if not dry_run:
                db.log_archive_entry(archive_id, sf.rel, "skipped",
                                     skip_reason="sidecar")
            continue

        try:
            digest = hash_full(sf.abs)
        except OSError as exc:
            stats["errors"] += 1
            if not dry_run:
                db.log_archive_entry(archive_id, sf.rel, "error",
                                     skip_reason=f"read_failed:{exc}"[:200])
            continue

        candidates = db.find_files_by_content_hash(digest)
        surviving = [c for c in candidates if Path(c["path"]).exists()]
        if surviving:
            survivor = surviving[0]
            matched = ("blake3_full" if survivor.get("blake3_full") == digest
                       else "source_blake3")
            outcome = collapse_duplicate(sf, survivor, matched, db, archive_id,
                                         src_root, dry_run)
            stats["collapsed"] += 1
            if any(a["what"] == "mtime" for a in outcome.adopted):
                stats["adopted_mtime"] += 1
            if len(stats["collapses"]) < 200:
                stats["collapses"].append(
                    {"entry": sf.rel, "survivor": survivor["path"],
                     "adopted": outcome.adopted, "declined": outcome.declined})
            if not dry_run:
                db.log_archive_entry(archive_id, sf.rel, "skipped",
                                     skip_reason="duplicate")
            continue

        dest_path = _unique_dest_path(dest, Path(sf.rel).name)
        if not dry_run:
            shutil.copy2(sf.abs, dest_path)          # COPY. never move.
            _index_saved_file(db, dest_path, source_hash=digest,
                              source=f"folder:{src_root.name}")
            db.log_archive_entry(archive_id, sf.rel, "kept",
                                 kept_path=str(dest_path))
        stats["kept"] += 1
        if len(stats["kept_examples"]) < 200:
            stats["kept_examples"].append({"entry": sf.rel, "dest": str(dest_path)})

        if progress and stats["seen"] % 100 == 0:
            progress(stats["seen"], len(files))

    if not dry_run:
        db.update_archive_status(archive_id, "complete",
                                 entries_processed=stats["seen"])
    stats["total_files_seen"] = len(files)
    return stats
