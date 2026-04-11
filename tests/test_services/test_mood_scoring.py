"""Tests for the MoodScoringService."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from journal.db.repository import SQLiteEntryRepository
from journal.providers.mood_scorer import RawMoodScore
from journal.services.mood_dimensions import MoodDimension
from journal.services.mood_scoring import MoodScoringService


@pytest.fixture
def dims() -> tuple[MoodDimension, ...]:
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


@pytest.fixture
def repo(db_conn) -> SQLiteEntryRepository:
    return SQLiteEntryRepository(db_conn)


class TestScoreEntry:
    def test_happy_path_writes_scores(self, repo, dims) -> None:
        scorer = MagicMock()
        scorer.score.return_value = [
            RawMoodScore("joy_sadness", 0.5, 0.9),
            RawMoodScore("agency", 0.7, None),
        ]
        service = MoodScoringService(scorer, repo, dims)
        entry = repo.create_entry("2026-04-01", "ocr", "test", 1)

        n = service.score_entry(entry.id, "test text")

        assert n == 2
        stored = repo.get_mood_scores(entry.id)
        assert len(stored) == 2
        by_dim = {s.dimension: s for s in stored}
        assert by_dim["joy_sadness"].score == 0.5
        assert by_dim["joy_sadness"].confidence == 0.9
        assert by_dim["agency"].score == 0.7
        assert by_dim["agency"].confidence is None

    def test_empty_dimensions_is_noop(self, repo) -> None:
        scorer = MagicMock()
        service = MoodScoringService(scorer, repo, ())
        entry = repo.create_entry("2026-04-01", "ocr", "test", 1)

        n = service.score_entry(entry.id, "text")

        assert n == 0
        scorer.score.assert_not_called()
        assert repo.get_mood_scores(entry.id) == []

    def test_empty_text_is_noop(self, repo, dims) -> None:
        scorer = MagicMock()
        service = MoodScoringService(scorer, repo, dims)
        entry = repo.create_entry("2026-04-01", "ocr", "test", 1)

        assert service.score_entry(entry.id, "") == 0
        assert service.score_entry(entry.id, "   \n  ") == 0
        scorer.score.assert_not_called()

    def test_scorer_exception_is_swallowed(
        self, repo, dims, caplog
    ) -> None:
        """Ingestion must never fail because mood scoring had a bad
        day. The service logs and returns 0."""
        scorer = MagicMock()
        scorer.score.side_effect = RuntimeError("anthropic is down")
        service = MoodScoringService(scorer, repo, dims)
        entry = repo.create_entry("2026-04-01", "ocr", "test", 1)

        with caplog.at_level("WARNING"):
            n = service.score_entry(entry.id, "some text")

        assert n == 0
        assert any("anthropic is down" in m for m in caplog.messages)
        assert repo.get_mood_scores(entry.id) == []

    def test_scorer_returning_empty_list_logs_and_returns_zero(
        self, repo, dims, caplog
    ) -> None:
        scorer = MagicMock()
        scorer.score.return_value = []
        service = MoodScoringService(scorer, repo, dims)
        entry = repo.create_entry("2026-04-01", "ocr", "test", 1)

        with caplog.at_level("WARNING"):
            assert service.score_entry(entry.id, "text") == 0
        assert any("no scores" in m.lower() for m in caplog.messages)

    def test_re_scoring_replaces_previous_values(
        self, repo, dims
    ) -> None:
        scorer = MagicMock()
        scorer.score.return_value = [
            RawMoodScore("joy_sadness", 0.5, None),
            RawMoodScore("agency", 0.7, None),
        ]
        service = MoodScoringService(scorer, repo, dims)
        entry = repo.create_entry("2026-04-01", "ocr", "test", 1)

        service.score_entry(entry.id, "first pass")

        # Second pass with different scores — replace, not append.
        scorer.score.return_value = [
            RawMoodScore("joy_sadness", -0.2, None),
            RawMoodScore("agency", 0.3, None),
        ]
        service.score_entry(entry.id, "second pass")

        stored = repo.get_mood_scores(entry.id)
        assert len(stored) == 2
        by_dim = {s.dimension: s.score for s in stored}
        assert by_dim["joy_sadness"] == -0.2
        assert by_dim["agency"] == 0.3
