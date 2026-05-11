"""REST API tests for the fitness pipeline (W9).

Covers the read-side routes in ``api/fitness.py`` plus the
job-creation route in ``api/ingestion.py``:

- ``GET  /api/fitness/activities?start=&end=&type=``
- ``GET  /api/fitness/daily?start=&end=``
- ``GET  /api/fitness/sync/status``
- ``POST /api/fitness/sync/{source}``  (job creation, in ``ingestion.py``)
- ``GET  /api/fitness/integrity``

The fixture stack mirrors ``test_api_jobs.py``: an in-process FastMCP
with the real route registrations and a fake auth middleware that
injects ``user_id=1``. Anonymous-401 enforcement lives in the
``RequireAuthMiddleware`` covered by ``test_auth.py`` — these tests
go through ``_FakeAuthMiddleware`` so the per-route auth dependency
(``get_authenticated_user``) is exercised but the middleware-level
401 path is out of scope.
"""

import json
import sqlite3
import time
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from journal.auth import AuthenticatedUser, _current_user_id
from journal.db.connection import get_connection
from journal.db.factory import ConnectionFactory
from journal.db.fitness_repository import FitnessRepository
from journal.db.jobs_repository import SQLiteJobRepository
from journal.db.migrations import run_migrations
from journal.models import FitnessActivity, FitnessAuthState, FitnessDaily
from journal.services.jobs import JobRunner

_TEST_USER_ID = 1


