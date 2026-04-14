"""Tests for MCP server tool functions."""

from unittest.mock import MagicMock

import pytest

from journal.db.repository import SQLiteEntryRepository
from journal.services.query import QueryService
from journal.vectorstore.store import InMemoryVectorStore

# Test the tool functions directly by calling the underlying service logic
# (The MCP framework handles routing; we test the business logic)


@pytest.fixture
def repo(db_conn):
    return SQLiteEntryRepository(db_conn)


@pytest.fixture
def vector_store():
    return InMemoryVectorStore()


@pytest.fixture
def mock_embeddings():
    provider = MagicMock()
    provider.embed_texts.return_value = [[1.0, 0.0, 0.0]]
    provider.embed_query.return_value = [1.0, 0.0, 0.0]
    return provider


@pytest.fixture
def query_service(repo, vector_store, mock_embeddings):
    return QueryService(repo, vector_store, mock_embeddings)


@pytest.fixture
def seeded_query(repo, vector_store, mock_embeddings):
    e1 = repo.create_entry("2026-03-22", "ocr", "Met Atlas in Vienna for coffee", 6)
    e2 = repo.create_entry("2026-03-23", "voice", "Quiet day reading at home", 5)
    repo.add_mood_score(e1.id, "overall", 0.7)
    repo.add_mood_score(e2.id, "overall", 0.3)

    vector_store.add_entry(
        e1.id,
        ["Met Atlas in Vienna for coffee"],
        [[1.0, 0.0, 0.0]],
        {"entry_date": "2026-03-22"},
    )
    vector_store.add_entry(
        e2.id,
        ["Quiet day reading at home"],
        [[0.0, 1.0, 0.0]],
        {"entry_date": "2026-03-23"},
    )

    return QueryService(repo, vector_store, mock_embeddings)


class TestSearchEntries:
    def test_returns_results(self, seeded_query):
        results = seeded_query.search_entries("Vienna")
        assert len(results) >= 1

    def test_empty_results(self, query_service):
        results = query_service.search_entries("nonexistent")
        assert results == []


class TestGetEntriesByDate:
    def test_found(self, seeded_query):
        entries = seeded_query.get_entries_by_date("2026-03-22")
        assert len(entries) == 1
        assert "Atlas" in entries[0].raw_text

    def test_not_found(self, seeded_query):
        entries = seeded_query.get_entries_by_date("2025-01-01")
        assert entries == []


class TestListEntries:
    def test_list_all(self, seeded_query):
        entries = seeded_query.list_entries()
        assert len(entries) == 2

    def test_list_filtered(self, seeded_query):
        entries = seeded_query.list_entries(start_date="2026-03-23")
        assert len(entries) == 1


class TestStatistics:
    def test_stats(self, seeded_query):
        stats = seeded_query.get_statistics()
        assert stats.total_entries == 2
        assert stats.total_words == 11


class TestMoodTrends:
    def test_trends(self, seeded_query):
        trends = seeded_query.get_mood_trends(granularity="day")
        assert len(trends) == 2


class TestTopicFrequency:
    def test_found(self, seeded_query):
        freq = seeded_query.get_topic_frequency("Vienna")
        assert freq.count == 1

    def test_not_found(self, seeded_query):
        freq = seeded_query.get_topic_frequency("nonexistent")
        assert freq.count == 0


class TestFinalTextUsage:
    """Verify that tools use final_text (not raw_text) for display."""

    def test_entry_has_final_text(self, repo):
        """Entries have final_text populated from raw_text by default."""
        entry = repo.create_entry("2026-04-01", "ocr", "Some OCR text", 3)
        assert entry.final_text == "Some OCR text"

    def test_list_entries_uses_final_text(self, repo):
        """list_entries returns entries with final_text for previews."""
        entry = repo.create_entry("2026-04-01", "ocr", "Original OCR text", 3)
        # Simulate corrected text
        repo.update_final_text(entry.id, "Corrected text", 2, 1)
        updated = repo.get_entry(entry.id)
        assert updated is not None
        assert updated.final_text == "Corrected text"
        assert updated.raw_text == "Original OCR text"

    def test_topic_frequency_entries_have_final_text(self, seeded_query):
        """topic_frequency entries should have final_text available."""
        freq = seeded_query.get_topic_frequency("Vienna")
        assert freq.count == 1
        entry = freq.entries[0]
        # final_text should be populated (defaults to raw_text)
        assert entry.final_text != ""


class TestMCPToolModuleImports:
    """Verify new MCP tool functions are importable."""

    def test_ingest_multi_page_tool_exists(self):
        from journal.mcp_server import journal_ingest_multi_page
        assert callable(journal_ingest_multi_page)

    def test_ingest_multi_page_from_url_tool_exists(self):
        from journal.mcp_server import journal_ingest_multi_page_from_url
        assert callable(journal_ingest_multi_page_from_url)

    def test_update_entry_text_tool_exists(self):
        from journal.mcp_server import journal_update_entry_text
        assert callable(journal_update_entry_text)

    def test_batch_job_tools_exist(self):
        """Work Unit 5b — async batch-job MCP tool wrappers."""
        from journal.mcp_server import (
            journal_backfill_mood_scores_batch,
            journal_extract_entities_batch,
            journal_get_job_status,
        )
        assert callable(journal_extract_entities_batch)
        assert callable(journal_backfill_mood_scores_batch)
        assert callable(journal_get_job_status)


