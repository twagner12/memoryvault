"""Repair mtimes that ingest stamped with its own run time.

`piexif.insert` rewrote files in place without restoring mtime, and
`extract_entry_to_path` writes a fresh file, so every JPEG the pipeline touched
came out claiming it was modified during the ingest run. 62,409 of the 99,256
files in the production vault are affected. Their EXIF is correct; only the
weaker of the two dates is wrong.

This pass sets mtime to the instant the file's own metadata describes. It never
changes content, so `blake3_full` stays valid — which is exactly why the hash is
checked *before* each write: a mismatch means the file is not what the database
thinks it is, and that is a reason to stop touching it, not to proceed.

**Where the value comes from, and why it is not simply Google's.**

The obvious source is `metadata_log`'s recorded `utc_epoch`. For the 1,663 files
whose EXIF the pipeline wrote, that is exactly right — the EXIF was written from
it. For the 59,056 that arrived carrying their own date, it is the wrong answer:
those got no write precisely because the file already knew better, and Google's
`photoTakenTime` is documented as unreliable (see corrections-pending.txt, where
two files disagree with their camera by nearly two years). Overwriting a
camera's own reading with Google's would be a downgrade.

So the file's own EXIF wins where it can be resolved to an absolute instant, and
Google's value is used only where it corroborates rather than contradicts.
"""

import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import piexif

from memoryvault.hasher import hash_full, hash_head, hash_tail
from memoryvault.metadata import read_exif

# The August 2026 Takeout run, and nothing else. Named for the event it
# describes rather than for "ingest" generally, because it is a historical fact
# with an end date, not a maintained property of the system.
#
# Folder ingest does not extend it: shutil.copy2 preserves the source mtime and
# the kept path never writes EXIF, so nothing it does clobbers a timestamp.
# Prefer `mtime_matches_own_exif` over this window wherever the file carries a
# date of its own — see folder.mtime_verdict.
TAKEOUT_INGEST_WINDOW = (
    datetime(2026, 8, 3, tzinfo=timezone.utc).timestamp(),
    datetime(2026, 8, 6, tzinfo=timezone.utc).timestamp(),
)

# Corroboration is not "close enough", it is "differs by a legal UTC offset".
#
# When a sidecar really describes the photo, the gap between the file's local
# wall clock and Google's absolute instant IS the zone offset — exact to the
# second. Every real offset is a whole number of 15-minute steps, no more than
# 14 hours from UTC. Anything else means the two timestamps describe different
# events, however close they happen to fall.
#
# A plain window is not enough. Measured over the vault, a ±26h window admits
# 272 files whose gap is an arbitrary value like +22.449h or +10.425h — Google
# numbering its exports such that a sidecar lands beside a neighbouring photo.
# They are near enough to look corroborated and wrong enough to matter, and the
# quantisation test rejects all 272 while keeping the 51,876 genuine ones.
CORROBORATION_MAX = 14 * 3600        # largest real UTC offset
OFFSET_QUANTUM = 900                 # every real offset is a multiple of 15 min
OFFSET_TOLERANCE = 60                # slack for clock rounding, well under 15 min


def _is_legal_utc_offset(delta: float) -> bool:
    if abs(delta) > CORROBORATION_MAX:
        return False
    nearest = round(delta / OFFSET_QUANTUM) * OFFSET_QUANTUM
    return abs(delta - nearest) <= OFFSET_TOLERANCE