class _FakeAuthMiddleware:
    """ASGI middleware that injects a test user (mirrors test_api_jobs.py)."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] in ("http", "websocket"):
            scope["user"] = AuthenticatedUser(
                user_id=_TEST_USER_ID,
                email="test@example.com",
                display_name="Test User",
                is_admin=False,
                is_active=True,
                email_verified=True,
            )
            token = _current_user_id.set(_TEST_USER_ID)
            try:
                await self.app(scope, receive, send)
            finally:
                _current_user_id.reset(token)
        else:
            await self.app(scope, receive, send)


# --------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------


@pytest.fixture
def fitness_factory(tmp_path: Path) -> ConnectionFactory:
    db_path = tmp_path / "fitness-api.db"
    f = ConnectionFactory(db_path)
    run_migrations(f.get())
    return f


@pytest.fixture
def fitness_db(fitness_factory: ConnectionFactory) -> sqlite3.Connection:
    """Calling-thread connection — used for raw SQL only.

    Until C2, ``api/fitness.py`` reads ``services['db_conn']`` from a
    request thread, so the legacy slot in the synthetic services dict
    is populated with a cross-thread-capable connection opened just
    for that purpose (see :func:`_legacy_conn`)."""
    return fitness_factory.get()


@pytest.fixture
def _legacy_conn(fitness_factory: ConnectionFactory) -> Generator[sqlite3.Connection]:
    """Cross-thread connection for the ``"db_conn"`` services slot.

    Removed in C2 when the API readers migrate to ``db_factory``.
    """
    conn = get_connection(fitness_factory.db_path, check_same_thread=False)
    yield conn
    conn.close()


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
    """JobRunner without fitness callables wired — submit_fitness_sync_*
    therefore raises RuntimeError ("not configured"). Tests that need
    the configured path use ``configured_runner`` instead."""
    runner = JobRunner(
        job_repository=jobs_repository,
        entity_extraction_service=object(),  # type: ignore[arg-type]
        mood_backfill_callable=lambda **_: None,  # type: ignore[arg-type]
        mood_scoring_service=object(),  # type: ignore[arg-type]
        entry_repository=object(),  # type: ignore[arg-type]
    )
    yield runner
    runner.shutdown(wait=True)


@pytest.fixture
def configured_runner(
    jobs_repository: SQLiteJobRepository,
) -> Generator[JobRunner]:
    """JobRunner wired with no-op fitness callables so submit_fitness_sync_*
    accepts the request. The callables return a result that mirrors
    a successful zero-row sync."""
    from journal.services.fitness.backfill import BackfillResult
    from journal.services.fitness.fetch import FitnessSyncResult
    from journal.services.fitness.normalize import NormalizeResult

    def _make_fetch(source: str):
        def _fetch(*, user_id: int) -> FitnessSyncResult:
            return FitnessSyncResult(
                status="success", run_id=0, rows_fetched=0, rows_normalized=0,
            )
        _fetch.__name__ = f"_fetch_{source}"
        return _fetch

    def _make_normalize(source: str):
        def _normalize(*, user_id: int) -> NormalizeResult:
            return NormalizeResult(source=source, rows_normalized=0, drift_count=0)
        _normalize.__name__ = f"_normalize_{source}"
        return _normalize

    def _make_backfill(source: str):
        def _backfill(
            *, user_id: int, start: str, end: str | None = None,
        ) -> BackfillResult:
            return BackfillResult(
                source=source,  # type: ignore[arg-type]
                final_status="complete",
                windows_attempted=1, windows_succeeded=1,
                rows_fetched=0, rows_normalized=0,
            )
        _backfill.__name__ = f"_backfill_{source}"
        return _backfill

    runner = JobRunner(
        job_repository=jobs_repository,
        entity_extraction_service=object(),  # type: ignore[arg-type]
        mood_backfill_callable=lambda **_: None,  # type: ignore[arg-type]
        mood_scoring_service=object(),  # type: ignore[arg-type]
        entry_repository=object(),  # type: ignore[arg-type]
        fetch_strava_callable=_make_fetch("strava"),
        fetch_garmin_callable=_make_fetch("garmin"),
        normalize_strava_callable=_make_normalize("strava"),
        normalize_garmin_callable=_make_normalize("garmin"),
        backfill_strava_callable=_make_backfill("strava"),
        backfill_garmin_callable=_make_backfill("garmin"),
    )
    yield runner
    runner.shutdown(wait=True)


def _make_services(
    fitness_factory: ConnectionFactory,
    legacy_conn: sqlite3.Connection,
    fitness_repo: FitnessRepository,
    jobs_repository: SQLiteJobRepository,
    job_runner: JobRunner,
) -> dict:
    return {
        "fitness_repo": fitness_repo,
        "job_repository": jobs_repository,
        "job_runner": job_runner,
        "db_factory": fitness_factory,
        # Legacy slot — read by api/fitness.py until C2.
        "db_conn": legacy_conn,
    }


@pytest.fixture
def services(
    fitness_factory: ConnectionFactory,
    _legacy_conn: sqlite3.Connection,
    fitness_repo: FitnessRepository,
    jobs_repository: SQLiteJobRepository,
    job_runner: JobRunner,
) -> dict:
    return _make_services(
        fitness_factory, _legacy_conn, fitness_repo, jobs_repository, job_runner,
    )


@pytest.fixture
def configured_services(
    fitness_factory: ConnectionFactory,
    _legacy_conn: sqlite3.Connection,
    fitness_repo: FitnessRepository,
    jobs_repository: SQLiteJobRepository,
    configured_runner: JobRunner,
) -> dict:
    return _make_services(
        fitness_factory, _legacy_conn, fitness_repo, jobs_repository,
        configured_runner,
    )


def _build_client(services: dict) -> TestClient:
    from mcp.server.fastmcp import FastMCP

    from journal.api import register_api_routes

    test_mcp = FastMCP("test-journal-fitness")
    register_api_routes(test_mcp, lambda: services)
    app = _FakeAuthMiddleware(test_mcp.streamable_http_app())
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def client(services: dict) -> Generator[TestClient]:
    with _build_client(services) as tc:
        yield tc


@pytest.fixture
def configured_client(configured_services: dict) -> Generator[TestClient]:
    with _build_client(configured_services) as tc:
        yield tc


# --------------------------------------------------------------------
# Seeding helpers — populate the DB with the minimum row set each
# test needs. We go through FitnessRepository (real code path) so
# the row layouts match production.
# --------------------------------------------------------------------


def _seed_raw_strava(
    repo: FitnessRepository,
    *,
    source_id: str,
    payload: dict[str, Any] | None = None,
) -> int:
    raw_id = repo.insert_raw(
        source="strava",
        user_id=_TEST_USER_ID,
        endpoint="activities",
        source_id=source_id,
        payload_json=json.dumps(payload or {"id": source_id}),
        sync_run_id=None,
    )
    assert raw_id is not None
    return raw_id


def _seed_raw_garmin(
    repo: FitnessRepository,
    *,
    endpoint: str,
    source_id: str,
    payload: dict[str, Any] | None = None,
) -> int:
    raw_id = repo.insert_raw(
        source="garmin",
        user_id=_TEST_USER_ID,
        endpoint=endpoint,
        source_id=source_id,
        payload_json=json.dumps(payload or {"id": source_id}),
        sync_run_id=None,
    )
    assert raw_id is not None
    return raw_id


def _seed_activity(
    repo: FitnessRepository,
    *,
    source: str,
    source_id: str,
    raw_ref_id: int,
    activity_type: str = "run",
    local_date: str = "2026-05-01",
    start_time: str = "2026-05-01T07:00:00Z",
) -> None:
    repo.upsert_activity(
        FitnessActivity(
            user_id=_TEST_USER_ID,
            source=source,
            source_id=source_id,
            activity_type=activity_type,
            source_subtype="Run",
            start_time=start_time,
            local_date=local_date,
            duration_s=1800,
            moving_time_s=1800,
            distance_m=5000.0,
            avg_hr_bpm=140,
            max_hr_bpm=160,
            avg_pace_s_per_km=360.0,
            raw_ref_id=raw_ref_id,
        ),
    )


def _seed_daily(
    repo: FitnessRepository,
    *,
    source: str,
    local_date: str,
    raw_ref_ids: list[int],
) -> None:
    repo.upsert_daily(
        FitnessDaily(
            user_id=_TEST_USER_ID,
            source=source,
            local_date=local_date,
            sleep_score=80,
            sleep_duration_s=27000,
            resting_hr_bpm=55,
            raw_ref_ids=raw_ref_ids,
        ),
    )


def _wait_for_terminal_job(
    client: TestClient, job_id: str, *, timeout: float = 5.0
) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get(f"/api/jobs/{job_id}")
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        if payload["status"] in ("succeeded", "failed"):
            return payload
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not reach terminal state in time")


# --------------------------------------------------------------------
# GET /api/fitness/activities
# --------------------------------------------------------------------


def test_list_activities_empty_returns_empty_array(client: TestClient) -> None:
    resp = client.get("/api/fitness/activities?start=2026-05-01&end=2026-05-31")
    assert resp.status_code == 200
    assert resp.json() == {"items": []}


def test_list_activities_returns_window(
    client: TestClient, fitness_repo: FitnessRepository
) -> None:
    raw1 = _seed_raw_strava(fitness_repo, source_id="A1")
    raw2 = _seed_raw_strava(fitness_repo, source_id="A2")
    raw_outside = _seed_raw_strava(fitness_repo, source_id="A3")
    _seed_activity(
        fitness_repo, source="strava", source_id="A1", raw_ref_id=raw1,
        activity_type="run", local_date="2026-05-02",
        start_time="2026-05-02T08:00:00Z",
    )
    _seed_activity(
        fitness_repo, source="strava", source_id="A2", raw_ref_id=raw2,
        activity_type="ride", local_date="2026-05-04",
        start_time="2026-05-04T09:00:00Z",
    )
    _seed_activity(
        fitness_repo, source="strava", source_id="A3", raw_ref_id=raw_outside,
        activity_type="run", local_date="2026-04-15",
        start_time="2026-04-15T08:00:00Z",
    )

    resp = client.get("/api/fitness/activities?start=2026-05-01&end=2026-05-31")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 2
    assert {i["source_id"] for i in items} == {"A1", "A2"}
    sample = next(i for i in items if i["source_id"] == "A1")
    # Spot-check the fields W15 (webapp) will read.
    assert sample["activity_type"] == "run"
    assert sample["local_date"] == "2026-05-02"
    assert sample["distance_m"] == 5000.0
    assert sample["raw_ref_id"] == raw1


def test_list_activities_type_filter(
    client: TestClient, fitness_repo: FitnessRepository
) -> None:
    raw_run = _seed_raw_strava(fitness_repo, source_id="R1")
    raw_ride = _seed_raw_strava(fitness_repo, source_id="C1")
    _seed_activity(
        fitness_repo, source="strava", source_id="R1", raw_ref_id=raw_run,
        activity_type="run", local_date="2026-05-02",
    )
    _seed_activity(
        fitness_repo, source="strava", source_id="C1", raw_ref_id=raw_ride,
        activity_type="ride", local_date="2026-05-03",
    )

    resp = client.get(
        "/api/fitness/activities?start=2026-05-01&end=2026-05-31&type=run",
    )
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["activity_type"] == "run"


def test_list_activities_out_of_range_returns_empty_not_404(
    client: TestClient, fitness_repo: FitnessRepository
) -> None:
    raw = _seed_raw_strava(fitness_repo, source_id="A1")
    _seed_activity(
        fitness_repo, source="strava", source_id="A1", raw_ref_id=raw,
        activity_type="run", local_date="2026-05-02",
    )
    resp = client.get("/api/fitness/activities?start=2025-01-01&end=2025-12-31")
    assert resp.status_code == 200
    assert resp.json() == {"items": []}


def test_list_activities_missing_params_returns_400(client: TestClient) -> None:
    resp = client.get("/api/fitness/activities?start=2026-05-01")
    assert resp.status_code == 400
    assert "end" in resp.json()["error"]


# --------------------------------------------------------------------
# GET /api/fitness/daily
# --------------------------------------------------------------------


def test_list_daily_empty(client: TestClient) -> None:
    resp = client.get("/api/fitness/daily?start=2026-05-01&end=2026-05-31")
    assert resp.status_code == 200
    assert resp.json() == {"items": []}


def test_list_daily_returns_window(
    client: TestClient, fitness_repo: FitnessRepository
) -> None:
    raw_id = _seed_raw_garmin(fitness_repo, endpoint="sleep", source_id="2026-05-02")
    _seed_daily(
        fitness_repo, source="garmin", local_date="2026-05-02", raw_ref_ids=[raw_id],
    )
    resp = client.get("/api/fitness/daily?start=2026-05-01&end=2026-05-31")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["local_date"] == "2026-05-02"
    assert items[0]["sleep_score"] == 80
    assert items[0]["raw_ref_ids"] == [raw_id]


def test_list_daily_missing_params_returns_400(client: TestClient) -> None:
    resp = client.get("/api/fitness/daily?end=2026-05-31")
    assert resp.status_code == 400


# --------------------------------------------------------------------
# GET /api/fitness/sync/status
# --------------------------------------------------------------------


def test_sync_status_empty_db_returns_null_per_source(client: TestClient) -> None:
    """The most-likely first-use state: no auth_state rows, no sync_run rows.
    Must return 200, not 500 or KeyError."""
    resp = client.get("/api/fitness/sync/status")
    assert resp.status_code == 200
    assert resp.json() == {"strava": None, "garmin": None}


def test_sync_status_populated(
    client: TestClient, fitness_repo: FitnessRepository
) -> None:
    fitness_repo.upsert_auth_state(
        FitnessAuthState(
            user_id=_TEST_USER_ID,
            source="strava",
            access_token="tok",
            refresh_token="ref",
            auth_status="ok",
        ),
    )
    run_id = fitness_repo.start_sync_run(user_id=_TEST_USER_ID, source="strava")
    fitness_repo.finish_sync_run(
        run_id, status="success", rows_fetched=3, rows_normalized=3,
    )

    resp = client.get("/api/fitness/sync/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["garmin"] is None
    strava = body["strava"]
    assert strava["auth_status"] == "ok"
    assert strava["auth_broken_since"] is None
    assert strava["last_success_at"] is not None
    assert len(strava["last_runs"]) == 1
    last = strava["last_runs"][0]
    assert last["status"] == "success"
    assert last["rows_fetched"] == 3
    assert last["rows_normalized"] == 3
    assert last["error_class"] is None


def test_sync_status_auth_broken_surfaces_since(
    client: TestClient, fitness_repo: FitnessRepository
) -> None:
    fitness_repo.transition_auth(
        user_id=_TEST_USER_ID,
        source="garmin",
        status="broken",
        at="2026-05-08T12:00:00Z",
    )
    resp = client.get("/api/fitness/sync/status")
    assert resp.status_code == 200
    garmin = resp.json()["garmin"]
    assert garmin["auth_status"] == "broken"
    assert garmin["auth_broken_since"] == "2026-05-08T12:00:00Z"
    assert garmin["last_success_at"] is None
    assert garmin["last_runs"] == []


# --------------------------------------------------------------------
# POST /api/fitness/sync/{source}
# --------------------------------------------------------------------


def test_sync_post_strava_returns_202_with_job_id(
    configured_client: TestClient,
) -> None:
    resp = configured_client.post("/api/fitness/sync/strava")
    assert resp.status_code == 202
    body = resp.json()
    assert "job_id" in body
    assert body["status"] in ("queued", "running", "succeeded")


def test_sync_post_garmin_returns_202_with_job_id(
    configured_client: TestClient,
) -> None:
    resp = configured_client.post("/api/fitness/sync/garmin")
    assert resp.status_code == 202
    assert "job_id" in resp.json()


def test_sync_post_unknown_source_returns_400(
    configured_client: TestClient,
) -> None:
    resp = configured_client.post("/api/fitness/sync/whoop")
    assert resp.status_code == 400
    assert "source" in resp.json()["error"].lower()


def test_sync_post_unconfigured_source_returns_503(
    client: TestClient,  # client uses the unconfigured runner
) -> None:
    """Strava/Garmin not wired on this runner — submit raises RuntimeError.
    Surfaced as 503 (not 500) so operators can tell "feature off" from
    "real bug"."""
    resp = client.post("/api/fitness/sync/strava")
    assert resp.status_code == 503
    assert "not configured" in resp.json()["error"].lower()


def test_sync_post_returns_existing_running_job_id(
    configured_services: dict,
    jobs_repository: SQLiteJobRepository,
) -> None:
    """When a fitness_sync_strava is already running for this user,
    a second POST returns 202 with the existing job_id — not a new id,
    not 409. We seed a synthetic running job directly so the test
    doesn't race the executor."""
    # Insert a synthetic running job_type=fitness_sync_strava row.
    existing = jobs_repository.create(
        "fitness_sync_strava", {"user_id": _TEST_USER_ID}, user_id=_TEST_USER_ID,
    )
    jobs_repository.mark_running(existing.id)

    with _build_client(configured_services) as tc:
        resp = tc.post("/api/fitness/sync/strava")
        assert resp.status_code == 202
        body = resp.json()
        assert body["job_id"] == existing.id
        assert body["status"] == "running"
        assert body.get("already_running") is True


