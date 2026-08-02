#!/usr/bin/env python3
"""Run the rebind pass end to end against a copy, and check what it did.

The shakedown fixture at /home/tim/photo-dedup-shakedown is validation data,
not scratch space, so this script never opens it for writing. It works on a
copy of the database and a copy of the vault, and starts by rewriting every
vault path in the copied database — so no row in it can even name the
original, which is a structural guarantee rather than a promise.

On btrfs the vault copy is effectively free:

    cp -a --reflink=always /home/tim/photo-dedup-shakedown/vault  <acc>/vault
    cp                     /home/tim/photo-dedup-shakedown/shakedown.db <acc>/rb.db
    ./scripts/rebind_acceptance.py --db <acc>/rb.db --vault <acc>/vault

Checks, all of which must pass:

  1.  312 sidecars bound — 27 from part 9, 285 from part 8
  2.  every bind is same-directory: the archive entry it matched sits in
      exactly the directory the sidecar was recorded in
  3.  a byte-level spot check of 10 targets: the date really is in the file
  4.  no (file, field) gained a duplicate outcome row
  5.  a second run binds nothing
"""

import argparse
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memoryvault.containers import detect_container, supports_exif  # noqa: E402
from memoryvault.database import Database  # noqa: E402
from memoryvault.hasher import hash_head  # noqa: E402
from memoryvault.metadata import get_exif_date, get_exif_offset  # noqa: E402
from memoryvault.rebind import plan_rebind, rebind_sidecars  # noqa: E402

FIXTURE_VAULT = "/home/tim/photo-dedup-shakedown/vault"
FIXTURE_DB = "/home/tim/photo-dedup-shakedown/shakedown.db"

EXPECTED_BOUND = 312
EXPECTED_BY_ARCHIVE = {
    "takeout-20260731T164006Z-1-009.zip": 27,
    "takeout-20260731T164006Z-1-008.zip": 285,
}
SPOT_CHECK = 10

failures: list[str] = []


def check(label: str, ok: bool, detail: str = ""):
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f"  — {detail}" if detail else ""))
    if not ok:
        failures.append(label)


def repoint(db_path: Path, vault: Path):
    """Rewrite every fixture vault path in the copy to point at the copy.

    Done before anything else so the run cannot reach the original vault even
    if a later step is wrong about which paths it touches.
    """
    conn = sqlite3.connect(str(db_path))
    old = FIXTURE_VAULT.rstrip("/") + "/"
    new = str(vault).rstrip("/") + "/"
    n = len(old)
    for table, column in (("files", "path"), ("archive_entries", "kept_path")):
        conn.execute(
            f"UPDATE {table} SET {column} = ? || substr({column}, ?) "
            f"WHERE {column} LIKE ?",
            (new, n + 1, old + "%"),
        )
    conn.commit()

    leaked = 0
    for table, column in (("files", "path"), ("archive_entries", "kept_path")):
        leaked += conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {column} LIKE ?",
            (old + "%",)).fetchone()[0]
    conn.close()
    check("copy database names no path inside the fixture vault", leaked == 0,
          f"{leaked} row(s) still pointing at {FIXTURE_VAULT}")


def snapshot(db: Database) -> dict:
    """What the pass is about to do, and what the targets look like first.

    The before-state is what makes the byte check mean anything: for a target
    the pass declines to write, "unchanged" is only checkable against a
    recorded prior. Head hash plus size plus mtime is enough — an EXIF write
    changes all three.
    """
    binds, _ = plan_rebind(db)
    snap = {}
    for b in binds:
        stat = b.target.stat()
        snap[b.row["id"]] = {
            "target": b.target,
            "archive_dir": b.row["archive_dir"],
            "media_name": b.media_name,
            "payload": json.loads(b.row["payload"]),
            "writable": _writable(b.target),
            "had_date": _readable_date(b.target),
            "size": stat.st_size,
            "mtime": stat.st_mtime,
            "head": hash_head(b.target),
        }
    return snap


def _writable(path: Path) -> bool:
    try:
        return supports_exif(detect_container(path))
    except OSError:
        return False


def _readable_date(path: Path) -> str | None:
    return get_exif_date(path) if _writable(path) else None


