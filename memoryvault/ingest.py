"""Takeout ingestion pipeline: stream from zip → hash → dedup → keep/skip.

Metadata handling funnels through `apply_sidecar`, the single place allowed to
decide what happens to a date or a location. Every (file, field) it touches
leaves a record — a `metadata_log` row, a `metadata_pending` row, or both in
the two cases where a partial success and an outstanding value coexist. That
is what makes findings #7, #8, #9 and #10 verifiable rather than hopeful.
"""

import json
import logging
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path

from memoryvault.archive import iter_entries, extract_entry_to_path, ArchiveEntry
from memoryvault.containers import detect_container, supports_exif
from memoryvault.database import Database
from memoryvault.hasher import hash_bytes, hash_full, hash_head, hash_tail
from memoryvault.metadata import (
    UnparseableExifError, can_have_exif, can_read_exif, has_metadata,
    get_exif_date, get_exif_gps,
    write_exif_date, write_exif_gps,
)
from memoryvault.phototime import resolve_capture_time
from memoryvault.sidecar_names import (
    UnmatchedReason, is_sidecar_name, resolve_media_name, split_sidecar_name,
)
from memoryvault.volumes import assert_volumes_reachable

logger = logging.getLogger(__name__)

# Extensions we consider media files
MEDIA_EXTENSIONS = {
    # Photos
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif",
    ".heic", ".heif", ".webp", ".raw", ".cr2", ".nef", ".arw", ".dng",
    # Video
    ".mp4", ".mov", ".avi", ".mkv", ".wmv", ".m4v", ".mpg", ".mpeg", ".3gp",
    # Audio
    ".mp3", ".wav", ".flac", ".aac", ".m4a", ".ogg", ".wma", ".aiff",
}


def is_media_file(path: str) -> bool:
    return Path(path).suffix.lower() in MEDIA_EXTENSIONS


def is_sidecar_json(path: str) -> bool:
    return is_sidecar_name(Path(path).name)


def is_metadata_json(path: str) -> bool:
    return Path(path).name.lower() == "metadata.json"


def scratch_path(dest_folder: Path, tmpdir: Path | None = None) -> Path:
    """Where this run spills entries too large to hold in memory.

    Defaults to a sibling of the destination rather than the system temp
    directory, for two reasons. `/tmp` is tmpfs on most modern Linux desktops —
    7.7 GB of RAM here — and `archive.py` spills every entry over 50 MB to it,
    so a large Takeout zip can exhaust memory. And a sibling is on the same
    filesystem as the destination, which keeps `extract_entry_to_path`'s move a
    rename instead of a cross-device copy of every kept file.

    A sibling rather than a child of the vault on purpose: `scan_folder` walks
    the vault with no ignore list, so scratch files left behind by a crashed
    run would be indexed as media.
    """
    if tmpdir is not None:
        return Path(tmpdir).resolve()
    return dest_folder.with_name(dest_folder.name + ".mvtmp")


@contextmanager
def _scratch(path: Path):
    """Point `tempfile` — and the 7z subprocess — at `path` for the duration.

    One lever rather than a parameter threaded through five `tempfile` call
    sites in `archive.py`. `TMPDIR` is set as well because `_stream_7z` shells
    out and would otherwise write to the system default. Ambient `TMPDIR` is
    deliberately *not* consulted as an input, so the scratch location has
    exactly one source: this argument.

    Not re-entrant, and not safe under concurrent ingests — `tempfile.tempdir`
    is process-global. Ingest is single-threaded and `TaskManager` runs one
    task at a time, which is what makes that acceptable.
    """
    path.mkdir(parents=True, exist_ok=True)
    previous_tempdir = tempfile.tempdir
    previous_env = os.environ.get("TMPDIR")

    tempfile.tempdir = str(path)
    os.environ["TMPDIR"] = str(path)
    try:
        yield path
    finally:
        tempfile.tempdir = previous_tempdir
        if previous_env is None:
            os.environ.pop("TMPDIR", None)
        else:
            os.environ["TMPDIR"] = previous_env
        try:
            path.rmdir()
        except OSError as exc:
            # Only removed when empty. Anything left is a spill from an entry
            # that failed mid-write, and deleting it blindly would destroy the
            # only evidence of what went wrong.
            logger.info("leaving scratch dir %s in place: %s", path, exc)