# --------------------------------------------------------------------
# GET /api/fitness/integrity
# --------------------------------------------------------------------


def test_integrity_clean_db_returns_empty(client: TestClient) -> None:
    resp = client.get("/api/fitness/integrity")
    assert resp.status_code == 200
    assert resp.json() == {"activities": [], "daily": []}


def test_integrity_with_orphans(
    client: TestClient,
    fitness_db: sqlite3.Connection,
    fitness_repo: FitnessRepository,
) -> None:
    """Seed an activity whose raw_ref_id points at a row that doesn't
    exist, plus a daily with a missing raw id. The integrity report
    must surface both."""
    real_raw = _seed_raw_strava(fitness_repo, source_id="REAL")
    _seed_activity(
        fitness_repo, source="strava", source_id="REAL", raw_ref_id=real_raw,
    )
    # Insert an activity row directly so we can give it a dangling raw_ref_id
    # without violating any CHECK constraints (the soft pointer has no FK).
    fitness_db.execute(
        """
        INSERT INTO fitness_activities (
            user_id, source, source_id, activity_type, source_subtype,
            start_time, local_date, duration_s, raw_ref_id
        ) VALUES (?, 'strava', 'ORPHAN', 'run', 'Run',
                  '2026-05-02T08:00:00Z', '2026-05-02', 1800, 99999)
        """,
        (_TEST_USER_ID,),
    )
    fitness_db.commit()

    # And a daily row pointing at a missing raw garmin id.
    _seed_daily(
        fitness_repo, source="garmin", local_date="2026-05-03",
        raw_ref_ids=[88888],
    )

    resp = client.get("/api/fitness/integrity")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["activities"]) == 1
    activity_orphan = body["activities"][0]
    assert activity_orphan["source"] == "strava"
    assert activity_orphan["raw_ref_id"] == 99999
    assert activity_orphan["issue"] == "raw_row_missing"

    assert len(body["daily"]) == 1
    daily_orphan = body["daily"][0]
    assert daily_orphan["source"] == "garmin"
    assert daily_orphan["missing_raw_ids"] == [88888]


