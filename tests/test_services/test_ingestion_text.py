"""Tests for text ingestion and skip_mood parameter."""

from unittest.mock import MagicMock

import pytest

from journal.db.repository import SQLiteEntryRepository
from journal.services.chunking import FixedTokenChunker
from journal.services.ingestion import IngestionService
from journal.vectorstore.store import InMemoryVectorStore


@pytest.fixture
def mock_embeddings():
    provider = MagicMock()
    provider.embed_texts.return_value = [[0.1, 0.2, 0.3]]
    return provider


@pytest.fixture
def mock_mood_scoring():
    scoring = MagicMock()
    scoring.score_entry.return_value = 5
    return scoring


@pytest.fixture
def ingestion_service(db_conn, mock_embeddings):
    repo = SQLiteEntryRepository(db_conn)
    vector_store = InMemoryVectorStore()
    return IngestionService(
        repository=repo,
        vector_store=vector_store,
        ocr_provider=MagicMock(),
        transcription_provider=MagicMock(),
        embeddings_provider=mock_embeddings,
        chunker=FixedTokenChunker(max_tokens=150, overlap_tokens=40),
    )


@pytest.fixture
def ingestion_with_mood(db_conn, mock_embeddings, mock_mood_scoring):
    repo = SQLiteEntryRepository(db_conn)
    vector_store = InMemoryVectorStore()
    return IngestionService(
        repository=repo,
        vector_store=vector_store,
        ocr_provider=MagicMock(),
        transcription_provider=MagicMock(),
        embeddings_provider=mock_embeddings,
        chunker=FixedTokenChunker(max_tokens=150, overlap_tokens=40),
        mood_scoring=mock_mood_scoring,
    )


class TestIngestText:
    def test_creates_entry_with_correct_fields(self, ingestion_service):
        entry = ingestion_service.ingest_text("Hello world today", "2026-04-12")
        assert entry.entry_date == "2026-04-12"
        assert entry.source_type == "text_entry"
        assert entry.raw_text == "Hello world today"
        assert entry.final_text == "Hello world today"
        assert entry.word_count == 3

    def test_custom_source_type(self, ingestion_service):
        entry = ingestion_service.ingest_text("Some text", "2026-04-12", "imported_text_file")
        assert entry.source_type == "imported_text_file"

    def test_chunks_and_embeds(self, ingestion_service, mock_embeddings):
        entry = ingestion_service.ingest_text(
            "A sufficiently long journal entry that has enough words to be chunked properly.",
            "2026-04-12",
        )
        mock_embeddings.embed_texts.assert_called_once()
        assert entry.chunk_count > 0

    def test_strips_whitespace(self, ingestion_service):
        entry = ingestion_service.ingest_text("  Hello world  \n\n  ", "2026-04-12")
        assert entry.raw_text == "Hello world"

    def test_empty_text_raises(self, ingestion_service):
        with pytest.raises(ValueError, match="must not be empty"):
            ingestion_service.ingest_text("   ", "2026-04-12")

    def test_skip_mood_true(self, ingestion_with_mood, mock_mood_scoring):
        ingestion_with_mood.ingest_text("Hello world", "2026-04-12", skip_mood=True)
        mock_mood_scoring.score_entry.assert_not_called()

    def test_skip_mood_false(self, ingestion_with_mood, mock_mood_scoring):
        ingestion_with_mood.ingest_text("Hello world", "2026-04-12", skip_mood=False)
        mock_mood_scoring.score_entry.assert_called_once()
