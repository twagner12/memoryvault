"""Source wizard blueprint — add folders and zips via guided workflow."""

import os
from pathlib import Path

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for

bp = Blueprint("sources", __name__)


@bp.route("/new")
def wizard():
    home_dir = os.path.expanduser("~")
    return render_template("sources/wizard.html", home_dir=home_dir)


@bp.route("/scan", methods=["POST"])
def scan():
    folder = request.form.get("folder", "").strip()
    source = request.form.get("source", "local").strip()

    if not folder:
        flash("Please enter a folder path.", "error")
        return redirect(url_for("sources.wizard"))

    folder_path = Path(folder)
    if not folder_path.exists():
        flash(f"Folder not found: {folder}", "error")
        return redirect(url_for("sources.wizard"))
    if not folder_path.is_dir():
        flash(f"Not a folder: {folder}", "error")
        return redirect(url_for("sources.wizard"))

    task_manager = current_app.config["TASK_MANAGER"]
    db_path = str(current_app.config["DB_PATH"])
    task_id = task_manager.submit_scan(folder, db_path, source)

    return redirect(url_for("sources.progress", task_id=task_id))


@bp.route("/ingest", methods=["POST"])
def ingest():
    archive = request.form.get("archive", "").strip()
    dest = request.form.get("dest", "").strip()
    title = request.form.get("title", "").strip() or None

    if not archive:
        flash("Please enter a zip file path.", "error")
        return redirect(url_for("sources.wizard"))
    if not dest:
        flash("Please enter a destination folder.", "error")
        return redirect(url_for("sources.wizard"))

    archive_path = Path(archive)
    if not archive_path.exists():
        flash(f"File not found: {archive}", "error")
        return redirect(url_for("sources.wizard"))
    if not archive_path.is_file():
        flash(f"Not a file: {archive}", "error")
        return redirect(url_for("sources.wizard"))

    # Check for duplicate zip — by fingerprint or by file path
    from memoryvault.web import get_db
    from memoryvault.hasher import hash_bytes
    db = get_db()

    # Check by file path first
    existing = db.conn.execute(
        "SELECT path, title, status, completed_at FROM archives WHERE path = ? AND status = 'complete'",
        (str(archive_path.resolve()),)
    ).fetchone()

    # Then check by fingerprint (catches renamed/moved copies)
    if not existing:
        file_size = archive_path.stat().st_size
        with open(archive_path, "rb") as f:
            head = f.read(1_048_576)
        fingerprint = hash_bytes(head + str(file_size).encode())
        existing = db.conn.execute(
            "SELECT path, title, status, completed_at FROM archives WHERE blake3 = ? AND status = 'complete'",
            (fingerprint,)
        ).fetchone()

    if existing:
        existing = dict(existing)
        name = existing.get("title") or Path(existing["path"]).name
        when = existing["completed_at"][:16].replace("T", " ") if existing.get("completed_at") else "unknown"
        flash(f"This file was already processed as \"{name}\" on {when}.", "error")
        return redirect(url_for("sources.wizard"))

    task_manager = current_app.config["TASK_MANAGER"]
    db_path = str(current_app.config["DB_PATH"])
    task_id = task_manager.submit_ingest(archive, dest, db_path, title=title)

    return redirect(url_for("sources.progress", task_id=task_id))


@bp.route("/progress/<task_id>")
def progress(task_id):
    task_manager = current_app.config["TASK_MANAGER"]
    task = task_manager.get_task(task_id)
    if not task:
        flash("Task not found.", "error")
        return redirect(url_for("dashboard.index"))

    return render_template("sources/progress.html", task_id=task_id, task=task)


@bp.route("/summary/<task_id>")
def summary(task_id):
    task_manager = current_app.config["TASK_MANAGER"]
    task = task_manager.get_task(task_id)
    if not task:
        flash("Task not found.", "error")
        return redirect(url_for("dashboard.index"))

    return render_template("sources/summary.html", task=task)


@bp.route("/archive/<int:archive_id>")
def archive_detail(archive_id):
    from memoryvault.web import get_db
    db = get_db()

    archive = db.conn.execute("SELECT * FROM archives WHERE id = ?", (archive_id,)).fetchone()
    if not archive:
        flash("Archive not found.", "error")
        return redirect(url_for("dashboard.index"))

    archive = dict(archive)

    # Get entry breakdown
    breakdown = db.conn.execute("""
        SELECT
            COALESCE(skip_reason, status) as reason,
            COUNT(*) as count
        FROM archive_entries
        WHERE archive_id = ?
        GROUP BY reason
    """, (archive_id,)).fetchall()
    breakdown = {r["reason"]: r["count"] for r in breakdown}

    # Get kept files total size
    kept_size = db.conn.execute("""
        SELECT COALESCE(SUM(f.size), 0) as total
        FROM archive_entries ae
        JOIN files f ON f.path = ae.kept_path
        WHERE ae.archive_id = ? AND ae.status = 'kept'
    """, (archive_id,)).fetchone()["total"]

    # Get metadata merge count
    merge_count = db.conn.execute("""
        SELECT COUNT(*) as cnt FROM metadata_log
        WHERE source_desc LIKE 'takeout:%'
        AND target_path IN (SELECT kept_path FROM archive_entries WHERE archive_id = ?)
    """, (archive_id,)).fetchone()["cnt"]

    return render_template("sources/archive_detail.html",
                           archive=archive,
                           breakdown=breakdown,
                           kept_size=kept_size,
                           merge_count=merge_count)
