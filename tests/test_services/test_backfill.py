"""Tests for the backfill service."""

from unittest.mock import MagicMock

import pytest

from journal.db.repository import SQLiteEntryRepository
from journal.services.backfill import (
    BackfillResult,
    RechunkResult,
    backfill_chunk_counts,
    rechunk_entries,
)
from journal.services.chunking import FixedTokenChunker


@pytest.fixture
def repo(db_conn):
    return SQLiteEntryRepository(db_conn)


@pytest.fixture
def chunker():
    return FixedTokenChunker(max_tokens=150, overlap_tokens=40)


def _insert(repo: SQLiteEntryRepository, text: str, *, final_text: str | None = None):
    """Helper: insert an entry and (optionally) set final_text, always with chunk_count=0."""
    entry = repo.create_entry("2026-03-01", "ocr", text, len(text.split()))
    if final_text is not None:
        repo.update_final_text(entry.id, final_text, len(final_text.split()), 0)
    # Force stale chunk_count so backfill has something to do.
    repo.update_chunk_count(entry.id, 0)
    return entry


class TestBackfillChunkCounts:
    def test_sets_chunk_count_from_raw_text(self, repo, chunker):
        entry = _insert(repo, "Short seeded entry text.")

        result = backfill_chunk_counts(repo, chunker)

        refreshed = repo.get_entry(entry.id)
        assert refreshed is not None
        assert refreshed.chunk_count >= 1
        assert result.updated == 1
        assert result.unchanged == 0
        assert result.skipped == 0
        assert result.errors == []

    def test_prefers_final_text_over_raw_text(self, repo, chunker):
        entry = _insert(
            repo,
            "raw",
            final_text="This is the corrected version of the entry with more words.",
        )

        result = backfill_chunk_counts(repo, chunker)

        refreshed = repo.get_entry(entry.id)
        assert refreshed is not None
        assert refreshed.chunk_count >= 1
        assert result.updated == 1

    def test_leaves_already_correct_rows_unchanged(self, repo, chunker):
        entry = _insert(repo, "Short text.")
        # Run once to populate the correct count, then again.
        backfill_chunk_counts(repo, chunker)

        result = backfill_chunk_counts(repo, chunker)

        assert result.updated == 0
        assert result.unchanged == 1
        refreshed = repo.get_entry(entry.id)
        assert refreshed is not None
        assert refreshed.chunk_count >= 1

    def test_skips_entries_with_no_text(self, repo, chunker):
        entry = repo.create_entry("2026-03-02", "ocr", "", 0)
        repo.update_chunk_count(entry.id, 0)

        result = backfill_chunk_counts(repo, chunker)

        assert result.skipped == 1
        assert result.updated == 0
        refreshed = repo.get_entry(entry.id)
        assert refreshed is not None
        assert refreshed.chunk_count == 0

    def test_handles_long_text_producing_multiple_chunks(self, repo):
        long_paragraph = (
            "Sentence one with a few words. " * 60
        ).strip()  # ~360 words, definitely > 150 tokens
        entry = _insert(repo, long_paragraph)

        backfill_chunk_counts(
            repo, FixedTokenChunker(max_tokens=150, overlap_tokens=40)
        )

        refreshed = repo.get_entry(entry.id)
        assert refreshed is not None
        assert refreshed.chunk_count > 1

    def test_processes_multiple_entries(self, repo, chunker):
        _insert(repo, "First entry.")
        _insert(repo, "Second entry with more words in it.")
        _insert(repo, "Third entry also short.")

        result = backfill_chunk_counts(repo, chunker)

        assert result.updated == 3
        assert result.unchanged == 0

    def test_chunker_exception_is_captured_in_errors(self, repo):
        _insert(repo, "Entry one.")
        _insert(repo, "Entry two.")

        # Build a flaky chunker that raises on the second call.
        call_count = {"n": 0}
        real_chunker = FixedTokenChunker(max_tokens=150, overlap_tokens=40)

        class FlakyChunker:
            def chunk(self, text: str) -> list[str]:
                call_count["n"] += 1
                if call_count["n"] == 2:
                    raise RuntimeError("boom")
                return real_chunker.chunk(text)

        result = backfill_chunk_counts(repo, FlakyChunker())

        assert result.updated == 1
        assert len(result.errors) == 1
        assert "boom" in result.errors[0]

    def test_result_dataclass_defaults(self):
        r = BackfillResult()
        assert r.updated == 0
        assert r.unchanged == 0
        assert r.skipped == 0
        assert r.errors == []


class TestRechunkEntries:
    def test_updates_every_entry_via_ingestion_service(self, repo):
        _insert(repo, "Entry one.")
        _insert(repo, "Entry two.")
        _insert(repo, "Entry three.")

        mock_ingestion = MagicMock()
        mock_ingestion.rechunk_entry.return_value = 2  # each returns 2 chunks

        result = rechunk_entries(mock_ingestion, repo)

        assert result.updated == 3
        assert result.skipped == 0
        assert result.errors == []
        assert result.new_total_chunks == 6
        assert mock_ingestion.rechunk_entry.call_count == 3

    def test_skips_entries_with_no_text(self, repo):
        _insert(repo, "Has text.")
        empty = repo.create_entry("2026-03-03", "ocr", "", 0)
        repo.update_chunk_count(empty.id, 0)

        mock_ingestion = MagicMock()
        mock_ingestion.rechunk_entry.return_value = 1

        result = rechunk_entries(mock_ingestion, repo)

        assert result.updated == 1
        assert result.skipped == 1
        assert mock_ingestion.rechunk_entry.call_count == 1

    def test_dry_run_propagates_flag(self, repo):
        _insert(repo, "Entry to dry-run.")

        mock_ingestion = MagicMock()
        mock_ingestion.rechunk_entry.return_value = 1

        rechunk_entries(mock_ingestion, repo, dry_run=True)

        mock_ingestion.rechunk_entry.assert_called_once()
        _args, kwargs = mock_ingestion.rechunk_entry.call_args
        assert kwargs["dry_run"] is True

    def test_per_entry_errors_do_not_abort_batch(self, repo):
        _insert(repo, "Good one.")
        _insert(repo, "Bad one.")
        _insert(repo, "Another good one.")

        mock_ingestion = MagicMock()
        mock_ingestion.rechunk_entry.side_effect = [
            1,                                  # first succeeds
            RuntimeError("embedding failed"),   # second raises
            3,                                  # third still processed
        ]

        result = rechunk_entries(mock_ingestion, repo)

        assert result.updated == 2
        assert len(result.errors) == 1
        assert "embedding failed" in result.errors[0]

    def test_old_and_new_total_chunks_tracked(self, repo):
        e1 = _insert(repo, "First entry.")
        e2 = _insert(repo, "Second entry.")
        # Pretend the stored chunk counts are 2 and 3 respectively.
        repo.update_chunk_count(e1.id, 2)
        repo.update_chunk_count(e2.id, 3)

        mock_ingestion = MagicMock()
        # New chunker produces more chunks — 4 and 5.
        mock_ingestion.rechunk_entry.side_effect = [4, 5]

        result = rechunk_entries(mock_ingestion, repo)

        assert result.old_total_chunks == 5  # 2 + 3
        assert result.new_total_chunks == 9  # 4 + 5

    def test_rechunk_result_defaults(self):
        r = RechunkResult()
        assert r.updated == 0
        assert r.skipped == 0
        assert r.old_total_chunks == 0
        assert r.new_total_chunks == 0
        assert r.errors == []
