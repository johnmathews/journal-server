"""SQLite repository for async batch jobs.

The JobRepository is the data layer for the `jobs` table. It owns
UUID generation, JSON (de)serialisation of params/result, and
timestamp bookkeeping for state transitions. Callers pass in plain
Python dicts and get a `Job` dataclass back — they never see the
raw `params_json` / `result_json` columns.

Typical lifecycle, driven by the JobRunner service:

    repo.create(job_type, params)        # -> 'queued'
    repo.mark_running(job_id)            # -> 'running', started_at set
    repo.update_progress(job_id, c, t)   # called periodically
    repo.mark_succeeded(job_id, result)  # -> 'succeeded', finished_at set
    #   (or)
    repo.mark_failed(job_id, error_msg)  # -> 'failed', finished_at set

`reconcile_stuck_jobs` is a startup hook that sweeps any row left in
a non-terminal state by a previous process — jobs do not resume
across server restarts.
"""

import json
import logging
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any

from journal.models import Job

log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _row_to_job(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"],
        type=row["type"],
        status=row["status"],
        params=json.loads(row["params_json"]),
        progress_current=row["progress_current"],
        progress_total=row["progress_total"],
        result=json.loads(row["result_json"]) if row["result_json"] is not None else None,
        error_message=row["error_message"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


class SQLiteJobRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def create(self, job_type: str, params: dict[str, Any]) -> Job:
        job_id = str(uuid.uuid4())
        created_at = _now_iso()
        params_json = json.dumps(params)
        self._conn.execute(
            "INSERT INTO jobs ("
            "id, type, status, params_json, progress_current, progress_total, "
            "result_json, error_message, created_at, started_at, finished_at"
            ") VALUES (?, ?, 'queued', ?, 0, 0, NULL, NULL, ?, NULL, NULL)",
            (job_id, job_type, params_json, created_at),
        )
        self._conn.commit()
        log.info("Created job %s of type %s", job_id, job_type)
        job = self.get(job_id)
        assert job is not None  # row was just inserted
        return job

    def mark_running(self, job_id: str) -> None:
        started_at = _now_iso()
        self._conn.execute(
            "UPDATE jobs SET status = 'running', started_at = ? WHERE id = ?",
            (started_at, job_id),
        )
        self._conn.commit()
        log.info("Job %s -> running", job_id)

    def update_progress(self, job_id: str, current: int, total: int) -> None:
        self._conn.execute(
            "UPDATE jobs SET progress_current = ?, progress_total = ? WHERE id = ?",
            (current, total, job_id),
        )
        self._conn.commit()

    def mark_succeeded(self, job_id: str, result: dict[str, Any]) -> None:
        finished_at = _now_iso()
        result_json = json.dumps(result)
        self._conn.execute(
            "UPDATE jobs SET status = 'succeeded', result_json = ?, "
            "finished_at = ? WHERE id = ?",
            (result_json, finished_at, job_id),
        )
        self._conn.commit()
        log.info("Job %s -> succeeded", job_id)

    def mark_failed(self, job_id: str, error_message: str) -> None:
        finished_at = _now_iso()
        self._conn.execute(
            "UPDATE jobs SET status = 'failed', error_message = ?, "
            "finished_at = ? WHERE id = ?",
            (error_message, finished_at, job_id),
        )
        self._conn.commit()
        log.warning("Job %s -> failed: %s", job_id, error_message)

    def get(self, job_id: str) -> Job | None:
        row = self._conn.execute(
            "SELECT * FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        return _row_to_job(row) if row else None

    def reconcile_stuck_jobs(self) -> int:
        """Fail any jobs left queued/running from a previous process.

        Run once at server startup. Jobs do not resume across
        processes, so the honest thing to do on restart is mark any
        non-terminal rows as failed with a diagnostic error message
        and a finished_at timestamp. Returns the number of rows
        touched.
        """
        finished_at = _now_iso()
        cursor = self._conn.execute(
            "UPDATE jobs SET status = 'failed', "
            "error_message = 'server restarted before job completed', "
            "finished_at = ? "
            "WHERE status IN ('queued', 'running')",
            (finished_at,),
        )
        self._conn.commit()
        count = cursor.rowcount
        if count:
            log.warning("Reconciled %d stuck job(s) to failed", count)
        return count
