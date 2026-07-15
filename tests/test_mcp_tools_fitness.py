"""MCP tool tests for the fitness pipeline (W10).

Covers all eight ``@mcp.tool()`` registrations in
``mcp_server/tools/fitness.py``:

- ``fitness_list_activities``
- ``fitness_list_daily``
- ``fitness_sync_status``
- ``fitness_integrity_check``
- ``fitness_trigger_sync`` (with the W9-mirrored dedup posture)
- ``fitness_correlate_sleep_mood``           (Q1 from fitness-schema.md §8)
- ``fitness_correlate_weekly_runs_stress``   (Q2)
- ``fitness_correlate_hrv_mood``             (Q3)

Each tool is invoked directly with a fake ``Context`` whose
``request_context.lifespan_context`` carries the same keys as the
production services dict. The ``_user_id(ctx)`` helper reads from
the ``_current_user_id`` ContextVar — set per-test via a fixture.

The Q1/Q2/Q3 SQL is reproduced verbatim from
``docs/fitness-schema.md`` §8. Tests seed prod-shaped data (fitness
rows + journal entries + mood scores) so a regression in either the
SQL or the schema would be caught here.
"""

import json
import sqlite3
from collections.abc import Generator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from journal.auth import _current_user_id
from journal.db.factory import ConnectionFactory
from journal.db.fitness_repository import FitnessRepository
from journal.db.jobs_repository import SQLiteJobRepository
from journal.db.migrations import run_migrations
from journal.mcp_server.tools import fitness as fitness_tools
from journal.models import FitnessActivity, FitnessAuthState, FitnessDaily
from journal.services.jobs import JobRunner

_TEST_USER_ID = 1


# --------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _set_test_user() -> Generator[None]:
    token = _current_user_id.set(_TEST_USER_ID)
    yield
    _current_user_id.reset(token)


@pytest.fixture
def fitness_factory(tmp_path: Path) -> ConnectionFactory:
    f = ConnectionFactory(tmp_path / "fitness-mcp.db")
    run_migrations(f.get())
    return f


@pytest.fixture
def db(fitness_factory: ConnectionFactory) -> sqlite3.Connection:
    """Calling-thread connection — for raw SQL seeding only."""
    return fitness_factory.get()


@pytest.fixture
def fitness_repo(fitness_factory: ConnectionFactory) -> FitnessRepository:
    return FitnessRepository(fitness_factory)


@pytest.fixture
def jobs_repository(fitness_factory: ConnectionFactory) -> SQLiteJobRepository:
    return SQLiteJobRepository(fitness_factory)


@pytest.fixture
def job_runner(
    jobs_repository: SQLiteJobRepository,
) -> Generator[JobRunner]:
    """Unconfigured runner — submit_fitness_sync_* raises RuntimeError."""
    runner = JobRunner(
        job_repository=jobs_repository,
        entity_extraction_service=object(),  # type: ignore[arg-type]
        mood_backfill_callable=lambda **_: None,  # type: ignore[arg-type]
        mood_scoring_service=object(),  # type: ignore[arg-type]
        entry_repository=object(),  # type: ignore[arg-type]
    )
    yield runner
    runner.shutdown(wait=True, cancel_futures=False)


@pytest.fixture
def configured_runner(
    jobs_repository: SQLiteJobRepository,
) -> Generator[JobRunner]:
    """Runner with no-op fitness callables wired so submit_fitness_sync_*
    accepts the request without raising."""
    from journal.services.fitness.backfill import BackfillResult
    from journal.services.fitness.fetch import FitnessSyncResult
    from journal.services.fitness.normalize import NormalizeResult

    def _fetch(*, user_id: int) -> FitnessSyncResult:
        return FitnessSyncResult(
            status="success", run_id=0, rows_fetched=0, rows_normalized=0,
        )

    def _norm(source: str):
        def _do(*, user_id: int) -> NormalizeResult:
            return NormalizeResult(source=source, rows_normalized=0, drift_count=0)
        return _do

    def _bf(source: str):
        def _do(
            *, user_id: int, start: str, end: str | None = None,
        ) -> BackfillResult:
            return BackfillResult(
                source=source,  # type: ignore[arg-type]
                final_status="complete",
                windows_attempted=1, windows_succeeded=1,
                rows_fetched=0, rows_normalized=0,
            )
        return _do

    runner = JobRunner(
        job_repository=jobs_repository,
        entity_extraction_service=object(),  # type: ignore[arg-type]
        mood_backfill_callable=lambda **_: None,  # type: ignore[arg-type]
        mood_scoring_service=object(),  # type: ignore[arg-type]
        entry_repository=object(),  # type: ignore[arg-type]
        fetch_strava_callable=_fetch,
        fetch_garmin_callable=_fetch,
        normalize_strava_callable=_norm("strava"),
        normalize_garmin_callable=_norm("garmin"),
        backfill_strava_callable=_bf("strava"),
        backfill_garmin_callable=_bf("garmin"),
    )
    yield runner
    runner.shutdown(wait=True, cancel_futures=False)


def _make_ctx(
    *,
    fitness_repo: FitnessRepository,
    jobs_repository: SQLiteJobRepository,
    job_runner: JobRunner,
    fitness_factory: ConnectionFactory,
    strava_enabled: bool = True,
) -> SimpleNamespace:
    """Build a fake MCP Context whose ``request_context.lifespan_context``
    carries the keys the tools expect. SimpleNamespace is enough — the
    tools only ever subscript the dict.

    ``strava_enabled`` defaults to True here so the pre-mothball tests
    keep exercising the live-Strava paths; production defaults to False
    (STRAVA_ENABLED unset — roadmap D8 mothball)."""
    services = {
        "fitness_repo": fitness_repo,
        "job_repository": jobs_repository,
        "job_runner": job_runner,
        "db_factory": fitness_factory,
        "config": SimpleNamespace(strava_enabled=strava_enabled),
    }
    return SimpleNamespace(
        request_context=SimpleNamespace(lifespan_context=services),
    )


