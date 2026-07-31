"""Takeout ingestion pipeline: stream from zip → hash → dedup → keep/skip."""

import json
from pathlib import Path

from memoryvault.archive import iter_entries, count_entries, extract_entry_to_path, ArchiveEntry
from memoryvault.database import Database
from memoryvault.hasher import hash_bytes, hash_full, hash_head, hash_tail
from memoryvault.metadata import (
    parse_takeout_sidecar, can_have_exif, has_metadata,
    get_exif_date, get_exif_gps,
    write_exif_date, write_exif_gps,
)
from memoryvault.volumes import assert_volumes_reachable

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
    p = path.lower()
    return p.endswith(".supplemental-metadata.json") or (
        p.endswith(".json") and not Path(path).stem.lower() == "metadata"
    )


def is_metadata_json(path: str) -> bool:
    return Path(path).name.lower() == "metadata.json"


def _media_name_for_sidecar(sidecar_path: str) -> str | None:
    """Given a sidecar JSON path, return the media file path it belongs to."""
    if sidecar_path.endswith(".supplemental-metadata.json"):
        return sidecar_path[:-len(".supplemental-metadata.json")]
    elif sidecar_path.endswith(".json"):
        return sidecar_path[:-len(".json")]
    return None


def ingest_archive(archive_path: Path, dest_folder: Path, db: Database,
                   progress_callback=None, title: str = None,
                   allow_unreachable_volumes: bool = False) -> dict:
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

    stats = {"kept": 0, "skipped": 0, "errors": 0, "merged_metadata": 0}

    # Sidecars are small (just JSON text) — safe to buffer
    sidecars: dict[str, dict] = {}
    # Track where media files were saved so late-arriving sidecars can be applied
    kept_media: dict[str, str] = {}  # archive_path -> dest_path

    processed_count = 0

    if progress_callback:
        progress_callback("start", processed=0, total=0)

    for entry in iter_entries(archive_path, skip_entries=already_processed):
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
            # Parse and store sidecar — these are tiny (< 1KB each)
            try:
                raw = entry.read_data()
                if not raw:
                    continue
                sidecar_data = json.loads(raw.decode("utf-8"))
                media_name = _media_name_for_sidecar(entry.path)
                if media_name:
                    parsed = _parse_sidecar_data(sidecar_data)
                    sidecars[media_name] = parsed

                    # Check if we already saved this media file (sidecar arrived late)
                    if media_name in kept_media:
                        merged = _apply_sidecar_to_file(
                            kept_media[media_name], parsed, entry.path, db)
                        if merged:
                            stats["merged_metadata"] += len(merged)
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
            db.log_archive_entry(archive_id, entry.path, "skipped", skip_reason="sidecar")
            stats["skipped"] += 1
            processed_count += 1
            continue

        if not is_media_file(entry.path):
            db.log_archive_entry(archive_id, entry.path, "skipped", skip_reason="not_media")
            stats["skipped"] += 1
            processed_count += 1
            continue

        # Process media file immediately — no buffering
        try:
            result = _process_media_entry(entry, dest_folder, db, sidecars, archive_id)
        except Exception as e:
            db.log_archive_entry(archive_id, entry.path, "error", skip_reason=str(e)[:200])
            stats["errors"] += 1
            processed_count += 1
            continue
        stats[result["action"]] += 1
        if result.get("merged"):
            stats["merged_metadata"] += len(result["merged"])
        if result.get("dest_path"):
            kept_media[entry.path] = result["dest_path"]

        processed_count += 1

        if progress_callback and processed_count % 10 == 0:
            progress_callback("progress", processed=processed_count, total=processed_count,
                              action=result["action"], path=entry.path,
                              kept=stats["kept"], skipped=stats["skipped"],
                              errors=stats["errors"], merged=stats["merged_metadata"])

    # Mark archive complete
    db.update_archive_status(archive_id, "complete", entries_processed=processed_count)

    # Verification: compute sizes
    kept_size = 0
    skipped_dupe_count = 0
    for row in db.conn.execute(
        "SELECT status, skip_reason, kept_path FROM archive_entries WHERE archive_id = ?",
        (archive_id,)
    ).fetchall():
        if row["status"] == "kept" and row["kept_path"]:
            try:
                kept_size += Path(row["kept_path"]).stat().st_size
            except OSError:
                pass
        if row["skip_reason"] == "duplicate":
            skipped_dupe_count += 1

    stats["processed_count"] = processed_count
    stats["kept_size_bytes"] = kept_size
    stats["skipped_duplicate_count"] = skipped_dupe_count
    stats["sidecar_count"] = stats["skipped"] - skipped_dupe_count
    stats["verified"] = (stats["kept"] + stats["skipped"] + stats["errors"] == processed_count)

    if progress_callback:
        progress_callback("done", stats=stats)

    return stats


