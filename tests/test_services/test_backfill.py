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
    entry = repo.create_entry("2026-03-01", "photo", text, len(text.split()))
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
        entry = repo.create_entry("2026-03-02", "photo", "", 0)
        repo.update_chunk_count(entry.id, 0)

        result = backfill_chunk_counts(repo, chunker)

        assert result.skipped == 1
        assert result.updated == 0
        refreshed = repo.get_entry(entry.id)
        assert refreshed is not None
        assert refreshed.chunk_count == 0

    def test_populates_entry_chunks_table(self, repo, chunker):
        """Backfill must write chunks-with-offsets for legacy entries that
        have no rows in the entry_chunks table (pre-migration-0003)."""
        entry = _insert(
            repo,
            "Paragraph one with enough content.\n\nParagraph two with more words.",
        )
        # Sanity: no chunks persisted yet.
        assert repo.get_chunks(entry.id) == []

        backfill_chunk_counts(repo, chunker)

        persisted = repo.get_chunks(entry.id)
        assert len(persisted) >= 1
        # Offsets must be within the entry's text.
        entry_text = repo.get_entry(entry.id).final_text
        for chunk in persisted:
            assert 0 <= chunk.char_start <= chunk.char_end <= len(entry_text)
            assert chunk.token_count > 0

    def test_second_run_leaves_chunk_rows_unchanged(self, repo, chunker):
        """If chunks are already populated and count matches, backfill skips."""
        _insert(repo, "Short text.")
        backfill_chunk_counts(repo, chunker)  # first run populates
        result = backfill_chunk_counts(repo, chunker)  # second run should no-op
        assert result.updated == 0
        assert result.unchanged == 1

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
        empty = repo.create_entry("2026-03-03", "photo", "", 0)
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


