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

-- Metadata that was resolved but could not be embedded, or whose embedding
-- failed. Together with metadata_log this makes every outcome recorded:
-- merged (log), deferred (here), failed (here). Rows are self-contained so a
-- future drain pass needs neither the sidecar nor the original zip.
CREATE TABLE IF NOT EXISTS metadata_pending (
    id           INTEGER PRIMARY KEY,
    file_id      INTEGER REFERENCES files(id),
    file_path    TEXT NOT NULL,      -- denormalised: survives a re-index
    file_blake3  TEXT,               -- content at record time; drain re-verifies
    field        TEXT NOT NULL,      -- 'date' | 'gps'
    value        TEXT NOT NULL,      -- fully resolved JSON payload
    reason       TEXT NOT NULL,
    state        TEXT NOT NULL,      -- 'deferred' | 'failed'
    source_desc  TEXT,               -- 'takeout:<entry path within archive>'
    recorded_at  TEXT NOT NULL,
    applied_at   TEXT                -- NULL = outstanding
);

-- Sidecars whose media file could not be identified. Carries the parsed
-- payload as well as the name parts, because the Takeout zips are gone and
-- cannot be re-read — a later rebind pass must work from this table alone.
CREATE TABLE IF NOT EXISTS sidecars_unmatched (
    id              INTEGER PRIMARY KEY,
    archive_id      INTEGER REFERENCES archives(id),
    sidecar_path    TEXT NOT NULL,   -- full entry path of the JSON in the archive
    archive_dir     TEXT NOT NULL,   -- directory within the archive
    entry_name      TEXT NOT NULL,   -- sidecar basename
    media_stem      TEXT,            -- parsed core: the media name we sought
    counter         TEXT,            -- '(9)' if present
    reason          TEXT NOT NULL,   -- 'no_media_in_dir'|'ambiguous'|'unparseable'
    candidate_count INTEGER DEFAULT 0,
    payload         TEXT,            -- parsed sidecar JSON (date/geo)
    recorded_at     TEXT NOT NULL,
    rebound_at      TEXT,            -- NULL = still unmatched
    rebound_path    TEXT,            -- vault file it finally bound to
    rebound_outcome TEXT             -- JSON: what applying it actually did
);

-- What happened when a byte-identical incoming file was discarded.
--
-- Dedup used to collapse a duplicate and record only that it had: the survivor
-- it matched was never written down (all 952 Phase 2 rows carry kept_path
-- NULL), and any information the duplicate held that the survivor lacked was
-- either merged silently or lost silently. This is the metadata_pending
-- treatment applied to dedup: every rule that runs writes into `adopted` or
-- into `declined`, so an empty pair is a bug rather than an absence of news.
CREATE TABLE IF NOT EXISTS dedup_collapse (
    id              INTEGER PRIMARY KEY,
    archive_id      INTEGER REFERENCES archives(id),
    entry_path      TEXT NOT NULL,   -- the discarded file, as named at source
    source_root     TEXT,            -- which drive it came from; Phase 7 erases this
    survivor_path   TEXT NOT NULL,   -- the link that used to be missing
    survivor_blake3 TEXT,
    matched_on      TEXT NOT NULL,   -- 'blake3_full' | 'source_blake3'
    adopted         TEXT,            -- JSON: what was taken, and from where
    declined        TEXT,            -- JSON: what was offered and refused, with reasons
    collapsed_at    TEXT NOT NULL,
    UNIQUE(archive_id, entry_path)
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
CREATE INDEX IF NOT EXISTS idx_pending_outstanding
    ON metadata_pending(state, applied_at);
CREATE INDEX IF NOT EXISTS idx_pending_path ON metadata_pending(file_path);
CREATE INDEX IF NOT EXISTS idx_collapse_survivor ON dedup_collapse(survivor_path);
CREATE INDEX IF NOT EXISTS idx_unmatched_rebind
    ON sidecars_unmatched(rebound_at, archive_dir, media_stem);
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
    "sidecars_unmatched": {
        "rebound_path": "TEXT",
        "rebound_outcome": "TEXT",
    },
}


