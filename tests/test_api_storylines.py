"""REST API tests for the storylines read-side endpoints (Task 9).

Covers ``GET /api/storylines`` (list), ``GET /api/storylines/{id}``
(summary + chapter meta), and ``GET /api/storylines/{id}/chapters/{cid}``
(chapter detail: segments + addenda) on a real Starlette test client
wired to a real SQLite db. The JobRunner is built with a fake
``StorylineEngine`` on Pool B so create/refresh/unpublish routes (write
side) can be exercised without a real judge/narrator; the read routes
in this file only touch the repository directly.
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
from journal.services.storylines.engine import UpdateResult

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path


_TEST_USER_ID = 1


def _insert_user(factory: ConnectionFactory, email: str) -> int:
    """Insert a minimal user row (satisfies the ``storylines.user_id`` FK)."""
    conn = factory.get()
    cursor = conn.execute(
        "INSERT INTO users (email, password_hash, display_name)"
        " VALUES (?, ?, ?)",
        (email, "x", "Other User"),
    )
    conn.commit()
    user_id = cursor.lastrowid
    assert user_id is not None
    return user_id


def _insert_entry(factory: ConnectionFactory, user_id: int, entry_date: str) -> int:
    """Insert a minimal entry row (satisfies the ``storyline_chapter_entries``
    FK to ``entries``)."""
    conn = factory.get()
    text = f"Entry on {entry_date}"
    cursor = conn.execute(
        "INSERT INTO entries"
        " (entry_date, source_type, raw_text, final_text, word_count, user_id)"
        " VALUES (?, 'text', ?, ?, ?, ?)",
        (entry_date, text, text, len(text.split()), user_id),
    )
    conn.commit()
    entry_id = cursor.lastrowid
    assert entry_id is not None
    return entry_id


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

    mcp = FastMCP("test-storylines")
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


@pytest.fixture
def seeded_storyline(
    app_with_storylines: tuple[TestClient, dict[str, Any]],
) -> tuple[int, int, int]:
    """Create a storyline with one published chapter + a fresh draft.

    Returns ``(storyline_id, published_chapter_id, draft_chapter_id)``.
    Written directly through the repository (not the API) so read
    tests don't depend on the write routes / job runner.
    """
    _client, ctx = app_with_storylines
    repo: SQLiteStorylineRepository = ctx["repo"]
    sl = repo.create_storyline(
        user_id=_TEST_USER_ID, entity_ids=[ctx["entity_id"]], name="Seeded",
    )
    published, new_draft = repo.publish_draft(
        sl.id,
        title="Chapter One",
        segments=[{"kind": "text", "text": "It happened."}],
        source_entry_ids=[1],
        citation_count=2,
        model_used="test-model",
        new_draft_entry_ids=[],
    )
    return sl.id, published.id, new_draft.id


class TestListStorylines:
    def test_list_returns_empty_envelope(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
    ) -> None:
        client, _ = app_with_storylines
        resp = client.get("/api/storylines")
        assert resp.status_code == 200
        body = resp.json()
        assert body == {"items": [], "total": 0, "limit": 50, "offset": 0}

    def test_list_includes_unread_count_via_publish_and_set_read(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
        seeded_storyline: tuple[int, int, int],
    ) -> None:
        client, ctx = app_with_storylines
        sid, published_id, _draft_id = seeded_storyline
        resp = client.get("/api/storylines")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        item = body["items"][0]
        assert item["id"] == sid
        assert item["unread_count"] == 1
        assert item["chapter_count"] == 2  # published + draft
        assert item["status"] == "active"
        assert item["name"] == "Seeded"
        assert item["description"] == ""
        assert {"entity_id", "canonical_name"} <= item["anchors"][0].keys()

        ctx["repo"].set_read(published_id, True)
        resp2 = client.get("/api/storylines")
        assert resp2.json()["items"][0]["unread_count"] == 0

    def test_list_respects_pagination_params(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
    ) -> None:
        client, ctx = app_with_storylines
        repo: SQLiteStorylineRepository = ctx["repo"]
        for i in range(3):
            repo.create_storyline(
                user_id=_TEST_USER_ID, entity_ids=[ctx["entity_id"]],
                name=f"S{i}",
            )
        resp = client.get("/api/storylines?limit=1&offset=1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 3
        assert body["limit"] == 1
        assert body["offset"] == 1
        assert len(body["items"]) == 1

    def test_list_bad_pagination_params_returns_400(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
    ) -> None:
        client, _ = app_with_storylines
        resp = client.get("/api/storylines?limit=nope")
        assert resp.status_code == 400

    def test_list_search_filters_items_and_total(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
    ) -> None:
        client, ctx = app_with_storylines
        repo: SQLiteStorylineRepository = ctx["repo"]
        # Three matching + two non-matching storylines.
        for i in range(3):
            repo.create_storyline(
                user_id=_TEST_USER_ID, entity_ids=[ctx["entity_id"]],
                name=f"Marathon {i}",
            )
        for name in ("Cooking", "Cycling"):
            repo.create_storyline(
                user_id=_TEST_USER_ID, entity_ids=[ctx["entity_id"]],
                name=name,
            )
        resp = client.get("/api/storylines?search=marathon&limit=2")
        assert resp.status_code == 200
        body = resp.json()
        # total reflects the whole-table filtered count, not the page size.
        assert body["total"] == 3
        assert len(body["items"]) == 2
        assert all("Marathon" in item["name"] for item in body["items"])


class TestStorylineDetail:
    def test_detail_returns_chapters_seq_asc_draft_last(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
        seeded_storyline: tuple[int, int, int],
    ) -> None:
        client, _ = app_with_storylines
        sid, published_id, draft_id = seeded_storyline
        resp = client.get(f"/api/storylines/{sid}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == sid
        assert body["chapter_count"] == 2
        assert body["unread_count"] == 1
        chapters = body["chapters"]
        assert [c["id"] for c in chapters] == [published_id, draft_id]
        assert chapters[0]["state"] == "published"
        assert chapters[0]["seq"] == 1
        assert chapters[0]["citation_count"] == 2
        assert chapters[0]["first_entry_date"] is None  # entry_id=1 doesn't exist
        assert chapters[1]["state"] == "draft"
        assert chapters[1]["seq"] == 2
        # Chapter meta must not leak narrative content.
        assert "segments" not in chapters[0]
        assert "addenda" not in chapters[0]

    def test_detail_404_unknown_storyline(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
    ) -> None:
        client, _ = app_with_storylines
        resp = client.get("/api/storylines/99999")
        assert resp.status_code == 404

    def test_detail_404_wrong_user(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
    ) -> None:
        client, ctx = app_with_storylines
        other_user_id = _insert_user(ctx["factory"], "other@example.com")
        other = ctx["repo"].create_storyline(
            user_id=other_user_id, entity_ids=[ctx["entity_id"]], name="Not yours",
        )
        resp = client.get(f"/api/storylines/{other.id}")
        assert resp.status_code == 404


class TestStorylineChapterDetail:
    def test_chapter_detail_returns_segments_and_addenda(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
        seeded_storyline: tuple[int, int, int],
    ) -> None:
        client, ctx = app_with_storylines
        sid, published_id, _draft_id = seeded_storyline
        addendum_entry_id = _insert_entry(ctx["factory"], _TEST_USER_ID, "2026-05-01")
        ctx["repo"].append_addendum(
            published_id,
            segments=[{"kind": "text", "text": "Later:"}],
            entry_ids=[addendum_entry_id],
        )
        resp = client.get(f"/api/storylines/{sid}/chapters/{published_id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == published_id
        assert body["segments"] == [{"kind": "text", "text": "It happened."}]
        assert len(body["addenda"]) == 1
        assert body["addenda"][0]["entry_ids"] == [addendum_entry_id]
        assert body["model_used"] == "test-model"
        assert body["generated_at"] is not None
        # Meta fields present too.
        assert body["state"] == "published"
        assert body["citation_count"] == 2

    def test_chapter_detail_404_cross_storyline_cid(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
        seeded_storyline: tuple[int, int, int],
    ) -> None:
        client, ctx = app_with_storylines
        _sid, published_id, _draft_id = seeded_storyline
        other = ctx["repo"].create_storyline(
            user_id=_TEST_USER_ID, entity_ids=[ctx["entity_id"]], name="Other",
        )
        resp = client.get(f"/api/storylines/{other.id}/chapters/{published_id}")
        assert resp.status_code == 404

    def test_chapter_detail_404_unknown_chapter(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
        seeded_storyline: tuple[int, int, int],
    ) -> None:
        client, _ctx = app_with_storylines
        sid, _published_id, _draft_id = seeded_storyline
        resp = client.get(f"/api/storylines/{sid}/chapters/99999")
        assert resp.status_code == 404

    def test_chapter_detail_404_unknown_storyline(
        self,
        app_with_storylines: tuple[TestClient, dict[str, Any]],
        seeded_storyline: tuple[int, int, int],
    ) -> None:
        client, _ctx = app_with_storylines
        _sid, published_id, _draft_id = seeded_storyline
        resp = client.get(f"/api/storylines/99999/chapters/{published_id}")
        assert resp.status_code == 404


class TestStorylines503WhenUnwired:
    def test_list_503_when_repo_missing(self) -> None:
        mcp = FastMCP("test-storylines-unwired")
        register_storylines_routes(mcp, lambda: {})
        app = mcp.streamable_http_app()
        app.add_middleware(_FakeAuthMiddleware)
        client = TestClient(app)
        resp = client.get("/api/storylines")
        assert resp.status_code == 503

    def test_detail_503_when_repo_missing(self) -> None:
        mcp = FastMCP("test-storylines-unwired-2")
        register_storylines_routes(mcp, lambda: {})
        app = mcp.streamable_http_app()
        app.add_middleware(_FakeAuthMiddleware)
        client = TestClient(app)
        resp = client.get("/api/storylines/1")
        assert resp.status_code == 503
