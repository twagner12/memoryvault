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
    -- Hash of the bytes as they arrived from the source, before any metadata
    -- was written into the saved file. blake3_full always describes the file
    -- currently on disk; source_blake3 preserves archive-entry provenance.
    source_blake3 TEXT,
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
    title TEXT,
    blake3 TEXT,
    entries_total INTEGER,
    entries_processed INTEGER DEFAULT 0,
    status TEXT DEFAULT 'pending',
    completed_at TEXT,
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

CREATE TABLE IF NOT EXISTS resolutions (
    id INTEGER PRIMARY KEY,
    blake3_full TEXT NOT NULL,
    winner_path TEXT NOT NULL,
    action TEXT NOT NULL,
    confidence INTEGER DEFAULT 100,
    resolved_at TEXT NOT NULL,
    auto_resolved BOOLEAN DEFAULT FALSE,
    -- Set on verdicts produced by the old blanket auto-resolve, which ran with
    -- no human review and a scorer whose resolution term was inert. A stale
    -- row is history, not a decision: is_resolved() ignores it.
    stale INTEGER DEFAULT 0
);

"""

# Applied after column migrations: on an older database the columns these
# index may not exist until the migration has run.
SCHEMA_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_files_blake3_full ON files(blake3_full);
CREATE INDEX IF NOT EXISTS idx_files_size ON files(size);
CREATE INDEX IF NOT EXISTS idx_files_blake3_head ON files(blake3_head);
CREATE INDEX IF NOT EXISTS idx_files_source_blake3 ON files(source_blake3);
CREATE INDEX IF NOT EXISTS idx_files_path_stat ON files(path, size, mtime);
CREATE INDEX IF NOT EXISTS idx_resolutions_blake3 ON resolutions(blake3_full);
"""

