"""REST API tests for the storylines write endpoints (W6 + W7).

Covers the new optional regenerate body (start_date/end_date/mode) and
the create-auto-kicks-generation behavior. Builds on the same in-
process TestClient pattern as ``tests/test_api_storylines.py`` but
focuses on the W6/W7 behaviors specifically — duplicate-shape and
panel-serialisation cases stay in the older file.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from mcp.server.fastmcp import FastMCP
from starlette.testclient import TestClient

from journal.api.ingestion import register_ingestion_routes
from journal.api.storylines import register_storylines_routes
from journal.auth import AuthenticatedUser, _current_user_id
from journal.db.factory import ConnectionFactory
from journal.db.jobs_repository import SQLiteJobRepository
from journal.db.storyline_repository import SQLiteStorylineRepository
from journal.entitystore.store import SQLiteEntityStore
from journal.services.jobs import JobRunner
from journal.services.storylines.service import GenerationResult

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path


_TEST_USER_ID = 1


class _FakeAuthMiddleware:
    def __init__(self, app: Any) -> None:  # noqa: ANN401
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401
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


class _FakeGenerationService:
    """Captures call kwargs so we can assert what the worker received."""

    def __init__(self) -> None:
        self.calls: list[int] = []
        self.kwargs: list[dict[str, Any]] = []

    def regenerate(
        self,
        storyline_id: int,
        **kwargs: Any,  # noqa: ANN401
    ) -> GenerationResult:
        self.calls.append(storyline_id)
        self.kwargs.append(kwargs)
        return GenerationResult(storyline_id=storyline_id, entry_count=0)


@pytest.fixture
def app_with_storylines(
    tmp_path: Path,
) -> Generator[tuple[TestClient, dict[str, Any]]]:
    """Wire a Starlette TestClient against a real SQLite db + fake
    generation service. ``runner.shutdown(wait=True, cancel_futures=False)`` runs in
    teardown so the ThreadPoolExecutor is flushed before the process
    exits — missed shutdown causes CI segfaults (per memory)."""
    db_path = tmp_path / "api.db"
    factory = ConnectionFactory(db_path)
    from journal.db.migrations import run_migrations
    run_migrations(factory.get())

    entity_store = SQLiteEntityStore(factory)
    entity = entity_store.create_entity(
        entity_type="activity", canonical_name="Running",
        description="", first_seen="2026-02-15",
        user_id=_TEST_USER_ID,
    )

    storyline_repo = SQLiteStorylineRepository(factory)
    job_repo = SQLiteJobRepository(factory)
    gen_service = _FakeGenerationService()

    class _StubExtraction:
        def reembed_entity_for_description(
            self, _eid: int, *, user_id: int,  # noqa: ARG002
        ) -> dict[str, Any]:
            return {}

    class _StubMood:
        def score_entry(self, *_a: Any, **_k: Any) -> int:  # noqa: ANN401
            return 0

    runner = JobRunner(
        job_repository=job_repo,
        entity_extraction_service=_StubExtraction(),  # type: ignore[arg-type]
        mood_backfill_callable=lambda **_: None,
        mood_scoring_service=_StubMood(),  # type: ignore[arg-type]
        entry_repository=object(),  # type: ignore[arg-type]
        storyline_generation_service=gen_service,
    )

    services_dict: dict[str, Any] = {
        "storyline_repository": storyline_repo,
        "job_runner": runner,
    }

    mcp = FastMCP("test-storylines-write")
    register_storylines_routes(mcp, lambda: services_dict)
    register_ingestion_routes(mcp, lambda: services_dict)

    app = mcp.streamable_http_app()
    app.add_middleware(_FakeAuthMiddleware)

    client = TestClient(app)
    try:
        yield client, {
            "factory": factory,
            "repo": storyline_repo,
            "job_repo": job_repo,
            "entity_id": entity.id,
            "gen_service": gen_service,
            "runner": runner,
        }
    finally:
        runner.shutdown(wait=True, cancel_futures=False)
        factory.close_current()


# ── W7: create auto-kicks generation ────────────────────────────


class TestCreateAutoKick:
    def test_create_returns_generation_job_id(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
    ) -> None:
        client, ctx = app_with_storylines
        resp = client.post(
            "/api/storylines",
            json={"entity_ids": [ctx["entity_id"]], "name": "Running"},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert "generation_job_id" in body
        assert isinstance(body["generation_job_id"], str)
        assert body["generation_job_id"]
        # Standard storyline fields preserved.
        assert body["name"] == "Running"
        assert [a["id"] for a in body["anchors"]] == [ctx["entity_id"]]
        # The job row exists in the repository.
        job = ctx["job_repo"].get(body["generation_job_id"])
        assert job is not None
        assert job.type == "storyline_generation"

    def test_create_kicks_one_job_against_the_fake_service(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
    ) -> None:
        client, ctx = app_with_storylines
        resp = client.post(
            "/api/storylines",
            json={"entity_ids": [ctx["entity_id"]], "name": "Running"},
        )
        sid = resp.json()["id"]
        ctx["runner"].shutdown(wait=True, cancel_futures=False)
        assert ctx["gen_service"].calls == [sid]

    def test_create_duplicate_does_not_kick_job(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
    ) -> None:
        """409 on duplicate must NOT kick a generation job — the
        existing storyline already has (or will have) panels."""
        client, ctx = app_with_storylines
        client.post(
            "/api/storylines",
            json={"entity_ids": [ctx["entity_id"]], "name": "Running"},
        )
        # Reset the recorder to see whether the second call submits.
        ctx["runner"].shutdown(wait=True, cancel_futures=False)
        first_calls = list(ctx["gen_service"].calls)

        resp = client.post(
            "/api/storylines",
            json={"entity_ids": [ctx["entity_id"]], "name": "Running"},
        )
        assert resp.status_code == 409
        # No new job submitted by the duplicate request. The runner
        # has been shutdown so a new submit would raise; observing
        # the calls list unchanged is the cleaner assertion.
        assert ctx["gen_service"].calls == first_calls


# ── W6: regenerate body params ──────────────────────────────────


class TestRegenerateBodyParams:
    def test_empty_body_preserves_legacy_behavior(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
    ) -> None:
        """No body → replace, no date overrides, no mode. Backward
        compatible with the W5 contract — clients that don't send any
        JSON still get a 202 + job_id."""
        client, ctx = app_with_storylines
        created = client.post(
            "/api/storylines",
            json={"entity_ids": [ctx["entity_id"]], "name": "Running"},
        ).json()
        sid = created["id"]

        # Regenerate with no body at all.
        resp = client.post(f"/api/storylines/{sid}/regenerate")
        assert resp.status_code == 202
        body = resp.json()
        assert "job_id" in body

        ctx["runner"].shutdown(wait=True, cancel_futures=False)
        # The explicit regen recorded an entry with NO override kwargs.
        last = ctx["gen_service"].kwargs[-1]
        assert "start_date" not in last
        assert "end_date" not in last
        assert "mode" not in last

    def test_replace_mode_with_date_range(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
    ) -> None:
        client, ctx = app_with_storylines
        created = client.post(
            "/api/storylines",
            json={"entity_ids": [ctx["entity_id"]], "name": "Running"},
        ).json()
        sid = created["id"]

        resp = client.post(
            f"/api/storylines/{sid}/regenerate",
            json={
                "mode": "replace",
                "start_date": "2026-01-01",
                "end_date": "2026-03-31",
            },
        )
        assert resp.status_code == 202
        ctx["runner"].shutdown(wait=True, cancel_futures=False)
        # The fake recorded kwargs — last call is the explicit regen.
        last = ctx["gen_service"].kwargs[-1]
        assert last.get("mode") == "replace"
        assert last.get("start_date") == "2026-01-01"
        assert last.get("end_date") == "2026-03-31"

    def test_append_mode_passed_through(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
    ) -> None:
        client, ctx = app_with_storylines
        created = client.post(
            "/api/storylines",
            json={"entity_ids": [ctx["entity_id"]], "name": "Running"},
        ).json()
        sid = created["id"]

        resp = client.post(
            f"/api/storylines/{sid}/regenerate",
            json={"mode": "append", "start_date": "2099-04-01"},
        )
        assert resp.status_code == 202
        ctx["runner"].shutdown(wait=True, cancel_futures=False)
        last = ctx["gen_service"].kwargs[-1]
        assert last.get("mode") == "append"
        assert last.get("start_date") == "2099-04-01"

    def test_invalid_mode_returns_400(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
    ) -> None:
        client, ctx = app_with_storylines
        created = client.post(
            "/api/storylines",
            json={"entity_ids": [ctx["entity_id"]], "name": "Running"},
        ).json()
        sid = created["id"]

        resp = client.post(
            f"/api/storylines/{sid}/regenerate",
            json={"mode": "rewrite-from-scratch"},
        )
        assert resp.status_code == 400

    def test_non_string_field_returns_400(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
    ) -> None:
        client, ctx = app_with_storylines
        created = client.post(
            "/api/storylines",
            json={"entity_ids": [ctx["entity_id"]], "name": "Running"},
        ).json()
        sid = created["id"]

        resp = client.post(
            f"/api/storylines/{sid}/regenerate",
            json={"start_date": 42},
        )
        assert resp.status_code == 400

    def test_non_object_body_returns_400(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
    ) -> None:
        client, ctx = app_with_storylines
        created = client.post(
            "/api/storylines",
            json={"entity_ids": [ctx["entity_id"]], "name": "Running"},
        ).json()
        sid = created["id"]

        # A bare list isn't a valid request body shape.
        resp = client.post(
            f"/api/storylines/{sid}/regenerate",
            json=["nope"],
        )
        assert resp.status_code == 400
