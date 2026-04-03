"""Dashboard blueprint — stats overview and quick actions."""

from flask import Blueprint, render_template

from memoryvault.web import get_db

bp = Blueprint("dashboard", __name__)


@bp.route("/")
def index():
    db = get_db()
    total_files = db.file_count()
    dupe_groups = db.find_duplicate_groups()
    dupe_count = sum(len(g) - 1 for g in dupe_groups)
    wasted_bytes = sum(sum(f["size"] for f in g[1:]) for g in dupe_groups) if dupe_groups else 0

    # Duplicates avoided during imports — count and space saved
    dupes_avoided = db.conn.execute(
        "SELECT COUNT(*) as cnt FROM archive_entries WHERE skip_reason = 'duplicate'"
    ).fetchone()["cnt"]

    # Space saved: average size of kept files * number of duplicates avoided
    # (skipped duplicates are the same size as what they matched)
    avg_size_row = db.conn.execute(
        "SELECT AVG(size) as avg_size FROM files"
    ).fetchone()
    space_saved_bytes = int(dupes_avoided * (avg_size_row["avg_size"] or 0))

    # Recent archives — only show completed ones
    archives = db.conn.execute(
        "SELECT * FROM archives WHERE status = 'complete' ORDER BY id DESC"
    ).fetchall()

    return render_template("dashboard.html",
                           total_files=total_files,
                           dupe_groups=len(dupe_groups),
                           dupe_count=dupe_count,
                           wasted_gb=wasted_bytes / (1024 ** 3),
                           dupes_avoided=dupes_avoided,
                           space_saved_gb=space_saved_bytes / (1024 ** 3),
                           archives=[dict(a) for a in archives])
