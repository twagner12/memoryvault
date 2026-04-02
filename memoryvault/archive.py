"""Stream files from zip archives using libarchive, with 7z CLI fallback."""

import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import libarchive

# Files larger than this are written to a temp file instead of memory
LARGE_FILE_THRESHOLD = 50 * 1024 * 1024  # 50 MB


@dataclass
class ArchiveEntry:
    """A single file entry from an archive."""
    path: str                  # path within the archive
    size: int                  # uncompressed size
    data: bytes | None = None  # file contents for small files
    temp_path: Path | None = None  # temp file path for large files

    @property
    def is_large(self) -> bool:
        return self.temp_path is not None

    def read_data(self) -> bytes | None:
        """Read the entry data, whether from memory or temp file."""
        if self.data is not None:
            return self.data
        if self.temp_path and self.temp_path.exists():
            return self.temp_path.read_bytes()
        return None

    def cleanup(self):
        """Remove temp file if one was created."""
        if self.temp_path and self.temp_path.exists():
            try:
                self.temp_path.unlink()
            except OSError:
                pass


def stream_entries(archive_path: Path, skip_entries: set[str] = None) -> list[ArchiveEntry]:
    """Read all file entries from an archive using libarchive."""
    skip_entries = skip_entries or set()
    try:
        return list(_stream_libarchive(archive_path, skip_entries))
    except Exception:
        return list(_stream_7z(archive_path, skip_entries))


def iter_entries(archive_path: Path, skip_entries: set[str] = None):
    """Iterate over archive entries one at a time (generator).

    Small files (< 50 MB) are loaded into memory.
    Large files are extracted to a temp file — caller must call entry.cleanup().
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
    """Stream entries using libarchive-c.

    Small files are read into memory. Large files are written to temp files
    to avoid memory pressure.
    """
    with libarchive.file_reader(str(archive_path)) as archive:
        for entry in archive:
            if entry.isdir:
                continue
            if entry.pathname in skip_entries:
                # Must still consume the blocks to advance the archive
                for _ in entry.get_blocks():
                    pass
                continue

            # Estimate size from entry header (may be 0 for some formats)
            estimated_size = entry.size if hasattr(entry, 'size') and entry.size else 0

            if estimated_size > LARGE_FILE_THRESHOLD:
                # Large file: write to temp file
                yield _read_to_temp(entry)
            else:
                # Small file or unknown size: read into memory, spill to temp if too large
                data = b""
                for block in entry.get_blocks():
                    data += block
                    if len(data) > LARGE_FILE_THRESHOLD:
                        # Exceeded threshold mid-read, spill to temp
                        yield _spill_to_temp(entry.pathname, data, entry)
                        data = None
                        break

                if data is not None:
                    yield ArchiveEntry(
                        path=entry.pathname,
                        size=len(data),
                        data=data,
                    )


def _read_to_temp(entry) -> ArchiveEntry:
    """Read an archive entry directly to a temp file."""
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=Path(entry.pathname).suffix)
    size = 0
    try:
        for block in entry.get_blocks():
            tmp.write(block)
            size += len(block)
        tmp.close()
        return ArchiveEntry(
            path=entry.pathname,
            size=size,
            temp_path=Path(tmp.name),
        )
    except Exception:
        tmp.close()
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        return ArchiveEntry(path=entry.pathname, size=0, data=None)


def _spill_to_temp(pathname: str, initial_data: bytes, entry) -> ArchiveEntry:
    """Spill an in-progress read to a temp file."""
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=Path(pathname).suffix)
    size = len(initial_data)
    try:
        tmp.write(initial_data)
        for block in entry.get_blocks():
            tmp.write(block)
            size += len(block)
        tmp.close()
        return ArchiveEntry(
            path=pathname,
            size=size,
            temp_path=Path(tmp.name),
        )
    except Exception:
        tmp.close()
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        return ArchiveEntry(path=pathname, size=0, data=None)


def _stream_7z(archive_path: Path, skip_entries: set[str]):
    """Stream entries using 7z CLI as fallback."""
    entries = list_entries(archive_path)

    for entry_path in entries:
        if entry_path in skip_entries:
            continue

        with tempfile.TemporaryDirectory() as tmpdir:
            result = subprocess.run(
                ["7z", "e", "-y", f"-o{tmpdir}", str(archive_path), entry_path],
                capture_output=True, text=True, timeout=300,
            )
            if result.returncode != 0:
                yield ArchiveEntry(path=entry_path, size=0, data=None)
                continue

            extracted = Path(tmpdir) / Path(entry_path).name
            if extracted.exists():
                size = extracted.stat().st_size
                if size > LARGE_FILE_THRESHOLD:
                    # Move to a persistent temp location
                    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=extracted.suffix)
                    tmp.close()
                    extracted.rename(tmp.name)
                    yield ArchiveEntry(path=entry_path, size=size, temp_path=Path(tmp.name))
                else:
                    data = extracted.read_bytes()
                    yield ArchiveEntry(path=entry_path, size=len(data), data=data)
            else:
                yield ArchiveEntry(path=entry_path, size=0, data=None)


def extract_entry_to_path(entry: ArchiveEntry, dest: Path):
    """Write an archive entry's data to a destination path."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if entry.temp_path and entry.temp_path.exists():
        # Move temp file to destination (fast, no copy needed)
        import shutil
        shutil.move(str(entry.temp_path), str(dest))
    elif entry.data is not None:
        dest.write_bytes(entry.data)
    else:
        raise OSError(f"No data available for {entry.path}")
