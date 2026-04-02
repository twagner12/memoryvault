"""Takeout ingestion pipeline: stream from zip → hash → dedup → keep/skip."""

import json
from pathlib import Path

from memoryvault.archive import iter_entries, count_entries, extract_entry_to_path, ArchiveEntry
from memoryvault.database import Database
from memoryvault.hasher import hash_bytes, hash_full
from memoryvault.metadata import (
    parse_takeout_sidecar, can_have_exif, has_metadata,
    get_exif_date, get_exif_gps,
    write_exif_date, write_exif_gps,
)

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


def ingest_archive(archive_path: Path, dest_folder: Path, db: Database,
                   progress_callback=None) -> dict:
    """Ingest a Takeout zip: stream files, dedup against DB, keep unique files.

    Args:
        archive_path: Path to the zip file.
        dest_folder: Where to save unique files.
        db: Database instance.
        progress_callback: Optional callback(stage, **kwargs).

    Returns dict with stats: kept, skipped, errors, merged_metadata.
    """
    archive_path = archive_path.resolve()
    dest_folder = dest_folder.resolve()
    dest_folder.mkdir(parents=True, exist_ok=True)

    # Register archive and check for resume
    entry_count = count_entries(archive_path)
    archive_hash = None  # Skip hashing the zip itself for speed
    archive_id = db.register_archive(str(archive_path), blake3=archive_hash,
                                     entries_total=entry_count)

    archive_record = db.get_archive(str(archive_path))
    if archive_record["status"] == "complete":
        if progress_callback:
            progress_callback("already_done", archive=str(archive_path))
        return {"kept": 0, "skipped": 0, "errors": 0, "merged_metadata": 0}

    db.update_archive_status(archive_id, "in_progress")
    already_processed = db.get_processed_entries(archive_id)

    stats = {"kept": 0, "skipped": 0, "errors": 0, "merged_metadata": 0}

    # Pass 1: Collect all sidecar JSON data and buffer media entries
    # We need sidecars parsed before processing media files because
    # the sidecar may appear after its media file in the zip
    sidecars: dict[str, dict] = {}
    media_entries: list[ArchiveEntry] = []

    for entry in iter_entries(archive_path, skip_entries=already_processed):
        if entry.data is None:
            db.log_archive_entry(archive_id, entry.path, "error", skip_reason="corrupt")
            stats["errors"] += 1
            continue

        if is_metadata_json(entry.path):
            db.log_archive_entry(archive_id, entry.path, "skipped", skip_reason="album_metadata")
            stats["skipped"] += 1
            continue

        if is_sidecar_json(entry.path):
            try:
                sidecar_data = json.loads(entry.data.decode("utf-8"))
                name = entry.path
                if name.endswith(".supplemental-metadata.json"):
                    media_name = name[:-len(".supplemental-metadata.json")]
                elif name.endswith(".json"):
                    media_name = name[:-len(".json")]
                else:
                    media_name = None

                if media_name:
                    parsed = _parse_sidecar_data(sidecar_data)
                    sidecars[media_name] = parsed
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
            db.log_archive_entry(archive_id, entry.path, "skipped", skip_reason="sidecar")
            stats["skipped"] += 1
            continue

        if not is_media_file(entry.path):
            db.log_archive_entry(archive_id, entry.path, "skipped", skip_reason="not_media")
            stats["skipped"] += 1
            continue

        # Buffer media entries for pass 2
        media_entries.append(entry)

    # Pass 2: Process media entries with all sidecars available
    for entry in media_entries:
        result = _process_media_entry(entry, dest_folder, db, sidecars, archive_id)
        stats[result["action"]] += 1
        if result.get("merged"):
            stats["merged_metadata"] += len(result["merged"])

        if progress_callback:
            processed = stats["kept"] + stats["skipped"] + stats["errors"]
            progress_callback("progress", processed=processed, total=entry_count,
                              action=result["action"], path=entry.path)

    # Mark archive complete
    total_processed = stats["kept"] + stats["skipped"] + stats["errors"]
    db.update_archive_status(archive_id, "complete", entries_processed=total_processed)

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


