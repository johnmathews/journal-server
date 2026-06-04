"""Migration 0029 — extend ``fitness_activities.activity_type`` with
``'row'`` and backfill existing rowing rows from ``'other'``.

The interesting failure mode is data-shape, not schema-shape: a fresh
DB never had a ``Rowing`` row at ``activity_type='other'``, so a
schema-only test on a clean migration chain covers the destination
but not the journey. These tests seed prod-shaped rows (rowing
collapsed to ``'other'``, the pre-migration steady state) at the
post-0028 schema, then apply 0029 and assert the backfill landed.

Per ``CLAUDE.md`` feedback_migration_testing — query prod for data
anomalies first, write tests against the data-copy path on
prod-shaped state, make every migration re-runnable after partial
failure. Items 2 and 3 are exercised here; item 1 (prod probe) lives
in the run dir's evaluation-report.md.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from journal.db.connection import get_connection
from journal.db.migrations import (
    _executescript_idempotent,
    get_migration_files,
)

if TYPE_CHECKING:
    from pathlib import Path


_MIGRATION_0029 = "0029_fitness_activity_type_add_row.sql"


def _run_migrations_up_to(conn, target_version: int) -> None:
    """Apply every migration up to and including ``target_version``.

    Used to construct a pre-0029 fixture so the migration-under-test
    can be applied separately and asserted on. Mirrors
    ``run_migrations`` but stops once the target is reached.
    """
    for migration_file in get_migration_files():
        version = int(migration_file.stem.split("_")[0])
        if version > target_version:
            break
        _executescript_idempotent(
            conn, migration_file.read_text(), migration_file.name,
        )
        conn.execute(f"PRAGMA user_version = {version}")


def _read_migration_0029() -> str:
    files = get_migration_files()
    matches = [f for f in files if f.name == _MIGRATION_0029]
    assert matches, f"migration {_MIGRATION_0029} missing"
    return matches[0].read_text()


def _seed_user(conn) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO users (id, email, password_hash, display_name,
                                     email_verified, is_admin)
        VALUES (1, 'test@example.com', 'x', 'test', 1, 1)
        """,
    )


def _seed_activity_pre_0029(
    conn,
    *,
    source: str,
    source_id: str,
    source_subtype: str,
    activity_type: str = "other",
    local_date: str = "2026-05-10",
) -> None:
    """Insert a row directly via SQL so the pre-0029 CHECK constraint
    (seven values, no ``'row'``) is the one enforced.
    """
    conn.execute(
        """
        INSERT INTO fitness_activities (
            user_id, source, source_id, activity_type, source_subtype,
            start_time, local_date, duration_s, raw_ref_id
        ) VALUES (1, ?, ?, ?, ?, ?, ?, 1800, 1)
        """,
        (
            source, source_id, activity_type, source_subtype,
            f"{local_date}T10:00:00Z", local_date,
        ),
    )


@pytest.fixture
def pre_0029_conn(tmp_path: Path):
    """Connection at the post-0028 schema — `fitness_activities` exists
    with the seven-value CHECK and the migration runner's
    ``user_version`` is at 28."""
    db_path = tmp_path / "migration-0029.db"
    conn = get_connection(db_path)
    _run_migrations_up_to(conn, target_version=28)
    _seed_user(conn)
    conn.commit()
    return conn


def test_pre_0029_check_constraint_rejects_row(pre_0029_conn) -> None:
    """Sanity-check the fixture: at the pre-migration schema, inserting
    ``activity_type='row'`` must fail the CHECK. This pins the
    migration's reason for existing.
    """
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        _seed_activity_pre_0029(
            pre_0029_conn,
            source="strava", source_id="ROW-PRE",
            source_subtype="Rowing", activity_type="row",
        )