@pytest.fixture
def ctx(
    fitness_repo: FitnessRepository,
    jobs_repository: SQLiteJobRepository,
    job_runner: JobRunner,
    fitness_factory: ConnectionFactory,
) -> SimpleNamespace:
    return _make_ctx(
        fitness_repo=fitness_repo,
        jobs_repository=jobs_repository,
        job_runner=job_runner,
        fitness_factory=fitness_factory,
    )


@pytest.fixture
def configured_ctx(
    fitness_repo: FitnessRepository,
    jobs_repository: SQLiteJobRepository,
    configured_runner: JobRunner,
    fitness_factory: ConnectionFactory,
) -> SimpleNamespace:
    return _make_ctx(
        fitness_repo=fitness_repo,
        jobs_repository=jobs_repository,
        job_runner=configured_runner,
        fitness_factory=fitness_factory,
    )


@pytest.fixture
def strava_disabled_ctx(
    fitness_repo: FitnessRepository,
    jobs_repository: SQLiteJobRepository,
    configured_runner: JobRunner,
    fitness_factory: ConnectionFactory,
) -> SimpleNamespace:
    """Fully wired runner but STRAVA_ENABLED=false — the W1 mothball
    posture: the flag alone must make Strava unreachable."""
    return _make_ctx(
        fitness_repo=fitness_repo,
        jobs_repository=jobs_repository,
        job_runner=configured_runner,
        fitness_factory=fitness_factory,
        strava_enabled=False,
    )


# --------------------------------------------------------------------
# Seeding helpers
# --------------------------------------------------------------------


def _seed_raw_strava(repo: FitnessRepository, source_id: str) -> int:
    raw_id = repo.insert_raw(
        source="strava", user_id=_TEST_USER_ID, endpoint="activities",
        source_id=source_id, payload_json=json.dumps({"id": source_id}),
        sync_run_id=None,
    )
    assert raw_id is not None
    return raw_id


def _seed_raw_garmin(
    repo: FitnessRepository, endpoint: str, source_id: str,
) -> int:
    raw_id = repo.insert_raw(
        source="garmin", user_id=_TEST_USER_ID, endpoint=endpoint,
        source_id=source_id, payload_json=json.dumps({"id": source_id}),
        sync_run_id=None,
    )
    assert raw_id is not None
    return raw_id


def _seed_run(
    repo: FitnessRepository,
    *,
    source_id: str,
    local_date: str,
    distance_m: float = 5000.0,
    source: str = "strava",
) -> None:
    if source == "garmin":
        raw_id = _seed_raw_garmin(repo, "activities", source_id)
    else:
        raw_id = _seed_raw_strava(repo, source_id)
    repo.upsert_activity(
        FitnessActivity(
            user_id=_TEST_USER_ID, source=source, source_id=source_id,
            activity_type="run", source_subtype="Run",
            start_time=f"{local_date}T07:00:00Z", local_date=local_date,
            duration_s=1800, moving_time_s=1800, distance_m=distance_m,
            raw_ref_id=raw_id,
        ),
    )


def _seed_daily(
    repo: FitnessRepository,
    *,
    local_date: str,
    sleep_score: int | None = 80,
    sleep_efficiency_pct: float | None = 90.0,
    hrv: float | None = 55.0,
    stress_avg: int | None = None,
) -> None:
    raw_id = _seed_raw_garmin(repo, "sleep", local_date)
    repo.upsert_daily(
        FitnessDaily(
            user_id=_TEST_USER_ID, source="garmin", local_date=local_date,
            sleep_score=sleep_score,
            sleep_efficiency_pct=sleep_efficiency_pct,
            hrv_overnight_ms=hrv,
            stress_avg=stress_avg,
            raw_ref_ids=[raw_id],
        ),
    )


def _seed_entry_with_mood(
    db: sqlite3.Connection,
    *,
    entry_date: str,
    dimensions: dict[str, float],
) -> int:
    """Insert one entry on entry_date with the given mood dimensions."""
    cur = db.execute(
        """
        INSERT INTO entries (user_id, entry_date, source_type, raw_text,
            final_text, word_count)
        VALUES (?, ?, 'voice', ?, ?, ?)
        """,
        (_TEST_USER_ID, entry_date, "test", "test", 1),
    )
    entry_id = cur.lastrowid
    assert entry_id is not None
    for dim, score in dimensions.items():
        db.execute(
            "INSERT INTO mood_scores (entry_id, dimension, score) VALUES (?, ?, ?)",
            (entry_id, dim, score),
        )
    db.commit()
    return entry_id


# --------------------------------------------------------------------
# Read tools
# --------------------------------------------------------------------


def test_list_activities_returns_window(
    ctx: SimpleNamespace, fitness_repo: FitnessRepository,
) -> None:
    _seed_run(fitness_repo, source_id="A1", local_date="2026-05-02")
    _seed_run(fitness_repo, source_id="A2", local_date="2026-04-15")
    out = fitness_tools.fitness_list_activities(
        start="2026-05-01", end="2026-05-31", ctx=ctx,
    )
    assert len(out["items"]) == 1
    assert out["items"][0]["source_id"] == "A1"


