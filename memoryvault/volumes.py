"""Detect indexed files that live on volumes which are not currently attached.

A dedup skip decision is only safe if the copy being kept actually exists. When
a whole volume is detached — an external drive, an unmounted network share — the
index still claims ownership of every file on it, and an ingest would silently
discard incoming originals against rows nobody can read. This module identifies
those volumes so the ingest can refuse to run.

Individual missing files are handled separately, at the point of the skip
decision in `ingest`; this module answers the coarser question of whether an
entire volume has gone away.
"""

from pathlib import PurePosixPath

# Prefixes under which the *next* path component names a mounted volume.
# e.g. /run/media/tim/Seagate Backup Plus Drive/... -> that whole 4-component root.
_VOLUME_PARENTS = (
    ("run", "media", None),   # udisks2 / Omarchy: /run/media/<user>/<label>
    ("media", None),          # older udisks:     /media/<user>/<label>
    ("mnt",),                 # manual mounts:    /mnt/<label>
    ("Volumes",),             # macOS:            /Volumes/<label>
)


class UnreachableVolumeError(RuntimeError):
    """Raised when indexed rows point at a volume that is not attached."""


def volume_root(path: str) -> str | None:
    """Return the removable-volume root containing `path`, or None.

    None means the path is on the ordinary filesystem, where per-file existence
    checks are the right granularity.
    """
    if not path or not path.startswith("/"):
        return None

    parts = PurePosixPath(path).parts[1:]  # drop the leading "/"

    for pattern in _VOLUME_PARENTS:
        # `None` in a pattern is a wildcard component (a username).
        depth = len(pattern)
        if len(parts) <= depth:
            continue
        if all(expected is None or parts[i] == expected
               for i, expected in enumerate(pattern)):
            return "/" + "/".join(parts[: depth + 1])

    return None


def _is_reachable(root: str) -> bool:
    """True if the volume root is attached and readable.

    An empty mount point is treated as unreachable: udisks leaves the directory
    behind after an unmount, so existence alone is not evidence the disk is there.
    """
    import os

    if not os.path.isdir(root):
        return False
    try:
        with os.scandir(root) as it:
            return any(True for _ in it)
    except OSError:
        return False


def find_unreachable_volumes(db) -> list[dict]:
    """Return [{volume, row_count, sample_path}] for detached indexed volumes.

    The prefix is computed inside SQLite so a 200k-row index does not have to be
    pulled into Python just to be grouped.
    """
    db.conn.create_function("mv_volume_root", 1, volume_root)
    rows = db.conn.execute(
        "SELECT mv_volume_root(path) AS volume, COUNT(*) AS row_count, "
        "MIN(path) AS sample_path FROM files "
        "GROUP BY volume HAVING volume IS NOT NULL ORDER BY row_count DESC"
    ).fetchall()

    return [
        {"volume": r["volume"], "row_count": r["row_count"],
         "sample_path": r["sample_path"]}
        for r in rows
        if not _is_reachable(r["volume"])
    ]


def format_unreachable_error(unreachable: list[dict], override_hint: str) -> str:
    """Build the operator-facing message for an aborted run."""
    total = sum(v["row_count"] for v in unreachable)
    lines = [
        f"Refusing to start: {total:,} indexed file(s) live on "
        f"{len(unreachable)} volume(s) that are not attached.",
        "",
        "Deduplication would treat those files as already-owned and discard "
        "incoming copies that may exist nowhere else.",
        "",
    ]
    for v in unreachable:
        lines.append(f"  {v['volume']}  —  {v['row_count']:,} rows")
        lines.append(f"      e.g. {v['sample_path']}")
    lines.append("")
    lines.append(f"Attach the volume(s), or re-run with {override_hint} to accept the risk.")
    return "\n".join(lines)


def assert_volumes_reachable(db, override_hint: str = "--allow-unreachable-volumes"):
    """Raise UnreachableVolumeError if any indexed volume is detached."""
    unreachable = find_unreachable_volumes(db)
    if unreachable:
        raise UnreachableVolumeError(format_unreachable_error(unreachable, override_hint))
