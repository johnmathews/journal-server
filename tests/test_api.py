"""Tests for REST API endpoints."""

import sqlite3
from collections.abc import Generator
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from starlette.testclient import TestClient

from journal.auth import AuthenticatedUser, _current_user_id
from journal.db.connection import get_connection
from journal.db.migrations import run_migrations
from journal.db.repository import SQLiteEntryRepository
from journal.entitystore.store import SQLiteEntityStore
from journal.services.ingestion import IngestionService
from journal.services.query import QueryService

_TEST_USER_ID = 1


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
    """Provide a migrated SQLite connection that works across threads.

    The Starlette TestClient runs the ASGI app in a separate thread,
    so we need check_same_thread=False. Uses get_connection() to
    mirror production PRAGMAs (especially busy_timeout).
    """
    db_path = tmp_path / "test_api.db"
    conn = get_connection(db_path, check_same_thread=False)
    run_migrations(conn)
    yield conn
    conn.close()


@pytest.fixture
def repo(api_db_conn: sqlite3.Connection) -> SQLiteEntryRepository:
    return SQLiteEntryRepository(api_db_conn)


@pytest.fixture
def mock_vector_store() -> MagicMock:
    store = MagicMock()
    store.delete_entry = MagicMock()
    store.add_entry = MagicMock()
    return store


@pytest.fixture
def mock_embeddings() -> MagicMock:
    provider = MagicMock()
    provider.embed_texts = MagicMock(return_value=[[0.1] * 1024])
    provider.embed_query = MagicMock(return_value=[0.1] * 1024)
    return provider


@pytest.fixture
def services(
    repo: SQLiteEntryRepository,
    mock_vector_store: MagicMock,
    mock_embeddings: MagicMock,
) -> dict:
    mock_ocr = MagicMock()
    mock_transcription = MagicMock()

    from journal.services.chunking import FixedTokenChunker
    ingestion = IngestionService(
        repository=repo,
        vector_store=mock_vector_store,
        ocr_provider=mock_ocr,
        transcription_provider=mock_transcription,
        embeddings_provider=mock_embeddings,
        chunker=FixedTokenChunker(max_tokens=150, overlap_tokens=40),
    )
    query = QueryService(
        repository=repo,
        vector_store=mock_vector_store,
        embeddings_provider=mock_embeddings,
    )
    entity_store = SQLiteEntityStore(repo._conn)

    from journal.config import Config
    config = Config()

    return {
        "ingestion": ingestion,
        "query": query,
        "entity_store": entity_store,
        "config": config,
    }


@pytest.fixture
def client(services: dict) -> Generator[TestClient]:
    """Create a Starlette test client with the API routes registered."""
    from mcp.server.fastmcp import FastMCP

    from journal.api import register_api_routes

    # Create a minimal FastMCP instance for testing
    test_mcp = FastMCP("test-journal")
    register_api_routes(test_mcp, lambda: services)

    # Build the Starlette app
    app = _FakeAuthMiddleware(test_mcp.streamable_http_app())

    with TestClient(app, raise_server_exceptions=False) as tc:
        yield tc


def _seed_entries(repo: SQLiteEntryRepository, count: int = 5) -> list[int]:
    """Create test entries and return their IDs."""
    ids = []
    for i in range(count):
        entry = repo.create_entry(
            f"2026-03-{i + 1:02d}",
            "photo" if i % 2 == 0 else "voice",
            f"This is entry number {i + 1} with some words to count.",
            10,
        )
        ids.append(entry.id)
    return ids


class TestListEntries:
    def test_list_entries_returns_paginated_list(
        self, client: TestClient, repo: SQLiteEntryRepository
    ) -> None:
        _seed_entries(repo, 5)
        response = client.get("/api/entries")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 5
        assert data["limit"] == 20
        assert data["offset"] == 0
        assert len(data["items"]) == 5

    def test_list_entries_pagination(
        self, client: TestClient, repo: SQLiteEntryRepository
    ) -> None:
        _seed_entries(repo, 10)
        response = client.get("/api/entries?limit=3&offset=2")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 10
        assert data["limit"] == 3
        assert data["offset"] == 2
        assert len(data["items"]) == 3

    def test_list_entries_max_limit_capped(
        self, client: TestClient, repo: SQLiteEntryRepository
    ) -> None:
        _seed_entries(repo, 5)
        response = client.get("/api/entries?limit=200")
        assert response.status_code == 200
        data = response.json()
        assert data["limit"] == 100  # capped

    def test_list_entries_with_date_filters(
        self, client: TestClient, repo: SQLiteEntryRepository
    ) -> None:
        repo.create_entry("2026-01-15", "photo", "January entry", 2)
        repo.create_entry("2026-03-15", "photo", "March entry", 2)
        repo.create_entry("2026-05-15", "photo", "May entry", 2)

        response = client.get(
            "/api/entries?start_date=2026-02-01&end_date=2026-04-01"
        )
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert len(data["items"]) == 1
        assert data["items"][0]["entry_date"] == "2026-03-15"

    def test_list_entries_empty(self, client: TestClient) -> None:
        response = client.get("/api/entries")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 0
        assert data["items"] == []

    def test_list_entries_item_fields(
        self, client: TestClient, repo: SQLiteEntryRepository
    ) -> None:
        _seed_entries(repo, 1)
        response = client.get("/api/entries")
        item = response.json()["items"][0]
        # Summary should NOT include text fields
        assert "id" in item
        assert "entry_date" in item
        assert "source_type" in item
        assert "word_count" in item
        assert "chunk_count" in item
        assert "page_count" in item
        assert "created_at" in item
        assert "raw_text" not in item
        assert "final_text" not in item

    def test_list_entries_includes_page_count(
        self, client: TestClient, repo: SQLiteEntryRepository
    ) -> None:
        entry = repo.create_entry("2026-03-22", "photo", "Combined text", 2)
        repo.add_entry_page(entry.id, 1, "Page one text")
        repo.add_entry_page(entry.id, 2, "Page two text")

        response = client.get("/api/entries")
        item = response.json()["items"][0]
        assert item["page_count"] == 2


class TestGetEntry:
    def test_get_entry_returns_detail(
        self, client: TestClient, repo: SQLiteEntryRepository
    ) -> None:
        entry = repo.create_entry("2026-03-22", "photo", "Hello world", 2)
        response = client.get(f"/api/entries/{entry.id}")
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == entry.id
        assert data["entry_date"] == "2026-03-22"
        assert data["source_type"] == "photo"
        assert data["raw_text"] == "Hello world"
        assert data["final_text"] == "Hello world"
        assert data["word_count"] == 2
        assert data["language"] == "en"
        assert "created_at" in data
        assert "updated_at" in data
        assert "page_count" in data

    def test_get_entry_not_found(self, client: TestClient) -> None:
        response = client.get("/api/entries/999")
        assert response.status_code == 404
        assert "not found" in response.json()["error"].lower()

    def test_get_entry_with_pages(
        self, client: TestClient, repo: SQLiteEntryRepository
    ) -> None:
        entry = repo.create_entry("2026-03-22", "photo", "Combined text", 2)
        repo.add_entry_page(entry.id, 1, "Page one")
        repo.add_entry_page(entry.id, 2, "Page two")

        response = client.get(f"/api/entries/{entry.id}")
        data = response.json()
        assert data["page_count"] == 2

    def test_get_entry_includes_empty_uncertain_spans_by_default(
        self, client: TestClient, repo: SQLiteEntryRepository
    ) -> None:
        entry = repo.create_entry("2026-03-22", "photo", "Hello world", 2)
        response = client.get(f"/api/entries/{entry.id}")
        data = response.json()
        # The field is always present, even for entries with no spans —
        # this keeps the webapp's type contract clean (no branching on
        # "spans missing" vs "spans empty").
        assert "uncertain_spans" in data
        assert data["uncertain_spans"] == []

    def test_get_entry_returns_uncertain_spans_when_present(
        self, client: TestClient, repo: SQLiteEntryRepository
    ) -> None:
        entry = repo.create_entry(
            "2026-03-22", "photo", "Hello Ritsya from Vienna.", 4
        )
        repo.add_uncertain_spans(entry.id, [(6, 12), (18, 24)])
        response = client.get(f"/api/entries/{entry.id}")
        data = response.json()
        assert data["uncertain_spans"] == [
            {"char_start": 6, "char_end": 12},
            {"char_start": 18, "char_end": 24},
        ]
        # Sanity-check that the offsets land on the right words in raw_text.
        assert data["raw_text"][6:12] == "Ritsya"
        assert data["raw_text"][18:24] == "Vienna"

    def test_list_entries_does_not_include_uncertain_spans(
        self, client: TestClient, repo: SQLiteEntryRepository
    ) -> None:
        """The list endpoint serves a summary shape — uncertain_spans
        belong on the detail endpoint only, so we don't pay the extra
        query per row."""
        entry = repo.create_entry(
            "2026-03-22", "photo", "Hello Ritsya", 2
        )
        repo.add_uncertain_spans(entry.id, [(6, 12)])
        response = client.get("/api/entries")
        item = response.json()["items"][0]
        assert entry.id == item["id"]
        assert "uncertain_spans" not in item