def test_list_activities_type_filter(
    ctx: SimpleNamespace, fitness_repo: FitnessRepository,
) -> None:
    _seed_run(fitness_repo, source_id="R1", local_date="2026-05-02")
    # Seed a ride on the same window so the filter has to discriminate.
    raw = _seed_raw_strava(fitness_repo, "C1")
    fitness_repo.upsert_activity(
        FitnessActivity(
            user_id=_TEST_USER_ID, source="strava", source_id="C1",
            activity_type="ride", source_subtype="Ride",
            start_time="2026-05-03T07:00:00Z", local_date="2026-05-03",
            duration_s=1800, distance_m=20000.0, raw_ref_id=raw,
        ),
    )
    out = fitness_tools.fitness_list_activities(
        start="2026-05-01", end="2026-05-31", activity_type="run", ctx=ctx,
    )
    assert len(out["items"]) == 1
    assert out["items"][0]["activity_type"] == "run"


def test_list_daily_empty(ctx: SimpleNamespace) -> None:
    out = fitness_tools.fitness_list_daily(
        start="2026-05-01", end="2026-05-31", ctx=ctx,
    )
    assert out == {"items": []}


def test_list_daily_returns_window(
    ctx: SimpleNamespace, fitness_repo: FitnessRepository,
) -> None:
    _seed_daily(fitness_repo, local_date="2026-05-02")
    out = fitness_tools.fitness_list_daily(
        start="2026-05-01", end="2026-05-31", ctx=ctx,
    )
    assert len(out["items"]) == 1
    assert out["items"][0]["local_date"] == "2026-05-02"


def test_sync_status_empty_returns_null_per_source(ctx: SimpleNamespace) -> None:
    out = fitness_tools.fitness_sync_status(ctx=ctx)
    assert out == {"strava": None, "garmin": None}


def test_sync_status_populated(
    ctx: SimpleNamespace, fitness_repo: FitnessRepository,
) -> None:
    fitness_repo.upsert_auth_state(
        FitnessAuthState(
            user_id=_TEST_USER_ID, source="strava",
            access_token="tok", auth_status="ok",
        ),
    )
    run_id = fitness_repo.start_sync_run(user_id=_TEST_USER_ID, source="strava")
    fitness_repo.finish_sync_run(
        run_id, status="success", rows_fetched=5, rows_normalized=5,
    )
    out = fitness_tools.fitness_sync_status(ctx=ctx)
    assert out["garmin"] is None
    assert out["strava"]["auth_status"] == "ok"
    assert out["strava"]["last_runs"][0]["rows_normalized"] == 5


def test_integrity_clean(ctx: SimpleNamespace) -> None:
    out = fitness_tools.fitness_integrity_check(ctx=ctx)
    assert out == {"activities": [], "daily": []}


def test_integrity_with_orphans(
    ctx: SimpleNamespace, db: sqlite3.Connection,
) -> None:
    db.execute(
        """
        INSERT INTO fitness_activities (
            user_id, source, source_id, activity_type, source_subtype,
            start_time, local_date, duration_s, raw_ref_id
        ) VALUES (?, 'strava', 'ORPHAN', 'run', 'Run',
                  '2026-05-02T08:00:00Z', '2026-05-02', 1800, 99999)
        """,
        (_TEST_USER_ID,),
    )
    db.commit()
    out = fitness_tools.fitness_integrity_check(ctx=ctx)
    assert len(out["activities"]) == 1
    assert out["activities"][0]["raw_ref_id"] == 99999


def test_integrity_user_a_cannot_see_user_b_orphans(
    ctx: SimpleNamespace, db: sqlite3.Connection,
) -> None:
    """W1 of the fitness multi-user final-mile plan: the
    ``fitness_integrity_check`` MCP tool must scope to the calling
    user via ``_user_id(ctx)``. Seed orphans for both users; assert
    each user's tool invocation returns only their own.
    """
    user_a, user_b = 1, 2

    db.execute(
        """
        INSERT OR IGNORE INTO users (id, email, password_hash, display_name,
                                     email_verified, is_admin)
        VALUES (?, 'user2@example.com', 'x', 'user2', 1, 0)
        """,
        (user_b,),
    )
    db.execute(
        """
        INSERT INTO fitness_activities (
            user_id, source, source_id, activity_type, source_subtype,
            start_time, local_date, duration_s, raw_ref_id
        ) VALUES (?, 'strava', 'A-ORPHAN', 'run', 'Run',
                  '2026-05-02T08:00:00Z', '2026-05-02', 1800, 11111)
        """,
        (user_a,),
    )
    db.execute(
        """
        INSERT INTO fitness_activities (
            user_id, source, source_id, activity_type, source_subtype,
            start_time, local_date, duration_s, raw_ref_id
        ) VALUES (?, 'strava', 'B-ORPHAN', 'run', 'Run',
                  '2026-05-03T08:00:00Z', '2026-05-03', 1800, 22222)
        """,
        (user_b,),
    )
    db.commit()

    # The autouse ``_set_test_user`` fixture has set ``_current_user_id``
    # to user A; verify A sees only A's orphan.
    out_a = fitness_tools.fitness_integrity_check(ctx=ctx)
    a_refs = {o["raw_ref_id"] for o in out_a["activities"]}
    assert a_refs == {11111}, out_a

    # Swap the ContextVar to user B and re-invoke; B must only see B's.
    token = _current_user_id.set(user_b)
    try:
        out_b = fitness_tools.fitness_integrity_check(ctx=ctx)
    finally:
        _current_user_id.reset(token)
    b_refs = {o["raw_ref_id"] for o in out_b["activities"]}
    assert b_refs == {22222}, out_b
    assert a_refs.isdisjoint(b_refs)


