"""Tests for MCP server tool functions."""

from unittest.mock import MagicMock

import pytest

from journal.auth import _current_user_id
from journal.db.repository import SQLiteEntryRepository
from journal.services.query import QueryService
from journal.vectorstore.store import InMemoryVectorStore


@pytest.fixture(autouse=True)
def _set_test_user():
    """Set the contextvar so get_current_user_id() works in MCP tools."""
    token = _current_user_id.set(1)
    yield
    _current_user_id.reset(token)

# Test the tool functions directly by calling the underlying service logic
# (The MCP framework handles routing; we test the business logic)


@pytest.fixture
def repo(factory):
    return SQLiteEntryRepository(factory)


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
    e1 = repo.create_entry("2026-03-22", "photo", "Met Atlas in Vienna for coffee", 6)
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


class TestMoodTrendsBar:
    """The MCP tool renders an ASCII bar per trend row. It must scale
    the bar to each dimension's range so a unipolar dimension (range
    [0, 1]) does not render a half-bar at score 0 the way a bipolar
    neutral (range [-1, +1]) does. Regression test for the bar-scaling
    bug (W9)."""

    @staticmethod
    def _trailing_bar(line: str) -> str:
        """Extract the trailing run of ``+`` characters from a row."""
        stripped = line.rstrip()
        i = len(stripped)
        while i > 0 and stripped[i - 1] == "+":
            i -= 1
        return stripped[i:]

    def _bars(self, output: str) -> dict[tuple[str, str], str]:
        """Map ``(period, dimension) -> bar string`` from tool output."""
        bars: dict[tuple[str, str], str] = {}
        for line in output.splitlines():
            if "|" not in line or ":" not in line:
                continue
            head, _, tail = line.partition("|")
            period = head.strip()
            dimension = tail.split(":", 1)[0].strip()
            bars[(period, dimension)] = self._trailing_bar(line)
        return bars

    @pytest.fixture
    def bar_ctx(self, repo, vector_store, mock_embeddings):
        from journal.services.mood_dimensions import MoodDimension

        # bipolar neutral (0.0), unipolar absent (0.0), unipolar full (1.0)
        e0 = repo.create_entry("2026-03-22", "text", "neutral bipolar day", 3)
        e1 = repo.create_entry("2026-03-23", "text", "no frustration at all", 4)
        e2 = repo.create_entry("2026-03-24", "text", "totally frustrated", 2)
        repo.add_mood_score(e0.id, "overall", 0.0)
        repo.add_mood_score(e1.id, "frustration", 0.0)
        repo.add_mood_score(e2.id, "frustration", 1.0)

        dims = (
            MoodDimension(
                name="overall",
                positive_pole="good",
                negative_pole="bad",
                scale_type="bipolar",
                notes="",
            ),
            MoodDimension(
                name="frustration",
                positive_pole="frustrated",
                negative_pole="calm",
                scale_type="unipolar",
                notes="",
            ),
        )
        ctx = MagicMock()
        ctx.request_context.lifespan_context = {
            "query": QueryService(repo, vector_store, mock_embeddings),
            "mood_dimensions": dims,
        }
        return ctx

    def test_unipolar_zero_shorter_than_bipolar_zero(self, bar_ctx):
        from journal.mcp_server import journal_get_mood_trends

        out = journal_get_mood_trends(granularity="day", ctx=bar_ctx)
        bars = self._bars(out)

        bipolar_neutral = bars[("2026-03-22", "overall")]
        unipolar_zero = bars[("2026-03-23", "frustration")]
        unipolar_full = bars[("2026-03-24", "frustration")]

        # The core of the bug: unipolar 0 must be visibly shorter than a
        # bipolar neutral (which sits at the midpoint).
        assert len(unipolar_zero) < len(bipolar_neutral)
        assert len(unipolar_zero) <= 1  # empty / near-empty
        assert len(bipolar_neutral) == 5  # half of the 10-wide bar
        assert len(unipolar_full) == 10  # full bar
        assert len(unipolar_full) > len(bipolar_neutral)

    def test_docstring_describes_all_time_default(self):
        from journal.mcp_server import journal_get_mood_trends

        doc = journal_get_mood_trends.__doc__ or ""
        # The service applies no default window (None => all-time); the
        # docstring must not claim a "3 months ago" default.
        assert "3 months ago" not in doc


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
        entry = repo.create_entry("2026-04-01", "photo", "Some OCR text", 3)
        assert entry.final_text == "Some OCR text"

    def test_list_entries_uses_final_text(self, repo):
        """list_entries returns entries with final_text for previews."""
        entry = repo.create_entry("2026-04-01", "photo", "Original OCR text", 3)
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
    """Verify MCP tool functions are importable."""

    def test_ingest_media_tool_exists(self):
        from journal.mcp_server import journal_ingest_media
        assert callable(journal_ingest_media)

    def test_ingest_media_from_url_tool_exists(self):
        from journal.mcp_server import journal_ingest_media_from_url
        assert callable(journal_ingest_media_from_url)

    def test_ingest_text_tool_exists(self):
        from journal.mcp_server import journal_ingest_text
        assert callable(journal_ingest_text)

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
        from journal.db.factory import ConnectionFactory
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
        factory = ConnectionFactory(db_path)
        run_migrations(factory.get())
        repo = SQLiteJobRepository(factory)

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

        runner.shutdown(wait=True, cancel_futures=False)
        factory.close_current()

    def test_extract_entities_batch_happy_path(self, job_context):
        from journal.mcp_server import journal_extract_entities_batch

        ctx, repo, runner, extraction, _mood = job_context

        result = journal_extract_entities_batch(
            start_date="2026-01-01", ctx=ctx
        )
        assert result["status"] == "succeeded"
        assert result["job_id"]
        assert result["error_message"] is None
        assert result["result"]["entries_processed"] == 1
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

        def raise_invalid(params, **kwargs):
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
        assert status["result"]["entries_processed"] == 1
        assert status["error_message"] is None


