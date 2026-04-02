"""SQLite database for tracking scanned files, archives, and metadata merges."""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB_PATH = Path("memoryvault.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL,
    size INTEGER NOT NULL,
    blake3_full TEXT,
    blake3_head TEXT,
    blake3_tail TEXT,
    mtime REAL,
    has_exif_date BOOLEAN,
    has_exif_gps BOOLEAN,
    width INTEGER,
    height INTEGER,
    scan_time TEXT NOT NULL,
    source TEXT,
    UNIQUE(path)
);

CREATE TABLE IF NOT EXISTS archives (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL,
    blake3 TEXT,
    entries_total INTEGER,
    entries_processed INTEGER DEFAULT 0,
    status TEXT DEFAULT 'pending',
    UNIQUE(path)
);

CREATE TABLE IF NOT EXISTS archive_entries (
    id INTEGER PRIMARY KEY,
    archive_id INTEGER REFERENCES archives(id),
    entry_path TEXT NOT NULL,
    status TEXT DEFAULT 'pending',
    kept_path TEXT,
    skip_reason TEXT,
    UNIQUE(archive_id, entry_path)
);

CREATE TABLE IF NOT EXISTS metadata_log (
    id INTEGER PRIMARY KEY,
    target_path TEXT NOT NULL,
    source_desc TEXT,
    field TEXT,
    value TEXT,
    merge_time TEXT
);

CREATE INDEX IF NOT EXISTS idx_files_blake3_full ON files(blake3_full);
CREATE INDEX IF NOT EXISTS idx_files_size ON files(size);
CREATE INDEX IF NOT EXISTS idx_files_blake3_head ON files(blake3_head);
"""


class Database:
    def __init__(self, db_path: Path = DEFAULT_DB_PATH):
        self.db_path = db_path
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.close()

    @contextmanager
    def transaction(self):
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # --- File operations ---

    def upsert_file(self, **kwargs):
        """Insert or update a file record."""
        kwargs.setdefault("scan_time", datetime.now(timezone.utc).isoformat())
        columns = ", ".join(kwargs.keys())
        placeholders = ", ".join(f":{k}" for k in kwargs.keys())
        updates = ", ".join(f"{k}=excluded.{k}" for k in kwargs.keys() if k != "path")
        self.conn.execute(
            f"INSERT INTO files ({columns}) VALUES ({placeholders}) "
            f"ON CONFLICT(path) DO UPDATE SET {updates}",
            kwargs,
        )
        self.conn.commit()

    def bulk_upsert_files(self, records: list[dict]):
        """Insert or update multiple file records in a single transaction."""
        if not records:
            return
        now = datetime.now(timezone.utc).isoformat()
        for r in records:
            r.setdefault("scan_time", now)

        keys = records[0].keys()
        columns = ", ".join(keys)
        placeholders = ", ".join(f":{k}" for k in keys)
        updates = ", ".join(f"{k}=excluded.{k}" for k in keys if k != "path")
        with self.transaction():
            self.conn.executemany(
                f"INSERT INTO files ({columns}) VALUES ({placeholders}) "
                f"ON CONFLICT(path) DO UPDATE SET {updates}",
                records,
            )

    def get_file_by_path(self, path: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM files WHERE path = ?", (path,)).fetchone()
        return dict(row) if row else None

    def get_files_by_size(self, size: int) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM files WHERE size = ?", (size,)).fetchall()
        return [dict(r) for r in rows]

    def get_files_by_head_hash(self, blake3_head: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM files WHERE blake3_head = ?", (blake3_head,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_files_by_full_hash(self, blake3_full: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM files WHERE blake3_full = ?", (blake3_full,)
        ).fetchall()
        return [dict(r) for r in rows]

    def find_duplicate_groups(self) -> list[list[dict]]:
        """Find all groups of files sharing the same full BLAKE3 hash."""
        rows = self.conn.execute(
            "SELECT blake3_full FROM files WHERE blake3_full IS NOT NULL "
            "GROUP BY blake3_full HAVING COUNT(*) > 1"
        ).fetchall()
        groups = []
        for row in rows:
            files = self.get_files_by_full_hash(row["blake3_full"])
            groups.append(files)
        return groups

    def get_all_sizes_with_counts(self) -> list[tuple[int, int]]:
        """Return (size, count) for sizes that appear more than once."""
        rows = self.conn.execute(
            "SELECT size, COUNT(*) as cnt FROM files GROUP BY size HAVING cnt > 1"
        ).fetchall()
        return [(r["size"], r["cnt"]) for r in rows]

    def file_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) as cnt FROM files").fetchone()
        return row["cnt"]

    # --- Archive operations ---

    def register_archive(self, path: str, blake3: str = None, entries_total: int = 0) -> int:
        cursor = self.conn.execute(
            "INSERT INTO archives (path, blake3, entries_total) VALUES (?, ?, ?) "
            "ON CONFLICT(path) DO UPDATE SET blake3=excluded.blake3, entries_total=excluded.entries_total "
            "RETURNING id",
            (path, blake3, entries_total),
        )
        row = cursor.fetchone()
        self.conn.commit()
        return row["id"]

    def get_archive(self, path: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM archives WHERE path = ?", (path,)).fetchone()
        return dict(row) if row else None

    def update_archive_status(self, archive_id: int, status: str, entries_processed: int = None):
        if entries_processed is not None:
            self.conn.execute(
                "UPDATE archives SET status = ?, entries_processed = ? WHERE id = ?",
                (status, entries_processed, archive_id),
            )
        else:
            self.conn.execute(
                "UPDATE archives SET status = ? WHERE id = ?", (status, archive_id)
            )
        self.conn.commit()

    def log_archive_entry(self, archive_id: int, entry_path: str, status: str,
                          kept_path: str = None, skip_reason: str = None):
        self.conn.execute(
            "INSERT INTO archive_entries (archive_id, entry_path, status, kept_path, skip_reason) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(archive_id, entry_path) DO UPDATE SET "
            "status=excluded.status, kept_path=excluded.kept_path, skip_reason=excluded.skip_reason",
            (archive_id, entry_path, status, kept_path, skip_reason),
        )
        self.conn.commit()

    def get_processed_entries(self, archive_id: int) -> set[str]:
        rows = self.conn.execute(
            "SELECT entry_path FROM archive_entries WHERE archive_id = ? AND status != 'pending'",
            (archive_id,),
        ).fetchall()
        return {r["entry_path"] for r in rows}

    # --- Metadata log ---

    def log_metadata_merge(self, target_path: str, source_desc: str, field: str, value: str):
        self.conn.execute(
            "INSERT INTO metadata_log (target_path, source_desc, field, value, merge_time) "
            "VALUES (?, ?, ?, ?, ?)",
            (target_path, source_desc, field, value, datetime.now(timezone.utc).isoformat()),
        )
        self.conn.commit()