def ingest_archive(archive_path: Path, dest_folder: Path, db: Database,
                   progress_callback=None, title: str = None,
                   allow_unreachable_volumes: bool = False,
                   tmpdir: Path | None = None) -> dict:
    """Ingest a Takeout zip: stream files, dedup against DB, keep unique files.

    Single-pass streaming approach:
    - Sidecars (small JSON) are buffered in memory
    - Media files are processed immediately as they're encountered
    - If a sidecar arrives before its media file, it's applied during processing
    - If a sidecar arrives after its media file, it's applied retroactively

    Args:
        archive_path: Path to the zip file.
        dest_folder: Where to save unique files.
        db: Database instance.
        progress_callback: Optional callback(stage, **kwargs).
        allow_unreachable_volumes: Proceed even when indexed files live on a
            volume that is not attached. Off by default because deduplicating
            against unreadable rows discards incoming originals.
        tmpdir: Where to spill entries too large to hold in memory. Defaults
            to a sibling of `dest_folder` — see `scratch_path` for why that,
            rather than the system temp directory, is the safe default.

    Returns dict with stats: kept, skipped, errors, merged_metadata.

    Raises:
        UnreachableVolumeError: an indexed volume is detached and no override
            was given.
    """
    archive_path = archive_path.resolve()
    dest_folder = dest_folder.resolve()

    # Checked before any bytes are written, so an aborted run changes nothing.
    if not allow_unreachable_volumes:
        assert_volumes_reachable(db, override_hint="--allow-unreachable-volumes")

    dest_folder.mkdir(parents=True, exist_ok=True)

    # Register archive and check for resume
    # Fingerprint: hash first 1MB + file size (fast duplicate zip detection)
    file_size = archive_path.stat().st_size
    with open(archive_path, "rb") as f:
        head = f.read(1_048_576)
    archive_fingerprint = hash_bytes(head + str(file_size).encode())

    archive_id = db.register_archive(str(archive_path), blake3=archive_fingerprint,
                                     entries_total=0, title=title)

    archive_record = db.get_archive(str(archive_path))
    if archive_record["status"] == "complete":
        if progress_callback:
            progress_callback("already_done", archive=str(archive_path))
        return {"kept": 0, "skipped": 0, "errors": 0, "merged_metadata": 0}

    db.update_archive_status(archive_id, "in_progress")
    already_processed = db.get_processed_entries(archive_id)

    stats = {"kept": 0, "skipped": 0, "errors": 0, "merged_metadata": 0,
             "sidecars_matched": 0, "sidecars_unmatched": 0,
             "metadata_deferred": 0, "metadata_failed": 0,
             "metadata_already_present": 0}

    # Sidecars are small (just JSON text) — safe to buffer.
    # Keyed by the *resolved* media entry path, so a lookup by a media file's
    # own path finds it regardless of how Google mangled the sidecar name.
    sidecars: dict[str, dict] = {}
    # Every sidecar seen, so the post-pass can retry the ones that did not
    # bind while streaming and record whatever is still left over.
    seen_sidecars: list[dict] = []
    # The same records, reachable by candidate name. A media entry consuming
    # a sidecar marks it bound here, which is what stops the post-pass from
    # applying it a second time.
    records_by_candidate: dict[str, dict] = {}
    # Media filenames per directory within the archive, built as we stream.
    # The fuzzy tier needs a directory's full listing, which only exists once
    # the archive has been walked — hence the post-pass.
    dir_media: dict[str, list[str]] = {}
    # Where each media entry ended up on disk: the kept copy, or the surviving
    # duplicate its metadata was merged into.
    media_dest: dict[str, str] = {}

    processed_count = 0

    if progress_callback:
        progress_callback("start", processed=0, total=0)

    # Large entries spill to disk while streaming. Scoped to the run, so
    # the setting cannot leak into anything else in the process.
    with _scratch(scratch_path(dest_folder, tmpdir)):
        for entry in iter_entries(archive_path, skip_entries=already_processed):
            # One commit per entry: every row it writes lands together, or none.
            with db.batch():
                if entry.data is None and entry.temp_path is None:
                    db.log_archive_entry(archive_id, entry.path, "error", skip_reason="corrupt")
                    stats["errors"] += 1
                    processed_count += 1
                    continue

                if is_metadata_json(entry.path):
                    db.log_archive_entry(archive_id, entry.path, "skipped", skip_reason="album_metadata")
                    stats["skipped"] += 1
                    processed_count += 1
                    continue

                if is_sidecar_json(entry.path):
                    record = _read_sidecar_entry(entry)

                    if record is None:
                        # The JSON never parsed, so there is no payload to rebind
                        # from — but the sidecar existed, and saying so is the whole
                        # point of #6. Recorded with whatever the name yielded.
                        _record_unreadable_sidecar(db, archive_id, entry.path)
                        stats["sidecars_unmatched"] += 1
                        db.log_archive_entry(archive_id, entry.path, "skipped",
                                             skip_reason="sidecar")
                        stats["skipped"] += 1
                        processed_count += 1
                        continue

                    seen_sidecars.append(record)
                    # Register under every name shape this sidecar could belong to,
                    # so a media entry arriving later finds it by its own path.
                    for candidate in record["candidates"]:
                        if candidate not in sidecars:
                            sidecars[candidate] = record["parsed"]
                            records_by_candidate[candidate] = record

                    # The media file may already have been processed.
                    failure = None
                    try:
                        for candidate in record["candidates"]:
                            dest = media_dest.get(candidate)
                            if dest:
                                outcome = _apply_sidecar_to_file(
                                    dest, record["parsed"], entry.path, db)
                                _tally(stats, outcome)
                                record["bound"] = True
                                break
                    except Exception as e:
                        # Same boundary the media path uses: one bad sidecar is an
                        # entry-level error, not the end of the run.
                        failure = str(e)[:200]
                        record["failed"] = True

                    if failure is not None:
                        db.log_archive_entry(archive_id, entry.path, "error",
                                             skip_reason=failure)
                        stats["errors"] += 1
                    else:
                        db.log_archive_entry(archive_id, entry.path, "skipped",
                                             skip_reason="sidecar")
                        stats["skipped"] += 1
                    processed_count += 1
                    continue

                if not is_media_file(entry.path):
                    db.log_archive_entry(archive_id, entry.path, "skipped", skip_reason="not_media")
                    stats["skipped"] += 1
                    processed_count += 1
                    continue

                directory = str(Path(entry.path).parent)
                dir_media.setdefault(directory, []).append(Path(entry.path).name)

                # Process media file immediately — no buffering
                try:
                    result = _process_media_entry(entry, dest_folder, db, sidecars, archive_id)
                except Exception as e:
                    db.log_archive_entry(archive_id, entry.path, "error", skip_reason=str(e)[:200])
                    stats["errors"] += 1
                    processed_count += 1
                    continue
                stats[result["action"]] += 1
                if result.get("outcome"):
                    _tally(stats, result["outcome"])
                if result.get("dest_path"):
                    media_dest[entry.path] = result["dest_path"]
                if result.get("sidecar_applied"):
                    # This entry consumed a buffered sidecar, so the post-pass must
                    # not re-resolve and re-apply it. Harmless for a JPEG, whose date
                    # would then already be present, but a container that can only
                    # take the mtime has no such guard and would log a second
                    # merged_mtime_only row and a second deferred value.
                    consumed = records_by_candidate.get(entry.path)
                    if consumed is not None:
                        consumed["bound"] = True

                processed_count += 1

                if progress_callback and processed_count % 10 == 0:
                    progress_callback("progress", processed=processed_count, total=processed_count,
                                      action=result["action"], path=entry.path,
                                      kept=stats["kept"], skipped=stats["skipped"],
                                      errors=stats["errors"], merged=stats["merged_metadata"])

    # Sidecars that never bound while streaming get one more attempt now that
    # every directory's media listing is complete; whatever is still unbound
    # is recorded rather than dropped.
    _resolve_leftover_sidecars(db, archive_id, seen_sidecars, dir_media,
                               media_dest, stats)

    # Mark archive complete
    db.update_archive_status(archive_id, "complete", entries_processed=processed_count)

    # Verification: compute sizes
    kept_size = 0
    skipped_dupe_count = 0
    missing_kept = 0
    for row in db.conn.execute(
        "SELECT status, skip_reason, kept_path FROM archive_entries WHERE archive_id = ?",
        (archive_id,)
    ).fetchall():
        if row["status"] == "kept" and row["kept_path"]:
            try:
                kept_size += Path(row["kept_path"]).stat().st_size
            except OSError:
                # A file we logged as kept is no longer readable. That is a
                # real discrepancy, not a rounding error in the size total.
                missing_kept += 1
        if row["skip_reason"] == "duplicate":
            skipped_dupe_count += 1

    stats["processed_count"] = processed_count
    stats["kept_size_bytes"] = kept_size
    stats["missing_kept_files"] = missing_kept
    stats["skipped_duplicate_count"] = skipped_dupe_count
    stats["sidecar_count"] = stats["skipped"] - skipped_dupe_count
    stats["verified"] = (stats["kept"] + stats["skipped"] + stats["errors"] == processed_count)

    if progress_callback:
        progress_callback("done", stats=stats)

    return stats


