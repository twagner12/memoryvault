"""API blueprint — JSON and SSE endpoints."""

import json
import time

from flask import Blueprint, Response, abort, current_app, jsonify, send_file

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
