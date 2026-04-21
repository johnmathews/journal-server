"""Tests for user preferences — repository layer and API endpoints."""

import sqlite3
from collections.abc import Generator
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from starlette.testclient import TestClient

from journal.auth import AuthenticatedUser, _current_user_id
from journal.db.connection import get_connection
from journal.db.jobs_repository import SQLiteJobRepository
from journal.db.migrations import run_migrations
from journal.db.repository import SQLiteEntryRepository
from journal.db.user_repository import SQLiteUserRepository
from journal.entitystore.store import SQLiteEntityStore
from journal.services.ingestion import IngestionService
from journal.services.query import QueryService

_TEST_USER_ID = 1


# ── Fixtures ───────────────────────────────────────────────────────


@pytest.fixture
def user_repo(db_conn: sqlite3.Connection) -> SQLiteUserRepository:
    return SQLiteUserRepository(db_conn)


class _FakeAuthMiddleware:
    """ASGI middleware that injects a test user for API tests."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            scope["user"] = AuthenticatedUser(
                user_id=_TEST_USER_ID,
                email="test@example.com",
                display_name="Test User",
                is_admin=True,
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


@pytest.fixture
def api_db_conn(tmp_path: Path) -> Generator[sqlite3.Connection]:
    """Migrated SQLite connection that works across threads (for TestClient)."""
    db_path = tmp_path / "test_prefs_api.db"
    conn = get_connection(db_path, check_same_thread=False)
    run_migrations(conn)
    yield conn
    conn.close()


@pytest.fixture
def api_user_repo(api_db_conn: sqlite3.Connection) -> SQLiteUserRepository:
    return SQLiteUserRepository(api_db_conn)


@pytest.fixture
def api_repo(api_db_conn: sqlite3.Connection) -> SQLiteEntryRepository:
    return SQLiteEntryRepository(api_db_conn)


@pytest.fixture
def services(
    api_repo: SQLiteEntryRepository,
    api_user_repo: SQLiteUserRepository,
) -> dict:
    mock_vector_store = MagicMock()
    mock_vector_store.delete_entry = MagicMock()
    mock_vector_store.add_entry = MagicMock()
    mock_embeddings = MagicMock()
    mock_embeddings.embed_texts = MagicMock(return_value=[[0.1] * 1024])
    mock_embeddings.embed_query = MagicMock(return_value=[0.1] * 1024)
    mock_ocr = MagicMock()
    mock_transcription = MagicMock()

    from journal.services.chunking import FixedTokenChunker

    ingestion = IngestionService(
        repository=api_repo,
        vector_store=mock_vector_store,
        ocr_provider=mock_ocr,
        transcription_provider=mock_transcription,
        embeddings_provider=mock_embeddings,
        chunker=FixedTokenChunker(max_tokens=150, overlap_tokens=40),
        preprocess_images=False,
    )
    query = QueryService(
        repository=api_repo,
        vector_store=mock_vector_store,
        embeddings_provider=mock_embeddings,
    )
    entity_store = SQLiteEntityStore(api_repo._conn)
    job_repository = SQLiteJobRepository(api_repo._conn)

    from journal.config import Config
    from journal.services.runtime_settings import RuntimeSettings

    config = Config()
    runtime = RuntimeSettings(api_repo._conn, config)

    return {
        "ingestion": ingestion,
        "query": query,
        "entity_store": entity_store,
        "job_repository": job_repository,
        "config": config,
        "runtime_settings": runtime,
        "user_repo": api_user_repo,
    }


@pytest.fixture
def client(services: dict) -> Generator[TestClient]:
    """Create a Starlette test client with the API routes registered."""
    from mcp.server.fastmcp import FastMCP

    from journal.api import register_api_routes

    test_mcp = FastMCP("test-journal-prefs")
    register_api_routes(test_mcp, lambda: services)

    app = _FakeAuthMiddleware(test_mcp.streamable_http_app())

    with TestClient(app, raise_server_exceptions=False) as tc:
        yield tc


# ═══════════════════════════════════════════════════════════════════
# Repository tests
# ═══════════════════════════════════════════════════════════════════


class TestGetPreferences:
    def test_empty_initially(self, user_repo: SQLiteUserRepository) -> None:
        user = user_repo.create_user("prefs@example.com", "Prefs")
        assert user_repo.get_preferences(user.id) == {}

    def test_returns_all_preferences(
        self, user_repo: SQLiteUserRepository
    ) -> None:
        user = user_repo.create_user("all@example.com", "All")
        user_repo.set_preference(user.id, "theme", "dark")
        user_repo.set_preference(user.id, "page_size", 25)
        prefs = user_repo.get_preferences(user.id)
        assert prefs == {"theme": "dark", "page_size": 25}

    def test_does_not_return_other_users_prefs(
        self, user_repo: SQLiteUserRepository
    ) -> None:
        u1 = user_repo.create_user("u1@example.com", "U1")
        u2 = user_repo.create_user("u2@example.com", "U2")
        user_repo.set_preference(u1.id, "theme", "dark")
        user_repo.set_preference(u2.id, "theme", "light")
        assert user_repo.get_preferences(u1.id) == {"theme": "dark"}
        assert user_repo.get_preferences(u2.id) == {"theme": "light"}


class TestGetPreference:
    def test_returns_value(self, user_repo: SQLiteUserRepository) -> None:
        user = user_repo.create_user("single@example.com", "Single")
        user_repo.set_preference(user.id, "lang", "en")
        assert user_repo.get_preference(user.id, "lang") == "en"

    def test_returns_none_for_missing_key(
        self, user_repo: SQLiteUserRepository
    ) -> None:
        user = user_repo.create_user("miss@example.com", "Miss")
        assert user_repo.get_preference(user.id, "nonexistent") is None

    def test_returns_none_for_missing_user(
        self, user_repo: SQLiteUserRepository
    ) -> None:
        assert user_repo.get_preference(99999, "anything") is None


class TestSetPreference:
    def test_insert_new(self, user_repo: SQLiteUserRepository) -> None:
        user = user_repo.create_user("new@example.com", "New")
        user_repo.set_preference(user.id, "color", "blue")
        assert user_repo.get_preference(user.id, "color") == "blue"

    def test_upsert_existing(self, user_repo: SQLiteUserRepository) -> None:
        user = user_repo.create_user("ups@example.com", "Ups")
        user_repo.set_preference(user.id, "color", "blue")
        user_repo.set_preference(user.id, "color", "red")
        assert user_repo.get_preference(user.id, "color") == "red"

    def test_json_string(self, user_repo: SQLiteUserRepository) -> None:
        user = user_repo.create_user("jstr@example.com", "JStr")
        user_repo.set_preference(user.id, "greeting", "hello world")
        assert user_repo.get_preference(user.id, "greeting") == "hello world"

    def test_json_integer(self, user_repo: SQLiteUserRepository) -> None:
        user = user_repo.create_user("jint@example.com", "JInt")
        user_repo.set_preference(user.id, "count", 42)
        assert user_repo.get_preference(user.id, "count") == 42

    def test_json_float(self, user_repo: SQLiteUserRepository) -> None:
        user = user_repo.create_user("jflt@example.com", "JFlt")
        user_repo.set_preference(user.id, "ratio", 3.14)
        result = user_repo.get_preference(user.id, "ratio")
        assert abs(result - 3.14) < 1e-9

    def test_json_boolean(self, user_repo: SQLiteUserRepository) -> None:
        user = user_repo.create_user("jbool@example.com", "JBool")
        user_repo.set_preference(user.id, "enabled", True)
        assert user_repo.get_preference(user.id, "enabled") is True
        user_repo.set_preference(user.id, "disabled", False)
        assert user_repo.get_preference(user.id, "disabled") is False

    def test_json_null(self, user_repo: SQLiteUserRepository) -> None:
        user = user_repo.create_user("jnull@example.com", "JNull")
        user_repo.set_preference(user.id, "empty", None)
        # None round-trips via JSON null
        assert user_repo.get_preference(user.id, "empty") is None
        # But the key DOES exist — get_preferences should include it
        prefs = user_repo.get_preferences(user.id)
        assert "empty" in prefs
        assert prefs["empty"] is None

    def test_json_list(self, user_repo: SQLiteUserRepository) -> None:
        user = user_repo.create_user("jlist@example.com", "JList")
        user_repo.set_preference(user.id, "tags", ["a", "b", "c"])
        assert user_repo.get_preference(user.id, "tags") == ["a", "b", "c"]

    def test_json_nested_dict(self, user_repo: SQLiteUserRepository) -> None:
        user = user_repo.create_user("jdict@example.com", "JDict")
        value = {"layout": {"cols": 2, "rows": 3}, "filters": ["mood", "date"]}
        user_repo.set_preference(user.id, "dashboard", value)
        assert user_repo.get_preference(user.id, "dashboard") == value


class TestDeletePreference:
    def test_delete_existing(self, user_repo: SQLiteUserRepository) -> None:
        user = user_repo.create_user("del@example.com", "Del")
        user_repo.set_preference(user.id, "theme", "dark")
        assert user_repo.delete_preference(user.id, "theme") is True
        assert user_repo.get_preference(user.id, "theme") is None

    def test_delete_nonexistent_returns_false(
        self, user_repo: SQLiteUserRepository
    ) -> None:
        user = user_repo.create_user("delnone@example.com", "DelNone")
        assert user_repo.delete_preference(user.id, "nope") is False

    def test_delete_does_not_affect_other_keys(
        self, user_repo: SQLiteUserRepository
    ) -> None:
        user = user_repo.create_user("delother@example.com", "DelOther")
        user_repo.set_preference(user.id, "a", 1)
        user_repo.set_preference(user.id, "b", 2)
        user_repo.delete_preference(user.id, "a")
        assert user_repo.get_preference(user.id, "a") is None
        assert user_repo.get_preference(user.id, "b") == 2

    def test_delete_does_not_affect_other_users(
        self, user_repo: SQLiteUserRepository
    ) -> None:
        u1 = user_repo.create_user("delu1@example.com", "DelU1")
        u2 = user_repo.create_user("delu2@example.com", "DelU2")
        user_repo.set_preference(u1.id, "theme", "dark")
        user_repo.set_preference(u2.id, "theme", "light")
        user_repo.delete_preference(u1.id, "theme")
        assert user_repo.get_preference(u1.id, "theme") is None
        assert user_repo.get_preference(u2.id, "theme") == "light"


# ═══════════════════════════════════════════════════════════════════
# API endpoint tests
# ═══════════════════════════════════════════════════════════════════


class TestGetPreferencesAPI:
    def test_returns_empty_initially(self, client: TestClient) -> None:
        resp = client.get("/api/users/me/preferences")
        assert resp.status_code == 200
        assert resp.json() == {"preferences": {}}

    def test_returns_saved_preferences(self, client: TestClient) -> None:
        # Save some preferences first
        client.patch(
            "/api/users/me/preferences",
            json={"theme": "dark", "page_size": 25},
        )
        resp = client.get("/api/users/me/preferences")
        assert resp.status_code == 200
        data = resp.json()
        assert data["preferences"]["theme"] == "dark"
        assert data["preferences"]["page_size"] == 25


class TestPatchPreferencesAPI:
    def test_saves_and_returns_preferences(self, client: TestClient) -> None:
        resp = client.patch(
            "/api/users/me/preferences",
            json={"lang": "en", "notifications": True},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["preferences"]["lang"] == "en"
        assert data["preferences"]["notifications"] is True

    def test_upserts_existing_keys(self, client: TestClient) -> None:
        client.patch("/api/users/me/preferences", json={"color": "blue"})
        resp = client.patch("/api/users/me/preferences", json={"color": "red"})
        assert resp.status_code == 200
        assert resp.json()["preferences"]["color"] == "red"

    def test_preserves_other_keys(self, client: TestClient) -> None:
        client.patch("/api/users/me/preferences", json={"a": 1, "b": 2})
        resp = client.patch("/api/users/me/preferences", json={"b": 99})
        assert resp.status_code == 200
        prefs = resp.json()["preferences"]
        assert prefs["a"] == 1
        assert prefs["b"] == 99

    def test_complex_json_values(self, client: TestClient) -> None:
        body = {
            "layout": {"cols": 2, "rows": 3},
            "tags": ["mood", "date", "weather"],
            "count": 42,
            "active": True,
        }
        resp = client.patch("/api/users/me/preferences", json=body)
        assert resp.status_code == 200
        prefs = resp.json()["preferences"]
        assert prefs["layout"] == {"cols": 2, "rows": 3}
        assert prefs["tags"] == ["mood", "date", "weather"]
        assert prefs["count"] == 42
        assert prefs["active"] is True

    def test_invalid_json_returns_400(self, client: TestClient) -> None:
        resp = client.patch(
            "/api/users/me/preferences",
            content=b"not-json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400
        assert "error" in resp.json()

    def test_non_object_body_returns_400(self, client: TestClient) -> None:
        resp = client.patch("/api/users/me/preferences", json=["a", "b"])
        assert resp.status_code == 400
        assert "error" in resp.json()

    def test_empty_object_returns_current_preferences(
        self, client: TestClient
    ) -> None:
        # Seed a preference first
        client.patch("/api/users/me/preferences", json={"existing": "value"})
        resp = client.patch("/api/users/me/preferences", json={})
        assert resp.status_code == 200
        assert resp.json()["preferences"]["existing"] == "value"