def _read_sidecar_entry(entry: ArchiveEntry) -> dict | None:
    """Parse a sidecar entry into its payload and the media names it may name.

    Returns None only when the JSON itself is unreadable — that is a corrupt
    entry, not an unmatched sidecar, and there is nothing to rebind later.
    """
    raw = entry.read_data()
    if not raw:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None

    entry_path = Path(entry.path)
    directory = str(entry_path.parent)
    resolved = resolve_media_name(entry_path.name, candidates=[])
    parsed_name = resolved.parsed

    candidates = [f"{directory}/{name}" for name in parsed_name.all_candidates]

    return {
        "entry_path": entry.path,
        "entry_name": entry_path.name,
        "directory": directory,
        "parsed": _parse_sidecar_data(data),
        "name_parts": parsed_name,
        "candidates": candidates,
        "bound": False,
    }


def _tally(stats: dict, outcome: "Outcome"):
    """Fold one outcome into the run's counters."""
    stats["merged_metadata"] += outcome.log_count
    stats["metadata_deferred"] += len(outcome.deferred)
    stats["metadata_failed"] += len(outcome.failed)
    stats["metadata_already_present"] += len(outcome.already_present)


def _resolve_leftover_sidecars(db: Database, archive_id: int,
                               seen_sidecars: list[dict],
                               dir_media: dict[str, list[str]],
                               media_dest: dict[str, str], stats: dict):
    """Re-resolve unbound sidecars against complete directory listings.

    Anything still unbound is written to `sidecars_unmatched` *with its parsed
    payload*, because the Takeout zips will not be re-read: 18,840 sidecars
    were previously parsed, discarded and logged as `skipped/sidecar`,
    indistinguishable from success.
    """
    for record in seen_sidecars:
        if record.get("failed"):
            # Already recorded as an error against its entry while streaming.
            continue
        if record["bound"]:
            stats["sidecars_matched"] += 1
            continue

        try:
            with db.batch():       # one commit per leftover sidecar, or none
                _rebind_one_sidecar(db, archive_id, record, dir_media, media_dest,
                                    stats)
        except Exception as e:
            # Per record, so one bad sidecar cannot strand the archive at
            # 'in_progress'. Only a failure of the pass itself escapes.
            db.log_archive_entry(archive_id, record["entry_path"], "error",
                                 skip_reason=str(e)[:200])
            # It was tallied as skipped when it streamed past; its status has
            # just changed, so the kept+skipped+errors total still reconciles.
            stats["skipped"] -= 1
            stats["errors"] += 1