def _process_media_entry(entry: ArchiveEntry, dest_folder: Path, db: Database,
                         sidecars: dict, archive_id: int) -> dict:
    """Process a single media file from the archive.

    Returns dict with 'action' ('kept', 'skipped', or 'errors') and optional 'merged' list.
    """
    # Hash the file data
    full_hash = hash_bytes(entry.data)
    size = len(entry.data)

    # Check if we already have this exact file
    existing = db.get_files_by_full_hash(full_hash)
    if existing:
        # Duplicate — skip it, but check if we can merge metadata
        merged = _try_merge_sidecar_to_existing(entry.path, existing[0], sidecars, db)
        db.log_archive_entry(archive_id, entry.path, "skipped", skip_reason="duplicate")
        return {"action": "skipped", "merged": merged}

    # Progressive check: same size files
    same_size = db.get_files_by_size(size)
    if same_size:
        # Hash head for comparison
        head_data = entry.data[:65_536]
        head_hash = hash_bytes(head_data)

        for existing_file in same_size:
            if existing_file.get("blake3_head") == head_hash:
                # Very likely a duplicate, check full hash
                if existing_file.get("blake3_full") == full_hash:
                    merged = _try_merge_sidecar_to_existing(
                        entry.path, existing_file, sidecars, db)
                    db.log_archive_entry(archive_id, entry.path, "skipped",
                                         skip_reason="duplicate")
                    return {"action": "skipped", "merged": merged}

    # New file — save it
    filename = Path(entry.path).name
    dest_path = _unique_dest_path(dest_folder, filename)

    try:
        extract_entry_to_path(entry, dest_path)
    except OSError:
        db.log_archive_entry(archive_id, entry.path, "error", skip_reason="write_failed")
        return {"action": "errors"}

    # Apply sidecar metadata if available
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

    # Compute head/tail hashes for DB
    head_hash = hash_bytes(entry.data[:65_536])
    tail_hash = hash_bytes(entry.data[-4_096:]) if size > 4_096 else head_hash

    # Check metadata on the saved file
    meta = has_metadata(dest_path) if can_have_exif(dest_path) else {
        "has_exif_date": False, "has_exif_gps": False
    }

    # Add to database
    db.upsert_file(
        path=str(dest_path),
        size=size,
        blake3_full=full_hash,
        blake3_head=head_hash,
        blake3_tail=tail_hash,
        mtime=dest_path.stat().st_mtime,
        has_exif_date=meta["has_exif_date"],
        has_exif_gps=meta["has_exif_gps"],
        source="takeout",
    )

    db.log_archive_entry(archive_id, entry.path, "kept", kept_path=str(dest_path))
    return {"action": "kept", "merged": merged}


def _try_merge_sidecar_to_existing(entry_path: str, existing: dict,
                                    sidecars: dict, db: Database) -> list[str]:
    """Try to merge sidecar metadata into an existing file that was kept."""
    merged = []
    sidecar = sidecars.get(entry_path)
    if not sidecar:
        return merged

    existing_path = Path(existing["path"])
    if not existing_path.exists() or not can_have_exif(existing_path):
        return merged

    if sidecar.get("date") and not existing.get("has_exif_date"):
        try:
            write_exif_date(existing_path, sidecar["date"])
            db.log_metadata_merge(str(existing_path), f"takeout:{entry_path}", "date",
                                  sidecar["date"])
            merged.append("date")
        except Exception:
            pass

    if sidecar.get("lat") is not None and not existing.get("has_exif_gps"):
        try:
            write_exif_gps(existing_path, sidecar["lat"], sidecar["lon"])
            db.log_metadata_merge(str(existing_path), f"takeout:{entry_path}", "gps",
                                  f"{sidecar['lat']},{sidecar['lon']}")
            merged.append("gps")
        except Exception:
            pass

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
