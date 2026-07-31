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

    Files whose size and mtime match their existing row are skipped without
    being re-hashed; the progress callback reports how many via a
    "skipped_unchanged" stage.

    Returns the number of files scanned, including skipped ones.
    """
    folder = folder.resolve()

    if progress_callback:
        progress_callback("listing", 0, 0)

    files = iter_files(folder)
    total = len(files)

    if progress_callback:
        progress_callback("start", total, 0)

    # Files whose size and mtime still match their row have not changed, so
    # re-reading them would produce the hashes we already hold. Loaded once
    # rather than queried per file.
    known = db.get_stat_index(str(folder))

    batch = []
    scanned = 0
    skipped_unchanged = 0

    for i, path in enumerate(files):
        try:
            stat = path.stat()
            previous = known.get(str(path))
            if previous is not None and previous == (stat.st_size, stat.st_mtime):
                skipped_unchanged += 1
                scanned += 1
                if progress_callback and scanned % 100 == 0:
                    progress_callback("progress", total, scanned)
                continue

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
        # Emitted as its own stage so the count of short-circuited files is
        # visible rather than hidden inside the scanned total.
        progress_callback("skipped_unchanged", total, skipped_unchanged)
        progress_callback("done", total, scanned)

    return scanned
