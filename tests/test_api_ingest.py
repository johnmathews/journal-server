"""Tests for entry creation API endpoints."""

import io
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
from journal.services.chunking import FixedTokenChunker
from journal.services.ingestion import IngestionService
from journal.services.jobs import JobRunner
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
    db_path = tmp_path / "test_api_ingest.db"
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
    api_db_conn: sqlite3.Connection,
) -> Generator[dict]:
    ingestion = IngestionService(
        repository=repo,
        vector_store=mock_vector_store,
        ocr_provider=MagicMock(),
        transcription_provider=MagicMock(),
        embeddings_provider=mock_embeddings,
        chunker=FixedTokenChunker(max_tokens=150, overlap_tokens=40),
    )
    query = QueryService(
        repository=repo,
        vector_store=mock_vector_store,
        embeddings_provider=mock_embeddings,
    )
    # Minimal job infrastructure for the image endpoint
    job_repo = SQLiteJobRepository(api_db_conn)
    mock_extraction = MagicMock()
    mock_mood_scoring = MagicMock()
    mock_mood_scoring.score_entry = MagicMock(return_value=5)
    job_runner = JobRunner(
        job_repository=job_repo,
        entity_extraction_service=mock_extraction,
        mood_backfill_callable=MagicMock(),
        mood_scoring_service=mock_mood_scoring,
        entry_repository=repo,
        ingestion_service=ingestion,
    )
    yield {
        "ingestion": ingestion,
        "query": query,
        "job_runner": job_runner,
        "job_repository": job_repo,
    }
    job_runner.shutdown(wait=True)


@pytest.fixture
def client(services: dict) -> Generator[TestClient]:
    from mcp.server.fastmcp import FastMCP

    from journal.api import register_api_routes

    test_mcp = FastMCP("test-journal")
    register_api_routes(test_mcp, lambda: services)
    app = _FakeAuthMiddleware(test_mcp.streamable_http_app())
    with TestClient(app, raise_server_exceptions=False) as tc:
        yield tc