class TestBatchJobTools:
    """Integration tests for the async batch-job MCP tool wrappers.

    These tools call `_get_job_runner(ctx)` and
    `_get_job_repository(ctx)`, so the test fakes a `Context` that
    exposes a `lifespan_context` dict containing a live JobRunner +
    JobRepository pair wired to in-memory fakes.
    """

    @pytest.fixture
    def job_context(self, tmp_path):
        from journal.db.connection import get_connection
        from journal.db.jobs_repository import SQLiteJobRepository
        from journal.db.migrations import run_migrations
        from journal.models import ExtractionResult
        from journal.services.backfill import MoodBackfillResult
        from journal.services.jobs import JobRunner
        from tests.test_services.test_jobs_runner import (
            FakeEntityExtractionService,
            FakeMoodBackfill,
        )

        db_path = tmp_path / "mcp-jobs.db"
        conn = get_connection(db_path, check_same_thread=False)
        run_migrations(conn)
        repo = SQLiteJobRepository(conn)

        extraction_result = ExtractionResult(
            entry_id=1,
            extraction_run_id="run-1",
            entities_created=2,
            entities_matched=0,
            mentions_created=4,
            relationships_created=1,
            warnings=[],
        )
        extraction = FakeEntityExtractionService(
            batch_results=[extraction_result],
            single_result=extraction_result,
        )
        mood = FakeMoodBackfill(
            result=MoodBackfillResult(scored=5, skipped=2),
            entries_to_count=2,
        )
        runner = JobRunner(
            job_repository=repo,
            entity_extraction_service=extraction,  # type: ignore[arg-type]
            mood_backfill_callable=mood,
            mood_scoring_service=object(),  # type: ignore[arg-type]
            entry_repository=object(),  # type: ignore[arg-type]
        )

        # The tools read from `ctx.request_context.lifespan_context`
        # via `_get_job_runner(ctx)` helpers. Mock it directly rather
        # than booting a full FastMCP lifespan.
        ctx = MagicMock()
        ctx.request_context.lifespan_context = {
            "job_runner": runner,
            "job_repository": repo,
        }

        yield ctx, repo, runner, extraction, mood

        runner.shutdown(wait=True)
        conn.close()

    def test_extract_entities_batch_happy_path(self, job_context):
        from journal.mcp_server import journal_extract_entities_batch

        ctx, repo, runner, extraction, _mood = job_context

        result = journal_extract_entities_batch(
            start_date="2026-01-01", ctx=ctx
        )
        assert result["status"] == "succeeded"
        assert result["job_id"]
        assert result["error_message"] is None
        assert result["result"]["processed"] == 1
        assert result["result"]["entities_created"] == 2

        # The runner recorded the batch call with the right params.
        assert extraction.batch_calls[0]["start_date"] == "2026-01-01"

    def test_extract_entities_batch_validation_error(self, job_context):
        """Invalid params return a failed dict, not an exception."""
        from journal.mcp_server import journal_extract_entities_batch

        ctx, _repo, _runner, _extraction, _mood = job_context

        # `entry_id=-1` is still a valid int; to trigger a ValueError
        # we'd have to pass the wrong type. Force the validation by
        # patching the runner to raise.
        runner = ctx.request_context.lifespan_context["job_runner"]
        original = runner.submit_entity_extraction

        def raise_invalid(params):
            raise ValueError("bad params")

        runner.submit_entity_extraction = raise_invalid  # type: ignore[assignment]
        try:
            result = journal_extract_entities_batch(ctx=ctx)
        finally:
            runner.submit_entity_extraction = original  # type: ignore[assignment]

        assert result["status"] == "failed"
        assert result["job_id"] is None
        assert result["error_message"] == "bad params"
        assert result["result"] is None

    def test_backfill_mood_scores_batch_happy_path(self, job_context):
        from journal.mcp_server import (
            journal_backfill_mood_scores_batch,
        )

        ctx, _repo, _runner, _extraction, mood = job_context

        result = journal_backfill_mood_scores_batch(
            mode="stale-only", start_date="2026-01-01", ctx=ctx
        )
        assert result["status"] == "succeeded"
        assert result["result"]["scored"] == 5
        assert result["result"]["skipped"] == 2
        assert mood.calls[0]["mode"] == "stale-only"

    def test_backfill_mood_scores_batch_invalid_mode(self, job_context):
        """Bad mode surfaces as a structured failed dict."""
        from journal.mcp_server import (
            journal_backfill_mood_scores_batch,
        )

        ctx, _repo, _runner, _extraction, _mood = job_context

        result = journal_backfill_mood_scores_batch(
            mode="nonsense", ctx=ctx
        )
        assert result["status"] == "failed"
        assert result["job_id"] is None
        assert "mode" in result["error_message"]

    def test_get_job_status_unknown_id(self, job_context):
        from journal.mcp_server import journal_get_job_status

        ctx, _repo, _runner, _extraction, _mood = job_context
        result = journal_get_job_status("not-a-real-id", ctx=ctx)
        assert result["error"] == "Job not found"
        assert result["job_id"] == "not-a-real-id"

    def test_get_job_status_after_success(self, job_context):
        from journal.mcp_server import (
            journal_extract_entities_batch,
            journal_get_job_status,
        )

        ctx, _repo, _runner, _extraction, _mood = job_context
        submitted = journal_extract_entities_batch(ctx=ctx)
        status = journal_get_job_status(submitted["job_id"], ctx=ctx)
        assert status["id"] == submitted["job_id"]
        assert status["type"] == "entity_extraction"
        assert status["status"] == "succeeded"
        assert status["progress_total"] == 1
        assert status["result"]["processed"] == 1
        assert status["error_message"] is None