def check_counts(db: Database, stats: dict):
    check(f"{EXPECTED_BOUND} sidecars bound", stats["bound"] == EXPECTED_BOUND,
          f"got {stats['bound']}")

    by_name = {}
    for archive_id, count in stats["by_archive"].items():
        row = db.conn.execute("SELECT path FROM archives WHERE id = ?",
                              (archive_id,)).fetchone()
        by_name[Path(row["path"]).name] = count
    check("split across the two Takeout parts", by_name == EXPECTED_BY_ARCHIVE,
          f"got {by_name}")


def check_same_directory(db: Database):
    """Every bind must have matched an entry in the sidecar's own directory."""
    rows = db.conn.execute(
        "SELECT archive_dir, rebound_path, rebound_outcome "
        "FROM sidecars_unmatched WHERE rebound_at IS NOT NULL").fetchall()

    outside = []
    unverifiable = []
    for row in rows:
        media = json.loads(row["rebound_outcome"])["media_name"]
        expected_entry = f"{row['archive_dir']}/{media}"
        hit = db.conn.execute(
            "SELECT 1 FROM archive_entries WHERE entry_path = ? "
            "AND kept_path = ? LIMIT 1",
            (expected_entry, row["rebound_path"])).fetchone()
        if hit is None:
            # Either the file it bound to was never an entry of that directory
            # (a cross-directory bind) or it relocated. Separate the two.
            relocated = json.loads(row["rebound_outcome"]).get("relocated")
            (unverifiable if relocated else outside).append(
                (expected_entry, row["rebound_path"]))

    check("zero binds outside same-directory rules", not outside,
          f"{len(outside)} cross-directory bind(s): {outside[:3]}")
    if unverifiable:
        print(f"        (note: {len(unverifiable)} target(s) had relocated and "
              f"were resolved by content hash)")
    check("every rebound row records where it bound",
          all(r["rebound_path"] for r in rows))
    return len(rows)


def check_bytes(planned: dict, db: Database):
    """Open the files and confirm the pass did to each one what it claimed.

    Sampled across all three outcomes rather than the first ten rows. On this
    corpus 297 of the 312 targets already carry their own date, so a naive
    "first ten" sample would be almost entirely refusals and would never touch
    the EXIF write — which is exactly where a wrong timezone would show up.
    """
    buckets: dict[str, list] = {"merged": [], "mtime": [], "already": []}
    for item in planned.values():
        epoch = item["payload"].get("utc_epoch")
        if epoch is None:
            continue
        if item["had_date"] or (not item["writable"]
                                and int(item["mtime"]) == int(epoch)):
            item["bucket"] = "already"
        elif item["writable"]:
            item["bucket"] = "merged"
        else:
            item["bucket"] = "mtime"
        buckets[item["bucket"]].append(item)

    print("        available: " + ", ".join(
        f"{k}={len(v)}" for k, v in buckets.items()))

    # The rare outcomes get a guaranteed share; the plentiful one fills the
    # rest, so no branch can silently drop out of the sample.
    quota = {"merged": 3, "mtime": 3, "already": SPOT_CHECK}
    sample: list = []
    for key in ("merged", "mtime", "already"):
        take = min(quota[key], len(buckets[key]), SPOT_CHECK - len(sample))
        sample += buckets[key][:take]

    check(f"{SPOT_CHECK} targets spot-checked", len(sample) == SPOT_CHECK,
          "" if len(sample) == SPOT_CHECK else f"only {len(sample)} available")
    produced = {k for k, v in buckets.items() if v}
    sampled = {i["bucket"] for i in sample}
    check("every outcome the run produced is represented in the sample",
          produced == sampled, f"produced {produced}, sampled {sampled}")

    for item in sample:
        path = item["target"]
        epoch = item["payload"]["utc_epoch"]
        label = path.name

        if not path.exists():
            check(f"{label}: exists", False)
            continue

        if item["bucket"] == "merged":
            written = get_exif_date(path)
            offset = get_exif_offset(path)
            expected = _expected_wall_clock(epoch, offset)
            check(f"{label}: EXIF date written, and it is the sidecar instant",
                  written == expected,
                  f"file={written} expected={expected} offset={offset}")
        elif item["bucket"] == "mtime":
            check(f"{label}: mtime is the sidecar instant",
                  int(path.stat().st_mtime) == int(epoch),
                  f"mtime={int(path.stat().st_mtime)} expected={epoch}")
        else:
            stat = path.stat()
            unchanged = (stat.st_size == item["size"]
                         and stat.st_mtime == item["mtime"]
                         and hash_head(path) == item["head"])
            check(f"{label}: already dated, so the bytes were left alone",
                  unchanged,
                  f"size {item['size']}->{stat.st_size} "
                  f"mtime {item['mtime']}->{stat.st_mtime}")
            check(f"{label}: its own date survived",
                  get_exif_date(path) == item["had_date"],
                  f"{item['had_date']} -> {get_exif_date(path)}")


