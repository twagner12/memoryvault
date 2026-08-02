"""Bind sidecars whose media file was not in the same archive (#6).

Google splits one album directory across zip parts. Ingest sees one part at a
time, so a sidecar in part 9 describing a photo kept from part 8 cannot bind —
it is recorded in `sidecars_unmatched` *with its parsed payload*, because the
zips are not re-read. This pass is the second look, run once every part is in.

All 312 rebindable sidecars in the shakedown corpus are that exact shape: 27
part-9 sidecars whose media came from part 8, and 285 the other way round.

Three properties carry the whole design:

**It is not a second matcher.** Resolution goes through
`sidecar_names.resolve_media_name`, the same function ingest uses, given the
same thing: one directory's filenames. Counter arithmetic, truncated
`supplemental-metadata` suffixes, the fuzzy prefix tier and its refusal on
ambiguity are inherited rather than reimplemented, and a cross-directory bind
stays impossible because the candidate list never contains another directory's
names.

**Directory identity is the archive-internal path.** `archive_entries.
entry_path` and `sidecars_unmatched.archive_dir` are both paths *inside* the
zip, so `Takeout/Google Photos/Photos from 2021` is the same string in every
part it was split across. Matching is exact string equality on that dirname —
no normalisation, no prefix logic, nothing that could drift into a neighbouring
directory.

**Applying goes through the ingest choke point.** `_apply_sidecar_to_file`
carries the already-present and outstanding-value guards, so re-offering a
sidecar is a no-op rather than a second write. That is what makes a crash
mid-run safe: the row keeps `rebound_at` NULL and the next run re-offers it.
"""

import json
import logging
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from memoryvault.database import Database
from memoryvault.ingest import _apply_sidecar_to_file
from memoryvault.sidecar_names import UnmatchedReason, resolve_media_name

logger = logging.getLogger(__name__)


@dataclass
class Bind:
    """One sidecar resolved to one vault file, before anything is applied."""
    row: dict
    target: Path
    media_name: str
    # Rows carrying a byte-identical offer for the same directory and name.
    # They are marked alongside the bind but applied only once.
    duplicate_ids: list[int]
    relocated: bool = False


@dataclass
class Refusal:
    row: dict
    reason: str


def rebind_sidecars(db: Database, dry_run: bool = False,
                    progress_callback=None) -> dict:
    """Offer every still-unmatched sidecar to the files now in the vault.

    With `dry_run`, resolution and target checking run in full and nothing is
    written — no metadata, no mtime, no `rebound_at`. The report is identical,
    so what a dry run promises is what a real run does.

    Returns counts: `bound`, `refused` (by reason), `duplicate_rows` (identical
    offers collapsed onto a bind), and `by_archive` (binds attributed to the
    archive the *sidecar* came from).
    """
    binds, refusals = plan_rebind(db)
    refused: Counter = Counter(r.reason for r in refusals)

    if not dry_run:
        applied: list[Bind] = []
        for bind in binds:
            try:
                _apply_and_mark(db, bind)
            except Exception as exc:
                # Per sidecar, so one unwritable file cannot strand the pass.
                # The row keeps rebound_at NULL and will be re-offered; a
                # refused write has already been recorded in metadata_pending
                # by apply_sidecar, so nothing here is a silent loss.
                logger.warning("rebind failed for %s -> %s: %s",
                               bind.row["sidecar_path"], bind.target, exc)
                refused[f"apply_error:{type(exc).__name__}"] += 1
                continue
            applied.append(bind)
            if progress_callback and len(applied) % 50 == 0:
                progress_callback("progress", applied=len(applied),
                                  total=len(binds))
        # Report what happened, not what was planned.
        binds = applied

    stats = {
        "bound": len(binds),
        "refused": dict(refused),
        "duplicate_rows": sum(len(b.duplicate_ids) for b in binds),
        "by_archive": dict(Counter(b.row["archive_id"] for b in binds)),
        "dry_run": dry_run,
    }

    if progress_callback:
        progress_callback("done", stats=stats)
    return stats


def plan_rebind(db: Database) -> tuple[list[Bind], list[Refusal]]:
    """Resolve every outstanding sidecar without touching anything.

    Split out from the applying half so `--dry-run` reports from exactly the
    same decisions a real run acts on, rather than from a parallel estimate.
    """
    listing, kept = _directory_index(db)

    binds: list[Bind] = []
    refusals: list[Refusal] = []
    seen: dict[tuple, Bind] = {}

    for row in db.get_unmatched():
        key = (row["archive_dir"], row["entry_name"], row["payload"])
        existing = seen.get(key)
        if existing is not None:
            # The same sidecar, recorded again by a re-ingested archive. Mark
            # it alongside the bind, but apply the value once.
            existing.duplicate_ids.append(row["id"])
            continue

        bind, reason = _resolve_one(db, row, listing, kept)
        if bind is None:
            refusals.append(Refusal(row, reason))
            continue
        seen[key] = bind
        binds.append(bind)

    return binds, refusals