def _rebind_one_sidecar(db: Database, archive_id: int, record: dict,
                        dir_media: dict[str, list[str]],
                        media_dest: dict[str, str], stats: dict):
    """Resolve one leftover sidecar against its directory, or record it."""
    listing = dir_media.get(record["directory"], [])
    resolved = resolve_media_name(record["entry_name"], candidates=listing)

    dest = None
    if resolved.media_name:
        dest = media_dest.get(f"{record['directory']}/{resolved.media_name}")

    if dest:
        outcome = _apply_sidecar_to_file(dest, record["parsed"],
                                         record["entry_path"], db)
        _tally(stats, outcome)
        stats["sidecars_matched"] += 1
        return

    # Either no media name could be derived, or the media it names was
    # never kept (already a duplicate elsewhere, or absent from the zip).
    reason = resolved.reason or UnmatchedReason.NO_MEDIA_IN_DIR
    parts = record["name_parts"]
    db.record_unmatched_sidecar(
        archive_id=archive_id,
        sidecar_path=record["entry_path"],
        archive_dir=record["directory"],
        entry_name=record["entry_name"],
        media_stem=parts.media_name,
        counter=parts.counter,
        reason=reason.value,
        candidate_count=resolved.candidate_count,
        payload=json.dumps(record["parsed"]),
    )
    stats["sidecars_unmatched"] += 1


def _record_unreadable_sidecar(db: Database, archive_id: int, entry_path: str):
    """Record a sidecar whose JSON could not be read at all.

    Distinct from an unmatched one: there is no payload, so a rebind pass has
    nothing to re-apply. The row exists so the sidecar is still countable and
    inspectable instead of being indistinguishable from a successful skip.
    """
    path = Path(entry_path)
    parts = split_sidecar_name(path.name)
    db.record_unmatched_sidecar(
        archive_id=archive_id,
        sidecar_path=entry_path,
        archive_dir=str(path.parent),
        entry_name=path.name,
        media_stem=parts.media_name,
        counter=parts.counter,
        reason=UnmatchedReason.UNPARSEABLE.value,
        candidate_count=0,
        payload=None,
    )


def _parse_sidecar_data(data: dict) -> dict:
    """Extract the raw UTC epoch and GPS from sidecar JSON.

    The epoch is deliberately *not* converted to a datetime here. Takeout's
    timestamp is UTC; EXIF wants camera-local wall-clock time. Doing the
    conversion at parse time, with no location in hand, is what produced the
    ±14 h drift of finding #9 — so it is deferred to `phototime`, which has
    the file and its siblings available.
    """
    result = {"utc_epoch": None, "lat": None, "lon": None}

    for time_key in ("photoTakenTime", "creationTime"):
        time_data = data.get(time_key)
        if time_data and "timestamp" in time_data:
            try:
                ts = int(time_data["timestamp"])
            except (TypeError, ValueError):
                continue
            if ts > 0:
                result["utc_epoch"] = ts
                break

    for geo_key in ("geoData", "geoDataExif"):
        geo = data.get(geo_key)
        if geo:
            lat = geo.get("latitude", 0)
            lon = geo.get("longitude", 0)
            # Google writes 0,0 to mean "no location".
            if lat != 0 or lon != 0:
                result["lat"] = lat
                result["lon"] = lon
                break

    return result


@dataclass
class Outcome:
    """What happened to each field of one sidecar, for one file.

    A field appears in exactly one of these lists, except for the two
    deliberate dual states: a date that reached only the filesystem mtime is
    both `merged_mtime_only` and `deferred`, and a date written under an
    assumed UTC offset is both `merged` and `deferred`.
    """
    merged: list[str] = dataclass_field(default_factory=list)
    merged_mtime_only: list[str] = dataclass_field(default_factory=list)
    deferred: list[str] = dataclass_field(default_factory=list)
    failed: list[str] = dataclass_field(default_factory=list)
    already_present: list[str] = dataclass_field(default_factory=list)

    @property
    def changed_file(self) -> bool:
        """True when the file's bytes or mtime moved, so its row must follow."""
        return bool(self.merged or self.merged_mtime_only)

    @property
    def log_count(self) -> int:
        """Fields whose value actually reached the file — what `merged` counts.

        Not every `metadata_log` row: `already_present` also lands there, but
        it records a value that was *declined*, and folding it in here would
        inflate the run's "metadata merged" figure with non-merges.
        """
        return len(self.merged) + len(self.merged_mtime_only)


