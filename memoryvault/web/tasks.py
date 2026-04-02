"""Background task manager for scans and ingests."""

import uuid
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from memoryvault.database import Database
from memoryvault.scanner import scan_folder
from memoryvault.ingest import ingest_archive


@dataclass
class TaskInfo:
    id: str
    type: str                         # "scan" or "ingest"
    status: str = "pending"           # pending, running, complete, error
    progress: dict = field(default_factory=dict)
    result: dict | None = None
    error: str | None = None
    created_at: str = ""
    events: list = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add_event(self, data: dict):
        with self._lock:
            self.events.append(data)

    def get_events_since(self, index: int) -> list:
        with self._lock:
            return self.events[index:]


class TaskManager:
    def __init__(self):
        self._executor = ThreadPoolExecutor(max_workers=1)
        self._tasks: dict[str, TaskInfo] = {}

    def submit_scan(self, folder: str, db_path: str, source: str = "local") -> str:
        task_id = str(uuid.uuid4())[:8]
        task = TaskInfo(
            id=task_id,
            type="scan",
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        self._tasks[task_id] = task
        self._executor.submit(self._run_scan, task, folder, db_path, source)
        return task_id

    def submit_ingest(self, archive: str, dest: str, db_path: str) -> str:
        task_id = str(uuid.uuid4())[:8]
        task = TaskInfo(
            id=task_id,
            type="ingest",
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        self._tasks[task_id] = task
        self._executor.submit(self._run_ingest, task, archive, dest, db_path)
        return task_id

    def get_task(self, task_id: str) -> TaskInfo | None:
        return self._tasks.get(task_id)

    def get_recent_tasks(self, limit: int = 20) -> list[TaskInfo]:
        tasks = sorted(self._tasks.values(), key=lambda t: t.created_at, reverse=True)
        return tasks[:limit]

    def _run_scan(self, task: TaskInfo, folder: str, db_path: str, source: str):
        import time
        start_time = time.time()
        task.status = "running"
        task.add_event({"stage": "started", "type": "scan", "folder": folder})

        def progress(stage, total, current, error=None):
            elapsed = time.time() - start_time
            task.progress = {"stage": stage, "total": total, "current": current,
                             "elapsed": round(elapsed, 1)}
            task.add_event({
                "stage": stage, "total": total, "current": current,
                "error": error, "elapsed": round(elapsed, 1),
            })

        try:
            db = Database(Path(db_path))
            count = scan_folder(Path(folder), db, source=source, progress_callback=progress)
            db.close()
            elapsed = round(time.time() - start_time, 1)
            task.result = {"scanned": count, "elapsed": elapsed}
            task.status = "complete"
            task.add_event({"stage": "complete", "scanned": count, "elapsed": elapsed})
        except Exception as e:
            task.error = str(e)
            task.status = "error"
            task.add_event({"stage": "error", "error": str(e)})

    def _run_ingest(self, task: TaskInfo, archive: str, dest: str, db_path: str):
        import time
        start_time = time.time()
        task.status = "running"
        task.add_event({"stage": "started", "type": "ingest", "archive": archive})

        def progress(stage, **kwargs):
            elapsed = time.time() - start_time
            task.progress = {"stage": stage, "elapsed": round(elapsed, 1), **kwargs}
            task.add_event({"stage": stage, "elapsed": round(elapsed, 1), **kwargs})

        try:
            db = Database(Path(db_path))
            stats = ingest_archive(Path(archive), Path(dest), db, progress_callback=progress)
            db.close()
            elapsed = round(time.time() - start_time, 1)
            stats["elapsed"] = elapsed
            task.result = stats
            task.status = "complete"
            task.add_event({"stage": "complete", **stats})
        except Exception as e:
            task.error = str(e)
            task.status = "error"
            task.add_event({"stage": "error", "error": str(e)})