class TestIngestText:
    def test_creates_entry(self, client: TestClient) -> None:
        response = client.post(
            "/api/entries/ingest/text",
            json={"text": "Today I went for a walk in the park."},
        )
        assert response.status_code == 201
        data = response.json()
        assert "entry" in data
        assert data["entry"]["source_type"] == "manual"
        assert "walk" in data["entry"]["final_text"]
        assert data["mood_job_id"] is None

    def test_custom_date(self, client: TestClient) -> None:
        response = client.post(
            "/api/entries/ingest/text",
            json={"text": "A journal entry.", "entry_date": "2026-01-15"},
        )
        assert response.status_code == 201
        assert response.json()["entry"]["entry_date"] == "2026-01-15"

    def test_custom_source_type(self, client: TestClient) -> None:
        response = client.post(
            "/api/entries/ingest/text",
            json={"text": "Imported text.", "source_type": "import"},
        )
        assert response.status_code == 201
        assert response.json()["entry"]["source_type"] == "import"

    def test_missing_text(self, client: TestClient) -> None:
        response = client.post("/api/entries/ingest/text", json={})
        assert response.status_code == 400
        assert "text" in response.json()["error"].lower()

    def test_empty_text(self, client: TestClient) -> None:
        response = client.post(
            "/api/entries/ingest/text", json={"text": "   "}
        )
        assert response.status_code == 400

    def test_invalid_json(self, client: TestClient) -> None:
        response = client.post(
            "/api/entries/ingest/text",
            content=b"not json",
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 400


class TestIngestFile:
    def _upload(
        self, client: TestClient, content: str = "Hello from a file.",
        filename: str = "entry.txt", entry_date: str | None = None,
    ):
        files = {"file": (filename, io.BytesIO(content.encode()), "text/plain")}
        data = {}
        if entry_date:
            data["entry_date"] = entry_date
        return client.post("/api/entries/ingest/file", files=files, data=data)

    def test_txt_upload(self, client: TestClient) -> None:
        response = self._upload(client, "My exported journal entry.")
        assert response.status_code == 201
        data = response.json()
        assert data["entry"]["source_type"] == "import"
        assert "exported" in data["entry"]["final_text"]

    def test_md_upload(self, client: TestClient) -> None:
        response = self._upload(client, "# My Day\n\nWent hiking.", filename="entry.md")
        assert response.status_code == 201
        assert "# My Day" in response.json()["entry"]["raw_text"]

    def test_custom_date(self, client: TestClient) -> None:
        response = self._upload(client, "Some text.", entry_date="2026-02-20")
        assert response.status_code == 201
        assert response.json()["entry"]["entry_date"] == "2026-02-20"

    def test_wrong_extension(self, client: TestClient) -> None:
        files = {"file": ("photo.jpg", io.BytesIO(b"not an image"), "image/jpeg")}
        response = client.post("/api/entries/ingest/file", files=files)
        assert response.status_code == 400
        assert ".md or .txt" in response.json()["error"]

    def test_empty_file(self, client: TestClient) -> None:
        response = self._upload(client, "   ")
        assert response.status_code == 400
        assert "empty" in response.json()["error"].lower()

    def test_no_file(self, client: TestClient) -> None:
        response = client.post("/api/entries/ingest/file", data={})
        assert response.status_code == 400


class TestIngestImages:
    def test_single_image(self, client: TestClient) -> None:
        files = {"images": ("page1.jpg", io.BytesIO(b"fake jpeg data"), "image/jpeg")}
        response = client.post("/api/entries/ingest/images", files=files)
        assert response.status_code == 202
        data = response.json()
        assert "job_id" in data
        assert data["status"] == "queued"

    def test_multiple_images(self, client: TestClient) -> None:
        files = [
            ("images", ("page1.jpg", io.BytesIO(b"fake page 1"), "image/jpeg")),
            ("images", ("page2.jpg", io.BytesIO(b"fake page 2"), "image/jpeg")),
        ]
        response = client.post("/api/entries/ingest/images", files=files)
        assert response.status_code == 202
        assert "job_id" in response.json()

    def test_no_images(self, client: TestClient) -> None:
        response = client.post("/api/entries/ingest/images", data={})
        assert response.status_code == 400

    def test_wrong_file_type(self, client: TestClient) -> None:
        files = {"images": ("doc.pdf", io.BytesIO(b"pdf content"), "application/pdf")}
        response = client.post("/api/entries/ingest/images", files=files)
        assert response.status_code == 400
        assert "unsupported type" in response.json()["error"].lower()

    def test_file_too_large(self, client: TestClient) -> None:
        # 11 MB file
        big_data = b"x" * (11 * 1024 * 1024)
        files = {"images": ("big.jpg", io.BytesIO(big_data), "image/jpeg")}
        response = client.post("/api/entries/ingest/images", files=files)
        assert response.status_code == 400
        assert "10 MB" in response.json()["error"]


class TestIngestAudio:
    def test_single_recording(self, client: TestClient) -> None:
        files = {"audio": ("rec.webm", io.BytesIO(b"fake webm data"), "audio/webm")}
        response = client.post("/api/entries/ingest/audio", files=files)
        assert response.status_code == 202
        data = response.json()
        assert "job_id" in data
        assert data["status"] == "queued"

    def test_multiple_recordings(self, client: TestClient) -> None:
        files = [
            ("audio", ("rec1.webm", io.BytesIO(b"fake recording 1"), "audio/webm")),
            ("audio", ("rec2.webm", io.BytesIO(b"fake recording 2"), "audio/webm")),
        ]
        response = client.post("/api/entries/ingest/audio", files=files)
        assert response.status_code == 202
        assert "job_id" in response.json()

    def test_no_audio(self, client: TestClient) -> None:
        response = client.post("/api/entries/ingest/audio", data={})
        assert response.status_code == 400

    def test_wrong_file_type(self, client: TestClient) -> None:
        files = {"audio": ("doc.pdf", io.BytesIO(b"pdf content"), "application/pdf")}
        response = client.post("/api/entries/ingest/audio", files=files)
        assert response.status_code == 400
        assert "unsupported type" in response.json()["error"].lower()

    def test_file_too_large(self, client: TestClient) -> None:
        big_data = b"x" * (101 * 1024 * 1024)
        files = {"audio": ("big.webm", io.BytesIO(big_data), "audio/webm")}
        response = client.post("/api/entries/ingest/audio", files=files)
        assert response.status_code == 400
        assert "100 MB" in response.json()["error"]

    def test_custom_entry_date(self, client: TestClient) -> None:
        files = {"audio": ("rec.webm", io.BytesIO(b"audio data"), "audio/webm")}
        data = {"entry_date": "2026-03-15"}
        response = client.post("/api/entries/ingest/audio", files=files, data=data)
        assert response.status_code == 202

    def test_mp3_accepted(self, client: TestClient) -> None:
        files = {"audio": ("rec.mp3", io.BytesIO(b"fake mp3"), "audio/mpeg")}
        response = client.post("/api/entries/ingest/audio", files=files)
        assert response.status_code == 202

    def test_wav_accepted(self, client: TestClient) -> None:
        files = {"audio": ("rec.wav", io.BytesIO(b"fake wav"), "audio/wav")}
        response = client.post("/api/entries/ingest/audio", files=files)
        assert response.status_code == 202


class TestAutoEntityExtraction:
    """Entity extraction should be queued on every ingestion path."""

    def test_text_ingest_queues_entity_extraction(
        self, client: TestClient, services: dict,
    ) -> None:
        response = client.post(
            "/api/entries/ingest/text",
            json={"text": "Today was a great day with Alice."},
        )
        assert response.status_code == 201
        data = response.json()
        assert data["entity_extraction_job_id"] is not None

    def test_file_ingest_queues_entity_extraction(
        self, client: TestClient, services: dict,
    ) -> None:
        files = {"file": ("entry.txt", io.BytesIO(b"Met Bob at the park."), "text/plain")}
        response = client.post("/api/entries/ingest/file", files=files)
        assert response.status_code == 201
        data = response.json()
        assert data["entity_extraction_job_id"] is not None


class TestPatchMoodScoring:
    """PATCH /api/entries/{id} should queue mood re-scoring when enabled."""

    def test_patch_text_queues_mood_scoring(
        self, client: TestClient, repo: SQLiteEntryRepository, services: dict,
    ) -> None:
        """When config.enable_mood_scoring is True, PATCH should queue a mood job."""
        from journal.config import Config

        config = Config(enable_mood_scoring=True)
        services["config"] = config

        entry = repo.create_entry("2026-04-01", "ocr", "raw text", 2)
        response = client.patch(
            f"/api/entries/{entry.id}",
            json={"final_text": "corrected text"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data.get("mood_job_id") is not None

    def test_patch_text_no_mood_without_config(
        self, client: TestClient, repo: SQLiteEntryRepository,
    ) -> None:
        """Without config, no mood job should be queued."""
        entry = repo.create_entry("2026-04-02", "ocr", "raw text", 2)
        response = client.patch(
            f"/api/entries/{entry.id}",
            json={"final_text": "corrected text"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data.get("mood_job_id") is None


class TestListEntriesUncertainSpanCount:
    """GET /api/entries should include uncertain_span_count per entry."""

    def test_entries_include_uncertain_span_count(
        self, client: TestClient, repo: SQLiteEntryRepository,
    ) -> None:
        entry = repo.create_entry("2026-04-01", "ocr", "Hello Ritsya.", 2)
        repo.add_uncertain_spans(entry.id, [(6, 12)])

        response = client.get("/api/entries")
        assert response.status_code == 200
        items = response.json()["items"]
        match = [e for e in items if e["id"] == entry.id]
        assert len(match) == 1
        assert match[0]["uncertain_span_count"] == 1

    def test_zero_spans_returns_zero(
        self, client: TestClient, repo: SQLiteEntryRepository,
    ) -> None:
        repo.create_entry("2026-04-02", "manual", "Clear text.", 2)

        response = client.get("/api/entries")
        assert response.status_code == 200
        items = response.json()["items"]
        assert all(e["uncertain_span_count"] == 0 for e in items)
