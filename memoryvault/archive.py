"""Stream files from zip archives using libarchive, with 7z CLI fallback."""

import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import libarchive


@dataclass
class ArchiveEntry:
    """A single file entry from an archive."""
    path: str           # path within the archive
    size: int           # uncompressed size
    data: bytes | None  # file contents (None if not yet read)


def stream_entries(archive_path: Path, skip_entries: set[str] = None) -> list[ArchiveEntry]:
    """Read all file entries from an archive using libarchive.

    Args:
        archive_path: Path to the zip/7z file.
        skip_entries: Set of entry paths to skip (for resume support).

    Yields ArchiveEntry objects with data loaded.
    Falls back to 7z CLI if libarchive fails.
    """
    skip_entries = skip_entries or set()
    try:
        return list(_stream_libarchive(archive_path, skip_entries))
    except Exception as e:
        # Fallback to 7z CLI for corrupt archives
        return list(_stream_7z(archive_path, skip_entries))


def iter_entries(archive_path: Path, skip_entries: set[str] = None):
    """Iterate over archive entries one at a time (generator).

    More memory-efficient than stream_entries for large archives.
    Falls back to 7z CLI if libarchive fails.
    """
    skip_entries = skip_entries or set()
    try:
        yield from _stream_libarchive(archive_path, skip_entries)
    except Exception:
        yield from _stream_7z(archive_path, skip_entries)


def list_entries(archive_path: Path) -> list[str]:
    """List all file entry paths in an archive without extracting."""
    entries = []
    try:
        with libarchive.file_reader(str(archive_path)) as archive:
            for entry in archive:
                if not entry.isdir:
                    entries.append(entry.pathname)
    except Exception:
        # Fallback to 7z
        result = subprocess.run(
            ["7z", "l", "-slt", str(archive_path)],
            capture_output=True, text=True, timeout=300,
        )
        for line in result.stdout.splitlines():
            if line.startswith("Path = ") and not line.endswith(str(archive_path)):
                entries.append(line[7:])
    return entries


def count_entries(archive_path: Path) -> int:
    """Count the number of file entries in an archive."""
    return len(list_entries(archive_path))


def _stream_libarchive(archive_path: Path, skip_entries: set[str]):
    """Stream entries using libarchive-c."""
    with libarchive.file_reader(str(archive_path)) as archive:
        for entry in archive:
            if entry.isdir:
                continue
            if entry.pathname in skip_entries:
                continue

            # Read the entry data
            data = b""
            for block in entry.get_blocks():
                data += block

            yield ArchiveEntry(
                path=entry.pathname,
                size=len(data),
                data=data,
            )


def _stream_7z(archive_path: Path, skip_entries: set[str]):
    """Stream entries using 7z CLI as fallback.

    Extracts files one at a time to a temp directory.
    """
    # First get the list of files
    entries = list_entries(archive_path)

    for entry_path in entries:
        if entry_path in skip_entries:
            continue

        with tempfile.TemporaryDirectory() as tmpdir:
            result = subprocess.run(
                ["7z", "e", "-y", f"-o{tmpdir}", str(archive_path), entry_path],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode != 0:
                yield ArchiveEntry(path=entry_path, size=0, data=None)
                continue

            # Find the extracted file
            extracted = Path(tmpdir) / Path(entry_path).name
            if extracted.exists():
                data = extracted.read_bytes()
                yield ArchiveEntry(
                    path=entry_path,
                    size=len(data),
                    data=data,
                )
            else:
                yield ArchiveEntry(path=entry_path, size=0, data=None)


def extract_entry_to_path(entry: ArchiveEntry, dest: Path):
    """Write an archive entry's data to a destination path."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(entry.data)