def _parse_sidecar_data(data: dict) -> dict:
    """Extract date and GPS from raw sidecar JSON data."""
    from datetime import datetime, timezone

    result = {"date": None, "lat": None, "lon": None}

    for time_key in ("photoTakenTime", "creationTime"):
        time_data = data.get(time_key)
        if time_data and "timestamp" in time_data:
            try:
                ts = int(time_data["timestamp"])
                if ts > 0:
                    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                    result["date"] = dt.isoformat()
                    break
            except (ValueError, OSError):
                continue

    for geo_key in ("geoData", "geoDataExif"):
        geo = data.get(geo_key)
        if geo:
            lat = geo.get("latitude", 0)
            lon = geo.get("longitude", 0)
            if lat != 0 or lon != 0:
                result["lat"] = lat
                result["lon"] = lon
                break

    return result


def _apply_sidecar_to_file(dest_path: str, sidecar: dict, source_desc: str,
                           db: Database) -> list[str]:
    """Apply sidecar metadata to an already-saved file (for late-arriving sidecars)."""
    merged = []
    path = Path(dest_path)
    if not path.exists() or not can_have_exif(path):
        return merged

    if sidecar.get("date") and not get_exif_date(path):
        try:
            write_exif_date(path, sidecar["date"])
            db.log_metadata_merge(dest_path, f"takeout:{source_desc}", "date", sidecar["date"])
            merged.append("date")
        except Exception:
            pass

    if sidecar.get("lat") is not None and not get_exif_gps(path):
        try:
            write_exif_gps(path, sidecar["lat"], sidecar["lon"])
            db.log_metadata_merge(dest_path, f"takeout:{source_desc}", "gps",
                                  f"{sidecar['lat']},{sidecar['lon']}")
            merged.append("gps")
        except Exception:
            pass

    if merged:
        # The file's bytes just changed; its row must follow.
        _index_saved_file(db, path)

    return merged


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
    size = entry.size

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
        merged = _try_merge_from_duplicate(entry, surviving[0], sidecars, db)
        db.log_archive_entry(archive_id, entry.path, "skipped", skip_reason="duplicate")
        return {"action": "skipped", "merged": merged}

    # Matched the index but every copy is gone — treat as new and record why,
    # so the decision is queryable rather than silent.
    keep_reason = "prior_copy_missing" if candidates else None

    # NOTE: the old same-size/head-hash probe that ran here was removed with
    # finding #11. It ended by comparing blake3_full to the incoming hash, which
    # find_files_by_content_hash already covers, so it could not match anything
    # new — and its head-hash comparison read a column that drifts once EXIF is
    # written into a saved file.

    # New file — save it
    filename = Path(entry.path).name
    dest_path = _unique_dest_path(dest_folder, filename)

    try:
        extract_entry_to_path(entry, dest_path)
    except OSError:
        db.log_archive_entry(archive_id, entry.path, "error", skip_reason="write_failed")
        return {"action": "errors"}

    # Apply sidecar metadata if available (sidecar arrived before media file)
    merged = []
    sidecar = sidecars.get(entry.path)
    if sidecar and can_have_exif(dest_path):
        if sidecar.get("date") and not get_exif_date(dest_path):
            try:
                write_exif_date(dest_path, sidecar["date"])
                db.log_metadata_merge(str(dest_path), f"takeout:{entry.path}", "date",
                                      sidecar["date"])
                merged.append("date")
            except Exception:
                pass

        if sidecar.get("lat") is not None and not get_exif_gps(dest_path):
            try:
                write_exif_gps(dest_path, sidecar["lat"], sidecar["lon"])
                db.log_metadata_merge(str(dest_path), f"takeout:{entry.path}", "gps",
                                      f"{sidecar['lat']},{sidecar['lon']}")
                merged.append("gps")
            except Exception:
                pass

    # Index the file as it now stands on disk. Metadata writes above changed
    # both its size and its bytes, so every hash must be taken after them.
    _index_saved_file(db, dest_path, source_hash=source_hash, source="takeout")

    db.log_archive_entry(archive_id, entry.path, "kept", kept_path=str(dest_path),
                         skip_reason=keep_reason)
    return {"action": "kept", "merged": merged, "dest_path": str(dest_path)}