# --------------------------------------------------------------------
# Operational tool: fitness_trigger_sync
# --------------------------------------------------------------------


def test_trigger_sync_unknown_source_returns_error(ctx: SimpleNamespace) -> None:
    out = fitness_tools.fitness_trigger_sync(source="whoop", ctx=ctx)
    assert "error" in out
    assert out["job_id"] is None


def test_trigger_sync_unconfigured_returns_error(ctx: SimpleNamespace) -> None:
    """Runner has no fitness callables — submit raises RuntimeError,
    surfaced as a structured error dict (not an exception)."""
    out = fitness_tools.fitness_trigger_sync(source="strava", ctx=ctx)
    assert out["job_id"] is None
    assert "not configured" in out["error"].lower()


def test_trigger_sync_configured_returns_job_id(
    configured_ctx: SimpleNamespace,
) -> None:
    out = fitness_tools.fitness_trigger_sync(source="strava", ctx=configured_ctx)
    assert out["job_id"] is not None
    assert "already_running" not in out


def test_trigger_sync_dedup_returns_existing_job(
    configured_ctx: SimpleNamespace, jobs_repository: SQLiteJobRepository,
) -> None:
    existing = jobs_repository.create(
        "fitness_sync_strava", {"user_id": _TEST_USER_ID}, user_id=_TEST_USER_ID,
    )
    jobs_repository.mark_running(existing.id)
    out = fitness_tools.fitness_trigger_sync(source="strava", ctx=configured_ctx)
    assert out["job_id"] == existing.id
    assert out["already_running"] is True


def test_trigger_sync_blocked_by_running_backfill(
    configured_ctx: SimpleNamespace, jobs_repository: SQLiteJobRepository,
) -> None:
    """W5 spanning idempotency: a running backfill should block a sync
    submit via the MCP tool surface, mirroring the REST surface."""
    existing = jobs_repository.create(
        "fitness_backfill_strava",
        {"user_id": _TEST_USER_ID, "start": "2026-01-01"},
        user_id=_TEST_USER_ID,
    )
    jobs_repository.mark_running(existing.id)
    out = fitness_tools.fitness_trigger_sync(source="strava", ctx=configured_ctx)
    assert out["job_id"] == existing.id
    assert out["already_running"] is True


# --------------------------------------------------------------------
# Operational tool: fitness_trigger_backfill (W5)
# --------------------------------------------------------------------


def test_trigger_backfill_unknown_source_returns_error(
    ctx: SimpleNamespace,
) -> None:
    out = fitness_tools.fitness_trigger_backfill(
        source="whoop", start="2026-01-01", ctx=ctx,
    )
    assert "error" in out
    assert out["job_id"] is None


def test_trigger_backfill_unconfigured_returns_error(
    ctx: SimpleNamespace,
) -> None:
    out = fitness_tools.fitness_trigger_backfill(
        source="strava", start="2026-01-01", ctx=ctx,
    )
    assert out["job_id"] is None
    assert "not configured" in out["error"].lower()


def test_trigger_backfill_missing_start_returns_error(
    configured_ctx: SimpleNamespace,
) -> None:
    out = fitness_tools.fitness_trigger_backfill(
        source="strava", start="", ctx=configured_ctx,
    )
    assert out["job_id"] is None
    assert "start" in out["error"].lower()


def test_trigger_backfill_configured_returns_job_id(
    configured_ctx: SimpleNamespace,
) -> None:
    out = fitness_tools.fitness_trigger_backfill(
        source="strava", start="2026-01-01", end="2026-02-01",
        ctx=configured_ctx,
    )
    assert out["job_id"] is not None
    assert "already_running" not in out


def test_trigger_backfill_blocked_by_running_sync(
    configured_ctx: SimpleNamespace, jobs_repository: SQLiteJobRepository,
) -> None:
    """A running sync blocks a backfill submit through the MCP tool."""
    existing = jobs_repository.create(
        "fitness_sync_strava", {"user_id": _TEST_USER_ID},
        user_id=_TEST_USER_ID,
    )
    jobs_repository.mark_running(existing.id)
    out = fitness_tools.fitness_trigger_backfill(
        source="strava", start="2026-01-01", ctx=configured_ctx,
    )
    assert out["job_id"] == existing.id
    assert out["already_running"] is True


# --------------------------------------------------------------------
# Correlation queries
# --------------------------------------------------------------------


def test_correlate_sleep_mood_joins_fitness_to_journal(
    ctx: SimpleNamespace,
    db: sqlite3.Connection,
    fitness_repo: FitnessRepository,
) -> None:
    _seed_daily(fitness_repo, local_date="2026-05-02", sleep_score=85)
    _seed_daily(fitness_repo, local_date="2026-05-03", sleep_score=60)
    _seed_entry_with_mood(
        db, entry_date="2026-05-02",
        dimensions={"energy_vigor": 0.7, "joy_sadness": 0.5},
    )
    _seed_entry_with_mood(
        db, entry_date="2026-05-03",
        dimensions={"energy_vigor": -0.4, "joy_sadness": -0.2},
    )

    out = fitness_tools.fitness_correlate_sleep_mood(
        start="2026-05-01", end="2026-05-31", ctx=ctx,
    )
    rows = out["rows"]
    assert len(rows) == 2
    by_date = {r["local_date"]: r for r in rows}
    assert by_date["2026-05-02"]["sleep_score"] == 85
    assert by_date["2026-05-02"]["energy"] == pytest.approx(0.7)
    assert by_date["2026-05-02"]["joy"] == pytest.approx(0.5)
    assert by_date["2026-05-03"]["energy"] == pytest.approx(-0.4)