def apply_sidecar(path: Path, sidecar: dict, db: Database, source_desc: str,
                  siblings: list[Path] | None = None) -> Outcome:
    """The one place metadata is applied. Every outcome is recorded.

    Routes on the file's *container*, sniffed from its bytes, never on its
    extension — 4,771 of the 12,924 files in the shakedown vault (37%) are
    not the format their name claims, most of them JPEGs wearing a `.png`
    name — and genuine HEIC cannot take an EXIF write at all.

    - container supports EXIF → write; on refusal record `failed`
    - container cannot        → set the mtime from the date (all a video can
                                hold) and record the real value as `deferred`

    Fields the file already carries are left alone: a sidecar is a fallback
    for missing metadata, not an authority over what the camera recorded.
    """
    outcome = Outcome()
    if not path.exists():
        raise FileNotFoundError(f"apply_sidecar: {path} does not exist")

    has_date = sidecar.get("utc_epoch") is not None
    has_gps = sidecar.get("lat") is not None
    if not has_date and not has_gps:
        return outcome

    container = detect_container(path)
    writable = supports_exif(container)

    capture = None
    if has_date:
        geo = ({"lat": sidecar["lat"], "lon": sidecar["lon"]} if has_gps
               else None)
        capture = resolve_capture_time(sidecar["utc_epoch"], geo, path,
                                       siblings=list(siblings or []))

    if writable:
        if has_date:
            _apply_date_to_exif(path, capture, sidecar["utc_epoch"], db,
                               source_desc, outcome)
        if has_gps:
            _apply_gps_to_exif(path, sidecar, db, source_desc, outcome)
    else:
        reason = f"unsupported_{container.value}"
        if has_date:
            _apply_date_as_mtime(path, sidecar["utc_epoch"], capture, db,
                                 source_desc, reason, outcome)
        if has_gps:
            # Guarded like every sibling branch. A genuine HEIC's GPS cannot be
            # read back, so the outstanding row is the only thing that knows
            # this location was already offered — the guard has to key on it.
            _record_pending_once(db, path, "gps", _gps_payload(sidecar),
                                 reason, "deferred", source_desc)
            outcome.deferred.append("gps")

    return outcome


def _record_already_present(db: Database, path: Path, field: str,
                            offered: dict, satisfied_by: str, db_source: str,
                            outcome: Outcome):
    """Record that the file already carried what the sidecar offered.

    The fourth outcome, and the one that was missing: 648 bound sidecars in the
    shakedown produced no row in either table because "the file already has
    this" was decided and then forgotten. Silence there is indistinguishable
    from a sidecar that never bound at all, which is the exact confusion #6 was
    about.

    `offered` carries the sidecar's raw values, never the resolved date
    rendering, so the JSON is a pure function of the offer: it cannot drift
    with a timezone lookup, which makes it a stable idempotency key. Keys are
    sorted for the same reason.
    """
    value = json.dumps({"field": field, "offered": offered,
                        "satisfied_by": satisfied_by}, sort_keys=True)

    # An outcome is still an outcome on a re-offer — the caller must hear it —
    # but the row is written once.
    outcome.already_present.append(field)
    if db.has_already_present(str(path), value):
        return

    db.log_metadata_merge(str(path), db_source, "already_present", value)


def _record_pending_once(db: Database, path: Path, field: str, value: str,
                         reason: str, state: str, source_desc: str) -> bool:
    """Record an outstanding value, unless that exact value is already recorded.

    The single door onto `record_pending` for this module, so no branch can
    acquire an unguarded one by accident. Two things depend on the check
    happening *here* rather than inside the database call:

    - The row is the drain pass's instruction. Two identical outstanding rows
      mean the value gets applied twice.
    - `hash_full` is a full re-read of the file, and it used to be evaluated as
      a call argument — so a re-offer paid for the hash whether or not the row
      was wanted. The duplicate-merge path re-offers the same sidecar every
      time a byte-identical copy arrives, which this corpus does constantly.

    Returns whether a row was written. Callers report the field as outstanding
    either way: suppressing a duplicate row must not turn into a silent zero.
    """
    if db.has_outstanding_pending(str(path), field, value):
        return False

    db.record_pending(str(path), field, value, reason, state,
                      source_desc=source_desc, file_blake3=hash_full(path))
    return True


def _date_payload(capture, utc_epoch: int) -> str:
    """Everything a drain pass needs to re-apply this date without the sidecar.

    Both renderings are stored: the local wall clock actually written to EXIF,
    and the original UTC instant it came from. Keeping the epoch means a later
    pass that learns the true timezone can redo the conversion, rather than
    having to undo ours.
    """
    from datetime import datetime, timezone

    return json.dumps({
        "datetime_local": capture.local_dt.isoformat(),
        "offset": capture.offset,
        "utc": datetime.fromtimestamp(utc_epoch, tz=timezone.utc).isoformat(),
        "utc_epoch": utc_epoch,
        "tz": capture.tz_name,
        "tz_source": capture.tz_source.value,
    })


def _gps_payload(sidecar: dict) -> str:
    return json.dumps({
        "lat": sidecar["lat"], "lon": sidecar["lon"],
        "altitude": None, "altitude_ref": None,
    })


def _apply_date_to_exif(path: Path, capture, utc_epoch: int, db: Database,
                        source_desc: str, outcome: Outcome):
    if get_exif_date(path):
        _record_already_present(db, path, "date", {"utc_epoch": utc_epoch},
                                "exif_date", source_desc, outcome)
        return

    payload = _date_payload(capture, utc_epoch)
    try:
        write_exif_date(path, capture.local_dt, capture.offset)
    except UnparseableExifError:
        _record_pending_once(db, path, "date", payload, "failed_unparseable",
                             "failed", source_desc)
        outcome.failed.append("date")
        return
    except Exception as exc:
        _record_pending_once(db, path, "date", payload,
                             f"failed_write_error:{type(exc).__name__}",
                             "failed", source_desc)
        outcome.failed.append("date")
        return

    # The EXIF write rewrote the file. `_insert` kept the mtime it had, but
    # that is the moment of extraction, not of capture — so set it. mtime is
    # format-agnostic and the instant is already in hand, which makes this the
    # cheapest possible way to stop a dated photo reading as modified today.
    # utc_epoch rather than the local wall clock: mtime is an absolute instant,
    # the same reasoning as `_apply_date_as_mtime`. The EXIF value remains the
    # stronger claim; this only keeps the weaker one from contradicting it.
    os.utime(path, (utc_epoch, utc_epoch))

    db.log_metadata_merge(str(path), source_desc, "date", payload)
    outcome.merged.append("date")

    if capture.is_assumed:
        # Written, but on a guessed offset. Recorded so a later pass can
        # revisit it once a better timezone source exists.
        _record_pending_once(db, path, "date", payload,
                             "tz_unknown_assumed_utc", "deferred", source_desc)
        outcome.deferred.append("date")


