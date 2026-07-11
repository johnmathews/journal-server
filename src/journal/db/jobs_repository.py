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

from journal.db.factory import ConnectionFactory
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
        status_detail=row["status_detail"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        user_id=row["user_id"],
        input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"],
        cost_usd=row["cost_usd"],
    )


class SQLiteJobRepository:
    """Repository for async batch jobs backed by SQLite.

    Construction takes a :class:`ConnectionFactory`. Each thread that
    calls a method gets its own ``sqlite3.Connection`` via
    ``threading.local`` inside the factory, so the shared-state commit
    race documented in ``docs/archive/sqlite-per-thread-connections-plan.md``
    is structurally impossible.
    """

    def __init__(self, factory: ConnectionFactory) -> None:
        self._factory = factory

    def _conn(self) -> sqlite3.Connection:
        return self._factory.get()

    @property
    def connection(self) -> sqlite3.Connection:
        """Underlying SQLite connection for the calling thread.

        Exposed for test setup / post-write assertions / operational
        diagnostics. Returns the calling thread's connection;
        cross-thread inspection from a test thread sees committed
        state via WAL — exactly what tests want.
        """
        return self._factory.get()

    def create(
        self,
        job_type: str,
        params: dict[str, Any],
        user_id: int | None = None,
    ) -> Job:
        job_id = str(uuid.uuid4())
        created_at = _now_iso()
        params_json = json.dumps(params)
        conn = self._conn()
        conn.execute(
            "INSERT INTO jobs ("
            "id, type, status, params_json, progress_current, progress_total, "
            "result_json, error_message, status_detail, created_at, started_at, "
            "finished_at, user_id"
            ") VALUES (?, ?, 'queued', ?, 0, 0, NULL, NULL, NULL, ?, NULL, NULL, ?)",
            (job_id, job_type, params_json, created_at, user_id),
        )
        conn.commit()
        log.info("Created job %s of type %s", job_id, job_type)
        job = self.get(job_id)
        assert job is not None  # row was just inserted
        return job

    def mark_running(self, job_id: str) -> None:
        started_at = _now_iso()
        conn = self._conn()
        conn.execute(
            "UPDATE jobs SET status = 'running', started_at = ? WHERE id = ?",
            (started_at, job_id),
        )
        conn.commit()
        log.info("Job %s -> running", job_id)

    def update_progress(self, job_id: str, current: int, total: int) -> None:
        conn = self._conn()
        conn.execute(
            "UPDATE jobs SET progress_current = ?, progress_total = ? WHERE id = ?",
            (current, total, job_id),
        )
        conn.commit()

    def update_status_detail(self, job_id: str, detail: str | None) -> None:
        conn = self._conn()
        conn.execute(
            "UPDATE jobs SET status_detail = ? WHERE id = ?",
            (detail, job_id),
        )
        conn.commit()

    def mark_succeeded(self, job_id: str, result: dict[str, Any]) -> None:
        finished_at = _now_iso()
        result_json = json.dumps(result)
        conn = self._conn()
        conn.execute(
            "UPDATE jobs SET status = 'succeeded', result_json = ?, "
            "status_detail = NULL, finished_at = ? WHERE id = ?",
            (result_json, finished_at, job_id),
        )
        conn.commit()
        log.info("Job %s -> succeeded", job_id)

    def mark_failed(self, job_id: str, error_message: str) -> None:
        finished_at = _now_iso()
        conn = self._conn()
        conn.execute(
            "UPDATE jobs SET status = 'failed', error_message = ?, "
            "status_detail = NULL, finished_at = ? WHERE id = ?",
            (error_message, finished_at, job_id),
        )
        conn.commit()
        log.warning("Job %s -> failed: %s", job_id, error_message)

    def record_usage(
        self,
        job_id: str,
        input_tokens: int | None,
        output_tokens: int | None,
        cost_usd: float | None,
    ) -> None:
        """Persist per-job LLM token usage (and eventually dollar cost).

        Called by the runner's usage-collection shim AFTER the worker's own
        ``mark_succeeded`` / ``mark_failed`` — so this is a follow-up UPDATE
        that runs for FAILED jobs too. Kept separate from the ``mark_*``
        transitions deliberately: usage is orthogonal to lifecycle, and the
        collector total isn't known until the whole call stack unwinds.
        ``cost_usd`` is ``None`` in W2; W3 will populate it.
        """
        conn = self._conn()
        conn.execute(
            "UPDATE jobs SET input_tokens = ?, output_tokens = ?, "
            "cost_usd = ? WHERE id = ?",
            (input_tokens, output_tokens, cost_usd, job_id),
        )
        conn.commit()
        log.info(
            "Job %s usage recorded (input=%s, output=%s, cost=%s)",
            job_id, input_tokens, output_tokens, cost_usd,
        )

    def get(self, job_id: str, user_id: int | None = None) -> Job | None:
        sql = "SELECT * FROM jobs WHERE id = ?"
        params: list[str | int] = [job_id]
        if user_id is not None:
            sql += " AND user_id = ?"
            params.append(user_id)
        conn = self._conn()
        row = conn.execute(sql, params).fetchone()
        return _row_to_job(row) if row else None

    def list_jobs(
        self,
        *,
        status: str | None = None,
        job_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
        user_id: int | None = None,
    ) -> tuple[list[Job], int]:
        """Return jobs ordered by created_at DESC with optional filters.

        Returns ``(jobs, total)`` where *total* is the unfiltered count
        matching the filters (before limit/offset), for pagination.
        """
        where_clauses: list[str] = []
        params: list[str | int] = []
        if status is not None:
            where_clauses.append("status = ?")
            params.append(status)
        if job_type is not None:
            where_clauses.append("type = ?")
            params.append(job_type)
        if user_id is not None:
            where_clauses.append("user_id = ?")
            params.append(user_id)

        where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

        conn = self._conn()
        total_row = conn.execute(
            f"SELECT COUNT(*) FROM jobs{where_sql}", params,
        ).fetchone()
        total: int = total_row[0] if total_row else 0

        rows = conn.execute(
            f"SELECT * FROM jobs{where_sql} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()

        return [_row_to_job(r) for r in rows], total

    def try_acquire_notification_lock(self, parent_job_id: str) -> bool:
        """Atomically claim the right to send a pipeline notification.

        Sets ``result_json._notification_sent = 1`` if it isn't
        already set. Returns True if this caller acquired the lock
        (and should fire the consolidated Pushover); returns False if
        another caller already acquired it.

        The UPDATE's WHERE clause performs the atomic check-and-set
        — concurrent callers race on the SQLite write lock, but only
        the first one will see the field absent and update it; later
        callers find it present and the UPDATE matches zero rows.
        """
        conn = self._conn()
        cursor = conn.execute(
            "UPDATE jobs SET result_json = json_set("
            "result_json, '$._notification_sent', 1"
            ") "
            "WHERE id = ? "
            "AND result_json IS NOT NULL "
            "AND json_extract(result_json, '$._notification_sent') IS NULL",
            (parent_job_id,),
        )
        conn.commit()
        return cursor.rowcount == 1

    def find_active_fitness_fetch_job(
        self, *, user_id: int, source: str,
    ) -> Job | None:
        """Return the in-flight fetch job for ``(user_id, source)`` or ``None``.

        "Fetch" spans both worker classes that read from the upstream
        provider — ``fitness_sync_{source}`` and
        ``fitness_backfill_{source}``. The W5 idempotency policy
        permits only one such job per ``(user_id, source)`` at a time,
        so this method is the single source of truth all submit paths
        (REST endpoint, MCP tool) consult before enqueueing.

        Ordered by ``created_at ASC`` so that if (by race) multiple
        rows exist, the *oldest* one is returned — that's the winner
        per the "first enqueued wins" policy.
        """
        sync_type = f"fitness_sync_{source}"
        backfill_type = f"fitness_backfill_{source}"
        conn = self._conn()
        row = conn.execute(
            "SELECT * FROM jobs"
            " WHERE user_id = ?"
            "   AND status IN ('queued', 'running')"
            "   AND type IN (?, ?)"
            " ORDER BY created_at ASC"
            " LIMIT 1",
            (user_id, sync_type, backfill_type),
        ).fetchone()
        return _row_to_job(row) if row else None

    def find_pending_open_regeneration(
        self, *, user_id: int, storyline_id: int,
    ) -> Job | None:
        """Return a QUEUED full-refresh ``storyline_generation`` for this
        storyline, or ``None``.

        Used to coalesce auto-regenerations during a burst ingest: if a
        plain open-chapter refresh for this storyline is already queued
        (not yet running), it will pick up the just-ingested entry when it
        runs, so queuing a second regeneration is redundant. Only
        ``queued`` jobs qualify — a ``running`` one may have already
        selected its source window, so a fresh regeneration is still
        needed to include newer entries. Chapter-scoped, date-ranged, and
        resegment jobs are excluded because they may not cover the new
        entry.
        """
        conn = self._conn()
        row = conn.execute(
            "SELECT * FROM jobs"
            " WHERE user_id = ?"
            "   AND status = 'queued'"
            "   AND type = 'storyline_generation'"
            "   AND json_extract(params_json, '$.storyline_id') = ?"
            "   AND json_extract(params_json, '$.chapter_id') IS NULL"
            "   AND json_extract(params_json, '$.start_date') IS NULL"
            "   AND json_extract(params_json, '$.end_date') IS NULL"
            "   AND json_extract(params_json, '$.resegment') IS NULL"
            " ORDER BY created_at ASC"
            " LIMIT 1",
            (user_id, storyline_id),
        ).fetchone()
        return _row_to_job(row) if row else None

    def has_active_jobs_for_entry(self, entry_id: int) -> list[Job]:
        """Return queued/running jobs whose params reference *entry_id*.

        Used by the delete-entry endpoint to prevent deletion while
        background jobs are still operating on the entry.
        """
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM jobs"
            " WHERE status IN ('queued', 'running')"
            " AND json_extract(params_json, '$.entry_id') = ?",
            (entry_id,),
        ).fetchall()
        return [_row_to_job(r) for r in rows]

    def reconcile_stuck_jobs(self) -> int:
        """Fail any jobs left queued/running from a previous process.

        Run once at server startup. Jobs do not resume across
        processes, so the honest thing to do on restart is mark any
        non-terminal rows as failed with a diagnostic error message
        and a finished_at timestamp. Returns the number of rows
        touched.
        """
        finished_at = _now_iso()
        conn = self._conn()
        cursor = conn.execute(
            "UPDATE jobs SET status = 'failed', "
            "error_message = 'server restarted before job completed', "
            "finished_at = ? "
            "WHERE status IN ('queued', 'running')",
            (finished_at,),
        )
        conn.commit()
        count = cursor.rowcount
        if count:
            log.warning("Reconciled %d stuck job(s) to failed", count)
        return count