# Columns added after the initial release. Applied by comparing against
# PRAGMA table_info rather than catching errors from a blind ALTER, so the
# migration is idempotent by construction and never swallows a real failure.
MIGRATIONS: dict[str, dict[str, str]] = {
    "files": {
        "blake3_head": "TEXT",
        "blake3_tail": "TEXT",
        "source_blake3": "TEXT",
        "mtime": "REAL",
        "has_exif_date": "BOOLEAN",
        "has_exif_gps": "BOOLEAN",
        "width": "INTEGER",
        "height": "INTEGER",
        "source": "TEXT",
    },
    "archives": {
        "completed_at": "TEXT",
        "title": "TEXT",
    },
    "resolutions": {
        "confidence": "INTEGER DEFAULT 100",
        "stale": "INTEGER DEFAULT 0",
    },
}


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
        self._apply_column_migrations()
        self.conn.executescript(SCHEMA_INDEXES)
        self.conn.commit()

    def _apply_column_migrations(self):
        """Add any columns missing from an older database.

        Idempotent: existing columns are detected via PRAGMA table_info and
        skipped, so no ALTER is issued that we expect to fail.
        """
        for table, columns in MIGRATIONS.items():
            existing = {
                row["name"]
                for row in self.conn.execute(f"PRAGMA table_info({table})")
            }
            if not existing:
                continue  # table absent entirely; CREATE above owns it
            for name, definition in columns.items():
                if name not in existing:
                    self.conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {name} {definition}"
                    )

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

    def find_files_by_content_hash(self, content_hash: str) -> list[dict]:
        """Find files matching `content_hash` as either their on-disk or source bytes.

        Ingest writes metadata into a file after saving it, so the same original
        photo arriving in a later archive hashes to the *source* value while the
        saved copy on disk hashes to something else. Both must match.
        """
        rows = self.conn.execute(
            "SELECT * FROM files WHERE blake3_full = ? OR source_blake3 = ?",
            (content_hash, content_hash),
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

    def register_archive(self, path: str, blake3: str = None, entries_total: int = 0,
                         title: str = None) -> int:
        cursor = self.conn.execute(
            "INSERT INTO archives (path, blake3, entries_total, title) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(path) DO UPDATE SET blake3=excluded.blake3, entries_total=excluded.entries_total, "
            "title=COALESCE(excluded.title, title) "
            "RETURNING id",
            (path, blake3, entries_total, title),
        )
        row = cursor.fetchone()
        self.conn.commit()
        return row["id"]

    def get_archive(self, path: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM archives WHERE path = ?", (path,)).fetchone()
        return dict(row) if row else None

    def update_archive_status(self, archive_id: int, status: str, entries_processed: int = None):
        completed_at = datetime.now(timezone.utc).isoformat() if status == "complete" else None
        if entries_processed is not None:
            self.conn.execute(
                "UPDATE archives SET status = ?, entries_processed = ?, completed_at = COALESCE(?, completed_at) WHERE id = ?",
                (status, entries_processed, completed_at, archive_id),
            )
        else:
            self.conn.execute(
                "UPDATE archives SET status = ?, completed_at = COALESCE(?, completed_at) WHERE id = ?",
                (status, completed_at, archive_id),
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

    # --- Resolutions ---

    def resolve_group(self, blake3_full: str, winner_path: str, action: str,
                      confidence: int = 100, auto_resolved: bool = False):
        self.conn.execute(
            "INSERT INTO resolutions (blake3_full, winner_path, action, confidence, resolved_at, auto_resolved) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (blake3_full, winner_path, action, confidence,
             datetime.now(timezone.utc).isoformat(), auto_resolved),
        )
        self.conn.commit()

    def is_resolved(self, blake3_full: str) -> bool:
        """True only for verdicts that still count. Stale rows do not."""
        row = self.conn.execute(
            "SELECT 1 FROM resolutions WHERE blake3_full = ? AND COALESCE(stale, 0) = 0",
            (blake3_full,),
        ).fetchone()
        return row is not None

    def get_resolved_hashes(self) -> set[str]:
        rows = self.conn.execute(
            "SELECT blake3_full FROM resolutions WHERE COALESCE(stale, 0) = 0"
        ).fetchall()
        return {r["blake3_full"] for r in rows}

    def get_stat_index(self, prefix: str) -> dict[str, tuple[int, float]]:
        """Map path -> (size, mtime) for every indexed file under `prefix`.

        Loaded in one query so a rescan can skip unchanged files without
        issuing a lookup per file. The range comparison uses the existing
        UNIQUE(path) index; LIKE would not.
        """
        rows = self.conn.execute(
            "SELECT path, size, mtime FROM files WHERE path >= ? AND path < ?",
            (prefix, prefix + "￿"),
        ).fetchall()
        return {
            r["path"]: (r["size"], r["mtime"])
            for r in rows
            if r["mtime"] is not None
        }

    def find_unresolved_duplicate_groups(self) -> list[list[dict]]:
        """Find duplicate groups that haven't been resolved yet."""
        resolved = self.get_resolved_hashes()
        all_groups = self.find_duplicate_groups()
        return [g for g in all_groups if g[0]["blake3_full"] not in resolved]

    def get_resolution_stats(self) -> dict:
        row = self.conn.execute(
            "SELECT COUNT(*) as total, "
            "SUM(CASE WHEN auto_resolved THEN 1 ELSE 0 END) as auto_count, "
            "SUM(CASE WHEN COALESCE(stale, 0) = 1 THEN 1 ELSE 0 END) as stale_count "
            "FROM resolutions WHERE COALESCE(stale, 0) = 0"
        ).fetchone()
        stale = self.conn.execute(
            "SELECT COUNT(*) as c FROM resolutions WHERE COALESCE(stale, 0) = 1"
        ).fetchone()["c"]
        return {
            "total": row["total"],
            "auto_resolved": row["auto_count"] or 0,
            "stale": stale,
        }


def mark_auto_resolutions_stale(db: Database) -> int:
    """Invalidate every machine-made verdict from the old blanket auto-resolve.

    Those rows were written for *all* duplicate groups at confidence=100 with no
    human review, chosen by a scorer whose resolution term was inert (width and
    height are NULL for every row) and whose format ranking trusts a file
    extension that is often wrong. They are kept for history but must never be
    read as reviewed decisions.

    Returns the number of rows newly marked. Idempotent: re-running marks none.
    """
    cursor = db.conn.execute(
        "UPDATE resolutions SET stale = 1 "
        "WHERE auto_resolved = 1 AND COALESCE(stale, 0) = 0"
    )
    db.conn.commit()
    return cursor.rowcount