def test_correlate_sleep_mood_no_journal_entry_yields_null_mood(
    ctx: SimpleNamespace,
    fitness_repo: FitnessRepository,
) -> None:
    """A day with sleep but no journal entry must surface the sleep
    row with energy/joy = None — the LEFT JOIN should hold."""
    _seed_daily(fitness_repo, local_date="2026-05-02", sleep_score=85)
    out = fitness_tools.fitness_correlate_sleep_mood(
        start="2026-05-01", end="2026-05-31", ctx=ctx,
    )
    assert len(out["rows"]) == 1
    assert out["rows"][0]["sleep_score"] == 85
    assert out["rows"][0]["energy"] is None
    assert out["rows"][0]["joy"] is None


def test_correlate_weekly_runs_stress_buckets_by_monday(
    ctx: SimpleNamespace,
    fitness_repo: FitnessRepository,
) -> None:
    """The Monday-of-week shift used by Q2 must put two runs in the
    same week if they fall on the same Mon-Sun span. 2026-05-04 is a
    Monday; 2026-05-08 is the Friday of the same week. The stress series
    is now the objective Garmin ``stress_avg`` daily metric (W4)."""
    _seed_run(
        fitness_repo, source_id="R1", local_date="2026-05-04", distance_m=5000.0,
    )
    _seed_run(
        fitness_repo, source_id="R2", local_date="2026-05-08", distance_m=8000.0,
    )
    _seed_daily(fitness_repo, local_date="2026-05-05", stress_avg=40)
    _seed_daily(fitness_repo, local_date="2026-05-07", stress_avg=60)

    out = fitness_tools.fitness_correlate_weekly_runs_stress(
        start="2026-05-01", end="2026-05-31", ctx=ctx,
    )
    rows = out["rows"]
    assert len(rows) == 1
    row = rows[0]
    assert row["week_start"] == "2026-05-04"
    assert row["distance_km"] == pytest.approx(13.0)
    assert row["stress_avg"] == pytest.approx(50.0)
    assert "stress_proxy" not in row


def test_correlate_weekly_runs_stress_dedups_same_run_across_sources(
    ctx: SimpleNamespace,
    fitness_repo: FitnessRepository,
) -> None:
    """W4 bug fix: a single watch run stored as both a garmin and a
    strava row must count once toward weekly distance (garmin preferred),
    not twice."""
    # Same 5km run on the same day, present in both sources.
    _seed_run(
        fitness_repo, source_id="G1", local_date="2026-05-04",
        distance_m=5000.0, source="garmin",
    )
    _seed_run(
        fitness_repo, source_id="S1", local_date="2026-05-04",
        distance_m=5000.0, source="strava",
    )
    out = fitness_tools.fitness_correlate_weekly_runs_stress(
        start="2026-05-01", end="2026-05-31", ctx=ctx,
    )
    rows = out["rows"]
    assert len(rows) == 1
    # Counted once (garmin), not 10km.
    assert rows[0]["distance_km"] == pytest.approx(5.0)


def test_correlate_weekly_runs_stress_strava_only_week_fallback(
    ctx: SimpleNamespace,
    fitness_repo: FitnessRepository,
) -> None:
    """W4: on days with no garmin run, the dedup falls back to strava so
    a strava-only week still sums."""
    _seed_run(
        fitness_repo, source_id="S1", local_date="2026-05-04",
        distance_m=5000.0, source="strava",
    )
    _seed_run(
        fitness_repo, source_id="S2", local_date="2026-05-06",
        distance_m=7000.0, source="strava",
    )
    out = fitness_tools.fitness_correlate_weekly_runs_stress(
        start="2026-05-01", end="2026-05-31", ctx=ctx,
    )
    rows = out["rows"]
    assert len(rows) == 1
    assert rows[0]["distance_km"] == pytest.approx(12.0)


def test_correlate_weekly_runs_stress_series_from_fitness_daily(
    ctx: SimpleNamespace,
    db: sqlite3.Connection,
    fitness_repo: FitnessRepository,
) -> None:
    """W4: the stress series is ``fitness_daily.stress_avg`` (objective),
    not the frustration mood proxy. Seeding a frustration mood entry must
    NOT influence the ``stress_avg`` output key."""
    _seed_run(
        fitness_repo, source_id="R1", local_date="2026-05-04", distance_m=5000.0,
    )
    _seed_daily(fitness_repo, local_date="2026-05-04", stress_avg=30)
    _seed_daily(fitness_repo, local_date="2026-05-06", stress_avg=50)
    # A frustration entry that would have moved the old stress_proxy —
    # it must be ignored now.
    _seed_entry_with_mood(
        db, entry_date="2026-05-05", dimensions={"frustration": 0.9},
    )
    out = fitness_tools.fitness_correlate_weekly_runs_stress(
        start="2026-05-01", end="2026-05-31", ctx=ctx,
    )
    rows = out["rows"]
    assert len(rows) == 1
    assert rows[0]["stress_avg"] == pytest.approx(40.0)
    # Sanity: the stats block correlates distance vs stress_avg.
    assert "distance_km_vs_stress_avg" in out["stats"]


