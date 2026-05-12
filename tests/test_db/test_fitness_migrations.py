"""Tests for fitness migrations 0023 / 0024 / 0025.

These cover:

1. Schema present after run_migrations() — every fitness_* table and
   index documented in fitness-schema.md exists with the expected
   columns.
2. CHECK constraints fire on out-of-range values.
3. UNIQUE constraints fire on duplicates.
4. Idempotent re-run via PRAGMA user_version reset.
5. Cross-file partial-install hazard — applying only 0023 leaves the
   system in a coherent (if incomplete) state.

The plan (W1 in docs/fitness-tier-plan.md) is the authoritative checklist.
"""

import sqlite3
from collections.abc import Generator
from pathlib import Path

import pytest

from journal.db.connection import get_connection
from journal.db.migrations import (
    MIGRATIONS_DIR,
    get_current_version,
    run_migrations,
)

# ── Schema present ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("table", "expected_columns"),
    [
        (
            "fitness_auth_state",
            {
                "id", "user_id", "source", "access_token", "refresh_token",
                "token_expires_at", "extra_state_json",
                "last_successful_login_at", "last_refresh_at", "auth_status",
                "auth_broken_since", "created_at", "updated_at",
            },
        ),
        (
            "fitness_sync_runs",
            {
                "id", "user_id", "source", "started_at", "finished_at",
                "status", "error_class", "error_message", "rows_fetched",
                "rows_normalized", "notes_json",
                # T7 (migration 0026): per-bucket counters for the
                # workouts-vs-wellness split.
                "workouts_fetched", "wellness_fetched",
                "workouts_normalized", "wellness_normalized",
            },
        ),
        (
            "fitness_raw_strava",
            {
                "id", "user_id", "source", "source_id", "endpoint",
                "fetched_at", "payload_json", "payload_sha256", "sync_run_id",
            },
        ),
        (
            "fitness_raw_garmin",
            {
                "id", "user_id", "source", "source_id", "endpoint",
                "fetched_at", "payload_json", "payload_sha256", "sync_run_id",
            },
        ),
        (
            "fitness_activities",
            {
                "id", "user_id", "source", "source_id", "activity_type",
                "source_subtype", "start_time", "local_date", "duration_s",
                "moving_time_s", "distance_m", "elevation_gain_m",
                "avg_hr_bpm", "max_hr_bpm", "avg_pace_s_per_km",
                "calories_kcal", "perceived_exertion", "extras_json",
                "raw_ref_id", "normalized_at",
            },
        ),
        (
            "fitness_daily",
            {
                "id", "user_id", "source", "local_date", "sleep_score",
                "sleep_duration_s", "sleep_efficiency_pct", "hrv_overnight_ms",
                "resting_hr_bpm", "body_battery_high", "body_battery_low",
                "stress_avg", "training_load_acute", "training_load_chronic",
                "training_readiness", "extras_json", "raw_ref_ids_json",
                "normalized_at",
            },
        ),
    ],
)
def test_fitness_table_has_expected_columns(
    db_conn: sqlite3.Connection, table: str, expected_columns: set[str],
) -> None:
    rows = db_conn.execute(f"PRAGMA table_info({table})").fetchall()
    column_names = {r["name"] for r in rows}
    assert column_names == expected_columns


@pytest.mark.parametrize(
    "index",
    [
        "idx_fit_sync_user_started",
        "idx_fit_sync_user_source_started",
        "idx_fit_raw_strava_user_fetched",
        "idx_fit_raw_strava_source_id",
        "idx_fit_raw_garmin_user_fetched",
        "idx_fit_raw_garmin_endpoint_key",
        "idx_fit_act_user_date",
        "idx_fit_act_user_type_date",
        "idx_fit_act_start",
        "idx_fit_daily_user_date",
    ],
)
def test_fitness_index_exists(db_conn: sqlite3.Connection, index: str) -> None:
    row = db_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
        (index,),
    ).fetchone()
    assert row is not None, f"missing index {index}"


def test_user_version_advances_to_25(db_conn: sqlite3.Connection) -> None:
    assert get_current_version(db_conn) >= 25


# ── Helpers ──────────────────────────────────────────────────────────


def _seed_user(conn: sqlite3.Connection, user_id: int = 1) -> None:
    """Insert a minimal user row so FKs into users(id) resolve."""
    conn.execute(
        """
        INSERT OR IGNORE INTO users (id, email, password_hash, display_name,
                                     email_verified, is_admin)
        VALUES (?, 'test@example.com', 'x', 'test', 1, 1)
        """,
        (user_id,),
    )


def _seed_sync_run(conn: sqlite3.Connection, source: str = "strava") -> int:
    cur = conn.execute(
        "INSERT INTO fitness_sync_runs (user_id, source, status) VALUES (1, ?, 'running')",
        (source,),
    )
    return cur.lastrowid  # type: ignore[return-value]


