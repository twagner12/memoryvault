"""Source wizard blueprint — add folders and zips via guided workflow."""

from pathlib import Path

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for

bp = Blueprint("sources", __name__)


@bp.route("/new")
def wizard():
    return render_template("sources/wizard.html")


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

    task_manager = current_app.config["TASK_MANAGER"]
    db_path = str(current_app.config["DB_PATH"])
    task_id = task_manager.submit_ingest(archive, dest, db_path)

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
