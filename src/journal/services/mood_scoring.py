"""Mood scoring service — bridges the `MoodScorer` provider and
the SQLite repository.

Kept as a thin layer with one job: run the scorer against an
entry, translate the provider's `RawMoodScore` objects into the
repository's `(dimension, score, confidence)` tuples, persist them
via `replace_mood_scores`, and log — but never raise — if the
scorer fails. Ingestion must never fail because mood scoring had
a bad day.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from journal.db.repository import EntryRepository
    from journal.providers.mood_scorer import MoodScorer
    from journal.services.mood_dimensions import MoodDimension

log = logging.getLogger(__name__)


class MoodScoringService:
    """Compose a `MoodScorer` provider with an `EntryRepository`
    and a cached view of the current `MoodDimension` set.

    Callers (ingestion, backfill CLI) pass the entry id + text and
    let the service handle scoring, persistence, and failure
    logging. The dimension tuple is held as an attribute rather
    than passed per call because every caller uses the same
    currently-loaded set and there's no test value in allowing
    per-call overrides.
    """

    def __init__(
        self,
        scorer: MoodScorer,
        repository: EntryRepository,
        dimensions: tuple[MoodDimension, ...],
    ) -> None:
        self._scorer = scorer
        self._repo = repository
        self._dimensions = dimensions

    @property
    def dimensions(self) -> tuple[MoodDimension, ...]:
        return self._dimensions

    def score_entry(self, entry_id: int, text: str) -> int:
        """Score a single entry and persist the results.

        Returns the number of scores written. Zero means either
        the scorer returned nothing usable or the underlying API
        call raised — in both cases a warning has been logged and
        the caller (typically ingestion) can continue unchanged.

        Never raises. Scoring is an enrichment, not a core step.
        """
        if not self._dimensions:
            log.debug(
                "Mood scoring skipped for entry %d: no dimensions loaded",
                entry_id,
            )
            return 0
        if not text or not text.strip():
            log.debug(
                "Mood scoring skipped for entry %d: empty text", entry_id
            )
            return 0

        try:
            raw_scores = self._scorer.score(text, self._dimensions)
        except Exception as e:
            log.warning(
                "Mood scorer failed for entry %d: %s", entry_id, e
            )
            return 0

        if not raw_scores:
            log.warning(
                "Mood scorer returned no scores for entry %d", entry_id
            )
            return 0

        rows: list[tuple[str, float, float | None, str | None]] = [
            (r.dimension_name, r.value, r.confidence, r.rationale)
            for r in raw_scores
        ]
        self._repo.replace_mood_scores(entry_id, rows)
        log.info(
            "Recorded %d mood scores for entry %d", len(rows), entry_id
        )
        return len(rows)
