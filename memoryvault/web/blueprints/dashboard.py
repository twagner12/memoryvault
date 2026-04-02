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

    # Recent archives
    archives = db.conn.execute(
        "SELECT * FROM archives ORDER BY id DESC LIMIT 5"
    ).fetchall()

    return render_template("dashboard.html",
                           total_files=total_files,
                           dupe_groups=len(dupe_groups),
                           dupe_count=dupe_count,
                           wasted_gb=wasted_bytes / (1024 ** 3),
                           archives=[dict(a) for a in archives])
