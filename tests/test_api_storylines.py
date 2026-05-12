"""REST API tests for the storylines endpoints (W8).

Covers GET list/detail and the POST create/regenerate/delete routes
on a real Starlette test client wired to a real SQLite db. The
JobRunner is built with a stub StorylineGenerationService whose
``regenerate`` returns a recorded call, so we can assert that
``POST /regenerate`` queued the job rather than running it
synchronously.
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
    db_path = tmp_path / "api.db"
    factory = ConnectionFactory(db_path)
    from journal.db.migrations import run_migrations
    run_migrations(factory.get())

    # Migration 0011 seeds an admin user with id=1, which matches _TEST_USER_ID.
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
        "entity_store": entity_store,
        "job_runner": runner,
    }

    mcp = FastMCP("test-storylines")
    register_storylines_routes(mcp, lambda: services_dict)
    register_ingestion_routes(mcp, lambda: services_dict)

    app = mcp.streamable_http_app()
    app.add_middleware(_FakeAuthMiddleware)

    client = TestClient(app)
    try:
        yield client, {
            "factory": factory,
            "repo": storyline_repo,
            "entity_store": entity_store,
            "entity_id": entity.id,
            "gen_service": gen_service,
            "runner": runner,
        }
    finally:
        runner.shutdown(wait=True)
        factory.close_current()


class TestStorylinesAPI:
    def test_list_returns_empty_envelope(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
    ) -> None:
        client, _ = app_with_storylines
        resp = client.get("/api/storylines")
        assert resp.status_code == 200
        body = resp.json()
        assert body == {"items": [], "total": 0, "limit": 50, "offset": 0}

    def test_create_then_list_then_detail(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
    ) -> None:
        client, ctx = app_with_storylines
        # Create
        resp = client.post(
            "/api/storylines",
            json={"entity_ids": [ctx["entity_id"]], "name": "Running"},
        )
        assert resp.status_code == 201
        created = resp.json()
        sid = created["id"]
        assert created["name"] == "Running"

        # List
        resp = client.get("/api/storylines")
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["id"] == sid

        # Detail
        resp = client.get(f"/api/storylines/{sid}")
        assert resp.status_code == 200
        detail = resp.json()
        assert detail["id"] == sid
        # No panels yet (haven't regenerated)
        assert detail["panels"] == {}

    def test_create_rejects_missing_fields(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
    ) -> None:
        client, _ = app_with_storylines
        resp = client.post("/api/storylines", json={})
        assert resp.status_code == 400

    def test_create_returns_409_on_duplicate(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
    ) -> None:
        client, ctx = app_with_storylines
        client.post(
            "/api/storylines",
            json={"entity_ids": [ctx["entity_id"]], "name": "Running"},
        )
        resp = client.post(
            "/api/storylines",
            json={"entity_ids": [ctx["entity_id"]], "name": "Running"},
        )
        assert resp.status_code == 409
        assert "storyline_id" in resp.json()

    def test_create_with_multiple_anchors_returns_anchors_list(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
    ) -> None:
        client, ctx = app_with_storylines
        store = ctx["entity_store"]
        ent_b = store.create_entity(
            entity_type="person", canonical_name="Sara",
            description="", first_seen="2026-02-15",
            user_id=_TEST_USER_ID,
        )
        ent_c = store.create_entity(
            entity_type="place", canonical_name="Vienna",
            description="", first_seen="2026-02-15",
            user_id=_TEST_USER_ID,
        )
        resp = client.post(
            "/api/storylines",
            json={
                "entity_ids": [ent_c.id, ctx["entity_id"], ent_b.id],
                "name": "Trio",
            },
        )
        assert resp.status_code == 201
        body = resp.json()
        ids = sorted(a["id"] for a in body["anchors"])
        assert ids == sorted([ent_c.id, ctx["entity_id"], ent_b.id])
        names = {a["canonical_name"] for a in body["anchors"]}
        assert names == {"Running", "Sara", "Vienna"}

    def test_create_rejects_empty_entity_ids(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
    ) -> None:
        client, _ = app_with_storylines
        resp = client.post(
            "/api/storylines",
            json={"entity_ids": [], "name": "Empty"},
        )
        assert resp.status_code == 400

    def test_create_rejects_above_cap(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
    ) -> None:
        client, ctx = app_with_storylines
        store = ctx["entity_store"]
        ids = [
            store.create_entity(
                entity_type="person", canonical_name=f"P{i}",
                description="", first_seen="2026-02-15",
                user_id=_TEST_USER_ID,
            ).id
            for i in range(20)
        ]
        resp = client.post(
            "/api/storylines",
            json={"entity_ids": ids, "name": "Too many"},
        )
        assert resp.status_code == 422
        assert "cap" in resp.json()["error"]

    def test_set_anchors_replaces_anchor_set(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
    ) -> None:
        client, ctx = app_with_storylines
        store = ctx["entity_store"]
        ent_b = store.create_entity(
            entity_type="person", canonical_name="Bob",
            description="", first_seen="2026-02-15",
            user_id=_TEST_USER_ID,
        )
        ent_c = store.create_entity(
            entity_type="person", canonical_name="Carol",
            description="", first_seen="2026-02-15",
            user_id=_TEST_USER_ID,
        )
        created = client.post(
            "/api/storylines",
            json={"entity_ids": [ctx["entity_id"]], "name": "Set-test"},
        ).json()
        sid = created["id"]

        resp = client.put(
            f"/api/storylines/{sid}/anchors",
            json={"entity_ids": [ent_b.id, ent_c.id]},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert sorted(a["id"] for a in body["anchors"]) == sorted(
            [ent_b.id, ent_c.id]
        )

        # Subsequent GET reflects the new anchors.
        detail = client.get(f"/api/storylines/{sid}").json()
        assert sorted(a["id"] for a in detail["anchors"]) == sorted(
            [ent_b.id, ent_c.id]
        )

    def test_set_anchors_404_for_unknown_storyline(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
    ) -> None:
        client, ctx = app_with_storylines
        resp = client.put(
            "/api/storylines/99999/anchors",
            json={"entity_ids": [ctx["entity_id"]]},
        )
        assert resp.status_code == 404

    def test_set_anchors_rejects_empty(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
    ) -> None:
        client, ctx = app_with_storylines
        created = client.post(
            "/api/storylines",
            json={"entity_ids": [ctx["entity_id"]], "name": "X"},
        ).json()
        resp = client.put(
            f"/api/storylines/{created['id']}/anchors",
            json={"entity_ids": []},
        )
        assert resp.status_code == 400

    def test_regenerate_queues_job(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
    ) -> None:
        client, ctx = app_with_storylines
        created = client.post(
            "/api/storylines",
            json={"entity_ids": [ctx["entity_id"]], "name": "Running"},
        ).json()
        sid = created["id"]

        resp = client.post(f"/api/storylines/{sid}/regenerate")
        assert resp.status_code == 202
        body = resp.json()
        assert "job_id" in body

        # Flush the executor and check the fake was called for both
        # the auto-kicked create job (W7) and the explicit regen.
        ctx["runner"].shutdown(wait=True)
        assert ctx["gen_service"].calls == [sid, sid]

    def test_regenerate_404_on_unknown(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
    ) -> None:
        client, _ = app_with_storylines
        resp = client.post("/api/storylines/99999/regenerate")
        assert resp.status_code == 404

    def test_detail_panel_segments_carry_entry_date_through_serialization(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
    ) -> None:
        """End-to-end through the DB and the API: a citation segment
        stored with entry_date must come back through GET
        /api/storylines/{id} with the entry_date field intact. The
        webapp's curation date toggle and narrative date eyebrows
        depend on this contract."""
        client, ctx = app_with_storylines
        created = client.post(
            "/api/storylines",
            json={"entity_ids": [ctx["entity_id"]], "name": "Running"},
        ).json()
        sid = created["id"]
        # Inject a curation panel with entry_date stamped on its
        # citations — emulating what _build_curation_segments now
        # produces.
        ctx["repo"].upsert_panel(
            storyline_id=sid,
            panel_kind="curation",
            segments=[
                {"kind": "text", "text": "It begins on 2026-02-15:"},
                {
                    "kind": "citation",
                    "entry_id": 1,
                    "quote": "q1",
                    "entry_date": "2026-02-15",
                },
                {"kind": "text", "text": "Two weeks later:"},
                {
                    "kind": "citation",
                    "entry_id": 2,
                    "quote": "q2",
                    "entry_date": "2026-03-01",
                },
            ],
            source_entry_ids=[1, 2],
            citation_count=2,
            model_used="test",
        )
        resp = client.get(f"/api/storylines/{sid}")
        assert resp.status_code == 200
        panels = resp.json()["panels"]
        assert "curation" in panels
        citations = [
            s for s in panels["curation"]["segments"] if s["kind"] == "citation"
        ]
        assert len(citations) == 2
        assert citations[0]["entry_date"] == "2026-02-15"
        assert citations[1]["entry_date"] == "2026-03-01"

    def test_detail_handles_legacy_panels_without_entry_date(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
    ) -> None:
        """Storylines generated before the server added entry_date
        must still serve cleanly. The webapp's fallback hides the
        absolute-date toggle for those panels."""
        client, ctx = app_with_storylines
        created = client.post(
            "/api/storylines",
            json={"entity_ids": [ctx["entity_id"]], "name": "Running"},
        ).json()
        sid = created["id"]
        ctx["repo"].upsert_panel(
            storyline_id=sid,
            panel_kind="curation",
            segments=[
                {"kind": "text", "text": "It begins:"},
                {"kind": "citation", "entry_id": 1, "quote": "q"},
            ],
            source_entry_ids=[1],
            citation_count=1,
            model_used="test",
        )
        resp = client.get(f"/api/storylines/{sid}")
        assert resp.status_code == 200
        citation = next(
            s
            for s in resp.json()["panels"]["curation"]["segments"]
            if s["kind"] == "citation"
        )
        assert "entry_date" not in citation

    def test_delete_removes_storyline(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
    ) -> None:
        client, ctx = app_with_storylines
        created = client.post(
            "/api/storylines",
            json={"entity_ids": [ctx["entity_id"]], "name": "Running"},
        ).json()
        sid = created["id"]
        resp = client.delete(f"/api/storylines/{sid}")
        assert resp.status_code == 200
        # Now 404
        resp = client.get(f"/api/storylines/{sid}")
        assert resp.status_code == 404
