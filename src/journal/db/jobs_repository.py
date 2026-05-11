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
import threading
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
    )


class SQLiteJobRepository:
    """Repository for async batch jobs backed by SQLite.

    Construction accepts either a :class:`ConnectionFactory` (preferred,
    used by production via ``mcp_server/bootstrap.py``) or a bare
    ``sqlite3.Connection`` (legacy, retained for tests that haven't been
    migrated to the factory model yet — see
    ``docs/sqlite-per-thread-connections-plan.md`` W3).

    On the **factory** path each thread that calls a method gets its
    own ``sqlite3.Connection`` via ``threading.local`` inside the
    factory, so the shared-state commit race documented in
    ``docs/sqlite-threading.md`` is structurally impossible. The
    ``_lock`` and ``_commit`` workaround below are no-ops on this path.

    On the **legacy connection** path the same ``Connection`` instance
    is shared across threads. The per-method ``threading.Lock``
    serialises ``execute`` + ``commit`` pairs within this repo, and
    ``_commit`` tolerates the ``no transaction is active`` race
    triggered by *other* repos sharing the connection. This path is
    deliberately less safe than the factory path and exists only as
    a migration ramp — new code should pass a ``ConnectionFactory``.
    """

    def __init__(
        self,
        factory_or_conn: ConnectionFactory | sqlite3.Connection,
    ) -> None:
        if isinstance(factory_or_conn, ConnectionFactory):
            self._factory: ConnectionFactory | None = factory_or_conn
            self._direct_conn: sqlite3.Connection | None = None
        else:
            self._factory = None
            self._direct_conn = factory_or_conn
            log.debug(
                "SQLiteJobRepository constructed with bare Connection "
                "(legacy path; see "
                "docs/sqlite-per-thread-connections-plan.md W3)",
            )
        self._lock = threading.Lock()

    def _conn(self) -> sqlite3.Connection:
        """Return the connection for the current call.

        Factory path: returns this thread's connection (lazily opened
        on first use). Legacy path: returns the single shared
        connection passed at construction.
        """
        if self._factory is not None:
            return self._factory.get()
        assert self._direct_conn is not None
        return self._direct_conn

    def _commit(self, conn: sqlite3.Connection, op: str) -> None:
        """Commit ``conn``, tolerating the legacy shared-connection
        commit race.

        On the factory path this is just a plain commit — each thread
        owns its connection, so the race condition this catches cannot
        happen. On the legacy connection path another repo may have
        ended this connection's implicit transaction between our
        ``execute()`` and ``commit()``; in that case the pending
        write was captured by the other repo's commit and the row is
        already persisted, so logging a warning and continuing is
        safe. See ``docs/sqlite-threading.md`` and
        ``docs/sqlite-per-thread-connections-plan.md``.
        """
        try:
            conn.commit()
        except sqlite3.OperationalError as exc:
            if "no transaction is active" not in str(exc):
                raise
            log.warning(
                "Shared-Connection commit race tolerated during %s "
                "(concurrent writer already committed) — see "
                "docs/sqlite-per-thread-connections-plan.md", op,
            )

    @property
    def connection(self) -> sqlite3.Connection:
        """Underlying SQLite connection for the current thread.

        Exposed for test setup / post-write assertions / operational
        diagnostics. On the factory path this returns the calling
        thread's connection (so cross-thread inspection from a test
        thread inspects the test thread's connection, which sees
        committed state via WAL — exactly what tests want). On the
        legacy path this returns the single shared connection.

        Production callers should prefer the named methods on this
        class; the property exists for tests.
        """
        return self._conn()

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
        with self._lock:
            conn.execute(
                "INSERT INTO jobs ("
                "id, type, status, params_json, progress_current, progress_total, "
                "result_json, error_message, status_detail, created_at, started_at, "
                "finished_at, user_id"
                ") VALUES (?, ?, 'queued', ?, 0, 0, NULL, NULL, NULL, ?, NULL, NULL, ?)",
                (job_id, job_type, params_json, created_at, user_id),
            )
            self._commit(conn, "create")
        log.info("Created job %s of type %s", job_id, job_type)
        job = self.get(job_id)
        assert job is not None  # row was just inserted
        return job

    def mark_running(self, job_id: str) -> None:
        started_at = _now_iso()
        conn = self._conn()
        with self._lock:
            conn.execute(
                "UPDATE jobs SET status = 'running', started_at = ? WHERE id = ?",
                (started_at, job_id),
            )
            self._commit(conn, "mark_running")
        log.info("Job %s -> running", job_id)

    def update_progress(self, job_id: str, current: int, total: int) -> None:
        conn = self._conn()
        with self._lock:
            conn.execute(
                "UPDATE jobs SET progress_current = ?, progress_total = ? WHERE id = ?",
                (current, total, job_id),
            )
            self._commit(conn, "update_progress")

    def update_status_detail(self, job_id: str, detail: str | None) -> None:
        conn = self._conn()
        with self._lock:
            conn.execute(
                "UPDATE jobs SET status_detail = ? WHERE id = ?",
                (detail, job_id),
            )
            self._commit(conn, "update_status_detail")

    def mark_succeeded(self, job_id: str, result: dict[str, Any]) -> None:
        finished_at = _now_iso()
        result_json = json.dumps(result)
        conn = self._conn()
        with self._lock:
            conn.execute(
                "UPDATE jobs SET status = 'succeeded', result_json = ?, "
                "status_detail = NULL, finished_at = ? WHERE id = ?",
                (result_json, finished_at, job_id),
            )
            self._commit(conn, "mark_succeeded")
        log.info("Job %s -> succeeded", job_id)

    def mark_failed(self, job_id: str, error_message: str) -> None:
        finished_at = _now_iso()
        conn = self._conn()
        with self._lock:
            conn.execute(
                "UPDATE jobs SET status = 'failed', error_message = ?, "
                "status_detail = NULL, finished_at = ? WHERE id = ?",
                (error_message, finished_at, job_id),
            )
            self._commit(conn, "mark_failed")
        log.warning("Job %s -> failed: %s", job_id, error_message)

    def get(self, job_id: str, user_id: int | None = None) -> Job | None:
        sql = "SELECT * FROM jobs WHERE id = ?"
        params: list[str | int] = [job_id]
        if user_id is not None:
            sql += " AND user_id = ?"
            params.append(user_id)
        conn = self._conn()
        with self._lock:
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
        with self._lock:
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
        with self._lock:
            cursor = conn.execute(
                "UPDATE jobs SET result_json = json_set("
                "result_json, '$._notification_sent', 1"
                ") "
                "WHERE id = ? "
                "AND result_json IS NOT NULL "
                "AND json_extract(result_json, '$._notification_sent') IS NULL",
                (parent_job_id,),
            )
            self._commit(conn, "try_acquire_notification_lock")
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
        with self._lock:
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

    def has_active_jobs_for_entry(self, entry_id: int) -> list[Job]:
        """Return queued/running jobs whose params reference *entry_id*.

        Used by the delete-entry endpoint to prevent deletion while
        background jobs are still operating on the entry.
        """
        conn = self._conn()
        with self._lock:
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
        with self._lock:
            cursor = conn.execute(
                "UPDATE jobs SET status = 'failed', "
                "error_message = 'server restarted before job completed', "
                "finished_at = ? "
                "WHERE status IN ('queued', 'running')",
                (finished_at,),
            )
            self._commit(conn, "reconcile_stuck_jobs")
            count = cursor.rowcount
        if count:
            log.warning("Reconciled %d stuck job(s) to failed", count)
        return count