def test_correlate_weekly_runs_stress_handles_year_boundary(
    ctx: SimpleNamespace,
    fitness_repo: FitnessRepository,
) -> None:
    """The Monday-shift arithmetic must NOT split a Dec/Jan week into
    two buckets the way ``strftime('%Y-%W')`` would. 2025-12-29 (Mon)
    through 2026-01-04 (Sun) is one Mon-Sun week — both runs land in
    the 2025-12-29 bucket."""
    _seed_run(
        fitness_repo, source_id="R1", local_date="2025-12-31", distance_m=5000.0,
    )
    _seed_run(
        fitness_repo, source_id="R2", local_date="2026-01-02", distance_m=10000.0,
    )
    out = fitness_tools.fitness_correlate_weekly_runs_stress(
        start="2025-12-01", end="2026-01-31", ctx=ctx,
    )
    rows = out["rows"]
    assert len(rows) == 1
    assert rows[0]["week_start"] == "2025-12-29"
    assert rows[0]["distance_km"] == pytest.approx(15.0)


def test_correlate_hrv_mood_rolling_window_handles_missing_days(
    ctx: SimpleNamespace,
    db: sqlite3.Connection,
    fitness_repo: FitnessRepository,
) -> None:
    """The recursive date_series CTE materialises one row per calendar
    day. With a 7-day window and seven days of data, every day inside
    the window has a non-null hrv_roll once the window is fully
    populated. A missing day in the middle does NOT shorten the
    window — AVG ignores NULLs."""
    # Seven consecutive HRV days, with day 4 missing on purpose.
    _seed_daily(fitness_repo, local_date="2026-05-01", hrv=50.0)
    _seed_daily(fitness_repo, local_date="2026-05-02", hrv=52.0)
    _seed_daily(fitness_repo, local_date="2026-05-03", hrv=54.0)
    # 2026-05-04 deliberately omitted.
    _seed_daily(fitness_repo, local_date="2026-05-05", hrv=56.0)
    _seed_daily(fitness_repo, local_date="2026-05-06", hrv=58.0)
    _seed_daily(fitness_repo, local_date="2026-05-07", hrv=60.0)
    _seed_entry_with_mood(
        db, entry_date="2026-05-03",
        dimensions={"joy_sadness": 0.4, "energy_vigor": 0.5},
    )

    out = fitness_tools.fitness_correlate_hrv_mood(
        start="2026-05-01", end="2026-05-07", window=7, ctx=ctx,
    )
    rows = out["rows"]
    # One row per calendar day — date_series guarantees this.
    assert len(rows) == 7
    dates = [r["d"] for r in rows]
    assert dates == [
        "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04",
        "2026-05-05", "2026-05-06", "2026-05-07",
    ]
    # Day 4 (missing HRV) inherits the rolling mean of days 1-3 — non-null.
    day4 = next(r for r in rows if r["d"] == "2026-05-04")
    assert day4["hrv_roll"] == pytest.approx((50.0 + 52.0 + 54.0) / 3)
    # Day 7 has all six recorded HRV values inside its 7-day window.
    day7 = next(r for r in rows if r["d"] == "2026-05-07")
    assert day7["hrv_roll"] == pytest.approx(
        (50.0 + 52.0 + 54.0 + 56.0 + 58.0 + 60.0) / 6,
    )


def test_correlate_hrv_mood_window_bounds_check(ctx: SimpleNamespace) -> None:
    out = fitness_tools.fitness_correlate_hrv_mood(
        start="2026-05-01", end="2026-05-07", window=0, ctx=ctx,
    )
    assert out["rows"] == []
    assert "error" in out


def test_correlate_hrv_mood_negative_lag_bounds_check(
    ctx: SimpleNamespace,
) -> None:
    out = fitness_tools.fitness_correlate_hrv_mood(
        start="2026-05-01", end="2026-05-07", lag_days=-1, ctx=ctx,
    )
    assert out["rows"] == []
    assert "error" in out


# --- W4: fatigue-facet columns and lag ------------------------------


def test_correlate_sleep_mood_returns_fatigue_facets(
    ctx: SimpleNamespace,
    db: sqlite3.Connection,
    fitness_repo: FitnessRepository,
) -> None:
    """Q1 surfaces physical_fatigue + mental_fatigue alongside energy."""
    _seed_daily(fitness_repo, local_date="2026-05-02", sleep_score=85)
    _seed_entry_with_mood(
        db, entry_date="2026-05-02",
        dimensions={
            "energy_vigor": 0.7,
            "joy_sadness": 0.5,
            "physical_fatigue": 0.3,
            "mental_fatigue": -0.2,
        },
    )
    out = fitness_tools.fitness_correlate_sleep_mood(
        start="2026-05-01", end="2026-05-31", ctx=ctx,
    )
    row = out["rows"][0]
    assert row["physical_fatigue"] == pytest.approx(0.3)
    assert row["mental_fatigue"] == pytest.approx(-0.2)
    assert row["energy"] == pytest.approx(0.7)