# --------------------------------------------------------------------
# POST /api/fitness/backfill/{source}   (W5)
# --------------------------------------------------------------------


def test_backfill_post_strava_returns_202_with_job_id(
    configured_client: TestClient,
) -> None:
    resp = configured_client.post(
        "/api/fitness/backfill/strava",
        json={"start": "2026-01-01", "end": "2026-02-01"},
    )
    assert resp.status_code == 202
    body = resp.json()
    assert "job_id" in body
    assert body["status"] in ("queued", "running", "succeeded")


def test_backfill_post_garmin_returns_202_with_job_id(
    configured_client: TestClient,
) -> None:
    resp = configured_client.post(
        "/api/fitness/backfill/garmin",
        json={"start": "2026-01-01"},  # end optional
    )
    assert resp.status_code == 202
    assert "job_id" in resp.json()


def test_backfill_post_unknown_source_400(
    configured_client: TestClient,
) -> None:
    resp = configured_client.post(
        "/api/fitness/backfill/whoop", json={"start": "2026-01-01"},
    )
    assert resp.status_code == 400
    assert "source" in resp.json()["error"].lower()


def test_backfill_post_missing_start_400(
    configured_client: TestClient,
) -> None:
    resp = configured_client.post("/api/fitness/backfill/strava", json={})
    assert resp.status_code == 400
    assert "start" in resp.json()["error"].lower()