class TestUpdateEntry:
    def test_patch_entry_updates_final_text(
        self, client: TestClient, repo: SQLiteEntryRepository
    ) -> None:
        entry = repo.create_entry("2026-03-22", "photo", "raw OCR output", 3)
        response = client.patch(
            f"/api/entries/{entry.id}",
            json={"final_text": "corrected text"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["final_text"] == "corrected text"
        assert data["raw_text"] == "raw OCR output"  # unchanged
        assert data["word_count"] == 2  # re-counted

    def test_patch_entry_preserves_uncertain_spans(
        self, client: TestClient, repo: SQLiteEntryRepository
    ) -> None:
        """Uncertainty is anchored to raw_text, which PATCH never
        touches — so the PATCH response must still carry the spans
        the entry was ingested with."""
        entry = repo.create_entry("2026-03-22", "photo", "Hello Ritsya.", 2)
        repo.add_uncertain_spans(entry.id, [(6, 12)])

        response = client.patch(
            f"/api/entries/{entry.id}",
            json={"final_text": "Completely different corrected text."},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["uncertain_spans"] == [{"char_start": 6, "char_end": 12}]
        # Raw text still carries the original letters at that offset.
        assert data["raw_text"][6:12] == "Ritsya"

    def test_patch_entry_not_found(self, client: TestClient) -> None:
        response = client.patch(
            "/api/entries/999",
            json={"final_text": "corrected text"},
        )
        assert response.status_code == 404

    def test_patch_entry_empty_body(self, client: TestClient, repo: SQLiteEntryRepository) -> None:
        entry = repo.create_entry("2026-03-22", "photo", "Hello", 1)
        response = client.patch(
            f"/api/entries/{entry.id}",
            content=b"",
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 400

    def test_patch_entry_missing_both_fields(
        self, client: TestClient, repo: SQLiteEntryRepository
    ) -> None:
        entry = repo.create_entry("2026-03-22", "photo", "Hello", 1)
        response = client.patch(
            f"/api/entries/{entry.id}",
            json={"other_field": "value"},
        )
        assert response.status_code == 400
        assert "final_text" in response.json()["error"] or "entry_date" in response.json()["error"]

    def test_patch_entry_date_only(
        self, client: TestClient, repo: SQLiteEntryRepository
    ) -> None:
        entry = repo.create_entry("2026-03-22", "photo", "Hello world", 2)
        response = client.patch(
            f"/api/entries/{entry.id}",
            json={"entry_date": "2026-02-17"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["entry_date"] == "2026-02-17"
        assert data["raw_text"] == "Hello world"  # unchanged

    def test_patch_entry_date_invalid_format(
        self, client: TestClient, repo: SQLiteEntryRepository
    ) -> None:
        entry = repo.create_entry("2026-03-22", "photo", "Hello", 1)
        response = client.patch(
            f"/api/entries/{entry.id}",
            json={"entry_date": "not-a-date"},
        )
        assert response.status_code == 400
        assert "ISO 8601" in response.json()["error"]

    def test_patch_entry_date_and_text(
        self, client: TestClient, repo: SQLiteEntryRepository
    ) -> None:
        entry = repo.create_entry("2026-03-22", "photo", "raw text", 2)
        response = client.patch(
            f"/api/entries/{entry.id}",
            json={"entry_date": "2026-01-01", "final_text": "corrected"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["entry_date"] == "2026-01-01"
        assert data["final_text"] == "corrected"

    def test_patch_entry_empty_final_text(
        self, client: TestClient, repo: SQLiteEntryRepository
    ) -> None:
        entry = repo.create_entry("2026-03-22", "photo", "Hello", 1)
        response = client.patch(
            f"/api/entries/{entry.id}",
            json={"final_text": "  "},
        )
        assert response.status_code == 400
        assert "empty" in response.json()["error"].lower()

    def test_patch_entry_non_string_final_text(
        self, client: TestClient, repo: SQLiteEntryRepository
    ) -> None:
        entry = repo.create_entry("2026-03-22", "photo", "Hello", 1)
        response = client.patch(
            f"/api/entries/{entry.id}",
            json={"final_text": 123},
        )
        assert response.status_code == 400

    def test_patch_text_succeeds_without_job_runner(
        self, client: TestClient, repo: SQLiteEntryRepository
    ) -> None:
        """Entity re-extraction is best-effort — PATCH must succeed even
        when the services dict has no job_runner (e.g. in test setups)."""
        entry = repo.create_entry("2026-03-22", "photo", "raw text", 2)
        response = client.patch(
            f"/api/entries/{entry.id}",
            json={"final_text": "corrected text"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["final_text"] == "corrected text"
        # No job_runner → no extraction job id in response
        assert "entity_extraction_job_id" not in data

    def test_patch_text_queues_entity_extraction(
        self,
        client: TestClient,
        repo: SQLiteEntryRepository,
        services: dict,
    ) -> None:
        """When a job_runner is present, PATCH text should fire an async
        entity re-extraction job and include the job id in the response."""
        mock_job = MagicMock()
        mock_job.id = "test-job-123"
        mock_runner = MagicMock()
        mock_runner.submit_entity_extraction = MagicMock(return_value=mock_job)
        mock_runner.submit_reprocess_embeddings = MagicMock(return_value=mock_job)
        mock_runner.submit_mood_score_entry = MagicMock(return_value=mock_job)
        services["job_runner"] = mock_runner

        entry = repo.create_entry("2026-03-22", "photo", "raw text", 2)

        response = client.patch(
            f"/api/entries/{entry.id}",
            json={"final_text": "corrected text"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["entity_extraction_job_id"] == "test-job-123"
        mock_runner.submit_entity_extraction.assert_called_once_with(
            {"entry_id": entry.id}
        )

    def test_patch_date_only_does_not_queue_extraction(
        self,
        client: TestClient,
        repo: SQLiteEntryRepository,
        services: dict,
    ) -> None:
        """Changing only entry_date should not trigger entity extraction."""
        mock_runner = MagicMock()
        services["job_runner"] = mock_runner

        entry = repo.create_entry("2026-03-22", "photo", "Hello world", 2)

        response = client.patch(
            f"/api/entries/{entry.id}",
            json={"entry_date": "2026-01-01"},
        )

        assert response.status_code == 200
        assert "entity_extraction_job_id" not in response.json()
        mock_runner.submit_entity_extraction.assert_not_called()


class TestVerifyDoubts:
    def test_verify_doubts_clears_spans_in_response(
        self, client: TestClient, repo: SQLiteEntryRepository
    ) -> None:
        entry = repo.create_entry("2026-03-22", "photo", "Hello Ritsya.", 2)
        repo.add_uncertain_spans(entry.id, [(6, 12)])
        response = client.post(f"/api/entries/{entry.id}/verify-doubts")
        assert response.status_code == 200
        data = response.json()
        assert data["doubts_verified"] is True
        assert data["uncertain_spans"] == []

    def test_verify_doubts_not_found(self, client: TestClient) -> None:
        response = client.post("/api/entries/999/verify-doubts")
        assert response.status_code == 404

    def test_verify_doubts_persists_across_get(
        self, client: TestClient, repo: SQLiteEntryRepository
    ) -> None:
        entry = repo.create_entry("2026-03-22", "photo", "Hello Ritsya.", 2)
        repo.add_uncertain_spans(entry.id, [(6, 12)])
        client.post(f"/api/entries/{entry.id}/verify-doubts")

        # GET detail should now show verified + empty spans
        response = client.get(f"/api/entries/{entry.id}")
        data = response.json()
        assert data["doubts_verified"] is True
        assert data["uncertain_spans"] == []

    def test_verify_doubts_zeroes_list_count(
        self, client: TestClient, repo: SQLiteEntryRepository
    ) -> None:
        entry = repo.create_entry("2026-03-22", "photo", "Hello Ritsya.", 2)
        repo.add_uncertain_spans(entry.id, [(6, 12)])

        # Before: count is 1
        response = client.get("/api/entries")
        item = [i for i in response.json()["items"] if i["id"] == entry.id][0]
        assert item["uncertain_span_count"] == 1

        client.post(f"/api/entries/{entry.id}/verify-doubts")

        # After: count is 0
        response = client.get("/api/entries")
        item = [i for i in response.json()["items"] if i["id"] == entry.id][0]
        assert item["uncertain_span_count"] == 0
        assert item["doubts_verified"] is True


class TestDeleteEntry:
    def test_delete_entry_removes_row(
        self,
        client: TestClient,
        repo: SQLiteEntryRepository,
        mock_vector_store: MagicMock,
    ) -> None:
        entry = repo.create_entry("2026-03-22", "photo", "Hello", 1)
        response = client.delete(f"/api/entries/{entry.id}")
        assert response.status_code == 200
        data = response.json()
        assert data == {"deleted": True, "id": entry.id}
        assert repo.get_entry(entry.id) is None
        mock_vector_store.delete_entry.assert_called_once_with(entry.id)

    def test_delete_entry_not_found(
        self, client: TestClient, mock_vector_store: MagicMock
    ) -> None:
        response = client.delete("/api/entries/999")
        assert response.status_code == 404
        assert "not found" in response.json()["error"].lower()
        mock_vector_store.delete_entry.assert_not_called()

    def test_delete_entry_cascades_pages(
        self, client: TestClient, repo: SQLiteEntryRepository
    ) -> None:
        entry = repo.create_entry("2026-03-22", "photo", "Combined", 1)
        repo.add_entry_page(entry.id, 1, "Page one")
        repo.add_entry_page(entry.id, 2, "Page two")

        response = client.delete(f"/api/entries/{entry.id}")
        assert response.status_code == 200
        assert repo.get_entry_pages(entry.id) == []

    def test_delete_entry_removes_from_list(
        self, client: TestClient, repo: SQLiteEntryRepository
    ) -> None:
        ids = _seed_entries(repo, 3)
        response = client.delete(f"/api/entries/{ids[0]}")
        assert response.status_code == 200

        list_response = client.get("/api/entries")
        data = list_response.json()
        assert data["total"] == 2
        assert ids[0] not in [item["id"] for item in data["items"]]


class TestGetEntryChunks:
    def test_returns_chunks_for_entry_with_chunks(
        self, client: TestClient, repo: SQLiteEntryRepository
    ) -> None:
        from journal.models import ChunkSpan
        entry = repo.create_entry("2026-03-22", "photo", "Entry text", 2)
        repo.replace_chunks(
            entry.id,
            [
                ChunkSpan(text="First chunk.", char_start=0, char_end=12, token_count=3),
                ChunkSpan(text="Second chunk.", char_start=14, char_end=27, token_count=3),
            ],
        )
        response = client.get(f"/api/entries/{entry.id}/chunks")
        assert response.status_code == 200
        data = response.json()
        assert data["entry_id"] == entry.id
        assert len(data["chunks"]) == 2
        assert data["chunks"][0] == {
            "index": 0,
            "text": "First chunk.",
            "char_start": 0,
            "char_end": 12,
            "token_count": 3,
        }
        assert data["chunks"][1]["index"] == 1

    def test_returns_404_chunks_not_backfilled(
        self, client: TestClient, repo: SQLiteEntryRepository
    ) -> None:
        entry = repo.create_entry("2026-03-22", "photo", "Unchunked entry", 2)
        response = client.get(f"/api/entries/{entry.id}/chunks")
        assert response.status_code == 404
        data = response.json()
        assert data["error"] == "chunks_not_backfilled"
        assert "backfill" in data["message"].lower()

    def test_returns_404_entry_not_found(self, client: TestClient) -> None:
        response = client.get("/api/entries/99999/chunks")
        assert response.status_code == 404
        data = response.json()
        assert data["error"] == "entry_not_found"


class TestGetEntryTokens:
    def test_returns_tokens_for_entry(
        self, client: TestClient, repo: SQLiteEntryRepository
    ) -> None:
        entry = repo.create_entry(
            "2026-03-22", "photo", "Hello world this is a test.", 6,
        )
        response = client.get(f"/api/entries/{entry.id}/tokens")
        assert response.status_code == 200
        data = response.json()
        assert data["entry_id"] == entry.id
        assert data["encoding"] == "cl100k_base"
        assert data["model_hint"] == "text-embedding-3-large"
        assert data["token_count"] == len(data["tokens"])
        assert data["token_count"] > 0
        # First token starts at position 0.
        assert data["tokens"][0]["char_start"] == 0
        # Every token has consistent fields.
        for tok in data["tokens"]:
            assert {"index", "token_id", "text", "char_start", "char_end"} <= tok.keys()
            assert tok["char_start"] <= tok["char_end"]

    def test_offsets_reconstruct_original_text(
        self, client: TestClient, repo: SQLiteEntryRepository
    ) -> None:
        """Concatenating each token's text by char_start must equal final_text."""
        text = "The quick brown fox jumps over the lazy dog."
        entry = repo.create_entry("2026-03-22", "photo", text, 9)
        response = client.get(f"/api/entries/{entry.id}/tokens")
        data = response.json()
        # Slicing by offsets reconstructs the original text.
        reconstructed = "".join(
            text[t["char_start"] : t["char_end"]] for t in data["tokens"]
        )
        assert reconstructed == text

    def test_unicode_text_reconstructs_correctly(
        self, client: TestClient, repo: SQLiteEntryRepository
    ) -> None:
        text = "Café résumé — naïve façade."
        entry = repo.create_entry("2026-03-22", "photo", text, 4)
        response = client.get(f"/api/entries/{entry.id}/tokens")
        data = response.json()
        reconstructed = "".join(
            t["text"] for t in data["tokens"]
        )
        assert reconstructed == text

    def test_returns_404_entry_not_found(self, client: TestClient) -> None:
        response = client.get("/api/entries/99999/tokens")
        assert response.status_code == 404
        data = response.json()
        assert data["error"] == "entry_not_found"

    def test_uses_final_text_when_different_from_raw(
        self, client: TestClient, repo: SQLiteEntryRepository
    ) -> None:
        raw = "raw has a typo"
        entry = repo.create_entry("2026-03-22", "photo", raw, 4)
        # Simulate a correction by updating final_text directly on the row.
        repo._conn.execute(
            "UPDATE entries SET final_text = ? WHERE id = ?",
            ("corrected text without any typo", entry.id),
        )
        repo._conn.commit()
        response = client.get(f"/api/entries/{entry.id}/tokens")
        data = response.json()
        reconstructed = "".join(t["text"] for t in data["tokens"])
        assert reconstructed == "corrected text without any typo"


class TestGetStats:
    def test_get_stats(
        self, client: TestClient, repo: SQLiteEntryRepository
    ) -> None:
        repo.create_entry("2026-01-15", "photo", "January entry words here", 4)
        repo.create_entry("2026-02-15", "photo", "February entry", 2)
        repo.create_entry("2026-03-15", "voice", "March entry", 2)

        response = client.get("/api/stats")
        assert response.status_code == 200
        data = response.json()
        assert data["total_entries"] == 3
        assert data["total_words"] == 8
        assert data["date_range_start"] == "2026-01-15"
        assert data["date_range_end"] == "2026-03-15"
        assert "avg_words_per_entry" in data
        assert "entries_per_month" in data

    def test_get_stats_with_date_filter(
        self, client: TestClient, repo: SQLiteEntryRepository
    ) -> None:
        repo.create_entry("2026-01-15", "photo", "Old entry", 2)
        repo.create_entry("2026-03-15", "photo", "New entry", 2)

        response = client.get("/api/stats?start_date=2026-03-01")
        assert response.status_code == 200
        data = response.json()
        assert data["total_entries"] == 1

    def test_get_stats_empty(self, client: TestClient) -> None:
        response = client.get("/api/stats")
        assert response.status_code == 200
        data = response.json()
        assert data["total_entries"] == 0
        assert data["total_words"] == 0


class TestSearch:
    """T1.4.c — GET /api/search endpoint."""

    @pytest.fixture
    def search_client(
        self, repo: SQLiteEntryRepository, mock_embeddings: MagicMock
    ) -> Generator[tuple[TestClient, object]]:
        """Test client that uses a real InMemoryVectorStore so the
        semantic path actually returns results. Yields (client, vector_store)
        so tests can pre-seed the vector store directly."""
        from mcp.server.fastmcp import FastMCP

        from journal.api import register_api_routes
        from journal.services.chunking import FixedTokenChunker
        from journal.vectorstore.store import InMemoryVectorStore

        real_vector_store = InMemoryVectorStore()
        mock_ocr = MagicMock()
        mock_transcription = MagicMock()
        ingestion = IngestionService(
            repository=repo,
            vector_store=real_vector_store,
            ocr_provider=mock_ocr,
            transcription_provider=mock_transcription,
            embeddings_provider=mock_embeddings,
            chunker=FixedTokenChunker(max_tokens=150, overlap_tokens=40),
        )
        query = QueryService(
            repository=repo,
            vector_store=real_vector_store,
            embeddings_provider=mock_embeddings,
        )
        services = {"ingestion": ingestion, "query": query}

        test_mcp = FastMCP("test-journal")
        register_api_routes(test_mcp, lambda: services)
        app = _FakeAuthMiddleware(test_mcp.streamable_http_app())
        with TestClient(app, raise_server_exceptions=False) as tc:
            yield tc, real_vector_store

    def test_search_missing_query(self, client: TestClient) -> None:
        response = client.get("/api/search")
        assert response.status_code == 400
        assert response.json()["error"] == "missing_query"

    def test_search_empty_query(self, client: TestClient) -> None:
        response = client.get("/api/search?q=%20%20")
        assert response.status_code == 400
        assert response.json()["error"] == "missing_query"

    def test_search_invalid_mode(self, client: TestClient) -> None:
        response = client.get("/api/search?q=vienna&mode=fuzzy")
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_mode"

    def test_search_keyword_returns_snippet(
        self, client: TestClient, repo: SQLiteEntryRepository
    ) -> None:
        repo.create_entry(
            "2026-03-22",
            "photo",
            "Walked through Vienna with Atlas today.",
            7,
        )
        repo.create_entry(
            "2026-03-23", "voice", "Stayed home and read", 4
        )
        response = client.get("/api/search?q=Vienna&mode=keyword")
        assert response.status_code == 200
        data = response.json()
        assert data["mode"] == "keyword"
        assert data["query"] == "Vienna"
        assert len(data["items"]) == 1
        item = data["items"][0]
        assert item["entry_date"] == "2026-03-22"
        assert item["matching_chunks"] == []
        assert item["snippet"] is not None
        assert "\u0002" in item["snippet"]
        assert "\u0003" in item["snippet"]

    def test_search_keyword_date_filter(
        self, client: TestClient, repo: SQLiteEntryRepository
    ) -> None:
        repo.create_entry("2026-01-15", "photo", "Vienna in January", 3)
        repo.create_entry("2026-03-15", "photo", "Vienna in March", 3)
        response = client.get(
            "/api/search?q=Vienna&mode=keyword&start_date=2026-03-01"
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data["items"]) == 1
        assert data["items"][0]["entry_date"] == "2026-03-15"

    def test_search_keyword_pagination(
        self, client: TestClient, repo: SQLiteEntryRepository
    ) -> None:
        for i in range(5):
            repo.create_entry(
                f"2026-03-{10 + i:02d}",
                "photo",
                f"Entry {i} mentions Atlas directly.",
                5,
            )
        page_one = client.get(
            "/api/search?q=Atlas&mode=keyword&limit=2&offset=0"
        ).json()
        page_two = client.get(
            "/api/search?q=Atlas&mode=keyword&limit=2&offset=2"
        ).json()
        assert len(page_one["items"]) == 2
        assert len(page_two["items"]) == 2
        ids_one = {i["entry_id"] for i in page_one["items"]}
        ids_two = {i["entry_id"] for i in page_two["items"]}
        assert ids_one.isdisjoint(ids_two)
        assert page_one["offset"] == 0
        assert page_one["limit"] == 2
        assert page_two["offset"] == 2

    def test_search_keyword_no_results(self, client: TestClient) -> None:
        response = client.get("/api/search?q=unicorn&mode=keyword")
        assert response.status_code == 200
        assert response.json()["items"] == []

    def test_search_keyword_malformed_query_returns_400(
        self, client: TestClient, repo: SQLiteEntryRepository
    ) -> None:
        """FTS5 parse errors (unterminated quote, bare operators) must
        surface as a 400, not a 500."""
        repo.create_entry("2026-03-22", "photo", "Anything at all", 3)
        response = client.get('/api/search?q="&mode=keyword')
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_query"

    def test_search_limit_clamped(
        self, client: TestClient, repo: SQLiteEntryRepository
    ) -> None:
        repo.create_entry("2026-03-22", "photo", "Vienna entry", 2)
        # limit=500 should be clamped to 50; bad ints should fall back to 10.
        response = client.get("/api/search?q=Vienna&mode=keyword&limit=500")
        assert response.status_code == 200
        assert response.json()["limit"] == 50

        response = client.get("/api/search?q=Vienna&mode=keyword&limit=notanint")
        assert response.status_code == 200
        assert response.json()["limit"] == 10

    def test_search_semantic_returns_chunk_offsets(
        self,
        search_client: tuple[TestClient, object],
        repo: SQLiteEntryRepository,
        mock_embeddings: MagicMock,
    ) -> None:
        """Semantic search returns matching_chunks with char offsets
        filled in from entry_chunks."""
        from journal.models import ChunkSpan

        client, vector_store = search_client

        entry_text = "Walked through Vienna with Atlas today."
        entry = repo.create_entry("2026-03-22", "photo", entry_text, 7)
        repo.replace_chunks(
            entry.id,
            [
                ChunkSpan(
                    text=entry_text,
                    char_start=0,
                    char_end=len(entry_text),
                    token_count=8,
                )
            ],
        )
        # Real InMemoryVectorStore — stores chunk_index on metadata.
        vector_store.add_entry(  # type: ignore[attr-defined]
            entry_id=entry.id,
            chunks=[entry_text],
            embeddings=[[0.1] * 1024],
            metadata={"entry_date": "2026-03-22", "user_id": _TEST_USER_ID},
        )

        response = client.get("/api/search?q=vienna&mode=semantic")
        assert response.status_code == 200
        data = response.json()
        assert data["mode"] == "semantic"
        assert len(data["items"]) == 1
        item = data["items"][0]
        assert item["snippet"] is None
        assert len(item["matching_chunks"]) == 1
        chunk = item["matching_chunks"][0]
        assert chunk["chunk_index"] == 0
        assert chunk["char_start"] == 0
        assert chunk["char_end"] == len(entry_text)

    def test_search_default_mode_is_semantic(
        self,
        search_client: tuple[TestClient, object],
        repo: SQLiteEntryRepository,
        mock_embeddings: MagicMock,
    ) -> None:
        client, vector_store = search_client
        repo.create_entry("2026-03-22", "photo", "Vienna trip", 2)
        vector_store.add_entry(  # type: ignore[attr-defined]
            entry_id=1,
            chunks=["Vienna trip"],
            embeddings=[[0.1] * 1024],
            metadata={"entry_date": "2026-03-22", "user_id": _TEST_USER_ID},
        )
        response = client.get("/api/search?q=vienna")
        assert response.status_code == 200
        assert response.json()["mode"] == "semantic"


class TestDashboardMoodDimensions:
    """T1.3b.vi — GET /api/dashboard/mood-dimensions."""

    @pytest.fixture
    def mood_client(
        self, repo: SQLiteEntryRepository, mock_embeddings: MagicMock
    ) -> Generator[tuple[TestClient, dict]]:
        from mcp.server.fastmcp import FastMCP

        from journal.api import register_api_routes
        from journal.services.chunking import FixedTokenChunker
        from journal.services.mood_dimensions import MoodDimension

        dimensions = (
            MoodDimension(
                name="joy_sadness",
                positive_pole="joy",
                negative_pole="sadness",
                scale_type="bipolar",
                notes="bipolar joy test",
            ),
            MoodDimension(
                name="agency",
                positive_pole="agency",
                negative_pole="apathy",
                scale_type="unipolar",
                notes="unipolar agency test",
            ),
        )
        mock_ocr = MagicMock()
        mock_transcription = MagicMock()

        ingestion = IngestionService(
            repository=repo,
            vector_store=MagicMock(),
            ocr_provider=mock_ocr,
            transcription_provider=mock_transcription,
            embeddings_provider=mock_embeddings,
            chunker=FixedTokenChunker(max_tokens=150, overlap_tokens=40),
        )
        query = QueryService(
            repository=repo,
            vector_store=MagicMock(),
            embeddings_provider=mock_embeddings,
        )
        services = {
            "ingestion": ingestion,
            "query": query,
            "mood_dimensions": dimensions,
        }
        test_mcp = FastMCP("test-journal")
        register_api_routes(test_mcp, lambda: services)
        app = _FakeAuthMiddleware(test_mcp.streamable_http_app())
        with TestClient(app, raise_server_exceptions=False) as tc:
            yield tc, services

    def test_mood_dimensions_returns_full_shape(
        self, mood_client: tuple[TestClient, dict]
    ) -> None:
        client, _ = mood_client
        resp = client.get("/api/dashboard/mood-dimensions")
        assert resp.status_code == 200
        data = resp.json()
        assert "dimensions" in data
        assert len(data["dimensions"]) == 2
        bipolar = next(
            d for d in data["dimensions"] if d["name"] == "joy_sadness"
        )
        assert bipolar["scale_type"] == "bipolar"
        assert bipolar["score_min"] == -1.0
        assert bipolar["score_max"] == 1.0
        assert bipolar["positive_pole"] == "joy"
        assert bipolar["negative_pole"] == "sadness"
        assert bipolar["notes"] == "bipolar joy test"

        unipolar = next(
            d for d in data["dimensions"] if d["name"] == "agency"
        )
        assert unipolar["scale_type"] == "unipolar"
        assert unipolar["score_min"] == 0.0
        assert unipolar["score_max"] == 1.0

    def test_mood_dimensions_empty_when_disabled(
        self,
        repo: SQLiteEntryRepository,
        mock_embeddings: MagicMock,
    ) -> None:
        """With scoring disabled, the services dict has no
        `mood_dimensions` key; the endpoint should return an
        empty array, not a 500."""
        from mcp.server.fastmcp import FastMCP

        from journal.api import register_api_routes
        from journal.services.chunking import FixedTokenChunker

        ingestion = IngestionService(
            repository=repo,
            vector_store=MagicMock(),
            ocr_provider=MagicMock(),
            transcription_provider=MagicMock(),
            embeddings_provider=mock_embeddings,
            chunker=FixedTokenChunker(max_tokens=150, overlap_tokens=40),
        )
        query = QueryService(
            repository=repo,
            vector_store=MagicMock(),
            embeddings_provider=mock_embeddings,
        )
        services = {"ingestion": ingestion, "query": query}
        test_mcp = FastMCP("test-journal")
        register_api_routes(test_mcp, lambda: services)
        with TestClient(
            _FakeAuthMiddleware(test_mcp.streamable_http_app()),
            raise_server_exceptions=False,
        ) as tc:
            resp = tc.get("/api/dashboard/mood-dimensions")
            assert resp.status_code == 200
            assert resp.json()["dimensions"] == []


class TestDashboardMoodTrends:
    """T1.3b.vi — GET /api/dashboard/mood-trends."""

    def test_happy_path_returns_trends(
        self, client: TestClient, repo: SQLiteEntryRepository
    ) -> None:
        e = repo.create_entry("2026-03-02", "photo", "mon entry", 2)
        repo.add_mood_score(e.id, "joy_sadness", 0.5)
        repo.add_mood_score(e.id, "agency", 0.7)

        resp = client.get("/api/dashboard/mood-trends?bin=week")
        assert resp.status_code == 200
        data = resp.json()
        assert data["bin"] == "week"
        assert len(data["bins"]) == 2
        by_dim = {b["dimension"]: b for b in data["bins"]}
        assert by_dim["joy_sadness"]["avg_score"] == 0.5
        assert by_dim["agency"]["avg_score"] == 0.7
        # Canonical Monday date.
        assert by_dim["joy_sadness"]["period"] == "2026-03-02"

    def test_dimension_filter(
        self, client: TestClient, repo: SQLiteEntryRepository
    ) -> None:
        e = repo.create_entry("2026-03-02", "photo", "x", 1)
        repo.add_mood_score(e.id, "joy_sadness", 0.5)
        repo.add_mood_score(e.id, "agency", 0.7)

        resp = client.get(
            "/api/dashboard/mood-trends?bin=week&dimension=agency"
        )
        data = resp.json()
        assert len(data["bins"]) == 1
        assert data["bins"][0]["dimension"] == "agency"

    def test_invalid_bin_returns_400(
        self, client: TestClient, repo: SQLiteEntryRepository
    ) -> None:
        resp = client.get("/api/dashboard/mood-trends?bin=fortnight")
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_bin"

    def test_empty_corpus_returns_empty(
        self, client: TestClient
    ) -> None:
        resp = client.get("/api/dashboard/mood-trends")
        assert resp.status_code == 200
        assert resp.json()["bins"] == []

    def test_503_when_services_not_initialized(self) -> None:
        from mcp.server.fastmcp import FastMCP

        from journal.api import register_api_routes

        test_mcp = FastMCP("test-journal")
        register_api_routes(test_mcp, lambda: None)
        with TestClient(
            _FakeAuthMiddleware(test_mcp.streamable_http_app()),
            raise_server_exceptions=False,
        ) as tc:
            assert tc.get("/api/dashboard/mood-trends").status_code == 503
            assert (
                tc.get("/api/dashboard/mood-dimensions").status_code == 503
            )


class TestHealth:
    """T1.2.d — GET /health route."""

    @pytest.fixture
    def health_client(
        self, repo: SQLiteEntryRepository, mock_embeddings: MagicMock
    ) -> Generator[tuple[TestClient, dict]]:
        """Health client with real InMemoryVectorStore and a live
        stats collector, so the snapshot-shaped payload matches
        what the production route would return."""
        from mcp.server.fastmcp import FastMCP

        from journal.api import register_api_routes
        from journal.config import Config
        from journal.services.chunking import FixedTokenChunker
        from journal.services.stats import InMemoryStatsCollector
        from journal.vectorstore.store import InMemoryVectorStore

        real_vector_store = InMemoryVectorStore()
        stats_collector = InMemoryStatsCollector()
        mock_ocr = MagicMock()
        mock_transcription = MagicMock()
        ingestion = IngestionService(
            repository=repo,
            vector_store=real_vector_store,
            ocr_provider=mock_ocr,
            transcription_provider=mock_transcription,
            embeddings_provider=mock_embeddings,
            chunker=FixedTokenChunker(max_tokens=150, overlap_tokens=40),
        )
        query = QueryService(
            repository=repo,
            vector_store=real_vector_store,
            embeddings_provider=mock_embeddings,
            stats=stats_collector,
        )
        # Config stub with plausible API key lengths so the liveness
        # block reports "ok" for both by default.
        config = Config(
            anthropic_api_key="a" * 40,
            openai_api_key="o" * 40,
        )
        services = {
            "ingestion": ingestion,
            "query": query,
            "config": config,
            "stats": stats_collector,
        }
        test_mcp = FastMCP("test-journal")
        register_api_routes(test_mcp, lambda: services)
        app = _FakeAuthMiddleware(test_mcp.streamable_http_app())
        with TestClient(app, raise_server_exceptions=False) as tc:
            yield tc, services

    def test_health_empty_server(
        self, health_client: tuple[TestClient, dict]
    ) -> None:
        client, _ = health_client
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["ingestion"]["total_entries"] == 0
        assert data["queries"]["total_queries"] == 0
        assert isinstance(data["checks"], list)
        # sqlite + chromadb + anthropic + openai = 4.
        assert len(data["checks"]) == 4

    def test_health_reflects_populated_corpus(
        self,
        health_client: tuple[TestClient, dict],
        repo: SQLiteEntryRepository,
    ) -> None:
        client, _ = health_client
        repo.create_entry("2026-03-22", "photo", "Vienna today", 2)
        repo.create_entry("2026-03-23", "voice", "a voice note", 3)
        data = client.get("/health").json()
        assert data["ingestion"]["total_entries"] == 2
        assert data["ingestion"]["by_source_type"] == {"photo": 1, "voice": 1}
        assert data["ingestion"]["row_counts"]["entries"] == 2

    def test_health_reflects_query_stats_after_searches(
        self,
        health_client: tuple[TestClient, dict],
        repo: SQLiteEntryRepository,
    ) -> None:
        client, services = health_client
        query_svc: QueryService = services["query"]
        repo.create_entry("2026-03-22", "photo", "Vienna today", 2)

        # Fire a semantic + keyword search, then snapshot via /health.
        query_svc.search_entries("vienna")
        query_svc.keyword_search("vienna")

        data = client.get("/health").json()
        assert data["queries"]["total_queries"] == 2
        by_type = data["queries"]["by_type"]
        assert by_type["semantic_search"]["count"] == 1
        assert by_type["keyword_search"]["count"] == 1

    def test_health_degraded_on_missing_api_key(
        self, repo: SQLiteEntryRepository, mock_embeddings: MagicMock
    ) -> None:
        """When api keys are empty the liveness block rolls up to
        `degraded` but the endpoint still returns 200 so a healthcheck
        probe can tell "wrong config" from "container not listening"."""
        from mcp.server.fastmcp import FastMCP

        from journal.api import register_api_routes
        from journal.config import Config
        from journal.services.chunking import FixedTokenChunker
        from journal.services.stats import InMemoryStatsCollector
        from journal.vectorstore.store import InMemoryVectorStore

        vs = InMemoryVectorStore()
        stats = InMemoryStatsCollector()
        ingestion = IngestionService(
            repository=repo,
            vector_store=vs,
            ocr_provider=MagicMock(),
            transcription_provider=MagicMock(),
            embeddings_provider=mock_embeddings,
            chunker=FixedTokenChunker(max_tokens=150, overlap_tokens=40),
        )
        query = QueryService(
            repository=repo,
            vector_store=vs,
            embeddings_provider=mock_embeddings,
            stats=stats,
        )
        config = Config(anthropic_api_key="", openai_api_key="")
        services = {
            "ingestion": ingestion,
            "query": query,
            "config": config,
            "stats": stats,
        }
        test_mcp = FastMCP("test-journal")
        register_api_routes(test_mcp, lambda: services)
        app = _FakeAuthMiddleware(test_mcp.streamable_http_app())
        with TestClient(app, raise_server_exceptions=False) as tc:
            resp = tc.get("/health")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "degraded"
            by_name = {c["name"]: c for c in data["checks"]}
            assert by_name["anthropic"]["status"] == "degraded"
            assert by_name["openai"]["status"] == "degraded"

    def test_health_503_when_services_not_initialized(self) -> None:
        from mcp.server.fastmcp import FastMCP

        from journal.api import register_api_routes

        test_mcp = FastMCP("test-journal")
        register_api_routes(test_mcp, lambda: None)
        with TestClient(
            _FakeAuthMiddleware(test_mcp.streamable_http_app()),
            raise_server_exceptions=False,
        ) as tc:
            resp = tc.get("/health")
            assert resp.status_code == 503

    def test_health_payload_never_includes_search_terms(
        self,
        health_client: tuple[TestClient, dict],
        repo: SQLiteEntryRepository,
    ) -> None:
        """Privacy guard: the payload must not carry a field that
        would surface what the user was searching for."""
        import json as _json

        client, services = health_client
        query_svc: QueryService = services["query"]
        repo.create_entry("2026-03-22", "photo", "sensitive marker word", 3)
        query_svc.keyword_search("sensitive")
        data = client.get("/health").json()
        dumped = _json.dumps(data)
        # Assert the search term does not appear anywhere in the
        # serialized envelope — query stats are counts-only.
        assert "sensitive" not in dumped


class TestSettings:
    """GET /api/settings — non-secret config values."""

    def test_settings_returns_config(
        self, client: TestClient,
    ) -> None:
        resp = client.get("/api/settings")
        assert resp.status_code == 200
        data = resp.json()
        # Top-level sections
        assert "ocr" in data
        assert "transcription" in data
        assert "embedding" in data
        assert "chunking" in data
        assert "entity_extraction" in data
        assert "features" in data
        # OCR block
        assert data["ocr"]["provider"] == "anthropic"
        assert "claude" in data["ocr"]["model"] or data["ocr"]["model"] != ""
        # Chunking block
        assert isinstance(data["chunking"]["max_tokens"], int)
        assert isinstance(data["chunking"]["embed_metadata_prefix"], bool)
        # Entity extraction block
        assert data["entity_extraction"]["model"] == "claude-opus-4-6"
        assert isinstance(data["entity_extraction"]["dedup_similarity_threshold"], float)
        # Features block
        assert isinstance(data["features"]["mood_scoring"], bool)
        assert isinstance(data["features"]["journal_author_name"], str)

    def test_settings_does_not_leak_secrets(
        self, client: TestClient,
    ) -> None:
        import json as _json

        resp = client.get("/api/settings")
        dumped = _json.dumps(resp.json())
        # No API keys or secret values should appear
        assert "api_key" not in dumped.lower()
        assert "bearer" not in dumped.lower()
        assert "password" not in dumped.lower()
        assert "sk-ant-" not in dumped
        assert "sk-" not in dumped
        assert "xoxb-" not in dumped


class TestDashboardWritingStats:
    """T1.3a.ii — GET /api/dashboard/writing-stats."""

    def test_default_bin_is_week(
        self, client: TestClient, repo: SQLiteEntryRepository
    ) -> None:
        repo.create_entry("2026-03-02", "photo", "hello world", 2)
        resp = client.get("/api/dashboard/writing-stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["bin"] == "week"
        assert len(data["bins"]) == 1
        assert data["bins"][0]["bin_start"] == "2026-03-02"
        assert data["bins"][0]["entry_count"] == 1
        assert data["bins"][0]["total_words"] == 2

    def test_month_bin(
        self, client: TestClient, repo: SQLiteEntryRepository
    ) -> None:
        repo.create_entry("2026-03-15", "photo", "march entry", 2)
        repo.create_entry("2026-04-15", "photo", "april entry", 2)
        resp = client.get("/api/dashboard/writing-stats?bin=month")
        data = resp.json()
        starts = [b["bin_start"] for b in data["bins"]]
        assert "2026-03-01" in starts
        assert "2026-04-01" in starts

    def test_quarter_bin(
        self, client: TestClient, repo: SQLiteEntryRepository
    ) -> None:
        repo.create_entry("2026-02-15", "photo", "q1", 2)
        repo.create_entry("2026-08-15", "photo", "q3", 2)
        resp = client.get("/api/dashboard/writing-stats?bin=quarter")
        data = resp.json()
        starts = [b["bin_start"] for b in data["bins"]]
        assert starts == ["2026-01-01", "2026-07-01"]

    def test_year_bin(
        self, client: TestClient, repo: SQLiteEntryRepository
    ) -> None:
        repo.create_entry("2025-06-15", "photo", "2025", 2)
        repo.create_entry("2026-06-15", "photo", "2026", 2)
        resp = client.get("/api/dashboard/writing-stats?bin=year")
        data = resp.json()
        starts = [b["bin_start"] for b in data["bins"]]
        assert starts == ["2025-01-01", "2026-01-01"]

    def test_invalid_bin_returns_400(
        self, client: TestClient, repo: SQLiteEntryRepository
    ) -> None:
        resp = client.get("/api/dashboard/writing-stats?bin=fortnight")
        assert resp.status_code == 400
        body = resp.json()
        assert body["error"] == "invalid_bin"

    def test_date_filter(
        self, client: TestClient, repo: SQLiteEntryRepository
    ) -> None:
        repo.create_entry("2026-01-15", "photo", "january", 2)
        repo.create_entry("2026-03-15", "photo", "march", 2)
        repo.create_entry("2026-06-15", "photo", "june", 2)
        resp = client.get(
            "/api/dashboard/writing-stats"
            "?bin=month&from=2026-02-01&to=2026-04-30"
        )
        data = resp.json()
        assert data["from"] == "2026-02-01"
        assert data["to"] == "2026-04-30"
        assert len(data["bins"]) == 1
        assert data["bins"][0]["bin_start"] == "2026-03-01"

    def test_empty_corpus_returns_empty_bins(
        self, client: TestClient
    ) -> None:
        resp = client.get("/api/dashboard/writing-stats")
        assert resp.status_code == 200
        assert resp.json()["bins"] == []

    def test_503_when_services_not_initialized(self) -> None:
        from mcp.server.fastmcp import FastMCP

        from journal.api import register_api_routes

        test_mcp = FastMCP("test-journal")
        register_api_routes(test_mcp, lambda: None)
        with TestClient(
            _FakeAuthMiddleware(test_mcp.streamable_http_app()),
            raise_server_exceptions=False,
        ) as tc:
            resp = tc.get("/api/dashboard/writing-stats")
            assert resp.status_code == 503


class TestRepositoryHelpers:
    """Test the new count_entries and get_page_count repository methods."""

    def test_count_entries(self, repo: SQLiteEntryRepository) -> None:
        assert repo.count_entries() == 0
        repo.create_entry("2026-03-01", "photo", "One", 1)
        repo.create_entry("2026-03-15", "photo", "Two", 1)
        repo.create_entry("2026-04-01", "photo", "Three", 1)
        assert repo.count_entries() == 3

    def test_count_entries_with_date_filter(
        self, repo: SQLiteEntryRepository
    ) -> None:
        repo.create_entry("2026-01-01", "photo", "Jan", 1)
        repo.create_entry("2026-03-01", "photo", "Mar", 1)
        repo.create_entry("2026-05-01", "photo", "May", 1)
        assert repo.count_entries(start_date="2026-02-01") == 2
        assert repo.count_entries(end_date="2026-02-01") == 1
        assert repo.count_entries(
            start_date="2026-02-01", end_date="2026-04-01"
        ) == 1

    def test_get_page_count(self, repo: SQLiteEntryRepository) -> None:
        entry = repo.create_entry("2026-03-22", "photo", "Text", 1)
        assert repo.get_page_count(entry.id) == 0
        repo.add_entry_page(entry.id, 1, "Page one")
        assert repo.get_page_count(entry.id) == 1
        repo.add_entry_page(entry.id, 2, "Page two")
        assert repo.get_page_count(entry.id) == 2

    def test_get_page_count_nonexistent_entry(
        self, repo: SQLiteEntryRepository
    ) -> None:
        assert repo.get_page_count(999) == 0


class TestEntryEntities:
    def test_entry_entities_includes_quotes(
        self,
        client: TestClient,
        repo: SQLiteEntryRepository,
        services: dict,
    ) -> None:
        entry = repo.create_entry("2026-04-01", "photo", "I met Alice at the park.", 5)
        entity_store: SQLiteEntityStore = services["entity_store"]
        entity = entity_store.create_entity("person", "Alice", "A friend", "2026-04-01")
        entity_store.create_mention(
            entity_id=entity.id,
            entry_id=entry.id,
            quote="Alice at the park",
            confidence=0.95,
            extraction_run_id="run-1",
        )
        entity_store.create_mention(
            entity_id=entity.id,
            entry_id=entry.id,
            quote="Alice",
            confidence=0.9,
            extraction_run_id="run-1",
        )
        resp = client.get(f"/api/entries/{entry.id}/entities")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        item = data["items"][0]
        assert item["canonical_name"] == "Alice"
        assert item["mention_count"] == 2
        assert set(item["quotes"]) == {"Alice at the park", "Alice"}

    def test_entry_entities_deduplicates_quotes(
        self,
        client: TestClient,
        repo: SQLiteEntryRepository,
        services: dict,
    ) -> None:
        entry = repo.create_entry("2026-04-01", "photo", "Alice Alice", 2)
        entity_store: SQLiteEntityStore = services["entity_store"]
        entity = entity_store.create_entity("person", "Alice", "", "2026-04-01")
        entity_store.create_mention(
            entity_id=entity.id,
            entry_id=entry.id,
            quote="Alice",
            confidence=0.9,
            extraction_run_id="run-1",
        )
        entity_store.create_mention(
            entity_id=entity.id,
            entry_id=entry.id,
            quote="Alice",
            confidence=0.85,
            extraction_run_id="run-1",
        )
        resp = client.get(f"/api/entries/{entry.id}/entities")
        data = resp.json()
        assert data["items"][0]["quotes"] == ["Alice"]


class TestUpdateEntity:
    def test_rename_entity(
        self, client: TestClient, services: dict
    ) -> None:
        entity_store: SQLiteEntityStore = services["entity_store"]
        entity = entity_store.create_entity("person", "Lizzie", "", "2026-01-01")
        resp = client.patch(
            f"/api/entities/{entity.id}",
            json={"canonical_name": "Lizzie Extance"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["canonical_name"] == "Lizzie Extance"
        assert data["entity_type"] == "person"

    def test_change_entity_type(
        self, client: TestClient, services: dict
    ) -> None:
        entity_store: SQLiteEntityStore = services["entity_store"]
        entity = entity_store.create_entity("other", "Monday", "", "2026-01-01")
        resp = client.patch(
            f"/api/entities/{entity.id}",
            json={"entity_type": "activity"},
        )
        assert resp.status_code == 200
        assert resp.json()["entity_type"] == "activity"

    def test_update_nonexistent_returns_404(
        self, client: TestClient, services: dict
    ) -> None:
        resp = client.patch("/api/entities/9999", json={"canonical_name": "X"})
        assert resp.status_code == 404

    def test_update_invalid_type_returns_400(
        self, client: TestClient, services: dict
    ) -> None:
        entity_store: SQLiteEntityStore = services["entity_store"]
        entity = entity_store.create_entity("person", "A", "", "2026-01-01")
        resp = client.patch(
            f"/api/entities/{entity.id}",
            json={"entity_type": "invalid"},
        )
        assert resp.status_code == 400

    def test_update_empty_name_returns_400(
        self, client: TestClient, services: dict
    ) -> None:
        entity_store: SQLiteEntityStore = services["entity_store"]
        entity = entity_store.create_entity("person", "A", "", "2026-01-01")
        resp = client.patch(
            f"/api/entities/{entity.id}",
            json={"canonical_name": "  "},
        )
        assert resp.status_code == 400


class TestDeleteEntity:
    def test_delete_entity(
        self, client: TestClient, services: dict
    ) -> None:
        entity_store: SQLiteEntityStore = services["entity_store"]
        entity = entity_store.create_entity("person", "Noise", "", "2026-01-01")
        resp = client.delete(f"/api/entities/{entity.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["deleted"] is True
        assert data["id"] == entity.id
        assert entity_store.get_entity(entity.id) is None

    def test_delete_nonexistent_returns_404(
        self, client: TestClient, services: dict
    ) -> None:
        resp = client.delete("/api/entities/9999")
        assert resp.status_code == 404


class TestMergeEntities:
    def test_merge_two_entities(
        self,
        client: TestClient,
        repo: SQLiteEntryRepository,
        services: dict,
    ) -> None:
        entity_store: SQLiteEntityStore = services["entity_store"]
        entry = repo.create_entry("2026-01-01", "photo", "text", 1)
        a = entity_store.create_entity("person", "Vienna's aunt", "", "2026-01-01")
        b = entity_store.create_entity("person", "Lizzie Extance", "", "2026-01-01")
        entity_store.create_mention(a.id, entry.id, "aunt", 0.9, "r1")

        resp = client.post(
            "/api/entities/merge",
            json={"survivor_id": b.id, "absorbed_ids": [a.id]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["survivor"]["canonical_name"] == "Lizzie Extance"
        assert data["absorbed_ids"] == [a.id]
        assert data["mentions_reassigned"] == 1

    def test_merge_missing_fields_returns_400(
        self, client: TestClient, services: dict
    ) -> None:
        resp = client.post("/api/entities/merge", json={"survivor_id": 1})
        assert resp.status_code == 400

    def test_merge_nonexistent_returns_400(
        self, client: TestClient, services: dict
    ) -> None:
        entity_store: SQLiteEntityStore = services["entity_store"]
        a = entity_store.create_entity("person", "A", "", "2026-01-01")
        resp = client.post(
            "/api/entities/merge",
            json={"survivor_id": a.id, "absorbed_ids": [9999]},
        )
        # Ownership check returns 404 (entity not found for this user)
        assert resp.status_code == 404


class TestMergeCandidates:
    def test_list_candidates(
        self, client: TestClient, services: dict
    ) -> None:
        entity_store: SQLiteEntityStore = services["entity_store"]
        a = entity_store.create_entity("person", "A", "", "2026-01-01")
        b = entity_store.create_entity("person", "B", "", "2026-01-01")
        entity_store.create_merge_candidate(a.id, b.id, 0.82, "run-1")

        resp = client.get("/api/entities/merge-candidates")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["similarity"] == pytest.approx(0.82)

    def test_resolve_candidate(
        self, client: TestClient, services: dict
    ) -> None:
        entity_store: SQLiteEntityStore = services["entity_store"]
        a = entity_store.create_entity("person", "A", "", "2026-01-01")
        b = entity_store.create_entity("person", "B", "", "2026-01-01")
        entity_store.create_merge_candidate(a.id, b.id, 0.82, "run-1")

        candidates = entity_store.list_merge_candidates()
        resp = client.patch(
            f"/api/entities/merge-candidates/{candidates[0].id}",
            json={"status": "dismissed"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "dismissed"

    def test_resolve_invalid_status_returns_400(
        self, client: TestClient, services: dict
    ) -> None:
        resp = client.patch(
            "/api/entities/merge-candidates/1",
            json={"status": "bogus"},
        )
        assert resp.status_code == 400


class TestMergeHistory:
    def test_merge_history_after_merge(
        self,
        client: TestClient,
        repo: SQLiteEntryRepository,
        services: dict,
    ) -> None:
        entity_store: SQLiteEntityStore = services["entity_store"]
        entry = repo.create_entry("2026-01-01", "photo", "text", 1)
        a = entity_store.create_entity("person", "Old Name", "desc", "2026-01-01")
        b = entity_store.create_entity("person", "New Name", "", "2026-01-01")
        entity_store.create_mention(a.id, entry.id, "old", 0.9, "r1")
        entity_store.merge_entities(b.id, [a.id])

        resp = client.get(f"/api/entities/{b.id}/merge-history")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["history"]) == 1
        assert data["history"][0]["absorbed_name"] == "Old Name"


class TestEntitySearchFilter:
    """Regression test for the tuple-unpack bug in GET /api/entities?search=..."""

    def test_search_filter_works(
        self, client: TestClient, services: dict
    ) -> None:
        entity_store: SQLiteEntityStore = services["entity_store"]
        entity_store.create_entity("person", "Atlas", "", "2026-01-01")
        entity_store.create_entity("person", "Ritsya", "", "2026-01-01")

        resp = client.get("/api/entities?search=atlas")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["items"]) == 1
        assert data["items"][0]["canonical_name"] == "Atlas"
