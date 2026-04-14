"""REST API tests for the async batch-job endpoints (Work Unit 5).

Covers:

- `POST /api/entities/extract` — submit + 202 + poll for success
- `POST /api/mood/backfill` — submit + 202 + poll for success
- `POST /api/mood/backfill` — invalid mode -> 400
- `POST /api/mood/backfill` — missing mode -> 400
- `POST /api/entities/extract` — unknown key -> 400
- `GET /api/jobs/{unknown_id}` -> 404
- `GET /api/jobs/{id}` -> full serialised job dict after success

The fixture boots a minimal test app by registering the REST routes
against an in-process FastMCP instance, plus a real JobRunner wired
to fake services. Tests wait on `job_runner.shutdown(wait=True)` to
flush the executor before asserting terminal state — the brief's
"never assert immediately after submit" discipline.
"""

import sqlite3
import time
from collections.abc import Callable, Generator
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from journal.db.connection import get_connection
from journal.db.jobs_repository import SQLiteJobRepository
from journal.db.migrations import run_migrations
from journal.models import ExtractionResult
from journal.services.backfill import MoodBackfillResult
from journal.services.jobs import JobRunner

# --------------------------------------------------------------------
# Fakes — the routes don't touch the extraction or backfill code
# directly, they go through the JobRunner, which we wire against
# these stand-ins.
# --------------------------------------------------------------------