def _expected_wall_clock(epoch: int, offset: str | None) -> str:
    """The local wall clock EXIF should carry, given the offset it declares.

    This is the check that #9 is fixed as well as #6: the sidecar's UTC
    instant, shifted by the offset the file itself claims, must be what
    DateTimeOriginal says. A file whose offset and wall clock disagree has had
    the timezone baked in wrongly.
    """
    if offset and len(offset) == 6 and offset[0] in "+-":
        sign = 1 if offset[0] == "+" else -1
        delta = sign * (int(offset[1:3]) * 3600 + int(offset[4:6]) * 60)
    else:
        delta = 0
    return datetime.fromtimestamp(epoch + delta, tz=timezone.utc).replace(
        tzinfo=None).isoformat()


def check_no_duplicate_outcomes(planned: dict, db: Database):
    """No target may carry the same outcome row twice."""
    offenders = []
    for item in planned.values():
        target = str(item["target"])

        log = Counter(
            (r["field"], r["value"]) for r in db.conn.execute(
                "SELECT field, value FROM metadata_log WHERE target_path = ?",
                (target,)).fetchall())
        pending = Counter(
            (r["field"], r["value"]) for r in db.conn.execute(
                "SELECT field, value FROM metadata_pending "
                "WHERE file_path = ? AND applied_at IS NULL",
                (target,)).fetchall())

        for table, counts in (("metadata_log", log), ("metadata_pending", pending)):
            for key, n in counts.items():
                if n > 1:
                    offenders.append(f"{Path(target).name} {table} {key[0]} ×{n}")

    check("no (file, field) gained a duplicate outcome row", not offenders,
          f"{len(offenders)}: {offenders[:3]}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path,
                        help="Copy of the shakedown database.")
    parser.add_argument("--vault", required=True, type=Path,
                        help="Copy of the shakedown vault.")
    args = parser.parse_args()

    for label, path in (("database", args.db), ("vault", args.vault)):
        if path.resolve() == Path(FIXTURE_DB).resolve() or \
                path.resolve() == Path(FIXTURE_VAULT).resolve():
            parser.error(f"refusing to run against the fixture {label}: {path}")

    print(f"Rebind acceptance\n  db:    {args.db}\n  vault: {args.vault}\n")

    print("Repointing the copy:")
    repoint(args.db, args.vault)

    db = Database(args.db)
    try:
        print("\nPlanning:")
        planned = snapshot(db)
        check(f"{EXPECTED_BOUND} sidecars planned",
              len(planned) == EXPECTED_BOUND, f"got {len(planned)}")

        print("\nRunning:")
        stats = rebind_sidecars(db)
        check_counts(db, stats)
        print(f"        refused: {stats['refused']}")
        print(f"        duplicate rows collapsed: {stats['duplicate_rows']}")

        print("\nSame-directory rules:")
        marked = check_same_directory(db)
        print(f"        {marked} row(s) marked rebound "
              f"({stats['bound']} binds + {stats['duplicate_rows']} duplicates)")

        print(f"\nByte-level spot check ({SPOT_CHECK} files):")
        check_bytes(planned, db)

        print("\nOutcome cardinality:")
        check_no_duplicate_outcomes(planned, db)

        print("\nIdempotency:")
        again = rebind_sidecars(db)
        check("a second run binds nothing", again["bound"] == 0,
              f"bound {again['bound']}")
        check("a second run refuses only what it refused before",
              again["refused"] == stats["refused"],
              f"{again['refused']} vs {stats['refused']}")
    finally:
        db.close()

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s) — " + "; ".join(failures))
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
