"""Reporting commands must not modify the database they report on.

This suite exists because of a concrete incident: running

    memoryvault --db .../shakedown.db volumes

moved the fixture's mtime from Aug 1 01:27 to Aug 2 20:22. Nothing in the
data changed — the row count and `PRAGMA integrity_check` were identical —
but the command had opened the database read-write, run
`PRAGMA journal_mode=WAL`, and executed the full `_init_schema()` (CREATE
TABLE IF NOT EXISTS, column migrations, index creation, COMMIT). Against a
current schema every statement was a no-op, so the damage was invisible.
Against an older database the same call would have silently migrated it, and
a report is the last place a schema rewrite should happen.

`Database.__init__` already had the fix — `read_only=True` opens through
SQLite's `mode=ro` URI and skips schema initialisation — but only
`rebind --dry-run` was passing it.

Two properties are asserted here, one per failure mode:

  1. A reporting command leaves the database file byte-identical, with its
     mtime untouched. This is the exact evidence that exposed the bug.
  2. A reporting command run against a database missing a migrated column
     leaves that column missing. It must surface the mismatch, not quietly
     rewrite the schema underneath a read.

A note on `-wal`/`-shm`: these assertions deliberately do *not* claim the
sidecar files are never created. SQLite needs the `-shm` shared-memory index
to read a WAL-mode database at all, and creates it even for a `mode=ro`
connection, so their presence does not distinguish a read-only open from a
read-write one. What distinguishes them is whether the main database file
changes — which is what `assert_db_untouched` measures — plus the fact that
any write on such a connection raises (see `test_readonly_connection_refuses_writes`).
"""

import hashlib
import subprocess
import sqlite3
import sys
import time
import uuid
from pathlib import Path

import pytest
from click.testing import CliRunner

from memoryvault.cli import cli
from memoryvault.database import Database

# Every command whose job is to report, not to change anything.
REPORTING_COMMANDS = ["volumes", "stats", "pending", "unmatched", "dupes"]

# Commands that legitimately write; listed so a new command added to the CLI
# without a decision about which side it falls on shows up as a test failure.
WRITE_COMMANDS = ["scan", "ingest", "merge", "migrate", "rebind", "repair-mtime"]


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _snapshot(path: Path) -> dict:
    st = path.stat()
    return {"mtime_ns": st.st_mtime_ns, "size": st.st_size, "sha256": _digest(path)}


def assert_db_untouched(path: Path, before: dict, what: str):
    """The database file itself must be bit-for-bit unchanged."""
    after = _snapshot(path)
    assert after["sha256"] == before["sha256"], (
        f"{what} changed the database contents "
        f"({before['sha256'][:12]} -> {after['sha256'][:12]})"
    )
    assert after["size"] == before["size"], f"{what} changed the database size"
    assert after["mtime_ns"] == before["mtime_ns"], (
        f"{what} touched the database file: mtime moved "
        f"{before['mtime_ns']} -> {after['mtime_ns']}. This is the exact "
        f"signature of an accidental read-write open."
    )