def _apply_gps_to_exif(path: Path, sidecar: dict, db: Database,
                       source_desc: str, outcome: Outcome):
    if get_exif_gps(path):
        _record_already_present(db, path, "gps",
                                {"lat": sidecar["lat"], "lon": sidecar["lon"]},
                                "exif_gps", source_desc, outcome)
        return

    payload = _gps_payload(sidecar)
    try:
        write_exif_gps(path, sidecar["lat"], sidecar["lon"])
    except UnparseableExifError:
        _record_pending_once(db, path, "gps", payload, "failed_unparseable",
                             "failed", source_desc)
        outcome.failed.append("gps")
        return
    except Exception as exc:
        _record_pending_once(db, path, "gps", payload,
                             f"failed_write_error:{type(exc).__name__}",
                             "failed", source_desc)
        outcome.failed.append("gps")
        return

    db.log_metadata_merge(str(path), source_desc, "gps", payload)
    outcome.merged.append("gps")


def _apply_date_as_mtime(path: Path, utc_epoch: int, capture, db: Database,
                         source_desc: str, reason: str, outcome: Outcome):
    """Recover a date for containers that cannot hold EXIF (#8).

    4,741 Takeout videos landed in the vault with their date discarded, to be
    re-dated by every downstream tool as the moment of ingest. The mtime is
    the only field an mp4 or a genuine HEIC will accept without an exiftool
    dependency, so it is set — and the real value is still recorded as
    outstanding, because an mtime is a weaker claim than embedded metadata.
    """
    # The EXIF paths refuse to overwrite a date the file already carries, via
    # `get_exif_date`. A container that can only hold an mtime needs the same
    # guard, and its mtime *is* the stored value — without this, any second
    # offer of the same date logs a duplicate `merged_mtime_only` row and a
    # duplicate deferred value. That happens whenever a byte-identical copy
    # arrives with its own sidecar, which the corpus does constantly: 51,863
    # entries are already skipped as duplicates.
    if int(path.stat().st_mtime) == int(utc_epoch):
        _record_already_present(db, path, "date", {"utc_epoch": utc_epoch},
                                "mtime", source_desc, outcome)
        return

    payload = _date_payload(capture, utc_epoch)

    # utc_epoch, not the local wall clock: mtime is an absolute instant.
    os.utime(path, (utc_epoch, utc_epoch))
    db.log_metadata_merge(str(path), source_desc, "merged_mtime_only", payload)
    outcome.merged_mtime_only.append("date")

    _record_pending_once(db, path, "date", payload, reason, "deferred",
                         source_desc)
    outcome.deferred.append("date")


def _apply_sidecar_to_file(dest_path: str, sidecar: dict, source_desc: str,
                           db: Database, siblings: list[Path] | None = None
                           ) -> Outcome:
    """Apply a late-arriving sidecar to an already-saved file."""
    path = Path(dest_path)
    if not path.exists():
        return Outcome()

    outcome = apply_sidecar(path, sidecar, db, f"takeout:{source_desc}",
                            siblings=siblings)
    if outcome.changed_file:
        # The file's bytes or mtime just changed; its row must follow.
        _index_saved_file(db, path)
    return outcome


def _process_media_entry(entry: ArchiveEntry, dest_folder: Path, db: Database,
                         sidecars: dict, archive_id: int) -> dict:
    """Process a single media file from the archive.

    Returns dict with 'action' ('kept', 'skipped', or 'errors'),
    optional 'merged' list, and optional 'dest_path'.
    """
    try:
        return _process_media_entry_inner(entry, dest_folder, db, sidecars, archive_id)
    finally:
        entry.cleanup()


