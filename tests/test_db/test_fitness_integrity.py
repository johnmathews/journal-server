"""Tests for the soft-pointer integrity checker.

The checker resolves `fitness_activities.raw_ref_id` (scalar) and
`fitness_daily.raw_ref_ids_json` (JSON array) into the per-source raw
tables and reports any orphans. Tests build deliberately-dirty fixture
DBs containing valid + orphaned references and assert the checker
finds the orphans without false positives.
"""

import sqlite3

from journal.db.fitness_integrity import (
    ActivityOrphan,
    DailyOrphan,
    check_fitness_integrity,
)


def _seed_user(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO users (id, email, password_hash, display_name,
                                     email_verified, is_admin)
        VALUES (1, 'test@example.com', 'x', 'test', 1, 1)
        """,
    )


def _insert_strava_raw(conn: sqlite3.Connection, source_id: str = "100") -> int:
    cur = conn.execute(
        """
        INSERT INTO fitness_raw_strava (user_id, source_id, endpoint,
            payload_json, payload_sha256)
        VALUES (1, ?, 'activities', '{}', 'sha')
        """,
        (source_id,),
    )
    return cur.lastrowid  # type: ignore[return-value]


def _insert_garmin_raw(conn: sqlite3.Connection, endpoint: str, source_id: str) -> int:
    cur = conn.execute(
        """
        INSERT INTO fitness_raw_garmin (user_id, source_id, endpoint,
            payload_json, payload_sha256)
        VALUES (1, ?, ?, '{}', ?)
        """,
        (source_id, endpoint, f"sha-{endpoint}-{source_id}"),
    )
    return cur.lastrowid  # type: ignore[return-value]


def _insert_activity(
    conn: sqlite3.Connection, *, source: str, source_id: str, raw_ref_id: int,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO fitness_activities (user_id, source, source_id, activity_type,
            source_subtype, start_time, local_date, duration_s, raw_ref_id)
        VALUES (1, ?, ?, 'run', 'Run', '2026-05-09T10:00:00Z', '2026-05-09', 100, ?)
        """,
        (source, source_id, raw_ref_id),
    )
    return cur.lastrowid  # type: ignore[return-value]


def _insert_daily(
    conn: sqlite3.Connection, *, local_date: str, raw_ref_ids_json: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO fitness_daily (user_id, source, local_date, raw_ref_ids_json)
        VALUES (1, 'garmin', ?, ?)
        """,
        (local_date, raw_ref_ids_json),
    )
    return cur.lastrowid  # type: ignore[return-value]


def test_clean_db_reports_no_orphans(db_conn: sqlite3.Connection) -> None:
    _seed_user(db_conn)
    raw_strava = _insert_strava_raw(db_conn, source_id="200")
    raw_garmin_act = _insert_garmin_raw(db_conn, "activities", "300")
    raw_garmin_sleep = _insert_garmin_raw(db_conn, "sleep", "2026-05-09")
    raw_garmin_hrv = _insert_garmin_raw(db_conn, "hrv", "2026-05-09")

    _insert_activity(db_conn, source="strava", source_id="200", raw_ref_id=raw_strava)
    _insert_activity(db_conn, source="garmin", source_id="300", raw_ref_id=raw_garmin_act)
    _insert_daily(
        db_conn,
        local_date="2026-05-09",
        raw_ref_ids_json=f"[{raw_garmin_sleep}, {raw_garmin_hrv}]",
    )

    report = check_fitness_integrity(db_conn)
    assert report.activities == []
    assert report.daily == []
    assert not report.has_orphans


def test_orphan_strava_activity_is_reported(db_conn: sqlite3.Connection) -> None:
    _seed_user(db_conn)
    activity_id = _insert_activity(
        db_conn, source="strava", source_id="999", raw_ref_id=99999,
    )
    report = check_fitness_integrity(db_conn)
    assert report.activities == [
        ActivityOrphan(activity_id=activity_id, source="strava", raw_ref_id=99999),
    ]
    assert report.daily == []


def test_orphan_garmin_activity_is_reported(db_conn: sqlite3.Connection) -> None:
    _seed_user(db_conn)
    activity_id = _insert_activity(
        db_conn, source="garmin", source_id="888", raw_ref_id=88888,
    )
    report = check_fitness_integrity(db_conn)
    assert report.activities == [
        ActivityOrphan(activity_id=activity_id, source="garmin", raw_ref_id=88888),
    ]


def test_cross_source_id_collision_does_not_false_match(
    db_conn: sqlite3.Connection,
) -> None:
    """A Strava raw row with id=N and a Garmin raw row with id=N
    can both exist (independent AUTOINCREMENT sequences). The integrity
    check must not treat a Strava activity referencing N as resolved
    because a Garmin row at id=N exists, or vice versa."""
    _seed_user(db_conn)
    # Make sure the two raw tables have a row at the same id.
    raw_strava = _insert_strava_raw(db_conn, source_id="200")
    raw_garmin = _insert_garmin_raw(db_conn, "activities", "300")
    # Activity points at the Garmin id from the Strava activity. Even
    # if raw_strava == raw_garmin numerically, this MUST be flagged.
    bad_activity = _insert_activity(
        db_conn, source="strava", source_id="zzz", raw_ref_id=raw_garmin + 1000,
    )
    # A correctly-pointing strava activity to make sure the join works.
    good_activity = _insert_activity(
        db_conn, source="strava", source_id="200", raw_ref_id=raw_strava,
    )
    report = check_fitness_integrity(db_conn)
    activity_ids = [o.activity_id for o in report.activities]
    assert bad_activity in activity_ids
    assert good_activity not in activity_ids


def test_daily_with_partial_orphan_refs_is_reported(
    db_conn: sqlite3.Connection,
) -> None:
    _seed_user(db_conn)
    raw_sleep = _insert_garmin_raw(db_conn, "sleep", "2026-05-09")
    daily_id = _insert_daily(
        db_conn,
        local_date="2026-05-09",
        raw_ref_ids_json=f"[{raw_sleep}, 99998, 99999]",
    )
    report = check_fitness_integrity(db_conn)
    assert report.activities == []
    assert report.daily == [
        DailyOrphan(daily_id=daily_id, source="garmin", missing_raw_ids=[99998, 99999]),
    ]


def test_daily_with_empty_refs_is_not_reported(db_conn: sqlite3.Connection) -> None:
    _seed_user(db_conn)
    _insert_daily(db_conn, local_date="2026-05-08", raw_ref_ids_json="[]")
    report = check_fitness_integrity(db_conn)
    assert report.daily == []
