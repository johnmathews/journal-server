"""Tests for the soft-pointer integrity checker.

The checker resolves `fitness_activities.raw_ref_id` (scalar) and
`fitness_daily.raw_ref_ids_json` (JSON array) into the per-source raw
tables and reports any orphans. Tests build deliberately-dirty fixture
DBs containing valid + orphaned references and assert the checker
finds the orphans without false positives.

Per-user scoping (W4): every assertion is for user_id=1 unless the test
explicitly exercises multi-user isolation.
"""

import sqlite3

from journal.db.fitness_integrity import (
    ActivityOrphan,
    DailyOrphan,
    check_fitness_integrity,
)


def _seed_user(
    conn: sqlite3.Connection, *, user_id: int = 1, email: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO users (id, email, password_hash, display_name,
                                     email_verified, is_admin)
        VALUES (?, ?, 'x', 'test', 1, 1)
        """,
        (user_id, email or f"u{user_id}@example.com"),
    )


def _insert_strava_raw(
    conn: sqlite3.Connection, source_id: str = "100", *, user_id: int = 1,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO fitness_raw_strava (user_id, source_id, endpoint,
            payload_json, payload_sha256)
        VALUES (?, ?, 'activities', '{}', ?)
        """,
        (user_id, source_id, f"sha-strava-{user_id}-{source_id}"),
    )
    return cur.lastrowid  # type: ignore[return-value]


def _insert_garmin_raw(
    conn: sqlite3.Connection, endpoint: str, source_id: str, *, user_id: int = 1,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO fitness_raw_garmin (user_id, source_id, endpoint,
            payload_json, payload_sha256)
        VALUES (?, ?, ?, '{}', ?)
        """,
        (user_id, source_id, endpoint, f"sha-{endpoint}-{user_id}-{source_id}"),
    )
    return cur.lastrowid  # type: ignore[return-value]


def _insert_activity(
    conn: sqlite3.Connection, *, source: str, source_id: str, raw_ref_id: int,
    user_id: int = 1,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO fitness_activities (user_id, source, source_id, activity_type,
            source_subtype, start_time, local_date, duration_s, raw_ref_id)
        VALUES (?, ?, ?, 'run', 'Run', '2026-05-09T10:00:00Z', '2026-05-09', 100, ?)
        """,
        (user_id, source, source_id, raw_ref_id),
    )
    return cur.lastrowid  # type: ignore[return-value]