def test_backfill_post_malformed_start_date_400(
    configured_client: TestClient,
) -> None:
    resp = configured_client.post(
        "/api/fitness/backfill/strava",
        json={"start": "not-a-date"},
    )
    assert resp.status_code == 400
    assert "yyyy-mm-dd" in resp.json()["error"].lower()


def test_backfill_post_end_before_start_400(
    configured_client: TestClient,
) -> None:
    resp = configured_client.post(
        "/api/fitness/backfill/strava",
        json={"start": "2026-03-01", "end": "2026-01-01"},
    )
    assert resp.status_code == 400
    assert "end" in resp.json()["error"].lower()


def test_backfill_post_unconfigured_source_503(
    client: TestClient,  # unconfigured runner
) -> None:
    resp = client.post(
        "/api/fitness/backfill/strava",
        json={"start": "2026-01-01"},
    )
    assert resp.status_code == 503
    assert "not configured" in resp.json()["error"].lower()


def test_backfill_post_returns_existing_running_sync_job_id(
    configured_services: dict,
    jobs_repository: SQLiteJobRepository,
) -> None:
    """W5 spanning idempotency, direction A: a running ``fitness_sync_strava``
    job blocks a backfill submit; the existing sync job_id comes back."""
    existing = jobs_repository.create(
        "fitness_sync_strava", {"user_id": _TEST_USER_ID},
        user_id=_TEST_USER_ID,
    )
    jobs_repository.mark_running(existing.id)

    with _build_client(configured_services) as tc:
        resp = tc.post(
            "/api/fitness/backfill/strava",
            json={"start": "2026-01-01"},
        )
        assert resp.status_code == 202
        body = resp.json()
        assert body["job_id"] == existing.id
        assert body["status"] == "running"
        assert body.get("already_running") is True