class TestIngestTextTool:
    """Integration tests for the journal_ingest_text MCP tool."""

    @pytest.fixture
    def ingest_ctx(self, factory):
        from journal.db.repository import SQLiteEntryRepository
        from journal.services.chunking import FixedTokenChunker
        from journal.services.ingestion import IngestionService
        from journal.vectorstore.store import InMemoryVectorStore

        mock_emb = MagicMock()
        mock_emb.embed_texts.return_value = [[0.1, 0.2, 0.3]]

        service = IngestionService(
            repository=SQLiteEntryRepository(factory),
            vector_store=InMemoryVectorStore(),
            ocr_provider=MagicMock(),
            transcription_provider=MagicMock(),
            embeddings_provider=mock_emb,
            chunker=FixedTokenChunker(max_tokens=150, overlap_tokens=40),
            preprocess_images=False,
        )

        ctx = MagicMock()
        ctx.request_context.lifespan_context = {"ingestion": service}
        return ctx

    def test_creates_entry_from_text(self, ingest_ctx):
        from journal.mcp_server import journal_ingest_text

        result = journal_ingest_text(
            text="Had a great day hiking in the mountains",
            date="2026-04-15",
            ctx=ingest_ctx,
        )
        assert "Text entry created successfully" in result
        assert "2026-04-15" in result
        assert "text_entry" in result

    def test_defaults_date_to_today(self, ingest_ctx):
        from journal.mcp_server import journal_ingest_text

        result = journal_ingest_text(
            text="A simple entry without a date",
            ctx=ingest_ctx,
        )
        assert "Text entry created successfully" in result
        assert "ID:" in result

    def test_custom_source_type(self, ingest_ctx):
        from journal.mcp_server import journal_ingest_text

        result = journal_ingest_text(
            text="Imported from a text file",
            date="2026-04-15",
            source_type="imported_text_file",
            ctx=ingest_ctx,
        )
        assert "imported_text_file" in result

    def test_empty_text_returns_error(self, ingest_ctx):
        from journal.mcp_server import journal_ingest_text

        result = journal_ingest_text(
            text="   ",
            date="2026-04-15",
            ctx=ingest_ctx,
        )
        assert "Error:" in result
        assert "empty" in result.lower()

    def test_reports_word_count(self, ingest_ctx):
        from journal.mcp_server import journal_ingest_text

        result = journal_ingest_text(
            text="one two three four five",
            date="2026-04-15",
            ctx=ingest_ctx,
        )
        assert "Words: 5" in result

    def test_reports_preview(self, ingest_ctx):
        from journal.mcp_server import journal_ingest_text

        result = journal_ingest_text(
            text="Had a great day hiking in the mountains",
            date="2026-04-15",
            ctx=ingest_ctx,
        )
        assert "hiking" in result


