"""API blueprint — JSON and SSE endpoints."""

import json
import time

from flask import Blueprint, Response, abort, current_app, jsonify, request, send_file

from memoryvault.web import get_db
from memoryvault.web.thumbnails import get_or_create_thumbnail

bp = Blueprint("api", __name__)


@bp.route("/stats")
def stats():
    db = get_db()
    total_files = db.file_count()
    dupe_groups = db.find_duplicate_groups()
    dupe_count = sum(len(g) - 1 for g in dupe_groups)
    wasted_bytes = sum(sum(f["size"] for f in g[1:]) for g in dupe_groups) if dupe_groups else 0

    return jsonify({
        "total_files": total_files,
        "duplicate_groups": len(dupe_groups),
        "extra_copies": dupe_count,
        "wasted_gb": round(wasted_bytes / (1024 ** 3), 1),
    })


@bp.route("/tasks/<task_id>")
def task_status(task_id):
    task_manager = current_app.config["TASK_MANAGER"]
    task = task_manager.get_task(task_id)
    if not task:
        return jsonify({"error": "Task not found"}), 404

    return jsonify({
        "id": task.id,
        "type": task.type,
        "status": task.status,
        "progress": task.progress,
        "result": task.result,
        "error": task.error,
    })


@bp.route("/tasks/<task_id>/events")
def task_events(task_id):
    """Server-Sent Events stream for task progress."""
    task_manager = current_app.config["TASK_MANAGER"]

    def generate():
        last_idx = 0
        while True:
            task = task_manager.get_task(task_id)
            if not task:
                yield f"event: error\ndata: {json.dumps({'error': 'Task not found'})}\n\n"
                break

            events = task.get_events_since(last_idx)
            for event in events:
                yield f"data: {json.dumps(event)}\n\n"
                last_idx += 1

            if task.status in ("complete", "error"):
                result = task.result or {"error": task.error}
                yield f"event: done\ndata: {json.dumps(result)}\n\n"
                break

            time.sleep(0.5)

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@bp.route("/browse")
def browse():
    """Browse the local filesystem. Returns JSON list of entries."""
    import os
    path = request.args.get("path", os.path.expanduser("~"))
    mode = request.args.get("mode", "folder")  # "folder" or "file"

    path = os.path.abspath(path)
    if not os.path.exists(path):
        return jsonify({"error": "Path not found", "path": path}), 404
    if not os.path.isdir(path):
        path = os.path.dirname(path)

    entries = []
    try:
        for name in sorted(os.listdir(path)):
            full = os.path.join(path, name)
            if name.startswith("."):
                continue
            try:
                is_dir = os.path.isdir(full)
                if mode == "folder" and not is_dir:
                    continue
                if mode == "file" and is_dir:
                    entries.append({"name": name, "path": full, "type": "folder"})
                    continue
                if mode == "file" and not is_dir:
                    ext = os.path.splitext(name)[1].lower()
                    if ext not in (".zip", ".7z", ".tar", ".gz", ".tgz"):
                        continue
                size = os.path.getsize(full) if not is_dir else None
                entries.append({
                    "name": name,
                    "path": full,
                    "type": "folder" if is_dir else "file",
                    "size": size,
                })
            except PermissionError:
                continue
    except PermissionError:
        return jsonify({"error": "Permission denied", "path": path}), 403

    # Add parent directory entry
    parent = os.path.dirname(path)
    return jsonify({
        "path": path,
        "parent": parent if parent != path else None,
        "entries": entries,
    })


@bp.route("/archives/<int:archive_id>/title", methods=["POST"])
def update_archive_title(archive_id):
    """Update an archive's title."""
    db = get_db()
    data = request.get_json()
    title = data.get("title", "").strip()

    row = db.conn.execute("SELECT id FROM archives WHERE id = ?", (archive_id,)).fetchone()
    if not row:
        return jsonify({"error": "Archive not found"}), 404

    db.conn.execute("UPDATE archives SET title = ? WHERE id = ?", (title or None, archive_id))
    db.conn.commit()
    return jsonify({"ok": True, "title": title})


@bp.route("/mkdir", methods=["POST"])
def mkdir():
    """Create a new folder."""
    import os
    data = request.get_json()
    parent = data.get("parent", "")
    name = data.get("name", "").strip()

    if not parent or not name:
        return jsonify({"error": "Parent path and folder name are required"}), 400

    if "/" in name or "\\" in name or name.startswith("."):
        return jsonify({"error": "Invalid folder name"}), 400

    new_path = os.path.join(os.path.abspath(parent), name)
    try:
        os.makedirs(new_path, exist_ok=True)
        return jsonify({"path": new_path})
    except OSError as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/thumbnail/<int:file_id>")
def thumbnail(file_id):
    """Serve a thumbnail for a file by its database ID."""
    db = get_db()
    row = db.conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
    if not row:
        abort(404)

    file_record = dict(row)
    thumb_dir = current_app.config["THUMB_DIR"]
    thumb_path = get_or_create_thumbnail(
        file_record["path"], file_record.get("blake3_full"), thumb_dir
    )

    if thumb_path and thumb_path.exists():
        return send_file(thumb_path, mimetype="image/jpeg",
                         max_age=86400)

    # Return a 1x1 transparent pixel as fallback
    abort(404)