def _process_media_entry_inner(entry: ArchiveEntry, dest_folder: Path, db: Database,
                               sidecars: dict, archive_id: int) -> dict:
    # Hash the source bytes — from disk for large files, from memory for small.
    # This is the entry's identity as it came out of the archive, and is stored
    # as source_blake3; it is NOT necessarily the hash of what we write, because
    # metadata may be embedded into the saved copy afterwards.
    if entry.is_large:
        source_hash = hash_full(entry.temp_path)
    else:
        source_hash = hash_bytes(entry.data)

    # A skip is only safe if the copy we would keep still exists. Rows can
    # outlive their files: a deleted vault, a reorganised folder, or a detached
    # drive all leave the index claiming ownership of bytes nobody can read.
    candidates = db.find_files_by_content_hash(source_hash)
    surviving = [c for c in candidates if Path(c["path"]).exists()]

    if surviving:
        outcome = _try_merge_from_duplicate(entry, surviving[0], sidecars, db)
        db.log_archive_entry(archive_id, entry.path, "skipped", skip_reason="duplicate")
        # The surviving copy is where this entry's metadata now lives, so a
        # late-arriving sidecar must be routed there rather than dropped.
        return {"action": "skipped", "outcome": outcome,
                "dest_path": surviving[0]["path"],
                "sidecar_applied": sidecars.get(entry.path) is not None}

    # Matched the index but every copy is gone — treat as new and record why,
    # so the decision is queryable rather than silent.
    keep_reason = "prior_copy_missing" if candidates else None

    # NOTE: the old same-size/head-hash probe that ran here was removed with
    # finding #11. It ended by comparing blake3_full to the incoming hash, which
    # find_files_by_content_hash already covers, so it could not match anything
    # new — and its head-hash comparison read a column that drifts once EXIF is
    # written into a saved file.

    # New file — save it. If this very content's vault copy went missing, put
    # it back at the recorded path so the existing row describes it again.
    filename = Path(entry.path).name
    dest_path = _missing_copy_in(candidates, dest_folder)
    adopted = False
    if dest_path is not None:
        keep_reason = "prior_copy_restored"
    else:
        # A crash between the extract and this entry's commit leaves an
        # unrecorded copy of exactly these bytes: record it, don't copy again.
        dest_path = _adoptable_copy(dest_folder, filename, source_hash, db)
        if dest_path is not None:
            keep_reason, adopted = "orphan_adopted", True
        else:
            dest_path = _unique_dest_path(dest_folder, filename, db)

    if not adopted:
        try:
            extract_entry_to_path(entry, dest_path)
        except OSError:
            db.log_archive_entry(archive_id, entry.path, "error", skip_reason="write_failed")
            return {"action": "errors"}

    # Apply sidecar metadata if available (sidecar arrived before media file)
    outcome = Outcome()
    sidecar = sidecars.get(entry.path)
    if sidecar:
        outcome = apply_sidecar(dest_path, sidecar, db, f"takeout:{entry.path}")

    # Index the file as it now stands on disk. Metadata writes above changed
    # both its size and its bytes, so every hash must be taken after them.
    _index_saved_file(db, dest_path, source_hash=source_hash, source="takeout")

    db.log_archive_entry(archive_id, entry.path, "kept", kept_path=str(dest_path),
                         skip_reason=keep_reason)
    return {"action": "kept", "outcome": outcome, "dest_path": str(dest_path),
            "sidecar_applied": sidecar is not None}


def _index_saved_file(db: Database, path: Path, source_hash: str | None = None,
                      source: str | None = None):
    """Record a saved file's hashes from its current on-disk bytes.

    Called after every write that can change the file, so `blake3_full`,
    `blake3_head`, `blake3_tail` and `size` always describe what is actually
    on disk. `source_blake3` is only written when supplied, so refreshing an
    existing row preserves the provenance recorded at ingest time.

    The bytes are forced to disk before the row is committed. Hashing reads the
    page cache, so without this a row can record a correct hash for bytes that
    never reached the platter — the 2026-09-09 hot-unplug left 14 such files
    empty and four more with no directory entry at all.
    """
    _sync_to_disk(path)
    stat = path.stat()
    size = stat.st_size

    fields = {
        "path": str(path),
        "size": size,
        "blake3_full": hash_full(path),
        "blake3_head": hash_head(path),
        "blake3_tail": hash_tail(path) if size > 4_096 else hash_head(path),
        "mtime": stat.st_mtime,
    }

    meta = has_metadata(path) if can_have_exif(path) else {
        "has_exif_date": False, "has_exif_gps": False
    }
    fields["has_exif_date"] = meta["has_exif_date"]
    fields["has_exif_gps"] = meta["has_exif_gps"]

    if source_hash is not None:
        fields["source_blake3"] = source_hash
    if source is not None:
        fields["source"] = source

    db.upsert_file(**fields)


def _try_merge_from_duplicate(entry: ArchiveEntry, existing: dict,
                              sidecars: dict, db: Database) -> Outcome:
    """Merge metadata from a skipped duplicate into the surviving copy.

    Two sources, both routed through `apply_sidecar` so the outcome is
    recorded either way:
    1. The Takeout JSON sidecar, if one named this entry
    2. The duplicate's own EXIF, which may carry what the kept copy lacks
    """
    import tempfile

    outcome = Outcome()
    existing_path = Path(existing["path"])
    if not existing_path.exists():
        return outcome

    sidecar = sidecars.get(entry.path)
    if sidecar:
        outcome = apply_sidecar(existing_path, sidecar, db,
                                f"takeout:{entry.path}")

    # File-to-file: the discarded duplicate may hold EXIF the kept copy lacks.
    # Only worth reading if something is still missing.
    if "date" not in outcome.merged or "gps" not in outcome.merged:
        if entry.is_large and entry.temp_path:
            _merge_from_source_file(existing_path, entry.temp_path, db,
                                    entry.path, outcome)
        elif entry.data:
            suffix = Path(entry.path).suffix
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                tmp.write(entry.data)
                tmp_path = Path(tmp.name)
            try:
                _merge_from_source_file(existing_path, tmp_path, db,
                                        entry.path, outcome)
            finally:
                try:
                    tmp_path.unlink()
                except OSError as exc:
                    logger.warning("could not remove temp file %s: %s",
                                   tmp_path, exc)

    if outcome.changed_file:
        # Metadata was written into the kept file, changing its bytes and size.
        _index_saved_file(db, existing_path)

    return outcome


