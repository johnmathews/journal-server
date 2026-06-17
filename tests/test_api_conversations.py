"""REST API tests for the conversations endpoints."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from mcp.server.fastmcp import FastMCP
from starlette.testclient import TestClient

from journal.api.conversations import register_conversations_routes
from journal.auth import AuthenticatedUser, _current_user_id
from journal.db.conversation_repository import SQLiteConversationRepository
from journal.db.factory import ConnectionFactory
from journal.models import SearchResult
from journal.providers.answerer import AnswerResult, AnswerUnavailable
from journal.services.conversations import ConversationService

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

_TEST_USER_ID = 1


class _FakeAuthMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] in ("http", "websocket"):
            scope["user"] = AuthenticatedUser(
                user_id=_TEST_USER_ID, email="t@example.com",
                display_name="T", is_admin=False, is_active=True,
                email_verified=True,
            )
            token = _current_user_id.set(_TEST_USER_ID)
            try:
                await self.app(scope, receive, send)
            finally:
                _current_user_id.reset(token)
        else:
            await self.app(scope, receive, send)


class _FakeQuery:
    def __init__(self, results: list[SearchResult]):
        self._results = results

    def search_entries(self, **kwargs):
        return self._results


class _FakeAnswerer:
    def __init__(self, result: AnswerResult | None = None, exc: Exception | None = None):
        self._result = result
        self._exc = exc

    def continue_conversation(self, history, passages):
        if self._exc is not None:
            raise self._exc
        return self._result


def _result(entry_id: int, date: str, text: str) -> SearchResult:
    return SearchResult(
        entry_id=entry_id, entry_date=date, text=text, score=1.0,
        matching_chunks=[], snippet=None,
    )


def _make_client(
    answerer: Any, tmp_path: Path
) -> tuple[TestClient, ConversationService, ConnectionFactory]:
    from journal.db.migrations import run_migrations

    factory = ConnectionFactory(tmp_path / "conv.db")
    run_migrations(factory.get())
    repo = SQLiteConversationRepository(factory)
    svc = ConversationService(
        repository=repo,
        query_service=_FakeQuery([_result(7, "2026-03-01", "Better now.")]),
        answerer=answerer,
        model="claude-sonnet-4-6",
    )
    services: dict[str, Any] = {"conversation": svc}
    mcp = FastMCP("test-conversations")
    register_conversations_routes(mcp, lambda: services)
    app = mcp.streamable_http_app()
    app.add_middleware(_FakeAuthMiddleware)
    return TestClient(app), svc, factory


@pytest.fixture
def client(tmp_path: Path) -> Generator[TestClient]:
    c, _svc, factory = _make_client(
        _FakeAnswerer(AnswerResult("Around 2026-03-01.", True, [7])), tmp_path
    )
    try:
        yield c
    finally:
        factory.close_current()


def _create(client: TestClient) -> dict:
    resp = client.post(
        "/api/conversations",
        json={
            "question": "when did my back hurt?",
            "answer": "On 2026-02-14.",
            "citations": [
                {"entry_id": 42, "entry_date": "2026-02-14", "snippet": "back"}
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_create_returns_seeded_conversation(client: TestClient) -> None:
    body = _create(client)
    assert body["title"] == "when did my back hurt?"
    assert [m["role"] for m in body["messages"]] == ["user", "assistant"]


def test_create_requires_question(client: TestClient) -> None:
    resp = client.post("/api/conversations", json={"question": "", "answer": "x"})
    assert resp.status_code == 400
    assert resp.json()["error"] == "missing_question"


def test_list_returns_summaries(client: TestClient) -> None:
    _create(client)
    resp = client.get("/api/conversations")
    assert resp.status_code == 200
    convs = resp.json()["conversations"]
    assert convs[0]["message_count"] == 2
    assert "messages" not in convs[0]


def test_get_returns_messages(client: TestClient) -> None:
    cid = _create(client)["id"]
    resp = client.get(f"/api/conversations/{cid}")
    assert resp.status_code == 200
    assert len(resp.json()["messages"]) == 2


def test_get_other_id_is_404(client: TestClient) -> None:
    resp = client.get("/api/conversations/99999")
    assert resp.status_code == 404
    assert resp.json()["error"] == "not_found"


def test_reply_appends_assistant_turn(client: TestClient) -> None:
    cid = _create(client)["id"]
    resp = client.post(
        f"/api/conversations/{cid}/messages",
        json={"message": "and when did it get better?"},
    )
    assert resp.status_code == 201, resp.text
    msg = resp.json()
    assert msg["role"] == "assistant"
    assert msg["content"] == "Around 2026-03-01."
    assert msg["citations"][0]["entry_id"] == 7


def test_reply_requires_message(client: TestClient) -> None:
    cid = _create(client)["id"]
    resp = client.post(f"/api/conversations/{cid}/messages", json={"message": "  "})
    assert resp.status_code == 400
    assert resp.json()["error"] == "missing_message"


def test_reply_missing_conversation_is_404(client: TestClient) -> None:
    resp = client.post(
        "/api/conversations/99999/messages", json={"message": "hi?"}
    )
    assert resp.status_code == 404


def test_reply_unavailable_is_502(tmp_path: Path) -> None:
    c, _svc, factory = _make_client(
        _FakeAnswerer(exc=AnswerUnavailable("boom")), tmp_path
    )
    try:
        create = c.post(
            "/api/conversations",
            json={"question": "q", "answer": "a", "citations": []},
        )
        cid = create.json()["id"]
        resp = c.post(f"/api/conversations/{cid}/messages", json={"message": "x"})
        assert resp.status_code == 502
        assert resp.json()["error"] == "answer_unavailable"
    finally:
        factory.close_current()


def test_delete_removes_conversation(client: TestClient) -> None:
    cid = _create(client)["id"]
    resp = client.delete(f"/api/conversations/{cid}")
    assert resp.status_code == 204
    assert client.get(f"/api/conversations/{cid}").status_code == 404


def test_delete_other_id_is_404(client: TestClient) -> None:
    resp = client.delete("/api/conversations/99999")
    assert resp.status_code == 404


def test_service_unwired_is_503() -> None:
    mcp = FastMCP("test-unwired")
    register_conversations_routes(mcp, lambda: {})
    app = mcp.streamable_http_app()
    app.add_middleware(_FakeAuthMiddleware)
    client = TestClient(app)
    resp = client.get("/api/conversations")
    assert resp.status_code == 503