# ── CHECK constraints fire ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("avg_hr_bpm", 300),       # > 250
        ("avg_hr_bpm", 10),        # < 20
        ("max_hr_bpm", 0),         # < 20
        ("perceived_exertion", 0),     # < 1
        ("perceived_exertion", 11),    # > 10
        ("distance_m", -1.0),
        ("duration_s", -1),
        ("elevation_gain_m", -0.1),
    ],
)
def test_fitness_activities_check_constraints_fire(
    db_conn: sqlite3.Connection, column: str, value: object,
) -> None:
    _seed_user(db_conn)
    columns = ["user_id", "source", "source_id", "activity_type",
               "source_subtype", "start_time", "local_date", "duration_s",
               "raw_ref_id"]
    values: list[object] = [1, "strava", "abc", "run", "Run",
                            "2026-05-09T10:00:00Z", "2026-05-09", 100, 1]
    if column in columns:
        idx = columns.index(column)
        values[idx] = value
    else:
        columns.append(column)
        values.append(value)
    placeholders = ",".join("?" * len(values))
    sql = f"INSERT INTO fitness_activities ({', '.join(columns)}) VALUES ({placeholders})"
    with pytest.raises(sqlite3.IntegrityError):
        db_conn.execute(sql, values)


def test_fitness_activities_invalid_activity_type_rejected(
    db_conn: sqlite3.Connection,
) -> None:
    _seed_user(db_conn)
    with pytest.raises(sqlite3.IntegrityError):
        db_conn.execute(
            """
            INSERT INTO fitness_activities (user_id, source, source_id,
                activity_type, source_subtype, start_time, local_date,
                duration_s, raw_ref_id)
            VALUES (1, 'strava', 'abc', 'flying', 'Flying',
                    '2026-05-09T10:00:00Z', '2026-05-09', 100, 1)
            """,
        )


def test_fitness_daily_score_ranges_enforced(
    db_conn: sqlite3.Connection,
) -> None:
    _seed_user(db_conn)
    # sleep_score outside 0..100
    with pytest.raises(sqlite3.IntegrityError):
        db_conn.execute(
            """
            INSERT INTO fitness_daily (user_id, source, local_date, sleep_score)
            VALUES (1, 'garmin', '2026-05-09', 120)
            """,
        )