def merge_metadata_between_files(target: Path, source: Path, db: Database
                                 ) -> Outcome:
    """Copy date/GPS the target lacks from another file, via the choke point.

    The public entry point for file-to-file merges (the `merge` command).
    Replaces `metadata.merge_metadata_from_file`, which wrote EXIF directly
    and discarded every failure.
    """
    outcome = Outcome()
    if not target.exists() or not source.exists():
        return outcome

    _merge_from_source_file(target, source, db, str(source), outcome)
    if outcome.changed_file:
        _index_saved_file(db, target)
    return outcome


def _merge_from_source_file(target: Path, source: Path, db: Database,
                            entry_path: str, outcome: Outcome):
    """Copy date/GPS the target lacks from a duplicate's own EXIF.

    Reuses the choke point by converting the source's EXIF into the same
    shape a sidecar produces, so a file-to-file merge is recorded exactly
    like a sidecar merge and cannot become a silent fourth path.
    """
    from datetime import datetime

    # The READ predicate, not the write one. Gating this on what piexif can
    # write meant every discarded HEIC duplicate was thrown away unread.
    if not can_read_exif(source):
        return

    offer: dict = {"utc_epoch": None, "lat": None, "lon": None}

    if "date" not in outcome.merged and "date" not in outcome.already_present:
        source_date = get_exif_date(source)
        if source_date:
            try:
                naive = datetime.fromisoformat(source_date)
            except ValueError:
                naive = None
            if naive is not None:
                # The source's DateTimeOriginal is already a local wall clock.
                # Recording it as a UTC epoch would re-shift it, so it is
                # applied directly rather than through the timezone tiers.
                _copy_exif_date(target, naive, source, db, entry_path, outcome)

    source_gps = get_exif_gps(source)
    if source_gps and "gps" not in outcome.merged and \
            "gps" not in outcome.already_present:
        offer["lat"], offer["lon"] = source_gps
        _apply_gps_to_exif(target, offer, db, f"file:{source}", outcome)


def _copy_exif_date(target: Path, local_dt, source: Path, db: Database,
                    entry_path: str, outcome: Outcome):
    """Carry a wall-clock date across, preserving the source's declared offset."""
    from memoryvault.metadata import get_exif_offset

    if get_exif_date(target):
        _record_already_present(db, target, "date",
                                {"datetime_local": local_dt.isoformat()},
                                "exif_date", f"file:{source}", outcome)
        return

    offset = get_exif_offset(source)
    payload = json.dumps({
        "datetime_local": local_dt.isoformat(),
        "offset": offset,
        "tz": None,
        "tz_source": "source_file",
    })

    try:
        write_exif_date(target, local_dt, offset)
    except UnparseableExifError:
        _record_pending_once(db, target, "date", payload, "failed_unparseable",
                             "failed", f"file:{source}")
        outcome.failed.append("date")
        return
    except Exception as exc:
        _record_pending_once(db, target, "date", payload,
                             f"failed_write_error:{type(exc).__name__}",
                             "failed", f"file:{source}")
        outcome.failed.append("date")
        return

    db.log_metadata_merge(str(target), f"file:{source}", "date", payload)
    outcome.merged.append("date")


def _sync_to_disk(path: Path):
    """fsync a saved file and its directory, so both its bytes and its name survive a crash."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _missing_copy_in(candidates: list[dict], dest_folder: Path) -> Path | None:
    """A row for this content whose vault file is gone, if there is one in dest_folder.

    Restoring to that path re-attaches the row to real bytes. Saving under a new
    name instead would leave the old row claiming a file nobody can read.
    """
    for c in candidates:
        p = Path(c["path"])
        if p.parent.resolve() == dest_folder.resolve() and not p.exists():
            return p
    return None


def _adoptable_copy(dest_folder: Path, filename: str, content_hash: str,
                    db: Database) -> Path | None:
    """An unrecorded file in dest_folder holding exactly these bytes, if one exists.

    Each entry's rows commit together (`db.batch()`), after its file is copied
    and fsynced. A crash in between leaves the copy on disk with no row, and
    the resumed run processes the entry again; without this it would save a
    second copy under the next free name and strand the first as an untracked
    extra. Walks the same name sequence as `_unique_dest_path` and stops at the
    first slot free on disk and in the index. A file with different bytes is
    never adopted; nor is a copy whose sidecar metadata was already written,
    which no longer matches and is left for the vault sweep to report.
    """
    stem, suffix = Path(filename).stem, Path(filename).suffix
    counter = 0
    while True:
        p = dest_folder / (filename if counter == 0 else f"{stem}({counter}){suffix}")
        if db.get_file_by_path(str(p)) is None:
            if not p.exists():
                return None
            if p.is_file() and hash_full(p) == content_hash:
                return p
        counter += 1


def _unique_dest_path(dest_folder: Path, filename: str, db: Database | None = None) -> Path:
    """Generate a unique destination path, adding (1), (2), etc. if needed.

    Given `db`, a name is also taken while a files row claims it. Checking the
    disk alone let a crash cost a photo: its directory entry was lost, the next
    file with the same name found the path free, and the ON CONFLICT(path)
    upsert overwrote the lost photo's row, dropping it from vault and index both.
    """
    def taken(p: Path) -> bool:
        return p.exists() or (db is not None and db.get_file_by_path(str(p)) is not None)

    dest = dest_folder / filename
    if not taken(dest):
        return dest

    stem = Path(filename).stem
    suffix = Path(filename).suffix
    counter = 1
    while True:
        dest = dest_folder / f"{stem}({counter}){suffix}"
        if not taken(dest):
            return dest
        counter += 1
