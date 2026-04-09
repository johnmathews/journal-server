"""Tests for REST API endpoints."""

import sqlite3
from collections.abc import Generator
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from starlette.testclient import TestClient

from journal.db.migrations import run_migrations
from journal.db.repository import SQLiteEntryRepository
from journal.services.ingestion import IngestionService
from journal.services.query import QueryService


@pytest.fixture
def api_db_conn(tmp_path: Path) -> Generator[sqlite3.Connection]:
    """Provide a migrated SQLite connection that works across threads.

    The Starlette TestClient runs the ASGI app in a separate thread,
    so we need check_same_thread=False.
    """
    db_path = tmp_path / "test_api.db"
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
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

    ingestion = IngestionService(
        repository=repo,
        vector_store=mock_vector_store,
        ocr_provider=mock_ocr,
        transcription_provider=mock_transcription,
        embeddings_provider=mock_embeddings,
        chunk_max_tokens=150,
        chunk_overlap_tokens=40,
    )
    query = QueryService(
        repository=repo,
        vector_store=mock_vector_store,
        embeddings_provider=mock_embeddings,
    )
    return {"ingestion": ingestion, "query": query}


@pytest.fixture
def client(services: dict) -> Generator[TestClient]:
    """Create a Starlette test client with the API routes registered."""
    from mcp.server.fastmcp import FastMCP

    from journal.api import register_api_routes

    # Create a minimal FastMCP instance for testing
    test_mcp = FastMCP("test-journal")
    register_api_routes(test_mcp, lambda: services)

    # Build the Starlette app
    app = test_mcp.streamable_http_app()

    with TestClient(app, raise_server_exceptions=False) as tc:
        yield tc


def _seed_entries(repo: SQLiteEntryRepository, count: int = 5) -> list[int]:
    """Create test entries and return their IDs."""
    ids = []
    for i in range(count):
        entry = repo.create_entry(
            f"2026-03-{i + 1:02d}",
            "ocr" if i % 2 == 0 else "voice",
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
        repo.create_entry("2026-01-15", "ocr", "January entry", 2)
        repo.create_entry("2026-03-15", "ocr", "March entry", 2)
        repo.create_entry("2026-05-15", "ocr", "May entry", 2)

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
        entry = repo.create_entry("2026-03-22", "ocr", "Combined text", 2)
        repo.add_entry_page(entry.id, 1, "Page one text")
        repo.add_entry_page(entry.id, 2, "Page two text")

        response = client.get("/api/entries")
        item = response.json()["items"][0]
        assert item["page_count"] == 2


class TestGetEntry:
    def test_get_entry_returns_detail(
        self, client: TestClient, repo: SQLiteEntryRepository
    ) -> None:
        entry = repo.create_entry("2026-03-22", "ocr", "Hello world", 2)
        response = client.get(f"/api/entries/{entry.id}")
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == entry.id
        assert data["entry_date"] == "2026-03-22"
        assert data["source_type"] == "ocr"
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
        entry = repo.create_entry("2026-03-22", "ocr", "Combined text", 2)
        repo.add_entry_page(entry.id, 1, "Page one")
        repo.add_entry_page(entry.id, 2, "Page two")

        response = client.get(f"/api/entries/{entry.id}")
        data = response.json()
        assert data["page_count"] == 2


class TestUpdateEntry:
    def test_patch_entry_updates_final_text(
        self, client: TestClient, repo: SQLiteEntryRepository
    ) -> None:
        entry = repo.create_entry("2026-03-22", "ocr", "raw OCR output", 3)
        response = client.patch(
            f"/api/entries/{entry.id}",
            json={"final_text": "corrected text"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["final_text"] == "corrected text"
        assert data["raw_text"] == "raw OCR output"  # unchanged
        assert data["word_count"] == 2  # re-counted

    def test_patch_entry_not_found(self, client: TestClient) -> None:
        response = client.patch(
            "/api/entries/999",
            json={"final_text": "corrected text"},
        )
        assert response.status_code == 404

    def test_patch_entry_empty_body(self, client: TestClient, repo: SQLiteEntryRepository) -> None:
        entry = repo.create_entry("2026-03-22", "ocr", "Hello", 1)
        response = client.patch(
            f"/api/entries/{entry.id}",
            content=b"",
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 400

    def test_patch_entry_missing_final_text(
        self, client: TestClient, repo: SQLiteEntryRepository
    ) -> None:
        entry = repo.create_entry("2026-03-22", "ocr", "Hello", 1)
        response = client.patch(
            f"/api/entries/{entry.id}",
            json={"other_field": "value"},
        )
        assert response.status_code == 400
        assert "final_text" in response.json()["error"]

    def test_patch_entry_empty_final_text(
        self, client: TestClient, repo: SQLiteEntryRepository
    ) -> None:
        entry = repo.create_entry("2026-03-22", "ocr", "Hello", 1)
        response = client.patch(
            f"/api/entries/{entry.id}",
            json={"final_text": "  "},
        )
        assert response.status_code == 400
        assert "empty" in response.json()["error"].lower()

    def test_patch_entry_non_string_final_text(
        self, client: TestClient, repo: SQLiteEntryRepository
    ) -> None:
        entry = repo.create_entry("2026-03-22", "ocr", "Hello", 1)
        response = client.patch(
            f"/api/entries/{entry.id}",
            json={"final_text": 123},
        )
        assert response.status_code == 400


class TestGetStats:
    def test_get_stats(
        self, client: TestClient, repo: SQLiteEntryRepository
    ) -> None:
        repo.create_entry("2026-01-15", "ocr", "January entry words here", 4)
        repo.create_entry("2026-02-15", "ocr", "February entry", 2)
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
        repo.create_entry("2026-01-15", "ocr", "Old entry", 2)
        repo.create_entry("2026-03-15", "ocr", "New entry", 2)

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


class TestRepositoryHelpers:
    """Test the new count_entries and get_page_count repository methods."""

    def test_count_entries(self, repo: SQLiteEntryRepository) -> None:
        assert repo.count_entries() == 0
        repo.create_entry("2026-03-01", "ocr", "One", 1)
        repo.create_entry("2026-03-15", "ocr", "Two", 1)
        repo.create_entry("2026-04-01", "ocr", "Three", 1)
        assert repo.count_entries() == 3

    def test_count_entries_with_date_filter(
        self, repo: SQLiteEntryRepository
    ) -> None:
        repo.create_entry("2026-01-01", "ocr", "Jan", 1)
        repo.create_entry("2026-03-01", "ocr", "Mar", 1)
        repo.create_entry("2026-05-01", "ocr", "May", 1)
        assert repo.count_entries(start_date="2026-02-01") == 2
        assert repo.count_entries(end_date="2026-02-01") == 1
        assert repo.count_entries(
            start_date="2026-02-01", end_date="2026-04-01"
        ) == 1

    def test_get_page_count(self, repo: SQLiteEntryRepository) -> None:
        entry = repo.create_entry("2026-03-22", "ocr", "Text", 1)
        assert repo.get_page_count(entry.id) == 0
        repo.add_entry_page(entry.id, 1, "Page one")
        assert repo.get_page_count(entry.id) == 1
        repo.add_entry_page(entry.id, 2, "Page two")
        assert repo.get_page_count(entry.id) == 2

    def test_get_page_count_nonexistent_entry(
        self, repo: SQLiteEntryRepository
    ) -> None:
        assert repo.get_page_count(999) == 0
