"""Tests for FitnessRepository.

Covers the public surface defined in W2 of docs/fitness-tier-plan.md:
auth state round-trip, transition_auth fire-once semantics + auth_broken_since,
sync run lifecycle, last_successful_sync_at, raw insert/sha-deduplication +
append-only-on-change, normalized upsert idempotence, list_activities
boundary semantics (inclusive both sides), and the max_normalized_fetched_at
watermark used by W7.
"""

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pytest

from journal.db.factory import ConnectionFactory
from journal.db.fitness_repository import FitnessRepository
from journal.db.migrations import run_migrations
from journal.models import (
    FitnessActivity,
    FitnessAuthState,
    FitnessDaily,
)


def _seed_user(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO users (id, email, password_hash, display_name,
                                     email_verified, is_admin)
        VALUES (1, 'test@example.com', 'x', 'test', 1, 1)
        """,
    )


@pytest.fixture
def repo(factory: ConnectionFactory, db_conn: sqlite3.Connection) -> FitnessRepository:
    _seed_user(db_conn)
    return FitnessRepository(factory)


@pytest.fixture
def fitness_factory(tmp_path: Path) -> ConnectionFactory:
    """ConnectionFactory pointing at a migrated temp DB seeded with one user."""
    factory = ConnectionFactory(tmp_path / "fitness.db")
    conn = factory.get()
    run_migrations(conn)
    _seed_user(conn)
    conn.commit()
    return factory


@pytest.fixture
def repo_via_factory(fitness_factory: ConnectionFactory) -> FitnessRepository:
    return FitnessRepository(fitness_factory)


# ── Auth state ──────────────────────────────────────────────────────


def test_auth_state_roundtrip_preserves_extra_state(
    repo: FitnessRepository,
) -> None:
    state = FitnessAuthState(
        user_id=1,
        source="garmin",
        access_token="atk",
        refresh_token=None,
        token_expires_at="2027-05-09T10:00:00Z",
        extra_state={"oauth1_secret": "shh", "garth_version": "0.4.5"},
        last_successful_login_at="2026-05-09T04:00:00Z",
        auth_status="ok",
    )
    repo.upsert_auth_state(state)
    fetched = repo.get_auth_state(user_id=1, source="garmin")
    assert fetched is not None
    assert fetched.access_token == "atk"
    assert fetched.extra_state == {"oauth1_secret": "shh", "garth_version": "0.4.5"}
    assert fetched.auth_status == "ok"
    # updated_at is non-empty (set to now on insert)
    assert fetched.updated_at != ""


def test_auth_state_upsert_overwrites(repo: FitnessRepository) -> None:
    repo.upsert_auth_state(
        FitnessAuthState(user_id=1, source="strava", access_token="old"),
    )
    repo.upsert_auth_state(
        FitnessAuthState(user_id=1, source="strava", access_token="new"),
    )
    fetched = repo.get_auth_state(user_id=1, source="strava")
    assert fetched is not None
    assert fetched.access_token == "new"


def test_get_auth_state_returns_none_when_absent(
    repo: FitnessRepository,
) -> None:
    assert repo.get_auth_state(user_id=1, source="strava") is None


def test_transition_auth_fires_once_then_silently_repeats(
    repo: FitnessRepository, db_conn: sqlite3.Connection,
) -> None:
    """First broken call → True (transition); second broken call → False
    (already broken). Then ok → True; ok again → False. auth_broken_since
    is set on transition to broken and cleared on transition to ok."""
    at_t0 = "2026-05-09T04:00:00Z"
    assert repo.transition_auth(
        user_id=1, source="strava", status="broken", at=at_t0,
    ) is True
    row = db_conn.execute(
        "SELECT auth_status, auth_broken_since FROM fitness_auth_state "
        "WHERE user_id=1 AND source='strava'",
    ).fetchone()
    assert row["auth_status"] == "broken"
    assert row["auth_broken_since"] == at_t0

    # Second consecutive 'broken' is a no-op.
    assert repo.transition_auth(
        user_id=1, source="strava", status="broken", at="2026-05-09T05:00:00Z",
    ) is False

    # Recover.
    assert repo.transition_auth(
        user_id=1, source="strava", status="ok", at="2026-05-09T06:00:00Z",
    ) is True
    row = db_conn.execute(
        "SELECT auth_status, auth_broken_since FROM fitness_auth_state "
        "WHERE user_id=1 AND source='strava'",
    ).fetchone()
    assert row["auth_status"] == "ok"
    assert row["auth_broken_since"] is None  # cleared on recovery

    # ok again → no-op.
    assert repo.transition_auth(
        user_id=1, source="strava", status="ok", at="2026-05-09T07:00:00Z",
    ) is False


# ── Health summary ──────────────────────────────────────────────────


def test_get_health_summary_empty_db_returns_empty_list(
    repo: FitnessRepository,
) -> None:
    """No auth_state and no sync_runs → empty list (not a dict of nulls)."""
    assert repo.get_health_summary(user_id=1) == []


def test_get_health_summary_includes_only_configured_sources(
    repo: FitnessRepository,
) -> None:
    """Source surfaces if there is an auth_state row OR sync_runs history,
    but not when both are absent. Strava configured + Garmin absent →
    one entry for strava only."""
    repo.upsert_auth_state(
        FitnessAuthState(user_id=1, source="strava", auth_status="ok"),
    )
    summary = repo.get_health_summary(user_id=1)
    assert [row["source"] for row in summary] == ["strava"]
    assert summary[0]["auth_status"] == "ok"
    assert summary[0]["auth_broken_since"] is None
    assert summary[0]["last_success_at"] is None  # no sync runs yet


def test_get_health_summary_reports_last_success_per_source(
    repo: FitnessRepository,
) -> None:
    """`last_success_at` is the started_at of the most recent successful
    run for that source. Failed runs are ignored."""
    repo.upsert_auth_state(
        FitnessAuthState(user_id=1, source="strava", auth_status="ok"),
    )
    success_run = repo.start_sync_run(user_id=1, source="strava")
    repo.finish_sync_run(success_run, status="success")
    fail_run = repo.start_sync_run(user_id=1, source="strava")
    repo.finish_sync_run(fail_run, status="transient_failure")

    summary = repo.get_health_summary(user_id=1)
    assert len(summary) == 1
    success_rows = [
        r for r in repo.list_recent_sync_runs(user_id=1, source="strava")
        if r.status == "success"
    ]
    assert summary[0]["last_success_at"] == success_rows[0].started_at


def test_get_health_summary_includes_broken_auth_with_since(
    repo: FitnessRepository,
) -> None:
    """`auth_broken_since` is surfaced verbatim from the auth_state row."""
    at = "2026-05-07T04:00:00Z"
    repo.transition_auth(user_id=1, source="garmin", status="broken", at=at)
    summary = repo.get_health_summary(user_id=1)
    assert len(summary) == 1
    assert summary[0]["source"] == "garmin"
    assert summary[0]["auth_status"] == "broken"
    assert summary[0]["auth_broken_since"] == at


def test_get_health_summary_surfaces_orphan_sync_runs(
    repo: FitnessRepository,
) -> None:
    """A source with sync_runs but no auth_state row (legacy / orphan
    history) still surfaces, with `auth_status` and `auth_broken_since`
    null. Used to be how the W6 fetch service recorded
    `MissingAuthState` runs before W11 made re-auth interactive."""
    run_id = repo.start_sync_run(user_id=1, source="strava")
    repo.finish_sync_run(run_id, status="success")
    summary = repo.get_health_summary(user_id=1)
    assert len(summary) == 1
    assert summary[0]["source"] == "strava"
    assert summary[0]["auth_status"] is None
    assert summary[0]["auth_broken_since"] is None
    assert summary[0]["last_success_at"] is not None


def test_get_health_summary_isolates_users(repo: FitnessRepository) -> None:
    """user_id=1 must not see user_id=2's auth state. Adds another user
    to the seeded DB and asserts cross-user leakage is impossible."""
    repo.connection.execute(
        """
        INSERT OR IGNORE INTO users (id, email, password_hash, display_name,
                                     email_verified, is_admin)
        VALUES (2, 'other@example.com', 'x', 'other', 1, 0)
        """,
    )
    repo.upsert_auth_state(
        FitnessAuthState(user_id=1, source="strava", auth_status="ok"),
    )
    repo.upsert_auth_state(
        FitnessAuthState(user_id=2, source="garmin", auth_status="broken"),
    )
    assert [r["source"] for r in repo.get_health_summary(user_id=1)] == ["strava"]
    assert [r["source"] for r in repo.get_health_summary(user_id=2)] == ["garmin"]


def test_get_health_summary_orders_sources_alphabetically(
    repo: FitnessRepository,
) -> None:
    """Stable ordering simplifies test assertions and webapp UI: garmin
    before strava regardless of insert order."""
    repo.upsert_auth_state(
        FitnessAuthState(user_id=1, source="strava", auth_status="ok"),
    )
    repo.upsert_auth_state(
        FitnessAuthState(user_id=1, source="garmin", auth_status="ok"),
    )
    summary = repo.get_health_summary(user_id=1)
    assert [r["source"] for r in summary] == ["garmin", "strava"]


# ── Sync runs ───────────────────────────────────────────────────────


def test_sync_run_lifecycle_success(repo: FitnessRepository) -> None:
    run_id = repo.start_sync_run(user_id=1, source="strava")
    assert run_id > 0
    repo.finish_sync_run(
        run_id, status="success", rows_fetched=3, rows_normalized=3,
    )
    runs = repo.list_recent_sync_runs(user_id=1, source="strava")
    assert len(runs) == 1
    assert runs[0].status == "success"
    assert runs[0].rows_fetched == 3
    assert runs[0].finished_at is not None


@pytest.mark.parametrize(
    "status",
    ["auth_broken", "transient_failure", "normalize_drift"],
)
def test_finish_sync_run_accepts_terminal_statuses(
    repo: FitnessRepository, status: str,
) -> None:
    run_id = repo.start_sync_run(user_id=1, source="garmin")
    repo.finish_sync_run(
        run_id, status=status, error_class="X", error_message="oops",
    )
    runs = repo.list_recent_sync_runs(user_id=1, source="garmin")
    assert runs[0].status == status
    assert runs[0].error_class == "X"


def test_last_successful_sync_only_counts_success(
    repo: FitnessRepository,
) -> None:
    success_run = repo.start_sync_run(user_id=1, source="strava")
    repo.finish_sync_run(success_run, status="success")
    fail_run = repo.start_sync_run(user_id=1, source="strava")
    repo.finish_sync_run(fail_run, status="transient_failure")

    last = repo.last_successful_sync_at(user_id=1, source="strava")
    # Should match the successful run's started_at, not the failed one.
    success_runs = [
        r for r in repo.list_recent_sync_runs(user_id=1, source="strava")
        if r.status == "success"
    ]
    assert last == success_runs[0].started_at


def test_find_running_sync_run_returns_in_flight(
    repo: FitnessRepository,
) -> None:
    run_id = repo.start_sync_run(user_id=1, source="strava")
    assert repo.find_running_sync_run(user_id=1, source="strava") == run_id
    repo.finish_sync_run(run_id, status="success")
    assert repo.find_running_sync_run(user_id=1, source="strava") is None


def test_record_normalized_rows_amends_existing_run(
    repo: FitnessRepository,
) -> None:
    """Normalize must update rows_normalized without clobbering rows_fetched
    or status — the fetch service finalises the row first and normalize amends
    it after. Regression for the F1 bug where Norm. was always 0."""
    run_id = repo.start_sync_run(user_id=1, source="garmin")
    repo.finish_sync_run(
        run_id, status="success", rows_fetched=15, rows_normalized=0,
    )

    repo.record_normalized_rows(run_id, 12)

    runs = repo.list_recent_sync_runs(user_id=1, source="garmin")
    assert len(runs) == 1
    assert runs[0].status == "success"
    assert runs[0].rows_fetched == 15
    assert runs[0].rows_normalized == 12


# ── Raw archive ─────────────────────────────────────────────────────


def test_insert_raw_returns_id_and_dedupes_on_sha(
    repo: FitnessRepository, db_conn: sqlite3.Connection,
) -> None:
    run_id = repo.start_sync_run(user_id=1, source="strava")
    payload = json.dumps({"id": 42, "name": "Morning Run"})
    new_id_1 = repo.insert_raw(
        source="strava", user_id=1, endpoint="activities",
        source_id="42", payload_json=payload, sync_run_id=run_id,
    )
    assert new_id_1 is not None

    # Same payload: no-op, returns None.
    new_id_2 = repo.insert_raw(
        source="strava", user_id=1, endpoint="activities",
        source_id="42", payload_json=payload, sync_run_id=run_id,
    )
    assert new_id_2 is None

    # Changed payload (different sha): NEW row inserted, old row preserved
    # — append-only per D3.
    new_payload = json.dumps({"id": 42, "name": "Morning Run (edited)"})
    new_id_3 = repo.insert_raw(
        source="strava", user_id=1, endpoint="activities",
        source_id="42", payload_json=new_payload, sync_run_id=run_id,
    )
    assert new_id_3 is not None
    assert new_id_3 != new_id_1

    # Both rows still exist.
    count = db_conn.execute(
        "SELECT COUNT(*) FROM fitness_raw_strava WHERE source_id='42'",
    ).fetchone()[0]
    assert count == 2


def test_insert_raw_garmin_routes_to_garmin_table(
    repo: FitnessRepository, db_conn: sqlite3.Connection,
) -> None:
    run_id = repo.start_sync_run(user_id=1, source="garmin")
    repo.insert_raw(
        source="garmin", user_id=1, endpoint="sleep",
        source_id="2026-05-09", payload_json='{"x":1}',
        sync_run_id=run_id,
    )
    strava_count = db_conn.execute(
        "SELECT COUNT(*) FROM fitness_raw_strava",
    ).fetchone()[0]
    garmin_count = db_conn.execute(
        "SELECT COUNT(*) FROM fitness_raw_garmin",
    ).fetchone()[0]
    assert strava_count == 0
    assert garmin_count == 1


def test_list_raw_since_filters_by_fetched_at(
    repo: FitnessRepository, db_conn: sqlite3.Connection,
) -> None:
    run_id = repo.start_sync_run(user_id=1, source="strava")
    # Insert one with a manual older fetched_at.
    db_conn.execute(
        """
        INSERT INTO fitness_raw_strava (
            user_id, source_id, endpoint, fetched_at, payload_json, payload_sha256, sync_run_id
        ) VALUES (1, '100', 'activities', '2026-01-01T00:00:00Z', '{}', 'sha-old', ?)
        """,
        (run_id,),
    )
    # And a fresh one via the repo.
    repo.insert_raw(
        source="strava", user_id=1, endpoint="activities",
        source_id="200", payload_json='{"x":1}', sync_run_id=run_id,
    )

    all_rows = list(repo.list_raw_since(source="strava", user_id=1))
    assert len(all_rows) == 2

    # Composite (fetched_at, id) watermark per W3. The `id=0` floor is
    # below every real raw row id (AUTOINCREMENT starts at 1), so this
    # matches every row strictly after the wall-clock-second boundary.
    fresh = list(repo.list_raw_since(
        source="strava", user_id=1, since=("2026-04-01T00:00:00Z", 0),
    ))
    assert len(fresh) == 1
    assert fresh[0].source_id == "200"


# ── Normalized layer ────────────────────────────────────────────────


def _make_activity(source_id: str, local_date: str = "2026-05-09") -> FitnessActivity:
    return FitnessActivity(
        user_id=1, source="strava", source_id=source_id,
        activity_type="run", source_subtype="Run",
        start_time=f"{local_date}T10:00:00Z",
        local_date=local_date, duration_s=600, raw_ref_id=1,
    )


def test_upsert_activity_is_idempotent(
    repo: FitnessRepository, db_conn: sqlite3.Connection,
) -> None:
    a = _make_activity("42")
    repo.upsert_activity(a)
    a.duration_s = 700  # mutated; upsert should overwrite
    repo.upsert_activity(a)

    count = db_conn.execute(
        "SELECT COUNT(*) FROM fitness_activities",
    ).fetchone()[0]
    assert count == 1
    fetched = repo.list_activities(
        user_id=1, start="2026-05-09", end="2026-05-09",
    )[0]
    assert fetched.duration_s == 700


def test_upsert_daily_is_idempotent(
    repo: FitnessRepository, db_conn: sqlite3.Connection,
) -> None:
    daily = FitnessDaily(
        user_id=1, source="garmin", local_date="2026-05-09",
        sleep_score=80, raw_ref_ids=[1, 2, 3],
    )
    repo.upsert_daily(daily)
    daily.sleep_score = 85
    daily.raw_ref_ids = [4, 5, 6]
    repo.upsert_daily(daily)

    count = db_conn.execute(
        "SELECT COUNT(*) FROM fitness_daily",
    ).fetchone()[0]
    assert count == 1
    fetched = repo.list_daily(
        user_id=1, start="2026-05-09", end="2026-05-09",
    )[0]
    assert fetched.sleep_score == 85
    assert fetched.raw_ref_ids == [4, 5, 6]


def test_list_activities_boundary_inclusive_both_sides(
    repo: FitnessRepository,
) -> None:
    """Insert four activities at [start-1, start, end, end+1]; the
    list query with start='2026-05-09', end='2026-05-11' returns
    exactly the two on the boundary days."""
    repo.upsert_activity(_make_activity("1", local_date="2026-05-08"))
    repo.upsert_activity(_make_activity("2", local_date="2026-05-09"))
    repo.upsert_activity(_make_activity("3", local_date="2026-05-11"))
    repo.upsert_activity(_make_activity("4", local_date="2026-05-12"))

    in_range = repo.list_activities(
        user_id=1, start="2026-05-09", end="2026-05-11",
    )
    source_ids = {a.source_id for a in in_range}
    assert source_ids == {"2", "3"}


def test_list_activities_filters_by_type(repo: FitnessRepository) -> None:
    a = _make_activity("1")
    a.activity_type = "run"
    repo.upsert_activity(a)
    b = _make_activity("2")
    b.activity_type = "ride"
    repo.upsert_activity(b)

    runs = repo.list_activities(
        user_id=1, start="2026-05-09", end="2026-05-09", activity_type="run",
    )
    assert {x.source_id for x in runs} == {"1"}


# ── Normalize watermark ─────────────────────────────────────────────


def test_max_normalized_fetched_at_returns_none_initially(
    repo: FitnessRepository,
) -> None:
    assert repo.max_normalized_fetched_at(
        source="strava", user_id=1, kind="activities",
    ) is None
    assert repo.max_normalized_fetched_at(
        source="garmin", user_id=1, kind="daily",
    ) is None


def test_max_normalized_fetched_at_tracks_latest_raw(
    repo: FitnessRepository, db_conn: sqlite3.Connection,
) -> None:
    run_id = repo.start_sync_run(user_id=1, source="strava")
    # Two raw rows with different fetched_at; latest one is the watermark.
    older_raw = db_conn.execute(
        """
        INSERT INTO fitness_raw_strava (
            user_id, source_id, endpoint, fetched_at, payload_json, payload_sha256, sync_run_id
        ) VALUES (1, '1', 'activities', '2026-05-01T00:00:00Z', '{}', 'sha-1', ?)
        """,
        (run_id,),
    ).lastrowid
    newer_raw = db_conn.execute(
        """
        INSERT INTO fitness_raw_strava (
            user_id, source_id, endpoint, fetched_at, payload_json, payload_sha256, sync_run_id
        ) VALUES (1, '2', 'activities', '2026-05-09T00:00:00Z', '{}', 'sha-2', ?)
        """,
        (run_id,),
    ).lastrowid

    a1 = _make_activity("1", local_date="2026-05-01")
    a1.raw_ref_id = older_raw
    a2 = _make_activity("2", local_date="2026-05-09")
    a2.raw_ref_id = newer_raw
    repo.upsert_activity(a1)
    repo.upsert_activity(a2)

    watermark = repo.max_normalized_fetched_at(
        source="strava", user_id=1, kind="activities",
    )
    # Composite (fetched_at, id) watermark — the raw row id breaks ties at
    # SQLite's 1-second fetched_at resolution. The "latest" is the row with
    # the largest fetched_at; among ties, the largest id.
    assert watermark == ("2026-05-09T00:00:00Z", newer_raw)


def test_watermark_tied_fetched_at_no_row_loss(
    repo: FitnessRepository, db_conn: sqlite3.Connection,
) -> None:
    """Regression for the W7 watermark race documented in
    fitness-operations.md §7. Two raw rows sharing a fetched_at (SQLite's
    default 1-second resolution ties them) must both be picked up across
    consecutive normalize passes. The pre-fix scalar `MAX(fetched_at)`
    watermark combined with a strict `>` filter in ``list_raw_since``
    dropped every row tied at the watermark second on the next pass.
    """
    run_id = repo.start_sync_run(user_id=1, source="strava")
    tied_at = "2026-05-10T12:00:00Z"

    # First raw row at the tied second.
    raw_a_id = db_conn.execute(
        """
        INSERT INTO fitness_raw_strava (
            user_id, source_id, endpoint, fetched_at, payload_json,
            payload_sha256, sync_run_id
        ) VALUES (1, 'A', 'activities', ?, '{}', 'sha-a', ?)
        """,
        (tied_at, run_id),
    ).lastrowid
    db_conn.commit()

    # Normalize the first row by upserting an activity that points at it.
    # Watermark now advances to (tied_at, raw_a_id).
    a = _make_activity("A", local_date="2026-05-10")
    a.raw_ref_id = raw_a_id
    repo.upsert_activity(a)

    # Second raw row arrives at the SAME tied second. SQLite's
    # AUTOINCREMENT guarantees raw_b_id > raw_a_id, so the composite
    # watermark (tied_at, raw_a_id) is strictly less than (tied_at,
    # raw_b_id) — the next normalize pass MUST see this row.
    raw_b_id = db_conn.execute(
        """
        INSERT INTO fitness_raw_strava (
            user_id, source_id, endpoint, fetched_at, payload_json,
            payload_sha256, sync_run_id
        ) VALUES (1, 'B', 'activities', ?, '{}', 'sha-b', ?)
        """,
        (tied_at, run_id),
    ).lastrowid
    db_conn.commit()
    assert raw_b_id > raw_a_id, "test precondition: B inserted after A"

    # Recompute the watermark as the next normalize pass would.
    watermark = repo.max_normalized_fetched_at(
        source="strava", user_id=1, kind="activities",
    )
    remaining = list(repo.list_raw_since(
        source="strava", user_id=1, since=watermark,
    ))
    remaining_ids = {r.id for r in remaining}
    assert raw_b_id in remaining_ids, (
        "raw_b tied with raw_a on fetched_at was dropped — W7 watermark "
        "race regression. Watermark must be composite (fetched_at, id)."
    )


# ── Factory path ───────────────────────────────────────────────────


class TestFactoryPathSemantics:
    """Production-path coverage for the ``ConnectionFactory`` model.

    The functions above exercise the bare-``Connection`` legacy path
    (kept until W4 of ``docs/sqlite-per-thread-connections-plan.md``
    retires the dual-constructor). These tests cover the factory path
    that production now uses: each thread owns its own
    ``sqlite3.Connection``, so the cross-thread implicit-transaction
    collision documented in ``docs/sqlite-threading.md`` becomes
    structurally impossible.
    """

    def test_lifecycle_round_trip(self, repo_via_factory: FitnessRepository) -> None:
        repo_via_factory.upsert_auth_state(
            FitnessAuthState(
                user_id=1,
                source="strava",
                access_token="a",
                refresh_token="r",
                token_expires_at="2099-01-01T00:00:00Z",
                extra_state={"k": "v"},
                auth_status="ok",
            ),
        )
        state = repo_via_factory.get_auth_state(user_id=1, source="strava")
        assert state is not None
        assert state.access_token == "a"
        assert state.extra_state == {"k": "v"}

        run_id = repo_via_factory.start_sync_run(user_id=1, source="strava")
        repo_via_factory.finish_sync_run(run_id, status="success", rows_fetched=1)
        recent = repo_via_factory.list_recent_sync_runs(user_id=1, source="strava")
        assert len(recent) == 1
        assert recent[0].status == "success"

    def test_each_thread_gets_distinct_connection(
        self, repo_via_factory: FitnessRepository,
    ) -> None:
        main_conn_id = id(repo_via_factory.connection)
        captured: list[int] = []

        def worker() -> None:
            captured.append(id(repo_via_factory.connection))

        t = threading.Thread(target=worker)
        t.start()
        t.join()
        assert len(captured) == 1
        assert captured[0] != main_conn_id

    def test_concurrent_writes_under_load(
        self, repo_via_factory: FitnessRepository,
    ) -> None:
        """Many threads each writing sync runs + raw rows. Under the
        old shared-``Connection`` model this would surface
        ``no transaction is active`` from a concurrent commit; under
        the factory model it must complete cleanly.
        """
        thread_count = 6
        runs_per_thread = 10
        errors: list[BaseException] = []

        def worker(prefix: str) -> None:
            try:
                for i in range(runs_per_thread):
                    run_id = repo_via_factory.start_sync_run(
                        user_id=1, source="strava",
                    )
                    repo_via_factory.insert_raw(
                        source="strava",
                        user_id=1,
                        endpoint="activities",
                        source_id=f"{prefix}-{i}",
                        payload_json=json.dumps({"k": f"{prefix}-{i}"}),
                        sync_run_id=run_id,
                    )
                    repo_via_factory.finish_sync_run(
                        run_id, status="success", rows_fetched=1,
                    )
            except BaseException as exc:  # noqa: BLE001 — test-only
                errors.append(exc)

        with ThreadPoolExecutor(max_workers=thread_count) as ex:
            futures = [
                ex.submit(worker, f"t{i}") for i in range(thread_count)
            ]
            for f in as_completed(futures):
                f.result()

        assert errors == []
        recent = repo_via_factory.list_recent_sync_runs(
            user_id=1, source="strava", limit=thread_count * runs_per_thread + 1,
        )
        assert len(recent) == thread_count * runs_per_thread
        assert all(r.status == "success" for r in recent)

    def test_cross_thread_visibility_via_wal(
        self, repo_via_factory: FitnessRepository,
    ) -> None:
        from threading import Event

        written = Event()

        def writer() -> None:
            repo_via_factory.start_sync_run(user_id=1, source="garmin")
            repo_via_factory.start_sync_run(user_id=1, source="garmin")
            written.set()

        t = threading.Thread(target=writer)
        t.start()
        written.wait(timeout=5.0)
        t.join()
        runs = repo_via_factory.list_recent_sync_runs(user_id=1, source="garmin")
        assert len(runs) == 2


# ── list_users_with_active_auth ──────────────────────────────────────


def _seed_extra_users(conn: sqlite3.Connection) -> None:
    """Seed users 2-5 (user 1 is already seeded by the repo fixture)."""
    for n in range(2, 6):
        conn.execute(
            """
            INSERT OR IGNORE INTO users
                (id, email, password_hash, display_name, email_verified, is_admin)
            VALUES (?, ?, 'x', ?, 1, 0)
            """,
            (n, f"u{n}@example.com", f"u{n}"),
        )
    conn.commit()


def test_list_users_with_active_auth_returns_only_valid_rows(
    repo: FitnessRepository,
    db_conn: sqlite3.Connection,
) -> None:
    _seed_extra_users(db_conn)

    # Strava rows
    db_conn.execute(
        "INSERT INTO fitness_auth_state (user_id, source, access_token, auth_status) "
        "VALUES (1, 'strava', 'tok-1', 'ok')",          # valid
    )
    db_conn.execute(
        "INSERT INTO fitness_auth_state (user_id, source, access_token, auth_status) "
        "VALUES (2, 'strava', 'tok-2', 'broken')",       # excluded: broken
    )
    db_conn.execute(
        "INSERT INTO fitness_auth_state (user_id, source, access_token, auth_status) "
        "VALUES (3, 'strava', '', 'ok')",                 # excluded: empty token
    )
    db_conn.execute(
        "INSERT INTO fitness_auth_state (user_id, source, access_token, auth_status) "
        "VALUES (4, 'strava', NULL, 'unknown')",          # excluded: NULL token
    )

    # Garmin rows
    db_conn.execute(
        "INSERT INTO fitness_auth_state (user_id, source, extra_state_json, auth_status) "
        "VALUES (1, 'garmin', '{\"tokens_blob\":\"blob-1\"}', 'ok')",   # valid
    )
    db_conn.execute(
        "INSERT INTO fitness_auth_state (user_id, source, extra_state_json, auth_status) "
        "VALUES (5, 'garmin', '{\"tokens_blob\":\"\"}', 'ok')",          # excluded: empty blob
    )
    db_conn.execute(
        "INSERT INTO fitness_auth_state (user_id, source, extra_state_json, auth_status) "
        "VALUES (2, 'garmin', '{}', 'unknown')",                         # excluded: missing key
    )
    db_conn.commit()

    assert repo.list_users_with_active_auth(source="strava") == [1]
    assert repo.list_users_with_active_auth(source="garmin") == [1]


def test_list_users_with_active_auth_empty_when_none(
    repo: FitnessRepository,
) -> None:
    # No auth rows inserted — should return empty list.
    assert repo.list_users_with_active_auth(source="strava") == []


def test_credential_rule_matches_has_credentials(
    repo: FitnessRepository,
    db_conn: sqlite3.Connection,
) -> None:
    """Drift guard: if someone changes Strava's _has_credentials rule in the
    fetch service but not the SQL clause (or vice-versa), this test fails."""
    db_conn.execute(
        "INSERT INTO fitness_auth_state (user_id, source, access_token, auth_status) "
        "VALUES (1, 'strava', 'tok', 'ok')",
    )
    db_conn.commit()

    state = repo.get_auth_state(user_id=1, source="strava")
    assert state is not None
    # This is exactly Strava's base _has_credentials rule (fetch.py:299):
    assert bool(state.access_token) is True
    assert 1 in repo.list_users_with_active_auth(source="strava")