class _FakeOCR:
    """OCR fake returning a real OCRResult (MagicMock would not survive
    ``ocr_result.text.strip()`` in the image ingest path)."""

    def __init__(self, texts: list[str] | None = None) -> None:
        self._texts = texts or ["Met a friend for coffee and we talked for hours"]
        self.calls = 0

    def extract(self, image_data: bytes, media_type: str, page_role=None):
        from journal.providers.ocr import OCRResult

        text = self._texts[min(self.calls, len(self._texts) - 1)]
        self.calls += 1
        return OCRResult(text=text)


class _FakeTranscription:
    """Transcription fake returning a real TranscriptionResult."""

    def __init__(self, text: str = "Talked about the garden and the weather") -> None:
        self._text = text

    def transcribe(self, audio_data: bytes, media_type: str, language: str = "en"):
        from journal.models import TranscriptionResult

        return TranscriptionResult(text=self._text)


@pytest.fixture
def media_ingest_ctx(factory):
    """Real IngestionService over in-memory repo/vector store with
    real-result OCR/transcription fakes; faked MCP Context around it."""
    from journal.db.repository import SQLiteEntryRepository
    from journal.services.chunking import FixedTokenChunker
    from journal.services.ingestion import IngestionService
    from journal.vectorstore.store import InMemoryVectorStore

    mock_emb = MagicMock()
    mock_emb.embed_texts.return_value = [[0.1, 0.2, 0.3]]

    service = IngestionService(
        repository=SQLiteEntryRepository(factory),
        vector_store=InMemoryVectorStore(),
        ocr_provider=_FakeOCR(),
        transcription_provider=_FakeTranscription(),
        embeddings_provider=mock_emb,
        chunker=FixedTokenChunker(max_tokens=150, overlap_tokens=40),
        preprocess_images=False,
    )

    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"ingestion": service}
    return ctx, service


def _b64(data: bytes) -> str:
    import base64

    return base64.b64encode(data).decode("ascii")


