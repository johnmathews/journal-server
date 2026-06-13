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

from journal.api.storylines import register_storylines_routes
from journal.api.storylines_write import register_storylines_write_routes
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
        self.chapter_calls: list[int] = []
        self.chapter_kwargs: list[dict[str, Any]] = []

    def regenerate(
        self,
        storyline_id: int,
        **kwargs: Any,  # noqa: ANN401
    ) -> GenerationResult:
        self.calls.append(storyline_id)
        self.kwargs.append(kwargs)
        return GenerationResult(storyline_id=storyline_id, entry_count=0)

    def regenerate_chapter(
        self,
        chapter_id: int,
        **kwargs: Any,  # noqa: ANN401
    ) -> GenerationResult:
        self.chapter_calls.append(chapter_id)
        self.chapter_kwargs.append(kwargs)
        return GenerationResult(storyline_id=0, entry_count=0)


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
    register_storylines_write_routes(mcp, lambda: services_dict)

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


# ── PATCH /api/storylines/{id} — rename ─────────────────────────────


class TestUpdateStoryline:
    def _create(self, client: TestClient, ctx: dict[str, Any]) -> int:
        return client.post(
            "/api/storylines",
            json={"entity_ids": [ctx["entity_id"]], "name": "Old name"},
        ).json()["id"]

    def test_rename_updates_name_and_returns_storyline(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
    ) -> None:
        client, ctx = app_with_storylines
        sid = self._create(client, ctx)

        resp = client.patch(
            f"/api/storylines/{sid}", json={"name": "New name"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == sid
        assert body["name"] == "New name"
        # Anchors are echoed back so the client can refresh the row.
        assert [a["id"] for a in body["anchors"]] == [ctx["entity_id"]]

        # Persisted: a GET reflects the new name.
        fetched = client.get(f"/api/storylines/{sid}").json()
        assert fetched["name"] == "New name"

    def test_rename_trims_whitespace(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
    ) -> None:
        client, ctx = app_with_storylines
        sid = self._create(client, ctx)
        resp = client.patch(
            f"/api/storylines/{sid}", json={"name": "   Trimmed   "},
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "Trimmed"

    def test_rename_does_not_kick_a_job(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
    ) -> None:
        """A rename is metadata-only — it must not regenerate panels."""
        client, ctx = app_with_storylines
        sid = self._create(client, ctx)
        ctx["runner"].shutdown(wait=True, cancel_futures=False)
        calls_before = list(ctx["gen_service"].calls)

        client.patch(f"/api/storylines/{sid}", json={"name": "Renamed"})
        assert ctx["gen_service"].calls == calls_before

    def test_empty_name_returns_400(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
    ) -> None:
        client, ctx = app_with_storylines
        sid = self._create(client, ctx)
        resp = client.patch(f"/api/storylines/{sid}", json={"name": "   "})
        assert resp.status_code == 400
        # Unchanged.
        assert client.get(f"/api/storylines/{sid}").json()["name"] == "Old name"

    def test_missing_name_returns_400(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
    ) -> None:
        client, ctx = app_with_storylines
        sid = self._create(client, ctx)
        resp = client.patch(f"/api/storylines/{sid}", json={})
        assert resp.status_code == 400

    def test_non_object_body_returns_400(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
    ) -> None:
        client, ctx = app_with_storylines
        sid = self._create(client, ctx)
        resp = client.patch(f"/api/storylines/{sid}", json=["nope"])
        assert resp.status_code == 400

    def test_unknown_storyline_returns_404(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
    ) -> None:
        client, _ctx = app_with_storylines
        resp = client.patch("/api/storylines/999999", json={"name": "X"})
        assert resp.status_code == 404


# ── chapter regenerate + rename ─────────────────────────────────────


@pytest.fixture
def seed_storyline(
    app_with_storylines: tuple[TestClient, dict[str, Any]],
) -> tuple[int, int]:
    """Create a storyline + seq-1 open chapter + two panels.

    Returns ``(storyline_id, chapter_id)``.
    """
    _client, ctx = app_with_storylines
    repo = ctx["repo"]
    sl = repo.create_storyline(
        user_id=_TEST_USER_ID, entity_ids=[ctx["entity_id"]],
        name="Seeded", start_date="2026-01-01", end_date="2026-03-01",
    )
    chapter = repo.create_chapter(
        storyline_id=sl.id, seq=1, title="Chapter 1",
        start_date="2026-01-01", end_date="2026-03-01", state="open",
    )
    repo.upsert_panel(
        chapter_id=chapter.id, panel_kind="narrative",
        segments=[{"kind": "text", "text": "prose"}],
        source_entry_ids=[1], citation_count=2, model_used="m",
    )
    repo.upsert_panel(
        chapter_id=chapter.id, panel_kind="curation",
        segments=[{"kind": "text", "text": "timeline"}],
        source_entry_ids=[1], citation_count=3, model_used="m",
    )
    return sl.id, chapter.id


class TestChapterRegenerate:
    def test_regenerate_single_chapter_queues_job(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
        seed_storyline: tuple[int, int],
    ) -> None:
        client, ctx = app_with_storylines
        sid, chapter_id = seed_storyline
        resp = client.post(
            f"/api/storylines/{sid}/chapters/{chapter_id}/regenerate",
        )
        assert resp.status_code == 202
        body = resp.json()
        assert "job_id" in body

        # Flush the executor; the worker routes the chapter_id payload to
        # the fake service's regenerate_chapter (replace-only).
        ctx["runner"].shutdown(wait=True, cancel_futures=False)
        assert ctx["gen_service"].chapter_calls == [chapter_id]
        assert ctx["gen_service"].chapter_kwargs[-1].get("mode") == "replace"

    def test_regenerate_chapter_404_for_wrong_storyline(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
        seed_storyline: tuple[int, int],
    ) -> None:
        client, _ctx = app_with_storylines
        _sid, chapter_id = seed_storyline
        resp = client.post(
            f"/api/storylines/99999/chapters/{chapter_id}/regenerate",
        )
        assert resp.status_code == 404

    def test_regenerate_chapter_404_for_unknown_chapter(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
        seed_storyline: tuple[int, int],
    ) -> None:
        client, _ctx = app_with_storylines
        sid, _chapter_id = seed_storyline
        resp = client.post(
            f"/api/storylines/{sid}/chapters/99999/regenerate",
        )
        assert resp.status_code == 404


class TestChapterRename:
    def test_rename_chapter(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
        seed_storyline: tuple[int, int],
    ) -> None:
        client, _ctx = app_with_storylines
        sid, chapter_id = seed_storyline
        resp = client.patch(
            f"/api/storylines/{sid}/chapters/{chapter_id}",
            json={"title": "The Move"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == chapter_id
        assert body["title"] == "The Move"

        # Persisted: a GET on the chapter reflects the new title.
        fetched = client.get(
            f"/api/storylines/{sid}/chapters/{chapter_id}",
        ).json()
        assert fetched["title"] == "The Move"

    def test_rename_chapter_trims_whitespace(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
        seed_storyline: tuple[int, int],
    ) -> None:
        client, _ctx = app_with_storylines
        sid, chapter_id = seed_storyline
        resp = client.patch(
            f"/api/storylines/{sid}/chapters/{chapter_id}",
            json={"title": "   Trimmed   "},
        )
        assert resp.status_code == 200
        assert resp.json()["title"] == "Trimmed"

    def test_rename_chapter_empty_returns_400(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
        seed_storyline: tuple[int, int],
    ) -> None:
        client, _ctx = app_with_storylines
        sid, chapter_id = seed_storyline
        resp = client.patch(
            f"/api/storylines/{sid}/chapters/{chapter_id}",
            json={"title": "   "},
        )
        assert resp.status_code == 400

    def test_rename_chapter_404_for_wrong_storyline(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
        seed_storyline: tuple[int, int],
    ) -> None:
        client, _ctx = app_with_storylines
        _sid, chapter_id = seed_storyline
        resp = client.patch(
            f"/api/storylines/99999/chapters/{chapter_id}",
            json={"title": "X"},
        )
        assert resp.status_code == 404


class TestCreateSeedsChapter:
    def test_create_seeds_one_open_chapter(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
    ) -> None:
        client, ctx = app_with_storylines
        created = client.post(
            "/api/storylines",
            json={"entity_ids": [ctx["entity_id"]], "name": "Running"},
        ).json()
        sid = created["id"]
        chapters = ctx["repo"].list_chapters(sid)
        assert len(chapters) == 1
        assert chapters[0].state == "open"
        assert chapters[0].seq == 1
        # Exactly one open chapter resolvable.
        assert ctx["repo"].get_open_chapter(sid) is not None
