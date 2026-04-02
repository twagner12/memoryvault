"""Scan local folders and populate the hash database."""

from pathlib import Path

from memoryvault.database import Database
from memoryvault.hasher import hash_file_progressive
from memoryvault.metadata import has_metadata


BATCH_SIZE = 500


def iter_files(folder: Path) -> list[Path]:
    """Recursively yield all files in a folder."""
    return [p for p in folder.rglob("*") if p.is_file()]


def scan_folder(folder: Path, db: Database, source: str = "local",
                progress_callback=None) -> int:
    """Scan a folder, hash all files progressively, and store in the database.

    Returns the number of files scanned.
    """
    folder = folder.resolve()
    files = iter_files(folder)
    total = len(files)

    if progress_callback:
        progress_callback("start", total, 0)

    batch = []
    scanned = 0

    for i, path in enumerate(files):
        try:
            info = hash_file_progressive(path)
            info["source"] = source
            meta = has_metadata(path)
            info["has_exif_date"] = meta["has_exif_date"]
            info["has_exif_gps"] = meta["has_exif_gps"]
            batch.append(info)
        except OSError as e:
            if progress_callback:
                progress_callback("error", total, scanned, str(e))
            continue

        if len(batch) >= BATCH_SIZE:
            db.bulk_upsert_files(batch)
            batch.clear()

        scanned += 1
        if progress_callback and scanned % 100 == 0:
            progress_callback("progress", total, scanned)

    if batch:
        db.bulk_upsert_files(batch)

    if progress_callback:
        progress_callback("done", total, scanned)

    return scanned
