"""SQLite repository for the fitness pipeline.

One repository covers all four fitness namespaces (auth state, sync
runs, raw archive, normalized) because the surface is small and the
operations cluster naturally — splitting per-table would force the
caller to juggle four objects to perform one sync run. Mirrors the
``SQLiteJobRepository`` single-file pattern.

Schema: migrations 0023/0024/0025. Design: docs/fitness-schema.md.

`payload_sha256` for raw rows is computed inside ``insert_raw`` so
callers cannot accidentally use a different hash function. Time
columns are stored as ISO 8601 UTC strings using the same clock as the
SQL DEFAULT clauses (``strftime('%Y-%m-%dT%H:%M:%SZ', 'now')``); see
``_now_iso`` below.

Schema §4 explicitly warns that ``fitness_auth_state.updated_at`` is
app-managed (no SQLite ON UPDATE trigger). Every UPDATE in this module
sets ``updated_at = ?`` in the same statement.
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from journal.db.factory import ConnectionFactory
from journal.models import (
    FitnessActivity,
    FitnessAuthState,
    FitnessDaily,
    FitnessRawRow,
    FitnessSyncRun,
)

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_hex(payload_json: str) -> str:
    return hashlib.sha256(payload_json.encode("utf-8")).hexdigest()


def _row_to_auth_state(row: sqlite3.Row) -> FitnessAuthState:
    return FitnessAuthState(
        id=row["id"],
        user_id=row["user_id"],
        source=row["source"],
        access_token=row["access_token"],
        refresh_token=row["refresh_token"],
        token_expires_at=row["token_expires_at"],
        extra_state=json.loads(row["extra_state_json"]) if row["extra_state_json"] else {},
        last_successful_login_at=row["last_successful_login_at"],
        last_refresh_at=row["last_refresh_at"],
        auth_status=row["auth_status"],
        auth_broken_since=row["auth_broken_since"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_sync_run(row: sqlite3.Row) -> FitnessSyncRun:
    return FitnessSyncRun(
        id=row["id"],
        user_id=row["user_id"],
        source=row["source"],
        status=row["status"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        error_class=row["error_class"],
        error_message=row["error_message"],
        rows_fetched=row["rows_fetched"],
        rows_normalized=row["rows_normalized"],
        notes=json.loads(row["notes_json"]) if row["notes_json"] else {},
    )


def _row_to_raw(row: sqlite3.Row) -> FitnessRawRow:
    return FitnessRawRow(
        id=row["id"],
        user_id=row["user_id"],
        source=row["source"],
        source_id=row["source_id"],
        endpoint=row["endpoint"],
        payload_json=row["payload_json"],
        payload_sha256=row["payload_sha256"],
        sync_run_id=row["sync_run_id"],
        fetched_at=row["fetched_at"],
    )


def _row_to_activity(row: sqlite3.Row) -> FitnessActivity:
    return FitnessActivity(
        id=row["id"],
        user_id=row["user_id"],
        source=row["source"],
        source_id=row["source_id"],
        activity_type=row["activity_type"],
        source_subtype=row["source_subtype"],
        start_time=row["start_time"],
        local_date=row["local_date"],
        duration_s=row["duration_s"],
        moving_time_s=row["moving_time_s"],
        distance_m=row["distance_m"],
        elevation_gain_m=row["elevation_gain_m"],
        avg_hr_bpm=row["avg_hr_bpm"],
        max_hr_bpm=row["max_hr_bpm"],
        avg_pace_s_per_km=row["avg_pace_s_per_km"],
        calories_kcal=row["calories_kcal"],
        perceived_exertion=row["perceived_exertion"],
        extras=json.loads(row["extras_json"]) if row["extras_json"] else {},
        raw_ref_id=row["raw_ref_id"],
        normalized_at=row["normalized_at"],
    )


def _row_to_daily(row: sqlite3.Row) -> FitnessDaily:
    return FitnessDaily(
        id=row["id"],
        user_id=row["user_id"],
        source=row["source"],
        local_date=row["local_date"],
        sleep_score=row["sleep_score"],
        sleep_duration_s=row["sleep_duration_s"],
        sleep_efficiency_pct=row["sleep_efficiency_pct"],
        hrv_overnight_ms=row["hrv_overnight_ms"],
        resting_hr_bpm=row["resting_hr_bpm"],
        body_battery_high=row["body_battery_high"],
        body_battery_low=row["body_battery_low"],
        stress_avg=row["stress_avg"],
        training_load_acute=row["training_load_acute"],
        training_load_chronic=row["training_load_chronic"],
        training_readiness=row["training_readiness"],
        extras=json.loads(row["extras_json"]) if row["extras_json"] else {},
        raw_ref_ids=json.loads(row["raw_ref_ids_json"]) if row["raw_ref_ids_json"] else [],
        normalized_at=row["normalized_at"],
    )


def _raw_table_for(source: str) -> str:
    if source == "strava":
        return "fitness_raw_strava"
    if source == "garmin":
        return "fitness_raw_garmin"
    raise ValueError(f"Unknown fitness source: {source!r}")


class FitnessRepository:
    """Data layer for the fitness pipeline.

    Construction accepts either a :class:`ConnectionFactory` (preferred,
    used by production via ``mcp_server/bootstrap.py``) or a bare
    ``sqlite3.Connection`` (legacy, retained for tests that haven't been
    migrated to the factory model yet — see
    ``docs/sqlite-per-thread-connections-plan.md`` W3).

    On the **factory** path each thread that calls a method gets its
    own ``sqlite3.Connection`` via ``threading.local`` inside the
    factory, so the shared-state commit race documented in
    ``docs/sqlite-threading.md`` is structurally impossible. The
    ``_lock`` is a no-op on this path.

    On the **legacy connection** path the same ``Connection`` instance
    is shared across threads. The per-method ``threading.Lock``
    serialises ``execute`` + ``commit`` pairs within this repo. This
    path is deliberately less safe than the factory path and exists
    only as a migration ramp — new code should pass a
    ``ConnectionFactory``.
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

    @property
    def connection(self) -> sqlite3.Connection:
        """Underlying SQLite connection for the current thread.

        On the factory path this returns the *calling* thread's
        connection (committed rows are visible via WAL across
        connections). On the legacy path this returns the single
        shared connection.
        """
        return self._conn()

    # ── Auth state ────────────────────────────────────────────────

    def get_auth_state(
        self, *, user_id: int, source: str,
    ) -> FitnessAuthState | None:
        conn = self._conn()
        with self._lock:
            row = conn.execute(
                "SELECT * FROM fitness_auth_state WHERE user_id = ? AND source = ?",
                (user_id, source),
            ).fetchone()
        return _row_to_auth_state(row) if row else None

    def get_auth_status(
        self, *, user_id: int, source: str,
    ) -> str | None:
        """Cheap projection: just the ``auth_status`` column (or ``None``
        when the row has been deleted).

        Workers use this between provider calls to detect a mid-run
        disconnect or auth-broken transition without paying for the full
        :meth:`get_auth_state` JSON parse on every iteration. Returns
        the literal column value (``"unknown" | "ok" | "broken"``) or
        ``None`` if no row exists for this ``(user_id, source)``.
        """
        conn = self._conn()
        with self._lock:
            row = conn.execute(
                "SELECT auth_status FROM fitness_auth_state "
                "WHERE user_id = ? AND source = ?",
                (user_id, source),
            ).fetchone()
        return row["auth_status"] if row else None

    def upsert_auth_state(self, state: FitnessAuthState) -> None:
        """Insert or update one auth-state row. Always sets
        ``updated_at`` to now (per schema §4 — app-managed)."""
        now = _now_iso()
        conn = self._conn()
        with self._lock:
            conn.execute(
                """
                INSERT INTO fitness_auth_state (
                    user_id, source, access_token, refresh_token, token_expires_at,
                    extra_state_json, last_successful_login_at, last_refresh_at,
                    auth_status, auth_broken_since, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, source) DO UPDATE SET
                    access_token = excluded.access_token,
                    refresh_token = excluded.refresh_token,
                    token_expires_at = excluded.token_expires_at,
                    extra_state_json = excluded.extra_state_json,
                    last_successful_login_at = excluded.last_successful_login_at,
                    last_refresh_at = excluded.last_refresh_at,
                    auth_status = excluded.auth_status,
                    auth_broken_since = excluded.auth_broken_since,
                    updated_at = ?
                """,
                (
                    state.user_id, state.source, state.access_token,
                    state.refresh_token, state.token_expires_at,
                    json.dumps(state.extra_state),
                    state.last_successful_login_at, state.last_refresh_at,
                    state.auth_status, state.auth_broken_since,
                    state.created_at or now, now,
                    now,
                ),
            )
            conn.commit()

    def delete_auth_state(self, *, user_id: int, source: str) -> bool:
        """Delete the auth row for ``(user_id, source)``. Returns True if a
        row was deleted, False if no matching row existed.

        Used by the W2 disconnect endpoint. Note that this only removes
        the credential row — historical ``fitness_activities`` /
        ``fitness_daily`` rows remain so a user can reconnect with the
        same upstream account and pick up where they left off.
        """
        conn = self._conn()
        with self._lock:
            cursor = conn.execute(
                "DELETE FROM fitness_auth_state WHERE user_id = ? AND source = ?",
                (user_id, source),
            )
            conn.commit()
            return cursor.rowcount > 0

    def transition_auth(
        self,
        *,
        user_id: int,
        source: str,
        status: Literal["ok", "broken"],
        at: str,
    ) -> bool:
        """Set ``auth_status`` and the matching ``auth_broken_since``
        column (set to ``at`` on transition to broken, NULL on
        transition to ok). Updates ``updated_at`` to now.

        Returns True iff the status actually changed — drives the
        D5 "fire-once" alerting policy. If the row does not exist,
        creates it and returns True.
        """
        now = _now_iso()
        conn = self._conn()
        with self._lock:
            existing = conn.execute(
                "SELECT auth_status FROM fitness_auth_state WHERE user_id = ? AND source = ?",
                (user_id, source),
            ).fetchone()
            if existing is None:
                broken_since = at if status == "broken" else None
                conn.execute(
                    """
                    INSERT INTO fitness_auth_state (
                        user_id, source, auth_status, auth_broken_since, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (user_id, source, status, broken_since, now),
                )
                conn.commit()
                return True
            if existing["auth_status"] == status:
                return False
            broken_since = at if status == "broken" else None
            conn.execute(
                """
                UPDATE fitness_auth_state
                SET auth_status = ?, auth_broken_since = ?, updated_at = ?
                WHERE user_id = ? AND source = ?
                """,
                (status, broken_since, now, user_id, source),
            )
            conn.commit()
            return True

    def get_health_summary(self, *, user_id: int) -> list[dict[str, Any]]:
        """Per-source snapshot for the `/api/health` fitness block.

        Returns one dict per source the user has interacted with — i.e.
        the union of sources with a `fitness_auth_state` row and sources
        with any `fitness_sync_runs` row. Sources with neither are
        omitted entirely (the W12 plan: do not show null sources).

        Each dict carries the three fields the health endpoint surfaces:
        `source`, `auth_status`, `auth_broken_since`, `last_success_at`.
        Orphan sources (sync_runs but no auth_state) yield `auth_status`
        and `auth_broken_since` as `None`.

        Single query: a UNION subquery enumerates the user's sources,
        LEFT JOIN pulls auth_state, correlated subquery pulls the latest
        successful run. Sub-millisecond against a personal-scale DB; the
        endpoint is low-traffic so we don't cache.
        """
        conn = self._conn()
        with self._lock:
            rows = conn.execute(
                """
                SELECT
                    src.source AS source,
                    a.auth_status AS auth_status,
                    a.auth_broken_since AS auth_broken_since,
                    (
                        SELECT MAX(started_at)
                        FROM fitness_sync_runs r
                        WHERE r.user_id = ?
                          AND r.source = src.source
                          AND r.status = 'success'
                    ) AS last_success_at
                FROM (
                    SELECT source FROM fitness_auth_state WHERE user_id = ?
                    UNION
                    SELECT DISTINCT source FROM fitness_sync_runs WHERE user_id = ?
                ) src
                LEFT JOIN fitness_auth_state a
                    ON a.user_id = ? AND a.source = src.source
                ORDER BY src.source
                """,
                (user_id, user_id, user_id, user_id),
            ).fetchall()
        return [
            {
                "source": row["source"],
                "auth_status": row["auth_status"],
                "auth_broken_since": row["auth_broken_since"],
                "last_success_at": row["last_success_at"],
            }
            for row in rows
        ]

    # ── Sync runs ─────────────────────────────────────────────────

    def start_sync_run(self, *, user_id: int, source: str) -> int:
        """Insert a new ``running`` sync-run row; returns its id."""
        conn = self._conn()
        with self._lock:
            cur = conn.execute(
                """
                INSERT INTO fitness_sync_runs (user_id, source, status)
                VALUES (?, ?, 'running')
                """,
                (user_id, source),
            )
            conn.commit()
            assert cur.lastrowid is not None
            return cur.lastrowid

    def finish_sync_run(
        self,
        run_id: int,
        *,
        status: Literal[
            "success", "auth_broken", "transient_failure", "normalize_drift",
        ],
        error_class: str | None = None,
        error_message: str | None = None,
        rows_fetched: int = 0,
        rows_normalized: int = 0,
        notes: dict[str, Any] | None = None,
    ) -> None:
        conn = self._conn()
        with self._lock:
            conn.execute(
                """
                UPDATE fitness_sync_runs
                SET status = ?, finished_at = ?, error_class = ?, error_message = ?,
                    rows_fetched = ?, rows_normalized = ?, notes_json = ?
                WHERE id = ?
                """,
                (
                    status, _now_iso(), error_class, error_message,
                    rows_fetched, rows_normalized,
                    json.dumps(notes or {}),
                    run_id,
                ),
            )
            conn.commit()

    def find_running_sync_run(
        self, *, user_id: int, source: str,
    ) -> int | None:
        """Return the id of an in-flight sync run, if one exists.

        Used by the W6 fetch service's single-run guard so concurrent
        token-refresh races (the JobRunner header's hazard list) cannot
        happen for fitness syncs in practice.
        """
        conn = self._conn()
        with self._lock:
            row = conn.execute(
                """
                SELECT id FROM fitness_sync_runs
                WHERE user_id = ? AND source = ? AND status = 'running'
                ORDER BY started_at DESC LIMIT 1
                """,
                (user_id, source),
            ).fetchone()
        return row["id"] if row else None

    def last_successful_sync_at(
        self, *, user_id: int, source: str,
    ) -> str | None:
        conn = self._conn()
        with self._lock:
            row = conn.execute(
                """
                SELECT MAX(started_at) AS at FROM fitness_sync_runs
                WHERE user_id = ? AND source = ? AND status = 'success'
                """,
                (user_id, source),
            ).fetchone()
        return row["at"] if row and row["at"] else None

    def list_recent_sync_runs(
        self, *, user_id: int, source: str, limit: int = 10,
    ) -> list[FitnessSyncRun]:
        conn = self._conn()
        with self._lock:
            rows = conn.execute(
                """
                SELECT * FROM fitness_sync_runs
                WHERE user_id = ? AND source = ?
                ORDER BY started_at DESC LIMIT ?
                """,
                (user_id, source, limit),
            ).fetchall()
        return [_row_to_sync_run(r) for r in rows]

    # ── Raw archive ───────────────────────────────────────────────

    def insert_raw(
        self,
        *,
        source: Literal["strava", "garmin"],
        user_id: int,
        endpoint: str,
        source_id: str,
        payload_json: str,
        sync_run_id: int | None,
    ) -> int | None:
        """INSERT OR IGNORE on the per-source UNIQUE constraint. Returns
        the new row id, or None if a row with identical
        ``payload_sha256`` already existed (no-op). A *changed* payload
        (different sha256 for the same logical key) inserts a new row;
        the old row stays in place per the append-only rule (D3)."""
        table = _raw_table_for(source)
        sha = _sha256_hex(payload_json)
        conn = self._conn()
        with self._lock:
            cur = conn.execute(
                f"""
                INSERT OR IGNORE INTO {table} (
                    user_id, source_id, endpoint, payload_json, payload_sha256, sync_run_id
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,  # noqa: S608 — table name is from a closed allowlist via _raw_table_for
                (user_id, source_id, endpoint, payload_json, sha, sync_run_id),
            )
            conn.commit()
            return cur.lastrowid if cur.rowcount else None

    def list_raw_since(
        self,
        *,
        source: Literal["strava", "garmin"],
        user_id: int,
        since: str | None = None,
    ) -> Iterator[FitnessRawRow]:
        table = _raw_table_for(source)
        conn = self._conn()
        with self._lock:
            if since is None:
                rows = conn.execute(
                    f"SELECT * FROM {table} WHERE user_id = ? ORDER BY fetched_at",  # noqa: S608
                    (user_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"""
                    SELECT * FROM {table}
                    WHERE user_id = ? AND fetched_at > ?
                    ORDER BY fetched_at
                    """,  # noqa: S608
                    (user_id, since),
                ).fetchall()
        for row in rows:
            yield _row_to_raw(row)

    # ── Normalized ────────────────────────────────────────────────

    def upsert_activity(self, activity: FitnessActivity) -> None:
        conn = self._conn()
        with self._lock:
            conn.execute(
                """
                INSERT INTO fitness_activities (
                    user_id, source, source_id, activity_type, source_subtype,
                    start_time, local_date, duration_s, moving_time_s, distance_m,
                    elevation_gain_m, avg_hr_bpm, max_hr_bpm, avg_pace_s_per_km,
                    calories_kcal, perceived_exertion, extras_json, raw_ref_id,
                    normalized_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, source, source_id) DO UPDATE SET
                    activity_type = excluded.activity_type,
                    source_subtype = excluded.source_subtype,
                    start_time = excluded.start_time,
                    local_date = excluded.local_date,
                    duration_s = excluded.duration_s,
                    moving_time_s = excluded.moving_time_s,
                    distance_m = excluded.distance_m,
                    elevation_gain_m = excluded.elevation_gain_m,
                    avg_hr_bpm = excluded.avg_hr_bpm,
                    max_hr_bpm = excluded.max_hr_bpm,
                    avg_pace_s_per_km = excluded.avg_pace_s_per_km,
                    calories_kcal = excluded.calories_kcal,
                    perceived_exertion = excluded.perceived_exertion,
                    extras_json = excluded.extras_json,
                    raw_ref_id = excluded.raw_ref_id,
                    normalized_at = excluded.normalized_at
                """,
                (
                    activity.user_id, activity.source, activity.source_id,
                    activity.activity_type, activity.source_subtype,
                    activity.start_time, activity.local_date,
                    activity.duration_s, activity.moving_time_s,
                    activity.distance_m, activity.elevation_gain_m,
                    activity.avg_hr_bpm, activity.max_hr_bpm,
                    activity.avg_pace_s_per_km, activity.calories_kcal,
                    activity.perceived_exertion,
                    json.dumps(activity.extras),
                    activity.raw_ref_id, _now_iso(),
                ),
            )
            conn.commit()

    def upsert_daily(self, daily: FitnessDaily) -> None:
        conn = self._conn()
        with self._lock:
            conn.execute(
                """
                INSERT INTO fitness_daily (
                    user_id, source, local_date, sleep_score, sleep_duration_s,
                    sleep_efficiency_pct, hrv_overnight_ms, resting_hr_bpm,
                    body_battery_high, body_battery_low, stress_avg,
                    training_load_acute, training_load_chronic, training_readiness,
                    extras_json, raw_ref_ids_json, normalized_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, source, local_date) DO UPDATE SET
                    sleep_score = excluded.sleep_score,
                    sleep_duration_s = excluded.sleep_duration_s,
                    sleep_efficiency_pct = excluded.sleep_efficiency_pct,
                    hrv_overnight_ms = excluded.hrv_overnight_ms,
                    resting_hr_bpm = excluded.resting_hr_bpm,
                    body_battery_high = excluded.body_battery_high,
                    body_battery_low = excluded.body_battery_low,
                    stress_avg = excluded.stress_avg,
                    training_load_acute = excluded.training_load_acute,
                    training_load_chronic = excluded.training_load_chronic,
                    training_readiness = excluded.training_readiness,
                    extras_json = excluded.extras_json,
                    raw_ref_ids_json = excluded.raw_ref_ids_json,
                    normalized_at = excluded.normalized_at
                """,
                (
                    daily.user_id, daily.source, daily.local_date,
                    daily.sleep_score, daily.sleep_duration_s,
                    daily.sleep_efficiency_pct, daily.hrv_overnight_ms,
                    daily.resting_hr_bpm, daily.body_battery_high,
                    daily.body_battery_low, daily.stress_avg,
                    daily.training_load_acute, daily.training_load_chronic,
                    daily.training_readiness,
                    json.dumps(daily.extras),
                    json.dumps(daily.raw_ref_ids),
                    _now_iso(),
                ),
            )
            conn.commit()

    def list_activities(
        self,
        *,
        user_id: int,
        start: str,
        end: str,
        activity_type: str | None = None,
    ) -> list[FitnessActivity]:
        """Inclusive on both sides of the date range."""
        conn = self._conn()
        with self._lock:
            if activity_type is None:
                rows = conn.execute(
                    """
                    SELECT * FROM fitness_activities
                    WHERE user_id = ? AND local_date BETWEEN ? AND ?
                    ORDER BY start_time
                    """,
                    (user_id, start, end),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM fitness_activities
                    WHERE user_id = ? AND local_date BETWEEN ? AND ?
                      AND activity_type = ?
                    ORDER BY start_time
                    """,
                    (user_id, start, end, activity_type),
                ).fetchall()
        return [_row_to_activity(r) for r in rows]

    def list_daily(
        self, *, user_id: int, start: str, end: str,
    ) -> list[FitnessDaily]:
        conn = self._conn()
        with self._lock:
            rows = conn.execute(
                """
                SELECT * FROM fitness_daily
                WHERE user_id = ? AND local_date BETWEEN ? AND ?
                ORDER BY local_date
                """,
                (user_id, start, end),
            ).fetchall()
        return [_row_to_daily(r) for r in rows]

    def max_normalized_local_date(
        self,
        *,
        source: Literal["strava", "garmin"],
        user_id: int,
        kind: Literal["activities", "daily"],
    ) -> str | None:
        """Resume watermark for backfill (W13).

        Returns the largest ``local_date`` over ``fitness_activities``
        (kind=``activities``) or ``fitness_daily`` (kind=``daily``) for
        the given user and source. Drives the per-source resume
        predicate so a backfill picks up where the previous run left
        off — *independently per source*: a Strava-only watermark
        would silently skip Garmin days if Strava had progressed
        further (and vice versa). NULL on first run (empty normalized
        table for this user/source/kind).
        """
        # Closed allowlist — table is never callee-supplied.
        table = "fitness_activities" if kind == "activities" else "fitness_daily"
        conn = self._conn()
        with self._lock:
            row = conn.execute(
                f"""
                SELECT MAX(local_date) AS d FROM {table}
                WHERE user_id = ? AND source = ?
                """,  # noqa: S608 — table from closed allowlist above
                (user_id, source),
            ).fetchone()
        return row["d"] if row and row["d"] else None

    def max_normalized_fetched_at(
        self,
        *,
        source: Literal["strava", "garmin"],
        user_id: int,
        kind: Literal["activities", "daily"],
    ) -> str | None:
        """Watermark for incremental normalize (W7).

        Returns the largest ``fetched_at`` of raw rows referenced by
        normalized rows in this user/source/kind. The normalize service
        reads raw rows with ``fetched_at > <watermark>`` to resume
        from the last point. NULL on first run (empty normalized table).

        For activities, the watermark is computed by joining
        ``fitness_activities.raw_ref_id`` against the matching raw
        table's ``fetched_at``. For daily, it is the max ``fetched_at``
        from raw rows whose id appears in any
        ``fitness_daily.raw_ref_ids_json`` array.
        """
        raw_table = _raw_table_for(source)
        conn = self._conn()
        with self._lock:
            if kind == "activities":
                row = conn.execute(
                    f"""
                    SELECT MAX(r.fetched_at) AS at
                    FROM fitness_activities fa
                    JOIN {raw_table} r ON r.id = fa.raw_ref_id
                    WHERE fa.user_id = ? AND fa.source = ?
                    """,  # noqa: S608
                    (user_id, source),
                ).fetchone()
            else:
                row = conn.execute(
                    f"""
                    SELECT MAX(r.fetched_at) AS at
                    FROM fitness_daily fd, json_each(fd.raw_ref_ids_json) j
                    JOIN {raw_table} r ON r.id = j.value
                    WHERE fd.user_id = ? AND fd.source = ?
                    """,  # noqa: S608
                    (user_id, source),
                ).fetchone()
        return row["at"] if row and row["at"] else None
