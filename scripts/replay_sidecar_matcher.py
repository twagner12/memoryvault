#!/usr/bin/env python3
"""Replay the new sidecar matcher over every real sidecar in the live DB (#6).

Read-only. Opens the database with SQLite's `mode=ro` URI so a bug here cannot
touch 142 MB of real index, and writes nothing anywhere.

Two acceptance bases are reported, because they answer different questions:

  all-sidecar          unmatched / every sidecar in the archive
  addressable-subset   unmatched / sidecars whose media file was actually
                       ingested from the same archive directory

The addressable basis is the one to gate on. A sidecar naming a photo that
Google never included in the export cannot be bound by any matcher, and
counting it as failure measures Takeout's completeness rather than ours.

Usage:
    ./.venv/bin/python scripts/replay_sidecar_matcher.py [--db memoryvault.db]
"""

import argparse
import random
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memoryvault.sidecar_names import (  # noqa: E402
    resolve_media_name, split_sidecar_name,
)

# Gate from the plan: at most 3% unmatched on the addressable basis, and no
# cross-directory binds at all.
MAX_UNMATCHED_PCT = 3.0
FUZZY_SAMPLE_SIZE = 50


def open_readonly(db_path: Path) -> sqlite3.Connection:
    """Open the live database read-only. Nothing here may modify it."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def load_entries(conn) -> tuple[dict, dict]:
    """Return (sidecars_by_dir, media_by_dir) from archive_entries."""
    sidecars = defaultdict(list)
    media = defaultdict(list)

    for row in conn.execute(
        "SELECT entry_path, status, skip_reason FROM archive_entries"
    ):
        entry = row["entry_path"]
        if not entry:
            continue
        path = Path(entry)
        directory = str(path.parent)

        if row["skip_reason"] == "sidecar":
            sidecars[directory].append(path.name)
        elif row["skip_reason"] != "album_metadata" and \
                not path.name.lower().endswith(".json"):
            # Every non-JSON entry the archive contained, kept or skipped —
            # a sidecar's media file is addressable either way.
            media[directory].append(path.name)

    return sidecars, media


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="memoryvault.db", type=Path)
    parser.add_argument("--seed", default=0, type=int,
                        help="Seed for the fuzzy sample (reproducible).")
    args = parser.parse_args()

    if not args.db.exists():
        print(f"No such database: {args.db}", file=sys.stderr)
        return 2

    conn = open_readonly(args.db)
    sidecars_by_dir, media_by_dir = load_entries(conn)

    total = 0
    matched = 0
    fuzzy_hits = []
    reasons = Counter()
    addressable_total = 0
    addressable_unmatched = 0
    cross_directory = 0

    name_parsed = 0

    for directory, names in sidecars_by_dir.items():
        listing = media_by_dir.get(directory, [])
        listing_set = set(listing)

        for name in names:
            total += 1
            if split_sidecar_name(name).media_name:
                name_parsed += 1
            result = resolve_media_name(name, candidates=listing)

            if result.media_name:
                matched += 1
                addressable_total += 1
                if result.media_name not in listing_set:
                    # Would mean the matcher invented a name not in this
                    # directory — impossible by construction, checked anyway.
                    cross_directory += 1
                if result.fuzzy:
                    fuzzy_hits.append((directory, name, result.media_name))
                continue

            reasons[result.reason.value if result.reason else "unknown"] += 1
            # Addressable means the media file is genuinely present in this
            # directory and we still failed to see it. A sidecar naming a
            # photo Google filed under a different year — or never exported —
            # cannot be bound by any matcher, and counting it would measure
            # Takeout's completeness rather than ours.
            if _media_is_present(result, listing_set):
                addressable_total += 1
                addressable_unmatched += 1

    print(f"Database: {args.db}  (read-only)")
    print(f"Directories with sidecars: {len(sidecars_by_dir):,}")
    print()
    print(f"Sidecars replayed:  {total:,}")
    # Directly comparable to REVIEW.md's 56.9% — that figure counted names
    # the old two-pattern matcher could turn into a media name at all, with
    # no check that the media existed.
    print(f"  name resolvable:  {name_parsed:,}  "
          f"({_pct(name_parsed, total):.1f}%)   "
          f"[REVIEW.md baseline: 56.9%]")
    print(f"  matched:          {matched:,}  ({_pct(matched, total):.1f}%)")
    print(f"  unmatched:        {total - matched:,}  "
          f"({_pct(total - matched, total):.1f}%)")
    print(f"  of which fuzzy:   {len(fuzzy_hits):,}")
    print()

    print("Unmatched by reason:")
    for reason, count in reasons.most_common():
        print(f"  {count:>7,}  {reason}")
    print()

    all_pct = _pct(total - matched, total)
    addr_pct = _pct(addressable_unmatched, addressable_total)
    print("Acceptance bases:")
    print(f"  all-sidecar:        {all_pct:.2f}% unmatched "
          f"({total - matched:,} / {total:,})")
    print(f"  addressable-subset: {addr_pct:.2f}% unmatched "
          f"({addressable_unmatched:,} / {addressable_total:,})")
    print()
    print(f"Cross-directory binds: {cross_directory}")
    print()

    _print_fuzzy_sample(fuzzy_hits, args.seed)

    ok = addr_pct <= MAX_UNMATCHED_PCT and cross_directory == 0
    print()
    if ok:
        print(f"PASS  addressable unmatched {addr_pct:.2f}% "
              f"<= {MAX_UNMATCHED_PCT}%, no cross-directory binds")
    else:
        print(f"FAIL  addressable unmatched {addr_pct:.2f}% "
              f"(gate {MAX_UNMATCHED_PCT}%), "
              f"cross-directory binds {cross_directory} (gate 0)")
    return 0 if ok else 1


def _media_is_present(result, listing_set: set) -> bool:
    """Is the media this sidecar names actually sitting in the directory?

    Checked independently of the matcher's own verdict, so it cannot excuse a
    miss: it asks whether any file in the directory carries the sidecar's
    parsed stem, ignoring case and the media extension. If one does, binding
    it was our job.
    """
    parsed = result.parsed
    if not parsed.media_name:
        return False

    stem = parsed.media_name
    if parsed.counter:
        stem = stem.replace(parsed.counter, "", 1)
    stem = stem.rsplit(".", 1)[0].lower()
    if not stem:
        return False

    for candidate in listing_set:
        lowered = candidate.lower()
        if not (lowered.rsplit(".", 1)[0] == stem or lowered.startswith(stem)):
            continue
        # A `(n)` sidecar is only addressable by a file carrying that same
        # counter. `DSC_1570(1).JPG` and `DSC_1570.JPG` are different photos,
        # so the base file's presence does not make the counter sidecar
        # bindable — refusing it is correct, not a miss.
        if parsed.counter and parsed.counter not in candidate:
            continue
        return True

    return False


def _print_fuzzy_sample(fuzzy_hits, seed: int):
    """Fuzzy binds are the only guesses made; they must be hand-checkable."""
    print(f"Fuzzy binds ({len(fuzzy_hits):,} total) — "
          f"random sample of up to {FUZZY_SAMPLE_SIZE} for hand checking:")
    if not fuzzy_hits:
        print("  (none)")
        return

    rng = random.Random(seed)
    sample = rng.sample(fuzzy_hits, min(FUZZY_SAMPLE_SIZE, len(fuzzy_hits)))
    for directory, sidecar, media in sorted(sample):
        print(f"  {sidecar}")
        print(f"      → {media}")
        print(f"        in {directory}")


def _pct(part: int, whole: int) -> float:
    return (100.0 * part / whole) if whole else 0.0


if __name__ == "__main__":
    raise SystemExit(main())
