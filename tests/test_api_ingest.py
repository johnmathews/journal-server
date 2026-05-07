"""Tests for entry creation API endpoints."""

import io
import sqlite3
from collections.abc import Generator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient

from journal.auth import AuthenticatedUser, _current_user_id
from journal.config import Config
from journal.db.connection import get_connection
from journal.db.jobs_repository import SQLiteJobRepository
from journal.db.migrations import run_migrations
from journal.db.repository import SQLiteEntryRepository
from journal.providers.transcription import (
    OpenAITranscribeProvider,
    RetryingTranscriptionProvider,
    build_transcription_provider,
)
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
    from journal.models import ExtractionResult

    ingestion = IngestionService(
        repository=repo,
        vector_store=mock_vector_store,
        ocr_provider=MagicMock(),
        transcription_provider=MagicMock(),
        embeddings_provider=mock_embeddings,
        chunker=FixedTokenChunker(max_tokens=150, overlap_tokens=40),
        preprocess_images=False,
    )
    query = QueryService(
        repository=repo,
        vector_store=mock_vector_store,
        embeddings_provider=mock_embeddings,
    )
    # Minimal job infrastructure for the image endpoint
    job_repo = SQLiteJobRepository(api_db_conn)
    # Return a proper ExtractionResult so background jobs can JSON-serialize
    # their result dict. A bare MagicMock causes TypeError in json.dumps()
    # which triggers lock contention with subsequent job submissions.
    mock_extraction = MagicMock()
    mock_extraction.extract_from_entry = MagicMock(
        return_value=ExtractionResult(
            entry_id=0, extraction_run_id="test",
            entities_created=0, entities_matched=0,
            mentions_created=0, relationships_created=0,
        )
    )
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
        assert data["entry"]["source_type"] == "text_entry"
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
            json={"text": "Imported text.", "source_type": "imported_text_file"},
        )
        assert response.status_code == 201
        assert response.json()["entry"]["source_type"] == "imported_text_file"

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
        assert data["entry"]["source_type"] == "imported_text_file"
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

        services["config"] = Config(enable_mood_scoring=True)

        entry = repo.create_entry("2026-04-01", "photo", "raw text", 2)
        response = client.patch(
            f"/api/entries/{entry.id}",
            json={"final_text": "corrected text"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data.get("mood_job_id") is not None

    def test_save_entry_pipeline_dispatches_workers_after_mark_succeeded(
        self, services: dict, repo: SQLiteEntryRepository,
    ) -> None:
        """Regression for the shared-connection race: every worker
        ``executor.submit`` must happen AFTER the parent pipeline's
        ``mark_succeeded`` commits. Otherwise the worker thread starts
        writing while the API thread is still mid-write, and the two
        collide on the ``check_same_thread=False`` connection (commit
        fails with ``sqlite3.OperationalError: not an error``).
        """
        job_runner = services["job_runner"]
        entry = repo.create_entry("2026-04-01", "photo", "raw text", 2)

        # Record interleaving of mark_succeeded vs executor.submit calls.
        events: list[str] = []
        original_mark_succeeded = job_runner._jobs.mark_succeeded
        original_executor_submit = job_runner._executor.submit

        def tracked_mark_succeeded(job_id: str, result: object) -> object:
            events.append(f"mark_succeeded:{job_id}")
            return original_mark_succeeded(job_id, result)

        def tracked_submit(*args: object, **kwargs: object) -> object:
            events.append("executor.submit")
            return original_executor_submit(*args, **kwargs)

        job_runner._jobs.mark_succeeded = tracked_mark_succeeded  # type: ignore[method-assign]
        job_runner._executor.submit = tracked_submit  # type: ignore[method-assign]
        try:
            job_runner.submit_save_entry_pipeline(
                entry_id=entry.id, user_id=1, enable_mood_scoring=True,
            )
        finally:
            job_runner._jobs.mark_succeeded = original_mark_succeeded  # type: ignore[method-assign]
            job_runner._executor.submit = original_executor_submit  # type: ignore[method-assign]

        # Pipeline ``mark_succeeded`` must precede every executor.submit
        # call. Worker self-bookkeeping (mark_running, mark_succeeded for
        # children) happens later on the worker thread and is irrelevant
        # to this invariant — only events recorded synchronously from
        # ``submit_save_entry_pipeline`` matter, so look at the position
        # of the FIRST executor.submit relative to the pipeline parent's
        # mark_succeeded.
        first_submit = events.index("executor.submit")
        pipeline_mark_succeeded = next(
            i for i, e in enumerate(events) if e.startswith("mark_succeeded:")
        )
        assert pipeline_mark_succeeded < first_submit, (
            f"executor.submit happened before mark_succeeded — race window "
            f"is open. Event order: {events[:first_submit + 1]}"
        )

    def test_patch_text_no_mood_without_config(
        self, client: TestClient, repo: SQLiteEntryRepository,
    ) -> None:
        """Without config, no mood job should be queued."""
        entry = repo.create_entry("2026-04-02", "photo", "raw text", 2)
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
        entry = repo.create_entry("2026-04-01", "photo", "Hello Ritsya.", 2)
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
        repo.create_entry("2026-04-02", "text_entry", "Clear text.", 2)

        response = client.get("/api/entries")
        assert response.status_code == 200
        items = response.json()["items"]
        assert all(e["uncertain_span_count"] == 0 for e in items)


class TestApiIngestWithFactory:
    """End-to-end check that the build_transcription_provider factory
    produces a TranscriptionProvider compatible with the IngestionService
    that the audio endpoint depends on.

    The existing TestIngestAudio fixtures inject a bare MagicMock as the
    transcription provider, so they would silently miss any DI mismatch
    introduced by the wrapper chain (e.g. a constructor signature change
    in OpenAITranscribeProvider, or RetryingTranscriptionProvider losing
    its `transcribe` method).
    """

    @pytest.fixture
    def factory_built_transcription(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> Generator[object]:
        """Build the transcription stack via the real factory.

        Patches the OpenAI / Gemini SDK boundaries so no network calls
        happen during construction. Returns the wrapper chain the
        factory would build for default config.
        """
        # Clear any inherited env that would shift the default stack.
        for var in (
            "TRANSCRIPTION_PROVIDER",
            "TRANSCRIPTION_MODEL",
            "TRANSCRIPTION_FALLBACK_ENABLED",
            "TRANSCRIPTION_FALLBACK_MODEL",
            "TRANSCRIPTION_RETRY_MAX_ATTEMPTS",
            "TRANSCRIPTION_RETRY_BASE_DELAY",
            "TRANSCRIPTION_RETRY_MAX_DELAY",
            "TRANSCRIPTION_SHADOW_PROVIDER",
            "TRANSCRIPTION_SHADOW_MODEL",
            "TRANSCRIPTION_CONTEXT_ENABLED",
        ):
            monkeypatch.delenv(var, raising=False)
        # Disable context loading to avoid filesystem dependencies.
        monkeypatch.setenv("TRANSCRIPTION_CONTEXT_ENABLED", "false")
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")

        with (
            patch("journal.providers.transcription.openai.OpenAI"),
            patch("journal.providers.transcription.genai.Client"),
        ):
            provider = build_transcription_provider(Config())
            yield provider

    @pytest.fixture
    def factory_services(
        self,
        repo: SQLiteEntryRepository,
        mock_vector_store: MagicMock,
        mock_embeddings: MagicMock,
        api_db_conn: sqlite3.Connection,
        factory_built_transcription: object,
    ) -> Generator[dict]:
        """Wire the IngestionService with the factory-built provider.

        Mirrors the regular `services` fixture, but swaps the inline
        transcription mock for the real wrapper chain.
        """
        from journal.models import ExtractionResult

        ingestion = IngestionService(
            repository=repo,
            vector_store=mock_vector_store,
            ocr_provider=MagicMock(),
            transcription_provider=factory_built_transcription,  # type: ignore[arg-type]
            embeddings_provider=mock_embeddings,
            chunker=FixedTokenChunker(max_tokens=150, overlap_tokens=40),
            preprocess_images=False,
        )
        query = QueryService(
            repository=repo,
            vector_store=mock_vector_store,
            embeddings_provider=mock_embeddings,
        )
        job_repo = SQLiteJobRepository(api_db_conn)
        mock_extraction = MagicMock()
        mock_extraction.extract_from_entry = MagicMock(
            return_value=ExtractionResult(
                entry_id=0, extraction_run_id="test",
                entities_created=0, entities_matched=0,
                mentions_created=0, relationships_created=0,
            ),
        )
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
        # ThreadPoolExecutor in JobRunner must shut down cleanly so the
        # background ingestion job (if it ran) doesn't leak past test exit
        # and segfault CI.
        job_runner.shutdown(wait=True)

    @pytest.fixture
    def factory_client(
        self, factory_services: dict,
    ) -> Generator[TestClient]:
        from mcp.server.fastmcp import FastMCP

        from journal.api import register_api_routes

        test_mcp = FastMCP("test-journal")
        register_api_routes(test_mcp, lambda: factory_services)
        app = _FakeAuthMiddleware(test_mcp.streamable_http_app())
        with TestClient(app, raise_server_exceptions=False) as tc:
            yield tc

    def test_audio_endpoint_works_with_default_factory(
        self,
        factory_client: TestClient,
        factory_built_transcription: object,
    ) -> None:
        """The audio endpoint must accept uploads when ingestion is wired
        with the factory-built transcription stack (default config:
        Retrying(OpenAI/gpt-4o-transcribe, fallback=whisper-1)).

        The endpoint queues a background job and returns 202; the test
        asserts the wrapper chain doesn't break that path. We don't run
        the queued job — that's a separate concern covered by the
        ingestion-stack tests.
        """
        # Sanity-check that the factory built what we expect.
        assert isinstance(
            factory_built_transcription, RetryingTranscriptionProvider,
        )
        assert isinstance(
            factory_built_transcription._primary, OpenAITranscribeProvider,
        )
        assert isinstance(
            factory_built_transcription._fallback, OpenAITranscribeProvider,
        )

        # Patch the OpenAI SDK so any background-job execution that
        # happens between submission and shutdown doesn't try a real
        # network call. The endpoint path itself never invokes it.
        fake_transcript = MagicMock()
        fake_transcript.text = "transcribed"
        fake_transcript.logprobs = None
        with patch(
            "journal.providers.transcription.openai.OpenAI",
        ) as mock_openai_cls:
            instance = mock_openai_cls.return_value
            instance.audio.transcriptions.create.return_value = fake_transcript

            files = {
                "audio": (
                    "rec.webm",
                    io.BytesIO(b"fake webm data"),
                    "audio/webm",
                ),
            }
            response = factory_client.post(
                "/api/entries/ingest/audio", files=files,
            )

        assert response.status_code == 202, (
            f"unexpected status {response.status_code}: {response.text}"
        )
        data = response.json()
        assert "job_id" in data
        assert data["status"] == "queued"
