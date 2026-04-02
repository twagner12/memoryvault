"""Tests for memoryvault.hasher."""

import tempfile
from pathlib import Path

from memoryvault.hasher import (
    hash_bytes, hash_head, hash_tail, hash_full,
    hash_file_progressive, find_exact_duplicates,
    HEAD_SIZE,
)


def _write_file(dir_path: Path, name: str, content: bytes) -> Path:
    p = dir_path / name
    p.write_bytes(content)
    return p


class TestHashFunctions:
    def test_hash_bytes_deterministic(self):
        assert hash_bytes(b"hello") == hash_bytes(b"hello")

    def test_hash_bytes_different(self):
        assert hash_bytes(b"hello") != hash_bytes(b"world")

    def test_hash_head_small_file(self, tmp_path):
        p = _write_file(tmp_path, "small.txt", b"hello world")
        head = hash_head(p)
        full = hash_full(p)
        # For files smaller than HEAD_SIZE, head hash reads the whole file
        assert head == full

    def test_hash_head_large_file(self, tmp_path):
        content = b"A" * (HEAD_SIZE + 1000)
        p = _write_file(tmp_path, "large.bin", content)
        head = hash_head(p)
        full = hash_full(p)
        # Head hash should differ from full hash for large files
        assert head != full

    def test_hash_tail(self, tmp_path):
        content = b"A" * 10000 + b"TAIL"
        p = _write_file(tmp_path, "file.bin", content)
        h = hash_tail(p)
        assert isinstance(h, str)
        assert len(h) == 64  # BLAKE3 hex digest length

    def test_hash_full_streaming(self, tmp_path):
        content = b"x" * 2_000_000  # 2 MB
        p = _write_file(tmp_path, "big.bin", content)
        h = hash_full(p)
        assert h == hash_bytes(content)


class TestProgressiveHashing:
    def test_small_file_all_hashes_equal(self, tmp_path):
        p = _write_file(tmp_path, "tiny.txt", b"tiny")
        result = hash_file_progressive(p)
        assert result["blake3_head"] == result["blake3_full"]
        assert result["blake3_tail"] == result["blake3_full"]
        assert result["size"] == 4

    def test_large_file_has_all_fields(self, tmp_path):
        content = b"X" * (HEAD_SIZE + 5000)
        p = _write_file(tmp_path, "large.bin", content)
        result = hash_file_progressive(p)
        assert "blake3_head" in result
        assert "blake3_tail" in result
        assert "blake3_full" in result
        assert result["size"] == len(content)
        assert "mtime" in result


class TestFindExactDuplicates:
    def test_no_duplicates(self, tmp_path):
        _write_file(tmp_path, "a.txt", b"aaa")
        _write_file(tmp_path, "b.txt", b"bbb")
        _write_file(tmp_path, "c.txt", b"ccc")
        paths = list(tmp_path.iterdir())
        groups = find_exact_duplicates(paths)
        assert groups == []

    def test_finds_exact_duplicates(self, tmp_path):
        _write_file(tmp_path, "a.txt", b"same content here")
        _write_file(tmp_path, "b.txt", b"same content here")
        _write_file(tmp_path, "c.txt", b"different")
        paths = list(tmp_path.iterdir())
        groups = find_exact_duplicates(paths)
        assert len(groups) == 1
        assert len(groups[0]) == 2

    def test_multiple_duplicate_groups(self, tmp_path):
        _write_file(tmp_path, "a1.txt", b"group A")
        _write_file(tmp_path, "a2.txt", b"group A")
        _write_file(tmp_path, "b1.txt", b"group B content")
        _write_file(tmp_path, "b2.txt", b"group B content")
        _write_file(tmp_path, "unique.txt", b"just me")
        paths = list(tmp_path.iterdir())
        groups = find_exact_duplicates(paths)
        assert len(groups) == 2

    def test_skips_empty_files(self, tmp_path):
        _write_file(tmp_path, "empty1.txt", b"")
        _write_file(tmp_path, "empty2.txt", b"")
        paths = list(tmp_path.iterdir())
        groups = find_exact_duplicates(paths)
        assert groups == []

    def test_same_size_different_content(self, tmp_path):
        _write_file(tmp_path, "a.txt", b"aaaa")
        _write_file(tmp_path, "b.txt", b"bbbb")
        paths = list(tmp_path.iterdir())
        groups = find_exact_duplicates(paths)
        assert groups == []

    def test_progress_callback(self, tmp_path):
        _write_file(tmp_path, "a.txt", b"same")
        _write_file(tmp_path, "b.txt", b"same")
        calls = []
        def cb(stage, before, after, *args):
            calls.append(stage)
        paths = list(tmp_path.iterdir())
        find_exact_duplicates(paths, progress_callback=cb)
        assert "size_filter" in calls
        assert "full_hash" in calls