class TestIngestMediaTool:
    """Behavioral tests for the journal_ingest_media MCP tool."""

    def test_image_happy_path(self, media_ingest_ctx):
        from journal.mcp_server import journal_ingest_media

        ctx, _service = media_ingest_ctx
        result = journal_ingest_media(
            source_type="image",
            data_base64=_b64(b"fake-image-bytes"),
            media_type="image/jpeg",
            date="2026-04-15",
            ctx=ctx,
        )
        assert "Entry ingested successfully" in result
        assert "2026-04-15" in result
        assert "Source: photo" in result
        assert "coffee" in result

    def test_voice_happy_path(self, media_ingest_ctx):
        from journal.mcp_server import journal_ingest_media

        ctx, _service = media_ingest_ctx
        result = journal_ingest_media(
            source_type="voice",
            data_base64=_b64(b"fake-audio-bytes"),
            media_type="audio/mp3",
            date="2026-04-15",
            ctx=ctx,
        )
        assert "Entry ingested successfully" in result
        assert "Source: voice" in result
        assert "garden" in result

    def test_invalid_source_type(self, media_ingest_ctx):
        from journal.mcp_server import journal_ingest_media

        ctx, _service = media_ingest_ctx
        result = journal_ingest_media(
            source_type="document",
            data_base64=_b64(b"whatever"),
            media_type="application/pdf",
            ctx=ctx,
        )
        assert result == "Invalid source_type 'document'. Must be 'image' or 'voice'."

    def test_malformed_base64_returns_error_string(self, media_ingest_ctx):
        """Malformed base64 must map to an "Error: ..." string, not raise.

        Note: b64decode is lenient about some junk (non-alphabet chars are
        discarded before decoding), so use an input that actually raises.
        """
        from journal.mcp_server import journal_ingest_media

        ctx, _service = media_ingest_ctx
        result = journal_ingest_media(
            source_type="image",
            data_base64="abc",  # 3 data chars -> binascii.Error
            media_type="image/jpeg",
            ctx=ctx,
        )
        assert result.startswith("Error:")

    def test_duplicate_image_returns_error_string(self, media_ingest_ctx):
        """Second upload of the same image maps the service ValueError to
        an "Error: ..." string like sibling tools do."""
        from journal.mcp_server import journal_ingest_media

        ctx, _service = media_ingest_ctx
        payload = _b64(b"same-image-bytes")
        first = journal_ingest_media(
            source_type="image",
            data_base64=payload,
            media_type="image/jpeg",
            date="2026-04-15",
            ctx=ctx,
        )
        assert "Entry ingested successfully" in first

        second = journal_ingest_media(
            source_type="image",
            data_base64=payload,
            media_type="image/jpeg",
            date="2026-04-16",
            ctx=ctx,
        )
        assert second.startswith("Error: Page 1 has already been uploaded")

    def test_date_defaults_to_today(self, media_ingest_ctx):
        from datetime import date as date_type

        from journal.mcp_server import journal_ingest_media

        ctx, _service = media_ingest_ctx
        result = journal_ingest_media(
            source_type="image",
            data_base64=_b64(b"undated-image-bytes"),
            media_type="image/jpeg",
            ctx=ctx,
        )
        assert "Entry ingested successfully" in result
        assert date_type.today().isoformat() in result


class TestIngestMultiPageTool:
    """Behavioral tests for the journal_ingest_multi_page MCP tool."""

    def test_happy_path_two_pages_one_entry(self, media_ingest_ctx):
        from journal.mcp_server import journal_ingest_multi_page

        ctx, service = media_ingest_ctx
        service.replace_ocr(
            _FakeOCR(["First page about the morning walk",
                      "Second page about the evening meal"])
        )
        result = journal_ingest_multi_page(
            images_base64=[_b64(b"page-one-bytes"), _b64(b"page-two-bytes")],
            media_types=["image/jpeg", "image/jpeg"],
            date="2026-04-15",
            ctx=ctx,
        )
        assert "Multi-page entry ingested successfully" in result
        assert "Pages: 2" in result
        # Both pages combined into ONE entry.
        assert service.repository.count_entries() == 1
        assert "morning walk" in result
        assert "evening meal" in result

    def test_length_mismatch_returns_error_string(self, media_ingest_ctx):
        from journal.mcp_server import journal_ingest_multi_page

        ctx, _service = media_ingest_ctx
        result = journal_ingest_multi_page(
            images_base64=[_b64(b"page-one-bytes"), _b64(b"page-two-bytes")],
            media_types=["image/jpeg"],
            date="2026-04-15",
            ctx=ctx,
        )
        assert result == "Error: images_base64 and media_types must have the same length."

    def test_malformed_base64_returns_error_string(self, media_ingest_ctx):
        from journal.mcp_server import journal_ingest_multi_page

        ctx, _service = media_ingest_ctx
        result = journal_ingest_multi_page(
            images_base64=["abc"],  # 3 data chars -> binascii.Error
            media_types=["image/jpeg"],
            ctx=ctx,
        )
        assert result.startswith("Error:")

    def test_duplicate_page_returns_error_string(self, media_ingest_ctx):
        from journal.mcp_server import journal_ingest_multi_page

        ctx, _service = media_ingest_ctx
        payload = _b64(b"reused-page-bytes")
        first = journal_ingest_multi_page(
            images_base64=[payload],
            media_types=["image/jpeg"],
            date="2026-04-15",
            ctx=ctx,
        )
        assert "Multi-page entry ingested successfully" in first

        second = journal_ingest_multi_page(
            images_base64=[payload],
            media_types=["image/jpeg"],
            date="2026-04-16",
            ctx=ctx,
        )
        assert second.startswith("Error: Page 1 has already been uploaded")

    def test_date_defaults_to_today(self, media_ingest_ctx):
        from datetime import date as date_type

        from journal.mcp_server import journal_ingest_multi_page

        ctx, _service = media_ingest_ctx
        result = journal_ingest_multi_page(
            images_base64=[_b64(b"undated-page-bytes")],
            media_types=["image/jpeg"],
            ctx=ctx,
        )
        assert "Multi-page entry ingested successfully" in result
        assert date_type.today().isoformat() in result