def _index_saved_file(db: Database, path: Path, source_hash: str | None = None,
                      source: str | None = None):
    """Record a saved file's hashes from its current on-disk bytes.

    Called after every write that can change the file, so `blake3_full`,
    `blake3_head`, `blake3_tail` and `size` always describe what is actually
    on disk. `source_blake3` is only written when supplied, so refreshing an
    existing row preserves the provenance recorded at ingest time.
    """
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
                              sidecars: dict, db: Database) -> list[str]:
    """Try to merge metadata from a skipped duplicate into the kept file.

    Checks two sources:
    1. The Takeout JSON sidecar (if available)
    2. The duplicate file's own EXIF data (file-to-file merge)
    """
    from memoryvault.metadata import merge_metadata_from_file
    import tempfile

    merged = []
    existing_path = Path(existing["path"])
    if not existing_path.exists():
        return merged

    # First: try sidecar metadata
    sidecar = sidecars.get(entry.path)
    if sidecar and can_have_exif(existing_path):
        if sidecar.get("date") and not existing.get("has_exif_date"):
            try:
                write_exif_date(existing_path, sidecar["date"])
                db.log_metadata_merge(str(existing_path), f"takeout:{entry.path}", "date",
                                      sidecar["date"])
                merged.append("date")
            except Exception:
                pass

        if sidecar.get("lat") is not None and not existing.get("has_exif_gps"):
            try:
                write_exif_gps(existing_path, sidecar["lat"], sidecar["lon"])
                db.log_metadata_merge(str(existing_path), f"takeout:{entry.path}", "gps",
                                      f"{sidecar['lat']},{sidecar['lon']}")
                merged.append("gps")
            except Exception:
                pass

    # Second: try file-to-file merge (duplicate may have EXIF the kept file lacks)
    if can_have_exif(existing_path) and ("date" not in merged or "gps" not in merged):
        # Need to write the duplicate to a temp file to read its EXIF
        if entry.is_large and entry.temp_path:
            source_path = entry.temp_path
            file_merged = merge_metadata_from_file(existing_path, source_path, db=db)
            merged.extend(file_merged)
        elif entry.data:
            suffix = Path(entry.path).suffix
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                tmp.write(entry.data)
                tmp_path = Path(tmp.name)
            try:
                file_merged = merge_metadata_from_file(existing_path, tmp_path, db=db)
                merged.extend(file_merged)
            finally:
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

    if merged:
        # Metadata was written into the kept file, changing its bytes and size.
        _index_saved_file(db, existing_path)

    return merged


def _unique_dest_path(dest_folder: Path, filename: str) -> Path:
    """Generate a unique destination path, adding (1), (2), etc. if needed."""
    dest = dest_folder / filename
    if not dest.exists():
        return dest

    stem = Path(filename).stem
    suffix = Path(filename).suffix
    counter = 1
    while True:
        dest = dest_folder / f"{stem}({counter}){suffix}"
        if not dest.exists():
            return dest
        counter += 1