class Database:
    def __init__(self, db_path: Path = DEFAULT_DB_PATH, read_only: bool = False):
        """Open the database, applying any missing schema.

        `read_only` opens through SQLite's `mode=ro` URI and skips schema
        initialisation, which is itself a write. It exists so an inspection —
        a `--dry-run`, a report — is *structurally* incapable of modifying the
        database rather than merely promising not to. Any write attempted on
        such a connection raises instead of succeeding quietly.
        """
        self.db_path = db_path
        self.read_only = read_only

        if read_only:
            self.conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            self.conn.row_factory = sqlite3.Row
            return

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

    # Entries that are neither media nor a candidate for one. Excluded when the
    # rebind pass builds its per-directory listings, so a sidecar can never be
    # mistaken for the media file it describes.
    NON_MEDIA_SKIP_REASONS = ("sidecar", "album_metadata", "not_media")

    def get_media_entries(self) -> list[dict]:
        """Every media entry from every archive, with where it was kept.

        Deliberately not scoped to one archive. Google splits an album
        directory across zip parts, so the sidecar and the photo it describes
        routinely arrive in different archives — and the entry path is the path
        *inside* the zip, so the directory string is identical in both.
        Restricting this to a single archive is precisely what stops those
        sidecars binding at ingest time.
        """
        placeholders = ", ".join("?" * len(self.NON_MEDIA_SKIP_REASONS))
        rows = self.conn.execute(
            f"SELECT entry_path, status, kept_path FROM archive_entries "
            f"WHERE skip_reason IS NULL OR skip_reason NOT IN ({placeholders})",
            self.NON_MEDIA_SKIP_REASONS,
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_sidecar_rebound(self, sidecar_id: int, target_path: str,
                             outcome: str):
        """Record that a sidecar finally found its media file, and what happened.

        Written after the metadata has been applied. A crash in between leaves
        `rebound_at` NULL and the next run re-offers the sidecar — which is a
        no-op, because `apply_sidecar` refuses to record or write a value the
        target already carries.
        """
        self.conn.execute(
            "UPDATE sidecars_unmatched SET rebound_at = ?, rebound_path = ?, "
            "rebound_outcome = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), target_path, outcome,
             sidecar_id),
        )
        self.conn.commit()

    def get_processed_entries(self, archive_id: int) -> set[str]:
        rows = self.conn.execute(
            "SELECT entry_path FROM archive_entries WHERE archive_id = ? AND status != 'pending'",
            (archive_id,),
        ).fetchall()
        return {r["entry_path"] for r in rows}

    # --- Metadata log ---

    def has_already_present(self, target_path: str, value: str) -> bool:
        """True when this exact refusal is already on record.

        The idempotency key for `already_present`, keyed on the value rather
        than the field: two sidecars may offer two *different* dates to the
        same already-dated file, and each refusal is its own fact. Only the
        byte-identical repeat — what a re-ingest produces — is suppressed.
        """
        row = self.conn.execute(
            "SELECT 1 FROM metadata_log WHERE target_path = ? "
            "AND field = 'already_present' AND value = ? LIMIT 1",
            (target_path, value),
        ).fetchone()
        return row is not None

    def log_metadata_merge(self, target_path: str, source_desc: str, field: str, value: str):
        self.conn.execute(
            "INSERT INTO metadata_log (target_path, source_desc, field, value, merge_time) "
            "VALUES (?, ?, ?, ?, ?)",
            (target_path, source_desc, field, value, datetime.now(timezone.utc).isoformat()),
        )
        self.conn.commit()

    # --- Metadata pending (deferred / failed) ---

    def record_pending(self, file_path: str, field: str, value: str, reason: str,
                       state: str, source_desc: str = None,
                       file_blake3: str = None, file_id: int = None) -> int:
        """Record metadata that was not embedded, and why.

        `state` is 'deferred' (the container cannot hold it) or 'failed' (the
        write was attempted and refused). Both are outstanding until a drain
        pass sets applied_at; neither is a silent loss.
        """
        if state not in ("deferred", "failed"):
            raise ValueError(f"state must be 'deferred' or 'failed', got {state!r}")
        if field not in ("date", "gps"):
            raise ValueError(f"field must be 'date' or 'gps', got {field!r}")

        cursor = self.conn.execute(
            "INSERT INTO metadata_pending "
            "(file_id, file_path, file_blake3, field, value, reason, state, "
            " source_desc, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (file_id, file_path, file_blake3, field, value, reason, state,
             source_desc, datetime.now(timezone.utc).isoformat()),
        )
        row = cursor.fetchone()
        self.conn.commit()
        return row["id"]

    def has_outstanding_pending(self, file_path: str, field: str,
                                value: str) -> bool:
        """True when this exact value is already recorded and still outstanding.

        The idempotency key for a deferral. Callers use it to skip both the
        insert *and* the work of building the row — the file hash in
        particular, which is a full re-read. Applied rows deliberately do not
        match: once a drain pass has stamped `applied_at`, the value is no
        longer outstanding and a fresh offer is new information.
        """
        row = self.conn.execute(
            "SELECT 1 FROM metadata_pending "
            "WHERE file_path = ? AND field = ? AND value = ? "
            "AND applied_at IS NULL LIMIT 1",
            (file_path, field, value),
        ).fetchone()
        return row is not None

    def get_pending(self, file_path: str = None, outstanding_only: bool = True
                    ) -> list[dict]:
        sql = "SELECT * FROM metadata_pending WHERE 1=1"
        params: list = []
        if file_path is not None:
            sql += " AND file_path = ?"
            params.append(file_path)
        if outstanding_only:
            sql += " AND applied_at IS NULL"
        rows = self.conn.execute(sql + " ORDER BY id", params).fetchall()
        return [dict(r) for r in rows]

    def get_pending_summary(self) -> list[dict]:
        """Outstanding pending rows grouped by state, field and reason."""
        rows = self.conn.execute(
            "SELECT state, field, reason, COUNT(*) AS count "
            "FROM metadata_pending WHERE applied_at IS NULL "
            "GROUP BY state, field, reason ORDER BY count DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    # --- Unmatched sidecars ---

    def record_unmatched_sidecar(self, archive_id: int, sidecar_path: str,
                                 archive_dir: str, entry_name: str, reason: str,
                                 media_stem: str = None, counter: str = None,
                                 candidate_count: int = 0,
                                 payload: str = None) -> int:
        """Record a sidecar whose media file could not be identified."""
        cursor = self.conn.execute(
            "INSERT INTO sidecars_unmatched "
            "(archive_id, sidecar_path, archive_dir, entry_name, media_stem, "
            " counter, reason, candidate_count, payload, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (archive_id, sidecar_path, archive_dir, entry_name, media_stem,
             counter, reason, candidate_count, payload,
             datetime.now(timezone.utc).isoformat()),
        )
        row = cursor.fetchone()
        self.conn.commit()
        return row["id"]

    def get_unmatched_summary(self) -> list[dict]:
        """Still-unmatched sidecars grouped by reason."""
        rows = self.conn.execute(
            "SELECT reason, COUNT(*) AS count FROM sidecars_unmatched "
            "WHERE rebound_at IS NULL GROUP BY reason ORDER BY count DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_unmatched(self, archive_id: int = None) -> list[dict]:
        sql = "SELECT * FROM sidecars_unmatched WHERE rebound_at IS NULL"
        params: list = []
        if archive_id is not None:
            sql += " AND archive_id = ?"
            params.append(archive_id)
        rows = self.conn.execute(sql + " ORDER BY id", params).fetchall()
        return [dict(r) for r in rows]

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
