"""REST API tests for the storylines write/job-creation endpoints (Task 9).

Covers create (+ auto-bootstrap), refresh, read/unread, chapter rename,
unpublish, PATCH/DELETE/PUT-anchors, and confirms the deleted routes
from the pre-redesign chapter-editing surface (add/split/merge/delete/
per-chapter-regenerate) are gone. Builds on the same in-process
TestClient pattern as ``tests/test_api_storylines.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from mcp.server.fastmcp import FastMCP
from starlette.testclient import TestClient

from journal.api.storylines import register_storylines_routes
from journal.api.storylines_write import MAX_ANCHORS, register_storylines_write_routes
from journal.auth import AuthenticatedUser, _current_user_id
from journal.db.factory import ConnectionFactory
from journal.db.jobs_repository import SQLiteJobRepository
from journal.db.storyline_repository import SQLiteStorylineRepository
from journal.entitystore.store import SQLiteEntityStore
from journal.services.jobs import JobRunner
from journal.services.storylines.engine import UpdateResult

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


class _FakeStorylineEngine:
    """Records every call; returns an empty :class:`UpdateResult`."""

    def __init__(self) -> None:
        self.update_calls: list[int] = []
        self.bootstrap_calls: list[int] = []
        self.refresh_calls: list[int] = []

    def update(self, storyline_id: int) -> UpdateResult:
        self.update_calls.append(storyline_id)
        return UpdateResult(storyline_id=storyline_id)

    def bootstrap(
        self, storyline_id: int, *, mark_read: bool = False,  # noqa: ARG002
    ) -> UpdateResult:
        self.bootstrap_calls.append(storyline_id)
        return UpdateResult(storyline_id=storyline_id)

    def refresh_draft(self, storyline_id: int) -> UpdateResult:
        self.refresh_calls.append(storyline_id)
        return UpdateResult(storyline_id=storyline_id)


@pytest.fixture
def app_with_storylines(
    tmp_path: Path,
) -> Generator[tuple[TestClient, dict[str, Any]]]:
    """Wire a Starlette TestClient against a real SQLite db + fake engine.

    ``runner.shutdown(wait=True, cancel_futures=False)`` runs in teardown
    so the ThreadPoolExecutor is flushed before the process exits —
    missed shutdown causes CI segfaults (per project memory).
    """
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
    engine = _FakeStorylineEngine()

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
        storyline_engine=engine,
        storyline_repository=storyline_repo,
    )

    services_dict: dict[str, Any] = {
        "storyline_repository": storyline_repo,
        "entity_store": entity_store,
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
            "entity_store": entity_store,
            "entity_id": entity.id,
            "engine": engine,
            "runner": runner,
        }
    finally:
        runner.shutdown(wait=True, cancel_futures=False)
        factory.close_current()


class TestCreateStoryline:
    def test_create_returns_201_with_storyline_and_bootstrap_job(
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
        assert "bootstrap_job_id" in body
        assert body["bootstrap_job_id"] is not None
        storyline = body["storyline"]
        assert storyline["name"] == "Running"
        assert storyline["status"] == "active"
        assert storyline["description"] == ""
        assert storyline["chapter_count"] == 1  # seq-1 draft, seeded by the repo
        assert storyline["chapters"][0]["state"] == "draft"
        assert [a["entity_id"] for a in storyline["anchors"]] == [ctx["entity_id"]]

        # The bootstrap job was actually queued on the fake engine.
        ctx["runner"].shutdown(wait=True, cancel_futures=False)
        assert ctx["engine"].bootstrap_calls == [storyline["id"]]

    def test_create_rejects_missing_fields(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
    ) -> None:
        client, _ = app_with_storylines
        resp = client.post("/api/storylines", json={})
        assert resp.status_code == 400

    def test_create_returns_409_on_duplicate_anchor_set_and_name(
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

    def test_create_rejects_zero_anchors_with_422(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
    ) -> None:
        client, _ = app_with_storylines
        resp = client.post(
            "/api/storylines", json={"entity_ids": [], "name": "Empty"},
        )
        assert resp.status_code == 422

    def test_create_rejects_above_cap_with_422(
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
            for i in range(MAX_ANCHORS + 1)
        ]
        resp = client.post(
            "/api/storylines", json={"entity_ids": ids, "name": "Too many"},
        )
        assert resp.status_code == 422
        assert "cap" in resp.json()["error"] or str(MAX_ANCHORS) in resp.json()["error"]

    def test_create_multiple_anchors_returns_anchors_list(
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
        resp = client.post(
            "/api/storylines",
            json={"entity_ids": [ent_b.id, ctx["entity_id"]], "name": "Duo"},
        )
        assert resp.status_code == 201
        anchors = resp.json()["storyline"]["anchors"]
        names = {a["canonical_name"] for a in anchors}
        assert names == {"Running", "Sara"}


class TestRefreshStoryline:
    def _create(self, client: TestClient, ctx: dict[str, Any]) -> int:
        return client.post(
            "/api/storylines",
            json={"entity_ids": [ctx["entity_id"]], "name": "Refresh me"},
        ).json()["storyline"]["id"]

    def test_refresh_queues_refresh_only_job(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
    ) -> None:
        client, ctx = app_with_storylines
        sid = self._create(client, ctx)

        # Pool B is single-worker, so this refresh just queues behind the
        # create's bootstrap job rather than racing it — no shutdown needed
        # before submitting.
        resp = client.post(f"/api/storylines/{sid}/refresh")
        assert resp.status_code == 202
        body = resp.json()
        assert "job_id" in body
        assert "status" in body

        ctx["runner"].shutdown(wait=True, cancel_futures=False)
        assert ctx["engine"].refresh_calls == [sid]
        # refresh must not trigger bootstrap/update.
        assert ctx["engine"].bootstrap_calls == [sid]  # only the original create
        assert ctx["engine"].update_calls == []

    def test_refresh_404_on_unknown_storyline(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
    ) -> None:
        client, _ = app_with_storylines
        resp = client.post("/api/storylines/99999/refresh")
        assert resp.status_code == 404


class TestReadUnread:
    @pytest.fixture
    def seeded(
        self, app_with_storylines: tuple[TestClient, dict[str, Any]],
    ) -> tuple[int, int, int]:
        _client, ctx = app_with_storylines
        repo: SQLiteStorylineRepository = ctx["repo"]
        sl = repo.create_storyline(
            user_id=_TEST_USER_ID, entity_ids=[ctx["entity_id"]], name="Readable",
        )
        published, new_draft = repo.publish_draft(
            sl.id, title="Ch1", segments=[{"kind": "text", "text": "x"}],
            source_entry_ids=[], citation_count=0, model_used="m",
            new_draft_entry_ids=[],
        )
        return sl.id, published.id, new_draft.id

    def test_read_marks_chapter_read(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
        seeded: tuple[int, int, int],
    ) -> None:
        client, _ctx = app_with_storylines
        sid, published_id, _draft_id = seeded
        resp = client.post(f"/api/storylines/{sid}/chapters/{published_id}/read")
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == published_id
        assert body["read_at"] is not None

    def test_unread_clears_read_at(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
        seeded: tuple[int, int, int],
    ) -> None:
        client, _ctx = app_with_storylines
        sid, published_id, _draft_id = seeded
        client.post(f"/api/storylines/{sid}/chapters/{published_id}/read")
        resp = client.post(f"/api/storylines/{sid}/chapters/{published_id}/unread")
        assert resp.status_code == 200
        assert resp.json()["read_at"] is None

    def test_read_400_on_draft_chapter(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
        seeded: tuple[int, int, int],
    ) -> None:
        client, _ctx = app_with_storylines
        sid, _published_id, draft_id = seeded
        resp = client.post(f"/api/storylines/{sid}/chapters/{draft_id}/read")
        assert resp.status_code == 400

    def test_read_404_cross_storyline(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
        seeded: tuple[int, int, int],
    ) -> None:
        client, ctx = app_with_storylines
        _sid, published_id, _draft_id = seeded
        other = ctx["repo"].create_storyline(
            user_id=_TEST_USER_ID, entity_ids=[ctx["entity_id"]], name="Other",
        )
        resp = client.post(
            f"/api/storylines/{other.id}/chapters/{published_id}/read",
        )
        assert resp.status_code == 404


class TestRenameChapter:
    @pytest.fixture
    def seeded(
        self, app_with_storylines: tuple[TestClient, dict[str, Any]],
    ) -> tuple[int, int]:
        _client, ctx = app_with_storylines
        repo: SQLiteStorylineRepository = ctx["repo"]
        sl = repo.create_storyline(
            user_id=_TEST_USER_ID, entity_ids=[ctx["entity_id"]], name="Renameable",
        )
        draft = repo.get_draft(sl.id)
        assert draft is not None
        return sl.id, draft.id

    def test_rename_chapter_returns_200_meta(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
        seeded: tuple[int, int],
    ) -> None:
        client, _ctx = app_with_storylines
        sid, cid = seeded
        resp = client.patch(
            f"/api/storylines/{sid}/chapters/{cid}", json={"title": "The Move"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == cid
        assert body["title"] == "The Move"
        assert "segments" not in body

        fetched = client.get(f"/api/storylines/{sid}/chapters/{cid}").json()
        assert fetched["title"] == "The Move"

    def test_rename_chapter_empty_title_returns_400(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
        seeded: tuple[int, int],
    ) -> None:
        client, _ctx = app_with_storylines
        sid, cid = seeded
        resp = client.patch(
            f"/api/storylines/{sid}/chapters/{cid}", json={"title": "   "},
        )
        assert resp.status_code == 400

    def test_rename_chapter_missing_title_returns_400(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
        seeded: tuple[int, int],
    ) -> None:
        client, _ctx = app_with_storylines
        sid, cid = seeded
        resp = client.patch(f"/api/storylines/{sid}/chapters/{cid}", json={})
        assert resp.status_code == 400

    def test_rename_chapter_404_for_wrong_storyline(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
        seeded: tuple[int, int],
    ) -> None:
        client, _ctx = app_with_storylines
        _sid, cid = seeded
        resp = client.patch(
            f"/api/storylines/99999/chapters/{cid}", json={"title": "X"},
        )
        assert resp.status_code == 404


class TestUnpublish:
    def _seed_published(
        self, ctx: dict[str, Any],
    ) -> int:
        repo: SQLiteStorylineRepository = ctx["repo"]
        sl = repo.create_storyline(
            user_id=_TEST_USER_ID, entity_ids=[ctx["entity_id"]], name="Unpublish me",
        )
        repo.publish_draft(
            sl.id, title="Ch1", segments=[{"kind": "text", "text": "x"}],
            source_entry_ids=[], citation_count=0, model_used="m",
            new_draft_entry_ids=[],
        )
        return sl.id

    def test_unpublish_returns_202_and_queues_job(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
    ) -> None:
        client, ctx = app_with_storylines
        sid = self._seed_published(ctx)
        resp = client.post(f"/api/storylines/{sid}/chapters/unpublish")
        assert resp.status_code == 202
        body = resp.json()
        assert "job_id" in body

        ctx["runner"].shutdown(wait=True, cancel_futures=False)
        # The worker folds the published chapter back and re-narrates
        # via refresh_draft.
        assert ctx["engine"].refresh_calls == [sid]
        chapters = ctx["repo"].list_chapters(sid)
        assert len(chapters) == 1
        assert chapters[0].state == "draft"

    def test_unpublish_400_when_no_published_chapter(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
    ) -> None:
        client, ctx = app_with_storylines
        repo: SQLiteStorylineRepository = ctx["repo"]
        sl = repo.create_storyline(
            user_id=_TEST_USER_ID, entity_ids=[ctx["entity_id"]], name="No chapters",
        )
        resp = client.post(f"/api/storylines/{sl.id}/chapters/unpublish")
        assert resp.status_code == 400

    def test_unpublish_404_on_unknown_storyline(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
    ) -> None:
        client, _ctx = app_with_storylines
        resp = client.post("/api/storylines/99999/chapters/unpublish")
        assert resp.status_code == 404


class TestUpdateStoryline:
    def _create(self, client: TestClient, ctx: dict[str, Any]) -> int:
        return client.post(
            "/api/storylines",
            json={"entity_ids": [ctx["entity_id"]], "name": "Old name"},
        ).json()["storyline"]["id"]

    def test_rename_updates_name(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
    ) -> None:
        client, ctx = app_with_storylines
        sid = self._create(client, ctx)
        resp = client.patch(f"/api/storylines/{sid}", json={"name": "New name"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == sid
        assert body["name"] == "New name"
        assert [a["entity_id"] for a in body["anchors"]] == [ctx["entity_id"]]

        fetched = client.get(f"/api/storylines/{sid}").json()
        assert fetched["name"] == "New name"

    def test_rename_trims_whitespace(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
    ) -> None:
        client, ctx = app_with_storylines
        sid = self._create(client, ctx)
        resp = client.patch(f"/api/storylines/{sid}", json={"name": "  Trimmed  "})
        assert resp.status_code == 200
        assert resp.json()["name"] == "Trimmed"

    def test_update_status_to_archived(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
    ) -> None:
        client, ctx = app_with_storylines
        sid = self._create(client, ctx)
        resp = client.patch(f"/api/storylines/{sid}", json={"status": "archived"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "archived"

    def test_update_invalid_status_returns_400(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
    ) -> None:
        client, ctx = app_with_storylines
        sid = self._create(client, ctx)
        resp = client.patch(f"/api/storylines/{sid}", json={"status": "bogus"})
        assert resp.status_code == 400

    def test_empty_name_returns_400(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
    ) -> None:
        client, ctx = app_with_storylines
        sid = self._create(client, ctx)
        resp = client.patch(f"/api/storylines/{sid}", json={"name": "   "})
        assert resp.status_code == 400
        assert client.get(f"/api/storylines/{sid}").json()["name"] == "Old name"

    def test_missing_body_fields_returns_400(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
    ) -> None:
        client, ctx = app_with_storylines
        sid = self._create(client, ctx)
        resp = client.patch(f"/api/storylines/{sid}", json={})
        assert resp.status_code == 400

    def test_unknown_storyline_returns_404(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
    ) -> None:
        client, _ctx = app_with_storylines
        resp = client.patch("/api/storylines/999999", json={"name": "X"})
        assert resp.status_code == 404


class TestDeleteStoryline:
    def test_delete_removes_storyline(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
    ) -> None:
        client, ctx = app_with_storylines
        created = client.post(
            "/api/storylines",
            json={"entity_ids": [ctx["entity_id"]], "name": "Running"},
        ).json()
        sid = created["storyline"]["id"]
        resp = client.delete(f"/api/storylines/{sid}")
        assert resp.status_code == 200
        assert client.get(f"/api/storylines/{sid}").status_code == 404

    def test_delete_404_on_unknown(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
    ) -> None:
        client, _ctx = app_with_storylines
        resp = client.delete("/api/storylines/999999")
        assert resp.status_code == 404


class TestSetAnchors:
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
        sid = created["storyline"]["id"]

        resp = client.put(
            f"/api/storylines/{sid}/anchors",
            json={"entity_ids": [ent_b.id, ent_c.id]},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert sorted(a["entity_id"] for a in body["anchors"]) == sorted(
            [ent_b.id, ent_c.id],
        )

        detail = client.get(f"/api/storylines/{sid}").json()
        assert sorted(a["entity_id"] for a in detail["anchors"]) == sorted(
            [ent_b.id, ent_c.id],
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
        sid = created["storyline"]["id"]
        resp = client.put(f"/api/storylines/{sid}/anchors", json={"entity_ids": []})
        assert resp.status_code == 400

    def test_set_anchors_rejects_above_cap(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
    ) -> None:
        client, ctx = app_with_storylines
        store = ctx["entity_store"]
        created = client.post(
            "/api/storylines",
            json={"entity_ids": [ctx["entity_id"]], "name": "Y"},
        ).json()
        sid = created["storyline"]["id"]
        ids = [
            store.create_entity(
                entity_type="person", canonical_name=f"P{i}",
                description="", first_seen="2026-02-15",
                user_id=_TEST_USER_ID,
            ).id
            for i in range(MAX_ANCHORS + 1)
        ]
        resp = client.put(f"/api/storylines/{sid}/anchors", json={"entity_ids": ids})
        assert resp.status_code == 422


class TestDeletedRoutesNowGone:
    """The pre-redesign chapter-editing surface is gone.

    Everything under ``/api/storylines/*`` that isn't in the Task 9
    route surface must not be routable to old handler code anymore.
    Four of the five paths below have no route at all → 404. The fifth
    (``DELETE .../chapters/{cid}``) shares its URL pattern with the
    still-live GET/PATCH chapter routes, so Starlette's router reports
    405 Method Not Allowed rather than 404 — that's the accurate signal
    that no DELETE handler exists on that path; verified against a live
    TestClient rather than assumed.
    """

    @pytest.fixture
    def sid_cid(
        self, app_with_storylines: tuple[TestClient, dict[str, Any]],
    ) -> tuple[int, int]:
        _client, ctx = app_with_storylines
        repo: SQLiteStorylineRepository = ctx["repo"]
        sl = repo.create_storyline(
            user_id=_TEST_USER_ID, entity_ids=[ctx["entity_id"]], name="Gone routes",
        )
        draft = repo.get_draft(sl.id)
        assert draft is not None
        return sl.id, draft.id

    def test_add_chapter_post_is_404(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
        sid_cid: tuple[int, int],
    ) -> None:
        client, _ctx = app_with_storylines
        sid, _cid = sid_cid
        resp = client.post(f"/api/storylines/{sid}/chapters", json={})
        assert resp.status_code == 404

    def test_split_chapter_post_is_404(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
        sid_cid: tuple[int, int],
    ) -> None:
        client, _ctx = app_with_storylines
        sid, cid = sid_cid
        resp = client.post(
            f"/api/storylines/{sid}/chapters/{cid}/split", json={"date": "2026-01-01"},
        )
        assert resp.status_code == 404

    def test_merge_chapters_post_is_404(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
        sid_cid: tuple[int, int],
    ) -> None:
        client, _ctx = app_with_storylines
        sid, cid = sid_cid
        resp = client.post(
            f"/api/storylines/{sid}/chapters/merge", json={"chapter_ids": [cid]},
        )
        assert resp.status_code == 404

    def test_regenerate_chapter_post_is_404(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
        sid_cid: tuple[int, int],
    ) -> None:
        client, _ctx = app_with_storylines
        sid, cid = sid_cid
        resp = client.post(f"/api/storylines/{sid}/chapters/{cid}/regenerate")
        assert resp.status_code == 404

    def test_regenerate_storyline_post_is_404(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
        sid_cid: tuple[int, int],
    ) -> None:
        """The old whole-storyline regenerate route is also gone (replaced
        by ``POST /{id}/refresh``)."""
        client, _ctx = app_with_storylines
        sid, _cid = sid_cid
        resp = client.post(f"/api/storylines/{sid}/regenerate")
        assert resp.status_code == 404

    def test_delete_chapter_is_not_routable(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
        sid_cid: tuple[int, int],
    ) -> None:
        client, _ctx = app_with_storylines
        sid, cid = sid_cid
        resp = client.delete(f"/api/storylines/{sid}/chapters/{cid}")
        # 405, not 200/204 — no DELETE handler is registered on this
        # path; GET (chapter detail) and PATCH (rename) still are.
        assert resp.status_code == 405