class TestBackfillMoodScores:
    """T1.3b.v — backfill_mood_scores across stale-only, force,
    prune-retired, and dry-run modes."""

    @pytest.fixture
    def dims(self):
        from journal.services.mood_dimensions import MoodDimension

        return (
            MoodDimension(
                name="joy_sadness",
                positive_pole="joy",
                negative_pole="sadness",
                scale_type="bipolar",
                notes="...",
            ),
            MoodDimension(
                name="agency",
                positive_pole="agency",
                negative_pole="apathy",
                scale_type="unipolar",
                notes="...",
            ),
        )

    def _make_service(self, repo, dims, scorer_side_effect=None):
        from journal.providers.mood_scorer import RawMoodScore
        from journal.services.mood_scoring import MoodScoringService

        scorer = MagicMock()
        if scorer_side_effect is not None:
            scorer.score.side_effect = scorer_side_effect
        else:
            scorer.score.return_value = [
                RawMoodScore("joy_sadness", 0.5, 0.9),
                RawMoodScore("agency", 0.7, None),
            ]
        return MoodScoringService(scorer, repo, dims), scorer

    def test_stale_only_scores_only_missing_entries(self, repo, dims):
        from journal.services.backfill import backfill_mood_scores

        # Three entries, one already fully scored, one partially, one bare.
        e1 = repo.create_entry("2026-04-01", "photo", "already scored", 2)
        e2 = repo.create_entry("2026-04-02", "photo", "partial", 1)
        repo.create_entry("2026-04-03", "photo", "bare", 1)
        repo.replace_mood_scores(
            e1.id, [("joy_sadness", 0.5, None, None), ("agency", 0.6, None, None)]
        )
        repo.replace_mood_scores(e2.id, [("joy_sadness", 0.3, None, None)])

        svc, scorer = self._make_service(repo, dims)
        result = backfill_mood_scores(
            repository=repo, mood_scoring=svc, mode="stale-only"
        )

        # Only e2 and e3 should have been re-scored.
        assert result.scored == 2
        assert scorer.score.call_count == 2
        # e1 still has its original scores.
        assert len(repo.get_mood_scores(e1.id)) == 2

    def test_force_rescores_every_entry_even_if_complete(
        self, repo, dims
    ):
        from journal.services.backfill import backfill_mood_scores

        e1 = repo.create_entry("2026-04-01", "photo", "already scored", 2)
        repo.replace_mood_scores(
            e1.id, [("joy_sadness", 0.5, None, None), ("agency", 0.6, None, None)]
        )

        svc, scorer = self._make_service(repo, dims)
        result = backfill_mood_scores(
            repository=repo, mood_scoring=svc, mode="force"
        )

        assert result.scored == 1
        assert scorer.score.call_count == 1
        # The scores were replaced (to the mock return of 0.5 / 0.7).
        by_dim = {
            s.dimension: s.score for s in repo.get_mood_scores(e1.id)
        }
        assert by_dim["joy_sadness"] == 0.5
        assert by_dim["agency"] == 0.7

    def test_dry_run_does_not_call_scorer(self, repo, dims):
        from journal.services.backfill import backfill_mood_scores

        repo.create_entry("2026-04-01", "photo", "x", 1)
        svc, scorer = self._make_service(repo, dims)

        result = backfill_mood_scores(
            repository=repo,
            mood_scoring=svc,
            mode="stale-only",
            dry_run=True,
        )
        assert result.dry_run is True
        assert result.scored == 1
        scorer.score.assert_not_called()
        # And nothing was written.
        assert repo.get_mood_scores(1) == []

    def test_prune_retired_removes_off_config_dims(self, repo, dims):
        from journal.services.backfill import backfill_mood_scores

        e = repo.create_entry("2026-04-01", "photo", "x", 1)
        # Write two current + one retired dimension.
        repo.replace_mood_scores(
            e.id,
            [
                ("joy_sadness", 0.5, None, None),
                ("agency", 0.6, None, None),
                ("retired_dim", 0.1, None, None),
            ],
        )
        svc, _ = self._make_service(repo, dims)
        result = backfill_mood_scores(
            repository=repo,
            mood_scoring=svc,
            mode="stale-only",
            prune_retired=True,
        )
        assert result.pruned == 1
        names = {s.dimension for s in repo.get_mood_scores(e.id)}
        assert names == {"joy_sadness", "agency"}

    def test_prune_retired_dry_run_counts_without_deleting(
        self, repo, dims
    ):
        from journal.services.backfill import backfill_mood_scores

        e = repo.create_entry("2026-04-01", "photo", "x", 1)
        repo.replace_mood_scores(
            e.id,
            [("joy_sadness", 0.5, None, None), ("retired_dim", 0.1, None, None)],
        )
        svc, _ = self._make_service(repo, dims)
        result = backfill_mood_scores(
            repository=repo,
            mood_scoring=svc,
            mode="stale-only",
            prune_retired=True,
            dry_run=True,
        )
        assert result.pruned == 1
        # Still present.
        names = {s.dimension for s in repo.get_mood_scores(e.id)}
        assert "retired_dim" in names

    def test_date_filter_stale_only(self, repo, dims):
        from journal.services.backfill import backfill_mood_scores

        repo.create_entry("2026-02-15", "photo", "feb", 1)
        repo.create_entry("2026-03-15", "photo", "mar", 1)
        repo.create_entry("2026-04-15", "photo", "apr", 1)

        svc, scorer = self._make_service(repo, dims)
        result = backfill_mood_scores(
            repository=repo,
            mood_scoring=svc,
            mode="stale-only",
            start_date="2026-03-01",
            end_date="2026-03-31",
        )
        assert result.scored == 1
        assert scorer.score.call_count == 1

    def test_scorer_exception_captured_per_entry(self, repo, dims):
        from journal.services.backfill import backfill_mood_scores

        repo.create_entry("2026-04-01", "photo", "one", 1)
        repo.create_entry("2026-04-02", "photo", "two", 1)
        # The MoodScoringService swallows exceptions internally and
        # returns 0 — so the service itself never raises at the
        # backfill layer. But we still want to verify that a
        # returned-zero-score counts as a skip, not a crash.
        from journal.providers.mood_scorer import RawMoodScore

        scorer = MagicMock()
        scorer.score.side_effect = [
            [
                RawMoodScore("joy_sadness", 0.1, None),
                RawMoodScore("agency", 0.2, None),
            ],
            RuntimeError("boom"),
        ]
        from journal.services.mood_scoring import MoodScoringService

        svc = MoodScoringService(scorer, repo, dims)
        result = backfill_mood_scores(
            repository=repo, mood_scoring=svc, mode="stale-only"
        )
        # First entry scored OK, second entry returned 0 (warning
        # logged). The backfill counts both but distinguishes
        # scored vs skipped.
        assert result.scored == 1
        assert result.skipped == 1

    def test_empty_dimensions_is_noop(self, repo):
        from journal.services.backfill import backfill_mood_scores
        from journal.services.mood_scoring import MoodScoringService

        repo.create_entry("2026-04-01", "photo", "x", 1)
        svc = MoodScoringService(MagicMock(), repo, ())
        result = backfill_mood_scores(
            repository=repo, mood_scoring=svc, mode="stale-only"
        )
        assert result.scored == 0

    def test_invalid_mode_raises(self, repo, dims):
        from journal.services.backfill import backfill_mood_scores

        svc, _ = self._make_service(repo, dims)
        with pytest.raises(ValueError, match="Unsupported mode"):
            backfill_mood_scores(
                repository=repo, mood_scoring=svc, mode="maybe"
            )

    def test_backfill_mood_scores_calls_progress_callback(
        self, repo, dims
    ):
        from journal.services.backfill import backfill_mood_scores

        repo.create_entry("2026-04-01", "photo", "one", 1)
        repo.create_entry("2026-04-02", "photo", "two", 1)
        repo.create_entry("2026-04-03", "photo", "three", 1)

        svc, _ = self._make_service(repo, dims)

        calls: list[tuple[int, int]] = []
        backfill_mood_scores(
            repository=repo,
            mood_scoring=svc,
            mode="stale-only",
            on_progress=lambda c, t: calls.append((c, t)),
        )

        assert calls == [(0, 3), (1, 3), (2, 3), (3, 3)]

    def test_backfill_mood_scores_progress_in_dry_run(self, repo, dims):
        """Dry-run still advances through the entry set; progress must fire."""
        from journal.services.backfill import backfill_mood_scores

        repo.create_entry("2026-04-01", "photo", "one", 1)
        repo.create_entry("2026-04-02", "photo", "two", 1)

        svc, scorer = self._make_service(repo, dims)

        calls: list[tuple[int, int]] = []
        backfill_mood_scores(
            repository=repo,
            mood_scoring=svc,
            mode="stale-only",
            dry_run=True,
            on_progress=lambda c, t: calls.append((c, t)),
        )
        scorer.score.assert_not_called()
        assert calls == [(0, 2), (1, 2), (2, 2)]

    def test_backfill_mood_scores_raising_callback_does_not_break_batch(
        self, repo, dims, caplog
    ):
        from journal.services.backfill import backfill_mood_scores

        repo.create_entry("2026-04-01", "photo", "one", 1)
        repo.create_entry("2026-04-02", "photo", "two", 1)

        svc, _ = self._make_service(repo, dims)

        def boom(current: int, total: int) -> None:
            raise RuntimeError("callback kaboom")

        with caplog.at_level("WARNING", logger="journal.services.backfill"):
            result = backfill_mood_scores(
                repository=repo,
                mood_scoring=svc,
                mode="stale-only",
                on_progress=boom,
            )

        # Batch still scored both entries.
        assert result.scored == 2
        # And the callback failure was logged.
        assert any(
            "Progress callback failed" in rec.message for rec in caplog.records
        )