def _insert_daily(
    conn: sqlite3.Connection, *, local_date: str, raw_ref_ids_json: str,
    user_id: int = 1,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO fitness_daily (user_id, source, local_date, raw_ref_ids_json)
        VALUES (?, 'garmin', ?, ?)
        """,
        (user_id, local_date, raw_ref_ids_json),
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

    report = check_fitness_integrity(db_conn, user_id=1)
    assert report.activities == []
    assert report.daily == []
    assert not report.has_orphans


def test_orphan_strava_activity_is_reported(db_conn: sqlite3.Connection) -> None:
    _seed_user(db_conn)
    activity_id = _insert_activity(
        db_conn, source="strava", source_id="999", raw_ref_id=99999,
    )
    report = check_fitness_integrity(db_conn, user_id=1)
    assert report.activities == [
        ActivityOrphan(activity_id=activity_id, source="strava", raw_ref_id=99999),
    ]
    assert report.daily == []


def test_orphan_garmin_activity_is_reported(db_conn: sqlite3.Connection) -> None:
    _seed_user(db_conn)
    activity_id = _insert_activity(
        db_conn, source="garmin", source_id="888", raw_ref_id=88888,
    )
    report = check_fitness_integrity(db_conn, user_id=1)
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
    report = check_fitness_integrity(db_conn, user_id=1)
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
    report = check_fitness_integrity(db_conn, user_id=1)
    assert report.activities == []
    assert report.daily == [
        DailyOrphan(daily_id=daily_id, source="garmin", missing_raw_ids=[99998, 99999]),
    ]


def test_daily_with_empty_refs_is_not_reported(db_conn: sqlite3.Connection) -> None:
    _seed_user(db_conn)
    _insert_daily(db_conn, local_date="2026-05-08", raw_ref_ids_json="[]")
    report = check_fitness_integrity(db_conn, user_id=1)
    assert report.daily == []


def test_orphans_are_user_scoped(db_conn: sqlite3.Connection) -> None:
    """W4 acceptance: orphans owned by user A must not appear in user B's
    report. Seed dangling pointers under both users and assert each user
    sees only their own."""
    _seed_user(db_conn, user_id=1, email="alice@example.com")
    _seed_user(db_conn, user_id=2, email="bob@example.com")

    alice_orphan_activity = _insert_activity(
        db_conn, source="strava", source_id="alice-orphan",
        raw_ref_id=77777, user_id=1,
    )
    bob_orphan_activity = _insert_activity(
        db_conn, source="garmin", source_id="bob-orphan",
        raw_ref_id=66666, user_id=2,
    )
    alice_orphan_daily = _insert_daily(
        db_conn, local_date="2026-05-09",
        raw_ref_ids_json="[11111, 22222]", user_id=1,
    )
    bob_orphan_daily = _insert_daily(
        db_conn, local_date="2026-05-09",
        raw_ref_ids_json="[33333, 44444]", user_id=2,
    )

    alice_report = check_fitness_integrity(db_conn, user_id=1)
    alice_activity_ids = [o.activity_id for o in alice_report.activities]
    alice_daily_ids = [o.daily_id for o in alice_report.daily]
    assert alice_activity_ids == [alice_orphan_activity]
    assert alice_daily_ids == [alice_orphan_daily]
    # Crucially: Bob's orphans do NOT leak into Alice's report.
    assert bob_orphan_activity not in alice_activity_ids
    assert bob_orphan_daily not in alice_daily_ids

    bob_report = check_fitness_integrity(db_conn, user_id=2)
    bob_activity_ids = [o.activity_id for o in bob_report.activities]
    bob_daily_ids = [o.daily_id for o in bob_report.daily]
    assert bob_activity_ids == [bob_orphan_activity]
    assert bob_daily_ids == [bob_orphan_daily]
    assert alice_orphan_activity not in bob_activity_ids
    assert alice_orphan_daily not in bob_daily_ids


def test_cross_user_raw_row_does_not_satisfy_soft_pointer(
    db_conn: sqlite3.Connection,
) -> None:
    """If user A's normalized activity has raw_ref_id=N and a raw row
    with id=N exists but is owned by user B, the integrity check must
    still flag the activity as an orphan — a cross-user join is data
    corruption, not a valid resolution."""
    _seed_user(db_conn, user_id=1, email="alice@example.com")
    _seed_user(db_conn, user_id=2, email="bob@example.com")

    # Bob owns a raw_strava row. Alice's activity points at it.
    bob_raw = _insert_strava_raw(db_conn, source_id="bob-100", user_id=2)
    alice_activity = _insert_activity(
        db_conn, source="strava", source_id="alice-100",
        raw_ref_id=bob_raw, user_id=1,
    )

    report = check_fitness_integrity(db_conn, user_id=1)
    activity_ids = [o.activity_id for o in report.activities]
    assert alice_activity in activity_ids, (
        "Alice's activity should be flagged as orphan even though a "
        "raw_strava row exists at that id — it belongs to Bob, not Alice."
    )


def test_cross_user_garmin_raw_does_not_satisfy_daily_pointer(
    db_conn: sqlite3.Connection,
) -> None:
    """Same as above but for the daily-rollup JSON-array path: a raw
    garmin row owned by user B at id=N must not silently satisfy user
    A's daily soft pointer to id=N."""
    _seed_user(db_conn, user_id=1, email="alice@example.com")
    _seed_user(db_conn, user_id=2, email="bob@example.com")

    # Bob owns a raw garmin sleep row. Alice's daily points at it.
    bob_raw = _insert_garmin_raw(db_conn, "sleep", "2026-05-09", user_id=2)
    alice_daily = _insert_daily(
        db_conn, local_date="2026-05-09",
        raw_ref_ids_json=f"[{bob_raw}]", user_id=1,
    )

    report = check_fitness_integrity(db_conn, user_id=1)
    assert report.daily == [
        DailyOrphan(
            daily_id=alice_daily, source="garmin", missing_raw_ids=[bob_raw],
        ),
    ]
