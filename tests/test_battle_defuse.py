"""Finding #2 (defuse) — stop auto-resolving every duplicate group.

`battle.index` resolved every unresolved group at confidence=100 before
checking whether any remained, so the review arena was unreachable and the
`resolutions` table filled with unreviewed machine verdicts. Only
unambiguous groups — identical size, one distinct extension — may
auto-resolve now; anything else waits for a human.
"""

import pytest

from memoryvault.database import Database
from memoryvault.web import create_app


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "test.db"
    Database(db_path).close()
    app = create_app(db_path=str(db_path))
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c, db_path


def add_group(db_path, blake3, files):
    db = Database(db_path)
    for path, size in files:
        db.upsert_file(path=path, size=size, blake3_full=blake3,
                       blake3_head=blake3, source="local")
    db.close()


class TestAutoResolveIsNarrow:
    def test_mixed_extension_group_stays_unresolved(self, client):
        c, db_path = client
        add_group(db_path, "hash_mixed", [
            ("/vault/photo.jpg", 1000),
            ("/vault/photo.heic", 1000),
        ])

        c.get("/battle/")

        db = Database(db_path)
        assert not db.is_resolved("hash_mixed"), \
            "mixed-extension group was auto-resolved without review"
        assert len(db.find_unresolved_duplicate_groups()) == 1
        db.close()

    def test_mixed_size_group_stays_unresolved(self, client):
        c, db_path = client
        add_group(db_path, "hash_sizes", [
            ("/vault/a.jpg", 1000),
            ("/vault/b.jpg", 2000),
        ])

        c.get("/battle/")

        db = Database(db_path)
        assert not db.is_resolved("hash_sizes")
        db.close()

    def test_identical_group_is_auto_resolved(self, client):
        c, db_path = client
        add_group(db_path, "hash_same", [
            ("/vault/a.jpg", 1000),
            ("/vault/copy_of_a.jpg", 1000),
        ])

        c.get("/battle/")

        db = Database(db_path)
        assert db.is_resolved("hash_same"), \
            "an unambiguous group should still auto-resolve"
        row = db.conn.execute(
            "SELECT auto_resolved, stale FROM resolutions WHERE blake3_full = 'hash_same'"
        ).fetchone()
        assert row["auto_resolved"]
        assert not row["stale"], "newly-made decisions are not stale"
        db.close()

    def test_extension_comparison_ignores_case(self, client):
        c, db_path = client
        add_group(db_path, "hash_case", [
            ("/vault/a.JPG", 1000),
            ("/vault/b.jpg", 1000),
        ])

        c.get("/battle/")

        db = Database(db_path)
        assert db.is_resolved("hash_case")
        db.close()

    def test_index_redirects_to_arena_when_work_remains(self, client):
        c, db_path = client
        add_group(db_path, "hash_mixed", [
            ("/vault/photo.jpg", 1000),
            ("/vault/photo.heic", 1000),
        ])

        response = c.get("/battle/")

        assert response.status_code == 302
        assert "/battle/arena" in response.headers["Location"]


class TestStaleResolutionMigration:
    def test_existing_auto_resolutions_are_marked_stale(self, tmp_path):
        from memoryvault.database import mark_auto_resolutions_stale

        db_path = tmp_path / "legacy.db"
        db = Database(db_path)
        for i in range(3):
            db.resolve_group(f"hash{i}", f"/vault/{i}.jpg", "keep_winner",
                             auto_resolved=True)
        db.resolve_group("manual", "/vault/m.jpg", "keep_winner", auto_resolved=False)
        db.close()

        db = Database(db_path)
        marked = mark_auto_resolutions_stale(db)
        assert marked == 3

        stale = db.conn.execute("SELECT COUNT(*) c FROM resolutions WHERE stale = 1").fetchone()["c"]
        assert stale == 3
        fresh = db.conn.execute(
            "SELECT stale FROM resolutions WHERE blake3_full = 'manual'"
        ).fetchone()["stale"]
        assert not fresh, "human decisions must not be invalidated"
        db.close()

    def test_migration_is_idempotent(self, tmp_path):
        from memoryvault.database import mark_auto_resolutions_stale

        db_path = tmp_path / "legacy.db"
        db = Database(db_path)
        db.resolve_group("hash0", "/vault/0.jpg", "keep_winner", auto_resolved=True)
        db.close()

        db = Database(db_path)
        assert mark_auto_resolutions_stale(db) == 1
        assert mark_auto_resolutions_stale(db) == 0, "already-stale rows re-marked"
        db.close()

    def test_stale_rows_are_not_treated_as_resolved(self, tmp_path):
        from memoryvault.database import mark_auto_resolutions_stale

        db_path = tmp_path / "legacy.db"
        db = Database(db_path)
        db.upsert_file(path="/vault/a.jpg", size=1, blake3_full="h", source="local")
        db.upsert_file(path="/vault/b.heic", size=2, blake3_full="h", source="local")
        db.resolve_group("h", "/vault/a.jpg", "keep_winner", auto_resolved=True)
        assert db.is_resolved("h")

        mark_auto_resolutions_stale(db)

        assert not db.is_resolved("h"), "a stale verdict must not count as reviewed"
        assert len(db.find_unresolved_duplicate_groups()) == 1
        db.close()