class TestIngestMediaFromUrlTool:
    """Behavioral tests for journal_ingest_media_from_url.

    URL fetching is monkeypatched at the service-method boundary —
    no network access."""

    @pytest.fixture
    def url_entry(self, media_ingest_ctx):
        _ctx, service = media_ingest_ctx
        return service.ingest_text(
            "A pre-made entry for the URL tool",
            "2026-04-20",
            "photo",
            skip_mood=True,
            user_id=1,
        )

    def test_image_url_happy_path(self, media_ingest_ctx, url_entry, monkeypatch):
        from journal.mcp_server import journal_ingest_media_from_url

        ctx, service = media_ingest_ctx
        calls: dict[str, object] = {}

        def fake_ingest(url, date, media_type, *, user_id):
            calls["url"] = url
            calls["date"] = date
            return url_entry

        monkeypatch.setattr(service, "ingest_image_from_url", fake_ingest)
        result = journal_ingest_media_from_url(
            source_type="image",
            url="https://example.com/page.jpg",
            date="2026-04-20",
            ctx=ctx,
        )
        assert "Entry ingested successfully" in result
        assert calls["url"] == "https://example.com/page.jpg"
        assert calls["date"] == "2026-04-20"

    def test_voice_url_happy_path(self, media_ingest_ctx, url_entry, monkeypatch):
        from journal.mcp_server import journal_ingest_media_from_url

        ctx, service = media_ingest_ctx
        calls: dict[str, object] = {}

        def fake_ingest(url, date, media_type, language, *, user_id):
            calls["url"] = url
            calls["language"] = language
            return url_entry

        monkeypatch.setattr(service, "ingest_voice_from_url", fake_ingest)
        result = journal_ingest_media_from_url(
            source_type="voice",
            url="https://example.com/note.mp3",
            language="nl",
            ctx=ctx,
        )
        assert "Entry ingested successfully" in result
        assert calls["url"] == "https://example.com/note.mp3"
        assert calls["language"] == "nl"

    def test_invalid_source_type(self, media_ingest_ctx):
        from journal.mcp_server import journal_ingest_media_from_url

        ctx, _service = media_ingest_ctx
        result = journal_ingest_media_from_url(
            source_type="document",
            url="https://example.com/file.pdf",
            ctx=ctx,
        )
        assert result == "Invalid source_type 'document'. Must be 'image' or 'voice'."

    def test_service_value_error_maps_to_error_string(
        self, media_ingest_ctx, monkeypatch
    ):
        """Duplicate-style ValueErrors from the service must map to an
        "Error: ..." string instead of raising through the tool."""
        from journal.mcp_server import journal_ingest_media_from_url

        ctx, service = media_ingest_ctx

        def raise_duplicate(url, date, media_type, *, user_id):
            raise ValueError(
                "This image has already been uploaded in another entry. "
                "Delete the existing entry first if you want to re-upload."
            )

        monkeypatch.setattr(service, "ingest_image_from_url", raise_duplicate)
        result = journal_ingest_media_from_url(
            source_type="image",
            url="https://example.com/dup.jpg",
            ctx=ctx,
        )
        assert result.startswith("Error: This image has already been uploaded")