def test_0029_backfills_rowing_strava_and_garmin(pre_0029_conn) -> None:
    """Seed prod-shaped rowing rows at ``activity_type='other'`` (the
    pre-migration steady state), apply 0029, assert all three rowing
    subtypes land at ``activity_type='row'`` and the control rows are
    untouched."""
    # Rowing — Strava, two rows so we know the backfill isn't a single-
    # row coincidence.
    _seed_activity_pre_0029(
        pre_0029_conn,
        source="strava", source_id="STRAVA-ROW-1",
        source_subtype="Rowing", local_date="2026-05-10",
    )
    _seed_activity_pre_0029(
        pre_0029_conn,
        source="strava", source_id="STRAVA-ROW-2",
        source_subtype="Rowing", local_date="2026-05-11",
    )
    # Rowing — Garmin, both typeKeys.
    _seed_activity_pre_0029(
        pre_0029_conn,
        source="garmin", source_id="GARMIN-ROW-1",
        source_subtype="rowing", local_date="2026-05-12",
    )
    _seed_activity_pre_0029(
        pre_0029_conn,
        source="garmin", source_id="GARMIN-ROW-2",
        source_subtype="indoor_rowing", local_date="2026-05-13",
    )
    # Controls. Yoga is a Strava 'other' that should stay 'other'.
    # Run is a happy-path Strava row that should keep 'run'.
    _seed_activity_pre_0029(
        pre_0029_conn,
        source="strava", source_id="STRAVA-YOGA",
        source_subtype="Yoga", local_date="2026-05-14",
    )
    _seed_activity_pre_0029(
        pre_0029_conn,
        source="strava", source_id="STRAVA-RUN",
        source_subtype="Run", activity_type="run",
        local_date="2026-05-15",
    )
    pre_0029_conn.commit()

    _executescript_idempotent(
        pre_0029_conn, _read_migration_0029(), _MIGRATION_0029,
    )

    rows = pre_0029_conn.execute(
        """
        SELECT source_id, activity_type, source_subtype
        FROM fitness_activities ORDER BY source_id
        """,
    ).fetchall()
    by_source_id = {r["source_id"]: r for r in rows}

    assert by_source_id["STRAVA-ROW-1"]["activity_type"] == "row"
    assert by_source_id["STRAVA-ROW-2"]["activity_type"] == "row"
    assert by_source_id["GARMIN-ROW-1"]["activity_type"] == "row"
    assert by_source_id["GARMIN-ROW-2"]["activity_type"] == "row"
    # Source_subtype is unchanged — the verbatim signal is preserved.
    assert by_source_id["STRAVA-ROW-1"]["source_subtype"] == "Rowing"
    assert by_source_id["GARMIN-ROW-2"]["source_subtype"] == "indoor_rowing"
    # Controls unchanged.
    assert by_source_id["STRAVA-YOGA"]["activity_type"] == "other"
    assert by_source_id["STRAVA-YOGA"]["source_subtype"] == "Yoga"
    assert by_source_id["STRAVA-RUN"]["activity_type"] == "run"


def test_0029_preserves_indexes(pre_0029_conn) -> None:
    """The three indexes from 0025 must still exist after the table
    rebuild. SQLite drops indexes when their backing table is dropped,
    so they must be recreated in the same migration."""
    _executescript_idempotent(
        pre_0029_conn, _read_migration_0029(), _MIGRATION_0029,
    )
    rows = pre_0029_conn.execute(
        """
        SELECT name FROM sqlite_master
        WHERE type = 'index' AND tbl_name = 'fitness_activities'
        """,
    ).fetchall()
    names = {r["name"] for r in rows}
    # Auto-indexes from UNIQUE / PK live alongside the named ones; the
    # named ones are what we explicitly create.
    assert "idx_fit_act_user_date" in names
    assert "idx_fit_act_user_type_date" in names
    assert "idx_fit_act_start" in names


def test_0029_allows_row_after_migration(pre_0029_conn) -> None:
    """Post-migration, ``activity_type='row'`` is accepted by the
    relaxed CHECK constraint. Otherwise the migration's only visible
    effect would be the backfill — the schema relaxation must also
    take effect."""
    _executescript_idempotent(
        pre_0029_conn, _read_migration_0029(), _MIGRATION_0029,
    )
    # Insert a fresh row at activity_type='row' through the same SQL
    # path; CHECK violation would raise IntegrityError.
    pre_0029_conn.execute(
        """
        INSERT INTO fitness_activities (
            user_id, source, source_id, activity_type, source_subtype,
            start_time, local_date, duration_s, raw_ref_id
        ) VALUES (1, 'strava', 'POST-ROW', 'row', 'Rowing',
                  '2026-05-20T10:00:00Z', '2026-05-20', 1800, 1)
        """,
    )
    pre_0029_conn.commit()
    row = pre_0029_conn.execute(
        "SELECT activity_type FROM fitness_activities "
        "WHERE source_id = 'POST-ROW'",
    ).fetchone()
    assert row["activity_type"] == "row"


def test_0029_is_safe_after_partial_failure(pre_0029_conn) -> None:
    """Simulate the failure mode the ``DROP TABLE IF EXISTS
    fitness_activities_new`` guard at the top of 0029 protects against:
    a prior crash left a half-built ``fitness_activities_new`` table
    behind. The migration must clean it up and complete on the retry.
    """
    _seed_activity_pre_0029(
        pre_0029_conn,
        source="strava", source_id="STRAVA-ROW-RETRY",
        source_subtype="Rowing", local_date="2026-05-16",
    )
    pre_0029_conn.commit()

    # Stale leftover from a hypothetical earlier partial run. Any
    # shape — we just need the table to exist; the DROP at the top of
    # 0029 should remove it.
    pre_0029_conn.execute(
        "CREATE TABLE fitness_activities_new "
        "(id INTEGER PRIMARY KEY, stale TEXT)",
    )
    pre_0029_conn.execute(
        "INSERT INTO fitness_activities_new (stale) VALUES ('junk')",
    )
    pre_0029_conn.commit()

    _executescript_idempotent(
        pre_0029_conn, _read_migration_0029(), _MIGRATION_0029,
    )

    # The retry succeeded: the rowing row was backfilled, and no
    # stale rows remain.
    row = pre_0029_conn.execute(
        "SELECT activity_type FROM fitness_activities "
        "WHERE source_id = 'STRAVA-ROW-RETRY'",
    ).fetchone()
    assert row["activity_type"] == "row"