def _resolve_one(db: Database, row: dict, listing: dict, kept: dict
                 ) -> tuple[Bind | None, str | None]:
    """Find the vault file one sidecar names, or say why it cannot be found."""
    if not row["payload"]:
        # An unreadable sidecar: recorded so it stays countable, but there is
        # no value to re-apply and there never will be.
        return None, "no_payload"

    directory = row["archive_dir"]
    resolved = resolve_media_name(row["entry_name"],
                                  sorted(listing.get(directory, ())))
    if resolved.media_name is None:
        reason = resolved.reason or UnmatchedReason.NO_MEDIA_IN_DIR
        return None, reason.value

    kept_path = kept.get(directory, {}).get(resolved.media_name)
    if kept_path is None:
        # The name is right and the directory is right, but no archive ever
        # recorded where this entry landed — it was skipped as a duplicate of a
        # copy kept from somewhere else, or it errored. Resolving it by content
        # would be a cross-directory bind through the back door.
        return None, "media_not_kept"

    target, reason, relocated = _locate_target(db, kept_path)
    if target is None:
        return None, reason

    return Bind(row=row, target=target, media_name=resolved.media_name,
                duplicate_ids=[], relocated=relocated), None


def _locate_target(db: Database, kept_path: str
                   ) -> tuple[Path | None, str | None, bool]:
    """Resolve a recorded `kept_path` to a file that exists right now.

    `kept_path` is where ingest put the file, not a promise about where it is.
    When it has moved, `source_blake3` is what finds it again: a file that had
    EXIF written at ingest hashes differently on disk than it did in the
    archive, so `blake3_full` alone would miss it. `find_files_by_content_hash`
    matches either column for exactly this reason.

    A file whose bytes have since changed is *not* a reason to refuse — that is
    the normal state of anything ingest wrote metadata into. Only a target we
    cannot point at, or can point at two of, is.
    """
    path = Path(kept_path)
    if path.exists():
        return path, None, False

    row = db.get_file_by_path(kept_path)
    if row is None:
        return None, "target_missing", False

    survivors = []
    for digest in (row["source_blake3"], row["blake3_full"]):
        if not digest:
            continue
        for candidate in db.find_files_by_content_hash(digest):
            candidate_path = Path(candidate["path"])
            if candidate_path.exists() and candidate_path not in survivors:
                survivors.append(candidate_path)

    if not survivors:
        return None, "target_missing", False
    if len(survivors) > 1:
        # Two byte-identical copies is a choice about which one owns the
        # metadata, and this pass does not make choices.
        return None, "ambiguous_target", False
    return survivors[0], None, True


def _directory_index(db: Database) -> tuple[dict[str, set[str]],
                                            dict[str, dict[str, str]]]:
    """Build, per archive directory, what was in it and where it was kept.

    Two maps rather than one, because they answer different questions:

    - `listing` is every media filename seen in that directory, including
      entries with no kept copy. The fuzzy tier judges ambiguity from it, and a
      name we cannot bind to must still be able to make a match ambiguous.
    - `kept` is only the names we can actually point at a file for.

    Built in Python rather than as a SQL join: `dirname`/`basename` are not
    SQL primitives, and the matcher needs a directory's complete listing in
    hand — the same reason ingest builds `dir_media` as it streams.
    """
    listing: dict[str, set[str]] = {}
    kept: dict[str, dict[str, str]] = {}

    for entry in db.get_media_entries():
        directory, _, name = entry["entry_path"].rpartition("/")
        listing.setdefault(directory, set()).add(name)
        if entry["kept_path"]:
            kept.setdefault(directory, {}).setdefault(name, entry["kept_path"])

    return listing, kept


def _apply_and_mark(db: Database, bind: Bind):
    """Apply one sidecar's payload, then record that it bound.

    In that order: a crash between the two leaves the row outstanding and the
    next run re-offers it, which the guards in `apply_sidecar` make a no-op.
    The reverse order would lose the value outright.
    """
    outcome = _apply_sidecar_to_file(
        str(bind.target), json.loads(bind.row["payload"]),
        bind.row["sidecar_path"], db)

    summary = json.dumps({
        "merged": outcome.merged,
        "merged_mtime_only": outcome.merged_mtime_only,
        "deferred": outcome.deferred,
        "failed": outcome.failed,
        "already_present": outcome.already_present,
        "media_name": bind.media_name,
        "relocated": bind.relocated,
    })

    for sidecar_id in [bind.row["id"], *bind.duplicate_ids]:
        db.mark_sidecar_rebound(sidecar_id, str(bind.target), summary)