def test_fitness_sync_runs_status_enum_enforced(
    db_conn: sqlite3.Connection,
) -> None:
    _seed_user(db_conn)
    sql = (
        "INSERT INTO fitness_sync_runs (user_id, source, status) "
        "VALUES (1, 'strava', 'frobbed')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        db_conn.execute(sql)


def test_fitness_auth_status_enum_enforced(db_conn: sqlite3.Connection) -> None:
    _seed_user(db_conn)
    with pytest.raises(sqlite3.IntegrityError):
        db_conn.execute(
            """
            INSERT INTO fitness_auth_state (user_id, source, auth_status)
            VALUES (1, 'strava', 'maybe')
            """,
        )


def test_fitness_raw_strava_source_locked_to_strava(
    db_conn: sqlite3.Connection,
) -> None:
    _seed_user(db_conn)
    # explicit 'garmin' source on the strava table is rejected by CHECK
    with pytest.raises(sqlite3.IntegrityError):
        db_conn.execute(
            """
            INSERT INTO fitness_raw_strava (user_id, source, source_id, endpoint,
                payload_json, payload_sha256)
            VALUES (1, 'garmin', '1', 'activities', '{}', 'x')
            """,
        )


def test_fitness_raw_garmin_endpoint_enum_enforced(
    db_conn: sqlite3.Connection,
) -> None:
    _seed_user(db_conn)
    with pytest.raises(sqlite3.IntegrityError):
        db_conn.execute(
            """
            INSERT INTO fitness_raw_garmin (user_id, source_id, endpoint,
                payload_json, payload_sha256)
            VALUES (1, '2026-05-09', 'unknown_endpoint', '{}', 'x')
            """,
        )


# ── UNIQUE constraints fire ──────────────────────────────────────────


def test_fitness_activities_unique_per_source(db_conn: sqlite3.Connection) -> None:
    _seed_user(db_conn)
    db_conn.execute(
        """
        INSERT INTO fitness_activities (user_id, source, source_id, activity_type,
            source_subtype, start_time, local_date, duration_s, raw_ref_id)
        VALUES (1, 'strava', 'abc', 'run', 'Run',
                '2026-05-09T10:00:00Z', '2026-05-09', 100, 1)
        """,
    )
    with pytest.raises(sqlite3.IntegrityError):
        db_conn.execute(
            """
            INSERT INTO fitness_activities (user_id, source, source_id, activity_type,
                source_subtype, start_time, local_date, duration_s, raw_ref_id)
            VALUES (1, 'strava', 'abc', 'ride', 'Ride',
                    '2026-05-09T11:00:00Z', '2026-05-09', 200, 2)
            """,
        )


def test_fitness_auth_unique_per_user_source(db_conn: sqlite3.Connection) -> None:
    _seed_user(db_conn)
    db_conn.execute(
        "INSERT INTO fitness_auth_state (user_id, source) VALUES (1, 'strava')",
    )
    with pytest.raises(sqlite3.IntegrityError):
        db_conn.execute(
            "INSERT INTO fitness_auth_state (user_id, source) VALUES (1, 'strava')",
        )


def test_fitness_raw_strava_unique_includes_sha(db_conn: sqlite3.Connection) -> None:
    _seed_user(db_conn)
    run_id = _seed_sync_run(db_conn, "strava")
    db_conn.execute(
        """
        INSERT INTO fitness_raw_strava (user_id, source_id, endpoint,
            payload_json, payload_sha256, sync_run_id)
        VALUES (1, '101', 'activities', '{"a":1}', 'sha-A', ?)
        """,
        (run_id,),
    )
    # Same logical key but different sha → different UNIQUE → succeeds.
    db_conn.execute(
        """
        INSERT INTO fitness_raw_strava (user_id, source_id, endpoint,
            payload_json, payload_sha256, sync_run_id)
        VALUES (1, '101', 'activities', '{"a":2}', 'sha-B', ?)
        """,
        (run_id,),
    )
    # Same key + same sha → UNIQUE violation.
    with pytest.raises(sqlite3.IntegrityError):
        db_conn.execute(
            """
            INSERT INTO fitness_raw_strava (user_id, source_id, endpoint,
                payload_json, payload_sha256, sync_run_id)
            VALUES (1, '101', 'activities', '{"a":1}', 'sha-A', ?)
            """,
            (run_id,),
        )


# ── Idempotent re-run ────────────────────────────────────────────────


def test_idempotent_rerun_from_pre_fitness_baseline(tmp_db_path: Path) -> None:
    """Set user_version to 22 (pre-fitness baseline), apply migrations,
    confirm advance through the fitness range. Roll back the version to
    22 and re-apply only the fitness migrations (23-25); the
    IF NOT EXISTS clauses must let them succeed on the pre-existing
    tables.

    Scoped to the fitness migrations because their idempotency on
    rollback is the property we want to pin down. Later destructive
    migrations (e.g. 0028's storylines rebuild) have their own
    re-runnability tests in ``test_migrations.py``."""
    conn = get_connection(tmp_db_path)
    run_migrations(conn)
    assert get_current_version(conn) >= 25

    conn.execute("PRAGMA user_version = 22")
    for migration_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
        version = int(migration_file.stem.split("_")[0])
        if version <= 22 or version > 25:
            continue
        conn.executescript(migration_file.read_text())
        conn.execute(f"PRAGMA user_version = {version}")
    assert get_current_version(conn) >= 25
    conn.close()


# ── Cross-file FK partial-install hazard ─────────────────────────────


def test_partial_install_at_0023_is_coherent(tmp_db_path: Path) -> None:
    """Apply only up to 0023 (auth + sync_runs); the system must work
    standalone — fitness_sync_runs accepts inserts and queries — even
    though the raw and normalized tables don't exist yet. This pins
    down the "no cross-file silent invariants" claim from the W1
    section of the tier plan."""
    conn = get_connection(tmp_db_path)
    # Run all migrations up to and including 0023, skip 0024+.
    for migration_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
        version = int(migration_file.stem.split("_")[0])
        if version > 23:
            break
        sql = migration_file.read_text()
        conn.executescript(sql)
        conn.execute(f"PRAGMA user_version = {version}")
    assert get_current_version(conn) == 23

    # fitness_sync_runs is independently usable.
    _seed_user(conn)
    cur = conn.execute(
        "INSERT INTO fitness_sync_runs (user_id, source, status) VALUES (1, 'strava', 'running')",
    )
    assert cur.lastrowid is not None

    # Raw/normalized tables don't exist yet — that's fine and expected.
    raw_present = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='fitness_raw_strava'",
    ).fetchone()
    assert raw_present is None
    conn.close()


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def db_conn_only_through_22(tmp_db_path: Path) -> Generator[sqlite3.Connection]:
    """Connection migrated only up to version 22 (pre-fitness)."""
    conn = get_connection(tmp_db_path)
    for migration_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
        version = int(migration_file.stem.split("_")[0])
        if version > 22:
            break
        sql = migration_file.read_text()
        conn.executescript(sql)
        conn.execute(f"PRAGMA user_version = {version}")
    yield conn
    conn.close()


def test_fitness_tables_absent_at_pre_fitness_baseline(
    db_conn_only_through_22: sqlite3.Connection,
) -> None:
    """Sentinel: confirm the pre-fitness baseline really has no fitness
    tables, so the idempotency test above is not just a no-op."""
    rows = db_conn_only_through_22.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'fitness_%'",
    ).fetchall()
    assert rows == []