def _columns(db_path: Path, table: str) -> set[str]:
    """Read a table's columns without going through Database (no migrations)."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def _orphan_wal_content(path: Path):
    """Leave committed-but-uncheckpointed content in the `-wal` file.

    This is the detail that makes the mtime assertion meaningful. In WAL mode
    a write goes to `-wal`, not to the database file, so a read-write open of
    a *quiet* database moves nothing and the bug hides. The main file's mtime
    moves when the last connection closes and checkpoints the outstanding WAL
    frames back into it — which is exactly what happened to shakedown.db:
    size identical, contents identical, mtime a day newer.

    Reproduced faithfully by committing from a subprocess that then dies via
    `os._exit`, skipping SQLite's cleanup, so the frames are left orphaned
    with no live connection holding them. Without this the reporting tests
    pass against the unfixed code.
    """
    # Unique per call: this helper runs more than once against the same file.
    marker = f"/data/orphan-{uuid.uuid4().hex}.jpg"
    writer = (
        "import sqlite3, datetime, os\n"
        f"c = sqlite3.connect({str(path)!r})\n"
        "c.execute('PRAGMA journal_mode=WAL')\n"
        "c.execute('INSERT INTO files (path,size,scan_time) VALUES (?,?,?)',\n"
        f"          ({marker!r}, 7,\n"
        "           datetime.datetime.now(datetime.timezone.utc).isoformat()))\n"
        "c.commit()\n"
        "os._exit(0)\n"
    )
    done = subprocess.run([sys.executable, "-c", writer], capture_output=True, text=True)
    assert done.returncode == 0, (
        f"orphan-WAL writer failed ({done.returncode}):\n{done.stderr}"
    )
    assert Path(str(path) + "-wal").stat().st_size > 0, (
        "fixture failed to leave orphaned WAL frames"
    )


@pytest.fixture
def populated_db(tmp_path) -> Path:
    """A real-schema database in the on-disk state shakedown.db was in.

    Built through `Database` so the schema and migrations are the production
    ones. Two files share a blake3 so `dupes`/`stats` have real work to do and
    the reporting queries actually touch rows. It is then left with orphaned
    WAL frames — see `_orphan_wal_content` for why that matters.
    """
    path = tmp_path / "report.db"
    db = Database(path)
    for p in ["/data/a.jpg", "/data/b.jpg"]:
        db.upsert_file(
            path=p, size=1000, blake3_full="dead" * 16,
            has_exif_date=1, has_exif_gps=0, width=100, height=100,
            source="local",
        )
    db.conn.commit()
    db.close()

    _orphan_wal_content(path)
    return path


# --------------------------------------------------------------------------
# Property 1 — a report leaves the database file untouched
# --------------------------------------------------------------------------

@pytest.mark.parametrize("command", REPORTING_COMMANDS)
def test_reporting_command_does_not_modify_db(populated_db, command):
    """The incident test: run the report, the file must not move."""
    before = _snapshot(populated_db)
    # mtime has 1ns resolution here but filesystems vary; make sure any write
    # would land on a distinguishable timestamp.
    time.sleep(0.01)

    result = CliRunner().invoke(cli, ["--db", str(populated_db), command])

    assert result.exit_code == 0, (
        f"{command} failed unexpectedly:\n{result.output}\n{result.exception!r}"
    )
    assert_db_untouched(populated_db, before, f"`{command}`")


@pytest.mark.parametrize("command", REPORTING_COMMANDS)
def test_reporting_command_opens_read_only(populated_db, command, monkeypatch):
    """The connection itself must be structurally incapable of writing.

    Asserted at the source rather than by inference: every `Database` opened
    during the command must carry `read_only=True`.
    """
    opened = []
    real_init = Database.__init__

    def spy(self, db_path=None, read_only=False, *a, **kw):
        opened.append(read_only)
        return real_init(self, db_path, read_only=read_only, *a, **kw)

    monkeypatch.setattr(Database, "__init__", spy)
    result = CliRunner().invoke(cli, ["--db", str(populated_db), command])

    assert result.exit_code == 0, f"{command} failed:\n{result.output}"
    assert opened, f"{command} never opened a Database"
    assert all(opened), (
        f"{command} opened the database read-write "
        f"(read_only flags seen: {opened}). Reporting commands must pass "
        f"read_only=True."
    )


def test_readonly_connection_refuses_writes(populated_db):
    """`read_only=True` is enforced by SQLite, not merely by convention."""
    db = Database(populated_db, read_only=True)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly database"):
            db.conn.execute("INSERT INTO files (path) VALUES ('/x.jpg')")
    finally:
        db.close()


def test_write_commands_still_open_read_write(populated_db, monkeypatch):
    """The fix must not have leaked into the write path.

    `scan` is the cheapest write command to drive end to end; if it were
    opened read-only it could not record anything.
    """
    opened = []
    real_init = Database.__init__

    def spy(self, db_path=None, read_only=False, *a, **kw):
        opened.append(read_only)
        return real_init(self, db_path, read_only=read_only, *a, **kw)

    monkeypatch.setattr(Database, "__init__", spy)
    folder = populated_db.parent / "scanme"
    folder.mkdir()
    (folder / "x.txt").write_bytes(b"hello")

    result = CliRunner().invoke(
        cli, ["--db", str(populated_db), "scan", str(folder)]
    )

    assert result.exit_code == 0, f"scan failed:\n{result.output}"
    assert opened and not any(opened), (
        f"scan must open the database read-write, got read_only={opened}"
    )


# --------------------------------------------------------------------------
# Property 2 — a report must not silently migrate an older schema
# --------------------------------------------------------------------------

# A column added by MIGRATIONS["files"], i.e. one an older database predates.
LEGACY_MISSING_COLUMN = "has_exif_date"


@pytest.fixture
def old_schema_db(populated_db) -> Path:
    """A database that predates one of the migrated columns."""
    conn = sqlite3.connect(populated_db)
    try:
        conn.execute(f"ALTER TABLE files DROP COLUMN {LEGACY_MISSING_COLUMN}")
        conn.commit()
    finally:
        conn.close()

    # That rw connection checkpointed on close; restore the orphaned-WAL state
    # so the mtime assertion keeps its teeth here too.
    _orphan_wal_content(populated_db)

    assert LEGACY_MISSING_COLUMN not in _columns(populated_db, "files")
    return populated_db


@pytest.mark.parametrize("command", REPORTING_COMMANDS)
def test_reporting_command_does_not_migrate_old_schema(old_schema_db, command):
    """A read must never rewrite the schema underneath itself.

    Whether the command succeeds, reports an error, or raises is secondary —
    the column must still be missing afterwards. Silently adding it is the
    behaviour that made this bug worth fixing.
    """
    before = _snapshot(old_schema_db)
    time.sleep(0.01)

    CliRunner().invoke(cli, ["--db", str(old_schema_db), command])

    assert LEGACY_MISSING_COLUMN not in _columns(old_schema_db, "files"), (
        f"`{command}` silently migrated the schema: it added "
        f"{LEGACY_MISSING_COLUMN!r} to a database that did not have it."
    )
    assert_db_untouched(old_schema_db, before, f"`{command}` on an old schema")


def test_missing_column_silently_degrades_scoring(old_schema_db, tmp_path):
    """Characterisation test for a gap this fix deliberately does not close.

    `score_file` reads its inputs with `dict.get` (dedup.py:51), so a column
    absent from the schema is indistinguishable from a column that is present
    and NULL. On a database predating `has_exif_date`, `dupes` therefore does
    not raise and does not warn — it quietly drops the 25-point EXIF bonus and
    may name a different winner than it would on a current schema.

    Worth being precise about what the read-only change did and did not do:
    the old read-write open would have added the column via ALTER TABLE, but
    only as NULL for every existing row, so the scoring was degraded exactly
    the same way. Auto-migration never protected this — it only hid the
    mismatch. Making it read-only leaves the degradation visible instead.

    Closing the gap properly means making `score_file` distinguish "missing
    column" from "NULL value", which changes duplicate winner selection and
    therefore what `merge` acts on. That is a separate decision, not part of
    passing `read_only=True`.

    This test pins the current behaviour so that change cannot happen by
    accident: if scoring learns to reject an incomplete schema, this fails and
    must be consciously rewritten.
    """
    old = CliRunner().invoke(cli, ["--db", str(old_schema_db), "dupes"])
    assert old.exit_code == 0, f"unexpected failure:\n{old.output}"

    # The same two rows, on a database that still has the column.
    current_path = tmp_path / "current.db"
    db = Database(current_path)
    for p in ["/data/a.jpg", "/data/b.jpg"]:
        db.upsert_file(
            path=p, size=1000, blake3_full="dead" * 16,
            has_exif_date=1, has_exif_gps=0, width=100, height=100,
            source="local",
        )
    db.conn.commit()
    db.close()
    current = CliRunner().invoke(cli, ["--db", str(current_path), "dupes"])
    assert current.exit_code == 0, f"unexpected failure:\n{current.output}"

    def winner_score(output: str) -> int:
        for line in output.splitlines():
            if "score=" in line:
                return int(line.split("score=")[1].split()[0])
        raise AssertionError(f"no score in output:\n{output}")

    degraded, intact = winner_score(old.output), winner_score(current.output)
    assert intact - degraded == 25, (
        "expected the missing column to cost exactly the EXIF-date bonus "
        f"(25 points); got intact={intact} degraded={degraded}. Scoring "
        "behaviour on an incomplete schema has changed — update this test "
        "deliberately."
    )


def test_write_path_still_migrates_old_schema(old_schema_db):
    """Control: the migration path is intact where it belongs.

    Without this, the test above could pass simply because migrations stopped
    working everywhere.
    """
    assert LEGACY_MISSING_COLUMN not in _columns(old_schema_db, "files")

    db = Database(old_schema_db)  # read-write: _init_schema runs
    db.close()

    assert LEGACY_MISSING_COLUMN in _columns(old_schema_db, "files"), (
        "the read-write path no longer migrates; this test's sibling would "
        "then pass for the wrong reason"
    )


# --------------------------------------------------------------------------
# Guard against drift
# --------------------------------------------------------------------------

def test_every_cli_command_is_classified():
    """A new command must be consciously placed on the read or write side."""
    known = set(REPORTING_COMMANDS) | set(WRITE_COMMANDS)
    # `serve` runs the web app and owns its own connection lifecycle.
    known.add("serve")
    actual = set(cli.commands)
    unclassified = actual - known
    assert not unclassified, (
        f"CLI commands not classified as reporting or writing: "
        f"{sorted(unclassified)}. Add them to REPORTING_COMMANDS or "
        f"WRITE_COMMANDS in this file and decide whether they need "
        f"read_only=True."
    )