def test_correlate_sleep_mood_lag_shifts_join(
    ctx: SimpleNamespace,
    db: sqlite3.Connection,
    fitness_repo: FitnessRepository,
) -> None:
    """Q1 with lag_days=1: fitness on day D joins to mood on day D+1.
    Sleep on 05-02 must pick up the mood entry written on 05-03."""
    _seed_daily(fitness_repo, local_date="2026-05-02", sleep_score=85)
    _seed_entry_with_mood(
        db, entry_date="2026-05-03",
        dimensions={"energy_vigor": 0.9, "joy_sadness": 0.4},
    )
    # lag=0 → no mood joins to the 05-02 sleep row.
    out0 = fitness_tools.fitness_correlate_sleep_mood(
        start="2026-05-01", end="2026-05-31", lag_days=0, ctx=ctx,
    )
    row0 = next(r for r in out0["rows"] if r["local_date"] == "2026-05-02")
    assert row0["energy"] is None

    # lag=1 → the 05-03 mood entry attaches to the 05-02 sleep row.
    out1 = fitness_tools.fitness_correlate_sleep_mood(
        start="2026-05-01", end="2026-05-31", lag_days=1, ctx=ctx,
    )
    row1 = next(r for r in out1["rows"] if r["local_date"] == "2026-05-02")
    assert row1["energy"] == pytest.approx(0.9)


def test_correlate_sleep_mood_stats_block(
    ctx: SimpleNamespace,
    db: sqlite3.Connection,
    fitness_repo: FitnessRepository,
) -> None:
    """Q1 returns a Pearson stats block over complete pairs."""
    for day, sleep, energy in [
        ("2026-05-01", 60, -0.5),
        ("2026-05-02", 70, 0.0),
        ("2026-05-03", 80, 0.5),
        ("2026-05-04", 90, 1.0),
    ]:
        _seed_daily(fitness_repo, local_date=day, sleep_score=sleep)
        _seed_entry_with_mood(
            db, entry_date=day,
            dimensions={"energy_vigor": energy, "joy_sadness": 0.0},
        )
    out = fitness_tools.fitness_correlate_sleep_mood(
        start="2026-05-01", end="2026-05-31", ctx=ctx,
    )
    stats = out["stats"]
    assert stats["sleep_vs_energy_vigor"]["n"] == 4
    assert stats["sleep_vs_energy_vigor"]["r"] == pytest.approx(1.0)
    assert "sleep_vs_joy" in stats


def test_correlate_hrv_mood_returns_fatigue_facets(
    ctx: SimpleNamespace,
    db: sqlite3.Connection,
    fitness_repo: FitnessRepository,
) -> None:
    """Q3 rolls physical_fatigue + mental_fatigue alongside energy."""
    _seed_daily(fitness_repo, local_date="2026-05-01", hrv=50.0)
    _seed_entry_with_mood(
        db, entry_date="2026-05-01",
        dimensions={
            "joy_sadness": 0.4,
            "energy_vigor": 0.5,
            "physical_fatigue": 0.2,
            "mental_fatigue": -0.1,
        },
    )
    out = fitness_tools.fitness_correlate_hrv_mood(
        start="2026-05-01", end="2026-05-01", window=7, ctx=ctx,
    )
    row = out["rows"][0]
    assert row["physical_fatigue_roll"] == pytest.approx(0.2)
    assert row["mental_fatigue_roll"] == pytest.approx(-0.1)
    assert row["energy_roll"] == pytest.approx(0.5)
    assert "hrv_vs_physical_fatigue" in out["stats"]
    assert "hrv_vs_mental_fatigue" in out["stats"]


def test_correlate_hrv_mood_lag_shifts_join(
    ctx: SimpleNamespace,
    db: sqlite3.Connection,
    fitness_repo: FitnessRepository,
) -> None:
    """Q3 with lag_days=1: HRV on day D correlates with mood on D+1.
    HRV on 05-01 must pick up the mood entry written on 05-02."""
    _seed_daily(fitness_repo, local_date="2026-05-01", hrv=50.0)
    _seed_entry_with_mood(
        db, entry_date="2026-05-02",
        dimensions={"joy_sadness": 0.4, "energy_vigor": 0.8},
    )
    out0 = fitness_tools.fitness_correlate_hrv_mood(
        start="2026-05-01", end="2026-05-01", window=7, lag_days=0, ctx=ctx,
    )
    assert out0["rows"][0]["energy_roll"] is None

    out1 = fitness_tools.fitness_correlate_hrv_mood(
        start="2026-05-01", end="2026-05-01", window=7, lag_days=1, ctx=ctx,
    )
    assert out1["rows"][0]["energy_roll"] == pytest.approx(0.8)


# --------------------------------------------------------------------
# Tool registry — meta-test
# --------------------------------------------------------------------


def test_all_eight_tools_registered() -> None:
    """The acceptance criterion: each tool is in the running MCP
    server's tool registry. Importing the package facade is enough
    for registration (side-effect import in mcp_server/__init__.py)."""
    from journal.mcp_server.app import mcp  # noqa: PLC0415

    expected = {
        "fitness_list_activities",
        "fitness_list_daily",
        "fitness_sync_status",
        "fitness_integrity_check",
        "fitness_trigger_sync",
        "fitness_trigger_backfill",
        "fitness_correlate_sleep_mood",
        "fitness_correlate_weekly_runs_stress",
        "fitness_correlate_hrv_mood",
        "fitness_divergence",
    }
    registered: set[str] = set()
    # FastMCP exposes registered tools on its tool manager.
    for tool in mcp._tool_manager._tools.values():
        registered.add(tool.name)
    missing = expected - registered
    assert not missing, f"Tools not registered: {missing}"