class FakeExtractionService:
    """Minimal stand-in for EntityExtractionService."""

    def __init__(
        self,
        batch_results: list[ExtractionResult] | None = None,
        single_result: ExtractionResult | None = None,
    ) -> None:
        self._batch_results = batch_results or []
        self._single_result = single_result

    def extract_batch(
        self,
        entry_ids: list[int] | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        stale_only: bool = False,
        *,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> list[ExtractionResult]:
        total = len(self._batch_results)
        if on_progress is not None:
            on_progress(0, total)
            for i in range(1, total + 1):
                on_progress(i, total)
        return list(self._batch_results)

    def extract_from_entry(self, entry_id: int) -> ExtractionResult:
        if self._single_result is None:
            raise AssertionError("single_result not configured")
        return self._single_result


class FakeMoodBackfill:
    """Callable stand-in for `backfill_mood_scores`."""

    def __init__(
        self, result: MoodBackfillResult | None = None
    ) -> None:
        self._result = result or MoodBackfillResult(scored=2, skipped=1)
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        *,
        repository: Any,
        mood_scoring: Any,
        mode: str,
        start_date: str | None = None,
        end_date: str | None = None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> MoodBackfillResult:
        self.calls.append(
            {
                "mode": mode,
                "start_date": start_date,
                "end_date": end_date,
            }
        )
        if on_progress is not None:
            on_progress(0, 3)
            on_progress(3, 3)
        return self._result


# --------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------


@pytest.fixture
def api_jobs_db(tmp_path: Path) -> Generator[sqlite3.Connection]:
    db_path = tmp_path / "jobs-api.db"
    # The Starlette TestClient runs in a worker thread; the JobRunner
    # runs in its own worker thread. Both must share this connection,
    # so `check_same_thread=False` matches production wiring.
    conn = get_connection(db_path, check_same_thread=False)
    run_migrations(conn)
    yield conn
    conn.close()


@pytest.fixture
def jobs_repository(
    api_jobs_db: sqlite3.Connection,
) -> SQLiteJobRepository:
    return SQLiteJobRepository(api_jobs_db)


@pytest.fixture
def extraction_result() -> ExtractionResult:
    return ExtractionResult(
        entry_id=1,
        extraction_run_id="run-1",
        entities_created=3,
        entities_matched=2,
        mentions_created=5,
        relationships_created=1,
        warnings=["w1"],
    )


@pytest.fixture
def fake_extraction(
    extraction_result: ExtractionResult,
) -> FakeExtractionService:
    return FakeExtractionService(
        batch_results=[extraction_result],
        single_result=extraction_result,
    )


@pytest.fixture
def fake_mood_backfill() -> FakeMoodBackfill:
    return FakeMoodBackfill()


@pytest.fixture
def job_runner(
    jobs_repository: SQLiteJobRepository,
    fake_extraction: FakeExtractionService,
    fake_mood_backfill: FakeMoodBackfill,
) -> Generator[JobRunner]:
    runner = JobRunner(
        job_repository=jobs_repository,
        entity_extraction_service=fake_extraction,  # type: ignore[arg-type]
        mood_backfill_callable=fake_mood_backfill,
        mood_scoring_service=object(),  # type: ignore[arg-type]
        entry_repository=object(),  # type: ignore[arg-type]
    )
    yield runner
    runner.shutdown(wait=True)


@pytest.fixture
def services(
    jobs_repository: SQLiteJobRepository, job_runner: JobRunner
) -> dict:
    return {
        "job_repository": jobs_repository,
        "job_runner": job_runner,
    }


@pytest.fixture
def client(services: dict) -> Generator[TestClient]:
    from mcp.server.fastmcp import FastMCP

    from journal.api import register_api_routes

    test_mcp = FastMCP("test-journal-jobs")
    register_api_routes(test_mcp, lambda: services)
    app = test_mcp.streamable_http_app()
    with TestClient(app, raise_server_exceptions=False) as tc:
        yield tc


def _wait_for_job(
    client: TestClient,
    job_id: str,
    *,
    timeout: float = 5.0,
    poll: float = 0.02,
) -> dict:
    """Poll the GET /api/jobs/{id} route until terminal state.

    Mirrors the webapp's polling pattern rather than synchronously
    shutting down the runner — proves the HTTP read path works.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get(f"/api/jobs/{job_id}")
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        if payload["status"] in ("succeeded", "failed"):
            return payload
        time.sleep(poll)
    raise AssertionError(
        f"Job {job_id} did not reach terminal state in {timeout}s"
    )


# --------------------------------------------------------------------
# POST /api/entities/extract
# --------------------------------------------------------------------


class TestEntityExtractionRoute:
    def test_submit_returns_202_and_succeeds(
        self, client: TestClient, job_runner: JobRunner
    ) -> None:
        resp = client.post(
            "/api/entities/extract",
            json={"start_date": "2026-01-01", "end_date": "2026-02-01"},
        )
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert "job_id" in body
        assert body["status"] == "queued"

        # Flush the executor so the terminal state is deterministic.
        job_runner.shutdown(wait=True)

        final = client.get(f"/api/jobs/{body['job_id']}")
        assert final.status_code == 200
        payload = final.json()
        assert payload["status"] == "succeeded"
        assert payload["type"] == "entity_extraction"
        assert payload["result"]["processed"] == 1
        assert payload["result"]["entities_created"] == 3
        assert payload["progress_current"] == 1
        assert payload["progress_total"] == 1

    def test_unknown_key_returns_400(self, client: TestClient) -> None:
        resp = client.post(
            "/api/entities/extract",
            json={"not_a_real_key": True},
        )
        assert resp.status_code == 400
        assert "not_a_real_key" in resp.json()["error"]

    def test_non_object_body_returns_400(
        self, client: TestClient
    ) -> None:
        resp = client.post("/api/entities/extract", json=["bogus"])
        assert resp.status_code == 400

    def test_entry_id_path_goes_through_jobs(
        self, client: TestClient, job_runner: JobRunner
    ) -> None:
        resp = client.post(
            "/api/entities/extract", json={"entry_id": 42}
        )
        assert resp.status_code == 202
        job_id = resp.json()["job_id"]

        job_runner.shutdown(wait=True)

        final = client.get(f"/api/jobs/{job_id}").json()
        assert final["status"] == "succeeded"
        # Single-entry path reports (1, 1) progress per runner contract.
        assert final["progress_current"] == 1
        assert final["progress_total"] == 1


# --------------------------------------------------------------------
# POST /api/mood/backfill
# --------------------------------------------------------------------


class TestMoodBackfillRoute:
    def test_submit_returns_202_and_succeeds(
        self,
        client: TestClient,
        job_runner: JobRunner,
        fake_mood_backfill: FakeMoodBackfill,
    ) -> None:
        resp = client.post(
            "/api/mood/backfill",
            json={"mode": "stale-only", "start_date": "2026-01-01"},
        )
        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "queued"

        job_runner.shutdown(wait=True)

        payload = client.get(f"/api/jobs/{body['job_id']}").json()
        assert payload["status"] == "succeeded"
        assert payload["type"] == "mood_backfill"
        assert payload["result"]["scored"] == 2
        assert payload["result"]["skipped"] == 1
        assert fake_mood_backfill.calls == [
            {
                "mode": "stale-only",
                "start_date": "2026-01-01",
                "end_date": None,
            }
        ]

    def test_invalid_mode_returns_400(
        self, client: TestClient
    ) -> None:
        resp = client.post(
            "/api/mood/backfill", json={"mode": "never-heard-of-it"}
        )
        assert resp.status_code == 400
        assert "mode" in resp.json()["error"]

    def test_missing_mode_returns_400(
        self, client: TestClient
    ) -> None:
        resp = client.post("/api/mood/backfill", json={})
        assert resp.status_code == 400
        assert "mode" in resp.json()["error"]

    def test_unknown_key_returns_400(self, client: TestClient) -> None:
        resp = client.post(
            "/api/mood/backfill",
            json={"mode": "force", "bogus": 1},
        )
        assert resp.status_code == 400


# --------------------------------------------------------------------
# GET /api/jobs/{job_id}
# --------------------------------------------------------------------


class TestListJobsRoute:
    def test_returns_empty_list_with_no_jobs(self, client: TestClient) -> None:
        resp = client.get("/api/jobs")
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        assert data["total"] == 0

    def test_returns_jobs_with_total(
        self, client: TestClient, job_runner: JobRunner
    ) -> None:
        r1 = client.post("/api/entities/extract", json={"stale_only": True})
        job_runner.shutdown(wait=True)
        _wait_for_job(client, r1.json()["job_id"])

        resp = client.get("/api/jobs")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1
        assert len(data["items"]) >= 1
        ids = [j["id"] for j in data["items"]]
        assert r1.json()["job_id"] in ids

    def test_filters_by_status(
        self, client: TestClient, job_runner: JobRunner
    ) -> None:
        client.post("/api/entities/extract", json={"stale_only": True})
        job_runner.shutdown(wait=True)

        resp = client.get("/api/jobs?status=succeeded")
        assert resp.status_code == 200
        assert all(j["status"] == "succeeded" for j in resp.json()["items"])

    def test_filters_by_type(
        self, client: TestClient, job_runner: JobRunner
    ) -> None:
        client.post("/api/entities/extract", json={"stale_only": True})
        client.post("/api/mood/backfill", json={"mode": "stale-only"})
        job_runner.shutdown(wait=True)

        resp = client.get("/api/jobs?type=entity_extraction")
        assert resp.status_code == 200
        assert all(
            j["type"] == "entity_extraction" for j in resp.json()["items"]
        )

    def test_pagination(
        self, client: TestClient, job_runner: JobRunner
    ) -> None:
        client.post("/api/entities/extract", json={"stale_only": True})
        client.post("/api/mood/backfill", json={"mode": "stale-only"})
        job_runner.shutdown(wait=True)

        resp = client.get("/api/jobs?limit=1&offset=0")
        data = resp.json()
        assert len(data["items"]) == 1
        assert data["total"] == 2
        assert data["offset"] == 0

        resp2 = client.get("/api/jobs?limit=1&offset=1")
        data2 = resp2.json()
        assert len(data2["items"]) == 1
        assert data2["items"][0]["id"] != data["items"][0]["id"]


class TestJobDetailRoute:
    def test_unknown_id_returns_404(self, client: TestClient) -> None:
        resp = client.get("/api/jobs/not-a-real-id")
        assert resp.status_code == 404
        assert resp.json() == {"error": "Job not found"}

    def test_returns_full_serialised_shape(
        self, client: TestClient, job_runner: JobRunner
    ) -> None:
        submit = client.post(
            "/api/entities/extract",
            json={"stale_only": True},
        )
        job_id = submit.json()["job_id"]
        job_runner.shutdown(wait=True)

        payload = _wait_for_job(client, job_id)
        expected_keys = {
            "id",
            "type",
            "status",
            "params",
            "progress_current",
            "progress_total",
            "result",
            "error_message",
            "created_at",
            "started_at",
            "finished_at",
        }
        assert set(payload.keys()) == expected_keys
        assert payload["id"] == job_id
        assert payload["params"] == {"stale_only": True}
        assert payload["error_message"] is None
        assert payload["started_at"] is not None
        assert payload["finished_at"] is not None
        assert payload["status"] == "succeeded"
