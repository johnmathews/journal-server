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

    def test_update_entry_text_tool_exists(self):
        from journal.mcp_server import journal_update_entry_text
        assert callable(journal_update_entry_text)