def _parse_exif_dt(raw):
    if not raw:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("ascii", "replace")
    try:
        return datetime.strptime(raw.strip("\x00 "), "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None


def _parse_offset(raw):
    """'-06:00' -> timedelta. Returns None when absent or malformed."""
    if not raw:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("ascii", "replace")
    raw = raw.strip("\x00 ")
    if len(raw) != 6 or raw[0] not in "+-" or raw[3] != ":":
        return None
    try:
        mins = int(raw[1:3]) * 60 + int(raw[4:6])
    except ValueError:
        return None
    return timedelta(minutes=-mins if raw[0] == "-" else mins)


def resolve_instant(path: Path, google_epoch: int | None) -> tuple:
    """Return (epoch, source, detail) or (None, reason, detail) if unresolvable.

    Preference order:
      1. The file's own DateTimeOriginal plus OffsetTimeOriginal — a complete,
         self-describing instant that owes nothing to Google.
      2. The file's own DateTimeOriginal corroborated by Google's utc_epoch:
         when the two describe the same moment to within any plausible UTC
         offset, Google supplies the offset the file omitted.
      3. Nothing. A local wall clock with no offset and no corroboration is not
         an instant, and guessing one is how the tz_unknown_assumed_utc rows got
         written in the first place.
    """
    exif = read_exif(path)
    if exif is None:
        return None, "no_readable_exif", ""
    dto = _parse_exif_dt(exif.get("Exif", {}).get(piexif.ExifIFD.DateTimeOriginal))
    if dto is None:
        return None, "no_datetimeoriginal", ""

    offset = _parse_offset(exif.get("Exif", {}).get(
        piexif.ExifIFD.OffsetTimeOriginal))
    if offset is not None:
        epoch = (dto.replace(tzinfo=timezone.utc) - offset).timestamp()
        return int(epoch), "exif_offset", f"{dto} offset {offset}"

    if google_epoch is None:
        return None, "no_offset_and_no_google_value", str(dto)

    naive_utc = dto.replace(tzinfo=timezone.utc).timestamp()
    delta = google_epoch - naive_utc
    if _is_legal_utc_offset(delta):
        return int(google_epoch), "google_corroborated", (
            f"{dto} + offset {delta / 3600:+.2f}h")
    if abs(delta) <= CORROBORATION_MAX:
        # Close, but not by a legal offset — so the two describe different
        # moments and the sidecar belongs to some other photo.
        return None, "google_offset_not_quantised", (
            f"exif {dto} vs google "
            f"{datetime.fromtimestamp(google_epoch, timezone.utc)} — "
            f"gap {delta / 3600:+.3f}h is not a whole 15-minute offset")
    return None, "google_contradicts_exif", (
        f"exif {dto} vs google "
        f"{datetime.fromtimestamp(google_epoch, timezone.utc)} — "
        f"{abs(delta) / 86400:.0f}d apart")


def _epoch_from_log_value(v: dict):
    """metadata_log stores the epoch at two different depths.

    A merged or merged_mtime_only row is the payload itself, so the epoch is at
    the top level. An `already_present` row records a *refusal*, and wraps what
    was offered:

        {"field": "date", "offered": {"utc_epoch": ...},
         "satisfied_by": "exif_date"}

    Reading only the top level silently loses every already_present row — 64,014
    of them, which is the bulk of the repairable population and precisely the
    files whose own EXIF beat Google's. Missing them does not corrupt anything,
    it just refuses to repair them, which is the kind of quiet under-delivery
    that looks like a finished job.
    """
    if v.get("utc_epoch"):
        return v["utc_epoch"]
    offered = v.get("offered")
    if isinstance(offered, dict) and offered.get("utc_epoch"):
        return offered["utc_epoch"]
    return None


def load_google_epochs(db) -> dict:
    """path -> the utc_epoch Google claimed, as recorded in metadata_log."""
    import json
    out = {}
    for row in db.conn.execute(
            "SELECT target_path, value FROM metadata_log "
            "WHERE field IN ('date', 'merged_mtime_only', 'already_present')"):
        if row["target_path"] in out:
            continue
        try:
            v = json.loads(row["value"])
        except Exception:
            continue
        epoch = _epoch_from_log_value(v)
        if epoch:
            out[row["target_path"]] = epoch
    return out


def find_candidates(db, window=TAKEOUT_INGEST_WINDOW) -> list:
    """Files whose mtime sits inside the ingest window. Cheap: DB only."""
    lo, hi = window
    rows = db.conn.execute(
        "SELECT path, mtime, size, blake3_full, blake3_head, blake3_tail "
        "FROM files WHERE mtime BETWEEN ? AND ? ORDER BY path", (lo, hi)).fetchall()
    return [dict(r) for r in rows]


def content_guard(path: Path, row: dict, full: bool = False) -> tuple:
    """Is this the file the database describes? Returns (ok, detail).

    `os.utime` does not touch content, so this is not protecting the bytes — it
    is refusing to act on a file whose identity is in doubt.

    Head-and-tail is the default because the cost difference is enormous and
    the marginal certainty is small. Reading 64 KB + 4 KB per file costs about
    3.5 GiB across the whole population; re-hashing every byte costs over 200
    GiB, through a USB enclosure that has now aborted writes once and dropped
    off the bus entirely once. The vault was fully re-hashed against
    `blake3_full` on 2026-08-05 with zero mismatches and nothing has written to
    it since, so a full re-read here re-proves a fact already established at
    the cost of the exact I/O that provokes the fault.

    What head-and-tail cannot see is corruption strictly in the middle of a
    file that leaves its size unchanged. `--full-hash` is there for when that
    matters more than the I/O.
    """
    st = path.stat()
    if row["size"] is not None and st.st_size != row["size"]:
        return False, f"size {st.st_size:,} != db {row['size']:,}"

    if full:
        actual = hash_full(path)
        if actual != row["blake3_full"]:
            return False, (f"blake3_full db {row['blake3_full'][:16]} "
                           f"!= disk {actual[:16]}")
        return True, ""

    if row.get("blake3_head"):
        actual = hash_head(path)
        if actual != row["blake3_head"]:
            return False, (f"blake3_head db {row['blake3_head'][:16]} "
                           f"!= disk {actual[:16]}")
    if row.get("blake3_tail"):
        # Mirrors _index_saved_file: a file at or under the tail size has its
        # head hash stored in both columns.
        actual = hash_tail(path) if st.st_size > 4_096 else hash_head(path)
        if actual != row["blake3_tail"]:
            return False, (f"blake3_tail db {row['blake3_tail'][:16]} "
                           f"!= disk {actual[:16]}")
    return True, ""


def repair_mtimes(db, *, dry_run=True, limit=0, verify_hash=True,
                  full_hash=False, log_path=None, progress=None) -> dict:
    """Set mtime to the instant the file's own metadata describes.

    Returns a stats dict. In dry-run nothing is written and the database is
    expected to have been opened read-only by the caller.
    """
    google = load_google_epochs(db)
    candidates = find_candidates(db)

    stats = {"candidates": len(candidates), "repaired": 0, "would_repair": 0,
             "skipped": 0, "by_source": {}, "by_skip": {}, "changes": [],
             "skips": []}

    log = open(log_path, "a") if log_path else None
    if log:
        log.write(f"\n{'='*78}\n")
        log.write(f"mtime repair {'DRY RUN' if dry_run else 'LIVE'} "
                  f"{datetime.now().isoformat()}  limit={limit or 'none'}\n")
        log.write(f"{'='*78}\n")

    done = 0
    for i, row in enumerate(candidates):
        if limit and done >= limit:
            break
        path = Path(row["path"])

        if not path.exists():
            stats["skipped"] += 1
            stats["by_skip"]["file_missing"] = stats["by_skip"].get("file_missing", 0) + 1
            stats["skips"].append((str(path), "file_missing", ""))
            continue

        epoch, source, detail = resolve_instant(path, google.get(str(path)))
        if epoch is None:
            stats["skipped"] += 1
            stats["by_skip"][source] = stats["by_skip"].get(source, 0) + 1
            if len(stats["skips"]) < 500:
                stats["skips"].append((str(path), source, detail))
            continue

        # Content guard. os.utime does not change bytes, so the stored hashes
        # stay valid across the repair — but if they do not match *now*, the
        # file is not the one the database describes and must be left alone.
        if verify_hash:
            ok, detail = content_guard(path, row, full=full_hash)
            if not ok:
                stats["skipped"] += 1
                stats["by_skip"]["hash_mismatch"] = \
                    stats["by_skip"].get("hash_mismatch", 0) + 1
                stats["skips"].append((str(path), "hash_mismatch", detail))
                continue

        old = path.stat().st_mtime
        if int(old) == int(epoch):
            stats["skipped"] += 1
            stats["by_skip"]["already_correct"] = \
                stats["by_skip"].get("already_correct", 0) + 1
            continue

        if not dry_run:
            os.utime(path, (epoch, epoch))
            db.conn.execute("UPDATE files SET mtime = ? WHERE path = ?",
                            (float(epoch), str(path)))
            db.conn.commit()
            stats["repaired"] += 1
        else:
            stats["would_repair"] += 1

        stats["by_source"][source] = stats["by_source"].get(source, 0) + 1
        if len(stats["changes"]) < 100000:
            stats["changes"].append((str(path), old, float(epoch), source, detail))
        if log:
            log.write(f"{'WOULD ' if dry_run else ''}SET  {path}\n")
            log.write(f"     old_mtime {datetime.fromtimestamp(old).isoformat()} "
                      f"({old:.0f})\n")
            log.write(f"     new_mtime {datetime.fromtimestamp(epoch).isoformat()} "
                      f"({epoch})\n")
            log.write(f"     source    {source}  {detail}\n")
        done += 1
        if progress and done % 200 == 0:
            progress(done, len(candidates))

    if log:
        log.write(f"-- {'would repair' if dry_run else 'repaired'} "
                  f"{stats['would_repair'] or stats['repaired']:,}, "
                  f"skipped {stats['skipped']:,}\n")
        log.close()
    return stats


def mtime_matches_own_exif(path) -> bool | None:
    """Does this file's mtime correspond to its own capture date?

    True  — the gap is a legal UTC offset, so the mtime describes the capture.
    False — the gap is not any offset that exists, so the mtime is an artefact.
    None  — the file carries no date to check against; the caller must fall back
            to something else.

    This is strictly better than asking whether an mtime falls inside a known
    ingest window: it is a property of the file rather than of one historical
    run, so it recognises a timestamp clobbered by anything, at any time, and it
    cannot go stale.
    """
    from memoryvault.metadata import can_read_exif, get_exif_date
    if not can_read_exif(path):
        return None
    iso = get_exif_date(path)
    if not iso:
        return None
    try:
        naive = datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None
    try:
        return _is_legal_utc_offset(path.stat().st_mtime - naive)
    except OSError:
        return None