def test_sync_post_returns_existing_running_backfill_job_id(
    configured_services: dict,
    jobs_repository: SQLiteJobRepository,
) -> None:
    """W5 spanning idempotency, direction B: a queued
    ``fitness_backfill_strava`` blocks a sync submit; the existing
    backfill job_id comes back."""
    existing = jobs_repository.create(
        "fitness_backfill_strava",
        {"user_id": _TEST_USER_ID, "start": "2026-01-01"},
        user_id=_TEST_USER_ID,
    )

    with _build_client(configured_services) as tc:
        resp = tc.post("/api/fitness/sync/strava")
        assert resp.status_code == 202
        body = resp.json()
        assert body["job_id"] == existing.id
        assert body["status"] == "queued"
        assert body.get("already_running") is True


def test_backfill_post_returns_existing_queued_backfill_job_id(
    configured_services: dict,
    jobs_repository: SQLiteJobRepository,
) -> None:
    """Same-class collision: a queued backfill returns its own job id
    when a second backfill submit hits."""
    existing = jobs_repository.create(
        "fitness_backfill_garmin",
        {"user_id": _TEST_USER_ID, "start": "2026-01-01"},
        user_id=_TEST_USER_ID,
    )

    with _build_client(configured_services) as tc:
        resp = tc.post(
            "/api/fitness/backfill/garmin",
            json={"start": "2026-02-01"},
        )
        assert resp.status_code == 202
        body = resp.json()
        assert body["job_id"] == existing.id
        assert body.get("already_running") is True
