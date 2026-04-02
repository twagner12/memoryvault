"""BLAKE3 hashing with progressive early-exit filtering for fast deduplication."""

from pathlib import Path

import blake3

HEAD_SIZE = 65_536  # 64 KB
TAIL_SIZE = 4_096   # 4 KB


def hash_bytes(data: bytes) -> str:
    """Hash raw bytes with BLAKE3, return hex digest."""
    return blake3.blake3(data).hexdigest()


def hash_head(path: Path) -> str:
    """Hash the first 64 KB of a file."""
    with open(path, "rb") as f:
        return hash_bytes(f.read(HEAD_SIZE))


def hash_tail(path: Path) -> str:
    """Hash the last 4 KB of a file."""
    size = path.stat().st_size
    with open(path, "rb") as f:
        if size > TAIL_SIZE:
            f.seek(size - TAIL_SIZE)
        return hash_bytes(f.read(TAIL_SIZE))


def hash_full(path: Path) -> str:
    """Hash the entire file with BLAKE3, streaming in 1 MB chunks."""
    h = blake3.blake3()
    with open(path, "rb") as f:
        while chunk := f.read(1_048_576):
            h.update(chunk)
    return h.hexdigest()


def hash_file_progressive(path: Path) -> dict:
    """Compute all hash stages for a file. Returns dict with size, head, tail, full hashes."""
    stat = path.stat()
    size = stat.st_size
    result = {
        "path": str(path),
        "size": size,
        "mtime": stat.st_mtime,
    }

    # For small files (≤ HEAD_SIZE), head hash IS the full hash
    if size <= HEAD_SIZE:
        full = hash_full(path)
        result["blake3_head"] = full
        result["blake3_tail"] = full
        result["blake3_full"] = full
    else:
        result["blake3_head"] = hash_head(path)
        result["blake3_tail"] = hash_tail(path)
        result["blake3_full"] = hash_full(path)

    return result


def find_exact_duplicates(paths: list[Path], progress_callback=None) -> list[list[Path]]:
    """Find exact duplicate groups using progressive filtering.

    Returns a list of groups, where each group is a list of paths that are
    identical. Single files (no duplicates) are not included.

    The progressive strategy:
      1. Group by file size — unique sizes can't be duplicates
      2. Group by head hash (first 64KB) — eliminates ~95% of same-size files
      3. Group by tail hash (last 4KB) — catches files differing at the end
      4. Group by full hash — confirms exact duplicates
    """
    # Stage 1: Group by size
    by_size: dict[int, list[Path]] = {}
    for p in paths:
        try:
            size = p.stat().st_size
            if size == 0:
                continue
            by_size.setdefault(size, []).append(p)
        except OSError:
            continue

    candidates = [group for group in by_size.values() if len(group) > 1]
    if progress_callback:
        total_candidates = sum(len(g) for g in candidates)
        progress_callback("size_filter", len(paths), total_candidates)

    # Stage 2: Group by head hash
    after_head = []
    for group in candidates:
        by_head: dict[str, list[Path]] = {}
        for p in group:
            try:
                h = hash_head(p)
                by_head.setdefault(h, []).append(p)
            except OSError:
                continue
        after_head.extend(g for g in by_head.values() if len(g) > 1)

    if progress_callback:
        total_after_head = sum(len(g) for g in after_head)
        progress_callback("head_filter", total_candidates, total_after_head)

    # Stage 3: Group by tail hash
    after_tail = []
    for group in after_head:
        by_tail: dict[str, list[Path]] = {}
        for p in group:
            try:
                h = hash_tail(p)
                by_tail.setdefault(h, []).append(p)
            except OSError:
                continue
        after_tail.extend(g for g in by_tail.values() if len(g) > 1)

    if progress_callback:
        total_after_tail = sum(len(g) for g in after_tail)
        progress_callback("tail_filter", total_after_head, total_after_tail)

    # Stage 4: Group by full hash
    duplicate_groups = []
    for group in after_tail:
        by_full: dict[str, list[Path]] = {}
        for p in group:
            try:
                h = hash_full(p)
                by_full.setdefault(h, []).append(p)
            except OSError:
                continue
        duplicate_groups.extend(g for g in by_full.values() if len(g) > 1)

    if progress_callback:
        total_dupes = sum(len(g) for g in duplicate_groups)
        progress_callback("full_hash", total_after_tail, total_dupes)

    return duplicate_groups