def test_tools_return_json_serialisable_dicts(ctx: SimpleNamespace) -> None:
    """Plan acceptance: 'All return JSON-serialisable dicts/lists.'"""
    payloads: list[Any] = [
        fitness_tools.fitness_list_activities(
            start="2026-05-01", end="2026-05-31", ctx=ctx,
        ),
        fitness_tools.fitness_list_daily(
            start="2026-05-01", end="2026-05-31", ctx=ctx,
        ),
        fitness_tools.fitness_sync_status(ctx=ctx),
        fitness_tools.fitness_integrity_check(ctx=ctx),
        fitness_tools.fitness_correlate_sleep_mood(
            start="2026-05-01", end="2026-05-31", ctx=ctx,
        ),
        fitness_tools.fitness_correlate_weekly_runs_stress(
            start="2026-05-01", end="2026-05-31", ctx=ctx,
        ),
        fitness_tools.fitness_correlate_hrv_mood(
            start="2026-05-01", end="2026-05-31", ctx=ctx,
        ),
        fitness_tools.fitness_divergence(
            start="2026-05-01", end="2026-05-31", ctx=ctx,
        ),
    ]
    for p in payloads:
        json.dumps(p)  # raises TypeError if not serialisable


# --------------------------------------------------------------------
# Divergence detector (fitness-schema.md §9)
# --------------------------------------------------------------------


def _seed_daily_signals(
    repo: FitnessRepository,
    *,
    local_date: str,
    hrv: float | None = None,
    sleep: int | None = None,
) -> None:
    raw_id = _seed_raw_garmin(repo, "sleep", f"div-{local_date}")
    repo.upsert_daily(
        FitnessDaily(
            user_id=_TEST_USER_ID, source="garmin", local_date=local_date,
            hrv_overnight_ms=hrv, sleep_score=sleep, raw_ref_ids=[raw_id],
        ),
    )


def test_fitness_divergence_returns_rows_and_summary(
    ctx: SimpleNamespace,
    db: sqlite3.Connection,
    fitness_repo: FitnessRepository,
) -> None:
    """A tired-but-recovered day classifies as likely_mental_fatigue and
    is counted in the summary; the payload is a JSON-serialisable dict."""
    testday = "2026-06-15"
    from datetime import date, timedelta  # noqa: PLC0415

    d0 = date.fromisoformat(testday)
    baseline = [(d0 - timedelta(days=i)).isoformat() for i in range(10, 0, -1)]
    for i, d in enumerate(baseline):
        lo = i % 2 == 0
        _seed_daily_signals(
            fitness_repo, local_date=d,
            hrv=50.0 if lo else 60.0, sleep=75 if lo else 85,
        )
        _seed_entry_with_mood(
            db, entry_date=d,
            dimensions={
                "physical_fatigue": 0.3 if lo else 0.5,
                "mental_fatigue": 0.3 if lo else 0.5,
            },
        )
    # Tired (phys z=+2) but recovered (hrv/sleep z=+1).
    _seed_daily_signals(fitness_repo, local_date=testday, hrv=60.0, sleep=85)
    _seed_entry_with_mood(
        db, entry_date=testday,
        dimensions={"physical_fatigue": 0.6, "mental_fatigue": 0.4},
    )

    out = fitness_tools.fitness_divergence(
        start=testday, end=testday, window=28, ctx=ctx,
    )
    json.dumps(out)  # serialisable
    assert len(out["rows"]) == 1
    row = out["rows"][0]
    assert row["quadrant"] == "likely_mental_fatigue"
    assert row["sufficient"] is True
    assert out["summary"] == {"likely_mental_fatigue": 1}


def test_fitness_divergence_empty_db_is_empty(ctx: SimpleNamespace) -> None:
    out = fitness_tools.fitness_divergence(
        start="2026-05-01", end="2026-05-31", ctx=ctx,
    )
    assert out == {"rows": [], "summary": {}}


def test_fitness_divergence_rejects_bad_window(ctx: SimpleNamespace) -> None:
    out = fitness_tools.fitness_divergence(
        start="2026-05-01", end="2026-05-31", window=0, ctx=ctx,
    )
    assert out["rows"] == []
    assert "error" in out


def test_fitness_divergence_rejects_malformed_date(ctx: SimpleNamespace) -> None:
    out = fitness_tools.fitness_divergence(
        start="not-a-date", end="2026-05-31", ctx=ctx,
    )
    assert out["rows"] == []
    assert "start" in out["error"]


# --------------------------------------------------------------------
# STRAVA_ENABLED mothball (W1)
# --------------------------------------------------------------------


def test_trigger_sync_strava_disabled_returns_error(
    strava_disabled_ctx: SimpleNamespace,
) -> None:
    out = fitness_tools.fitness_trigger_sync(
        source="strava", ctx=strava_disabled_ctx,
    )
    assert out["job_id"] is None
    assert out["error"] == "Strava integration is disabled on this server"


def test_trigger_backfill_strava_disabled_returns_error(
    strava_disabled_ctx: SimpleNamespace,
) -> None:
    out = fitness_tools.fitness_trigger_backfill(
        source="strava", start="2026-01-01", ctx=strava_disabled_ctx,
    )
    assert out["job_id"] is None
    assert out["error"] == "Strava integration is disabled on this server"


def test_trigger_sync_garmin_unaffected_when_strava_disabled(
    strava_disabled_ctx: SimpleNamespace,
) -> None:
    out = fitness_tools.fitness_trigger_sync(
        source="garmin", ctx=strava_disabled_ctx,
    )
    assert out["job_id"] is not None


def test_sync_status_keeps_both_keys_when_strava_disabled(
    strava_disabled_ctx: SimpleNamespace,
) -> None:
    """Webapp/MCP contract: the status shape is flag-independent — both
    source keys stay present so historical Strava state remains visible."""
    out = fitness_tools.fitness_sync_status(ctx=strava_disabled_ctx)
    assert set(out) == {"strava", "garmin"}
