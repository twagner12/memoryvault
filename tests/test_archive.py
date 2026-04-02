"""Tests for memoryvault.archive."""

import zipfile
from pathlib import Path

from memoryvault.archive import (
    iter_entries, list_entries, count_entries, extract_entry_to_path,
)


def _make_zip(path: Path, files: dict[str, bytes]) -> Path:
    """Create a zip file with the given files. files = {name: data}."""
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return path


class TestListEntries:
    def test_list_files(self, tmp_path):
        zp = _make_zip(tmp_path / "test.zip", {
            "photos/a.jpg": b"photo A",
            "photos/b.jpg": b"photo B",
            "photos/c.mp4": b"video C",
        })
        entries = list_entries(zp)
        assert len(entries) == 3
        assert "photos/a.jpg" in entries

    def test_count_entries(self, tmp_path):
        zp = _make_zip(tmp_path / "test.zip", {
            "a.jpg": b"a",
            "b.jpg": b"b",
        })
        assert count_entries(zp) == 2


class TestIterEntries:
    def test_reads_all_entries(self, tmp_path):
        zp = _make_zip(tmp_path / "test.zip", {
            "a.jpg": b"photo data",
            "b.jpg": b"other photo",
        })
        entries = list(iter_entries(zp))
        assert len(entries) == 2
        assert entries[0].data is not None
        assert entries[0].size > 0

    def test_skip_entries(self, tmp_path):
        zp = _make_zip(tmp_path / "test.zip", {
            "a.jpg": b"photo A",
            "b.jpg": b"photo B",
            "c.jpg": b"photo C",
        })
        entries = list(iter_entries(zp, skip_entries={"a.jpg", "c.jpg"}))
        assert len(entries) == 1
        assert entries[0].path == "b.jpg"

    def test_entry_data_matches(self, tmp_path):
        content = b"exact content to verify"
        zp = _make_zip(tmp_path / "test.zip", {"file.txt": content})
        entries = list(iter_entries(zp))
        assert entries[0].data == content


class TestExtractEntry:
    def test_extract_to_path(self, tmp_path):
        zp = _make_zip(tmp_path / "test.zip", {"photo.jpg": b"image data"})
        entries = list(iter_entries(zp))
        dest = tmp_path / "output" / "photo.jpg"
        extract_entry_to_path(entries[0], dest)
        assert dest.exists()
        assert dest.read_bytes() == b"image data"

    def test_creates_parent_dirs(self, tmp_path):
        zp = _make_zip(tmp_path / "test.zip", {"deep/nested/photo.jpg": b"data"})
        entries = list(iter_entries(zp))
        dest = tmp_path / "out" / "sub" / "photo.jpg"
        extract_entry_to_path(entries[0], dest)
        assert dest.exists()
