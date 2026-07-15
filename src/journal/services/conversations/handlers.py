"""Per-intent handlers for conversation replies.

Each handler turns a classified `IntentResult` + conversation history
into a `ReplyOutcome` (answer text + resolved citations). Handlers
depend only on a `QueryService`-shaped object and an `Answerer`, so they
are unit-testable with stubs. The grounding contract is identical across
handlers — only the material fed to the answerer differs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from journal.providers.answerer import AnswerPassage
from journal.services.conversations.dimensions import resolve_dimension
from journal.services.conversations.passages import (
    build_citations,
    select_passages,
    window_passage,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from journal.models import MoodTrend
    from journal.providers.answerer import Answerer, ConversationTurn
    from journal.providers.intent_classifier import IntentResult
    from journal.services.query import QueryService

#: Candidate pool retrieved before adaptive selection trims it.
_CANDIDATE_POOL = 20
_PASSAGE_FLOOR = 3
_PASSAGE_CEILING = 15
_SNIPPET_CHARS = 160


@dataclass(frozen=True)
class ReplyOutcome:
    """A handler's result: the answer plus persisted-shape citations."""

    answer: str
    answered: bool
    citations: list[dict]


class LookupHandler:
    """Today's hybrid path + adaptive count + one bounded re-retrieval."""

    def __init__(
        self,
        query_service: QueryService,
        answerer: Answerer,
        *,
        passage_chars: int = 800,
    ) -> None:
        self._query = query_service
        self._answerer = answerer
        self._passage_chars = passage_chars

    def handle(
        self,
        history: list[ConversationTurn],
        intent: IntentResult,
        user_id: int,
    ) -> ReplyOutcome:
        results = self._query.search_entries(
            query=intent.search_query,
            limit=_CANDIDATE_POOL,
            offset=0,
            user_id=user_id,
        )
        passages = select_passages(
            results,
            max_chars=self._passage_chars,
            floor=_PASSAGE_FLOOR,
            ceiling=_PASSAGE_CEILING,
        )
        by_id = {r.entry_id: (r.entry_date, r.text) for r in results}

        def _retrieve(query: str) -> list[AnswerPassage]:
            more = self._query.search_entries(
                query=query, limit=_CANDIDATE_POOL, offset=0, user_id=user_id
            )
            for r in more:
                by_id[r.entry_id] = (r.entry_date, r.text)
            return select_passages(
                more,
                max_chars=self._passage_chars,
                floor=_PASSAGE_FLOOR,
                ceiling=_PASSAGE_CEILING,
            )

        result = self._answerer.continue_conversation(
            history, passages, retrieve=_retrieve
        )
        return ReplyOutcome(
            answer=result.answer,
            answered=result.answered,
            citations=build_citations(
                result.cited_entry_ids, by_id, snippet_chars=_SNIPPET_CHARS
            ),
        )


def _entry_text(entry: object) -> str:
    return getattr(entry, "final_text", None) or getattr(entry, "raw_text", None) or ""


class AggregateHandler:
    """Count/frequency questions — answer leads with the number."""

    def __init__(
        self,
        query_service: QueryService,
        answerer: Answerer,
        *,
        passage_chars: int = 800,
    ) -> None:
        self._query = query_service
        self._answerer = answerer
        self._passage_chars = passage_chars

    def handle(
        self,
        history: list[ConversationTurn],
        intent: IntentResult,
        user_id: int,
    ) -> ReplyOutcome:
        topic = intent.topic or intent.search_query
        freq = self._query.get_topic_frequency(
            topic,
            start_date=intent.start_date,
            end_date=intent.end_date,
            user_id=user_id,
        )
        note = (
            f"The phrase/topic '{freq.topic}' appears in {freq.count} "
            f"journal entries"
            + (
                f" between {intent.start_date} and {intent.end_date}."
                if intent.start_date and intent.end_date
                else "."
            )
        )
        passages = [
            AnswerPassage(
                entry_id=e.id,
                entry_date=e.entry_date,
                text=_entry_text(e)[: self._passage_chars],
            )
            for e in freq.entries
        ]
        by_id = {e.id: (e.entry_date, _entry_text(e)) for e in freq.entries}
        result = self._answerer.continue_conversation(
            history, passages, context_note=note
        )
        return ReplyOutcome(
            answer=result.answer,
            answered=result.answered,
            citations=build_citations(
                result.cited_entry_ids, by_id, snippet_chars=_SNIPPET_CHARS
            ),
        )


class TemporalHandler:
    """When-did-X questions — retrieve date-ascending so the earliest wins."""

    def __init__(
        self,
        query_service: QueryService,
        answerer: Answerer,
        *,
        passage_chars: int = 800,
    ) -> None:
        self._query = query_service
        self._answerer = answerer
        self._passage_chars = passage_chars

    def handle(
        self,
        history: list[ConversationTurn],
        intent: IntentResult,
        user_id: int,
    ) -> ReplyOutcome:
        results = self._query.search_entries(
            query=intent.search_query,
            start_date=intent.start_date,
            end_date=intent.end_date,
            limit=_PASSAGE_CEILING,
            offset=0,
            user_id=user_id,
            sort="date_asc",
        )
        # Bypass adaptive selection: keep all date-ordered results (the
        # earliest evidencing entry must survive), windowed in place.
        passages = [
            AnswerPassage(
                entry_id=r.entry_id,
                entry_date=r.entry_date,
                text=window_passage(r, self._passage_chars),
            )
            for r in results
        ]
        by_id = {r.entry_id: (r.entry_date, r.text) for r in results}
        result = self._answerer.continue_conversation(history, passages)
        return ReplyOutcome(
            answer=result.answer,
            answered=result.answered,
            citations=build_citations(
                result.cited_entry_ids, by_id, snippet_chars=_SNIPPET_CHARS
            ),
        )


def _trend_note(trends: list[MoodTrend], resolved: str | None) -> str:
    """Serialize mood trends into a context note for the answerer.

    When `resolved` names a single facet, only that facet's series is
    summarized. When it is `None` (all dimensions), each dimension's
    series is labeled so the answerer can tell them apart — previously
    every facet's points were interleaved into one unlabeled series.
    """
    if resolved is not None:
        relevant = [t for t in trends if t.dimension == resolved]
        if not relevant:
            return (
                f"No mood-trend data is available for '{resolved}' "
                "in this period."
            )
        series = ", ".join(f"{t.period}={t.avg_score:.2f}" for t in relevant)
        return f"Mood trend for '{resolved}' (period=avg_score): {series}."

    if not trends:
        return "No mood-trend data is available for this period."
    parts: list[str] = []
    for dim in dict.fromkeys(t.dimension for t in trends):
        series = ", ".join(
            f"{t.period}={t.avg_score:.2f}" for t in trends if t.dimension == dim
        )
        parts.append(f"{dim}: {series}")
    return (
        "Mood trends by dimension (dimension: period=avg_score): "
        + "; ".join(parts)
        + "."
    )


class TrendHandler:
    """Change-over-time / mood questions — summarize the series as a note."""

    def __init__(
        self,
        query_service: QueryService,
        answerer: Answerer,
        *,
        passage_chars: int = 800,
        dimension_names: Sequence[str] = (),
    ) -> None:
        self._query = query_service
        self._answerer = answerer
        self._passage_chars = passage_chars
        self._dimension_names = tuple(dimension_names)

    def handle(
        self,
        history: list[ConversationTurn],
        intent: IntentResult,
        user_id: int,
    ) -> ReplyOutcome:
        trends = self._query.get_mood_trends(
            start_date=intent.start_date,
            end_date=intent.end_date,
            user_id=user_id,
        )
        # Validate the LLM-emitted dimension against the facets we know
        # about — the configured set plus any present in the data — so a
        # near-miss ("energy" → "energy_vigor") resolves instead of
        # yielding an empty series. Unresolvable/ambiguous → all dims.
        valid = list(
            dict.fromkeys([*self._dimension_names, *(t.dimension for t in trends)])
        )
        resolved = resolve_dimension(intent.dimension, valid)
        note = _trend_note(trends, resolved)
        results = self._query.search_entries(
            query=intent.search_query,
            limit=_PASSAGE_FLOOR,
            offset=0,
            user_id=user_id,
        )
        passages = select_passages(
            results,
            max_chars=self._passage_chars,
            floor=_PASSAGE_FLOOR,
            ceiling=_PASSAGE_FLOOR,
        )
        by_id = {r.entry_id: (r.entry_date, r.text) for r in results}
        result = self._answerer.continue_conversation(
            history, passages, context_note=note
        )
        return ReplyOutcome(
            answer=result.answer,
            answered=result.answered,
            citations=build_citations(
                result.cited_entry_ids, by_id, snippet_chars=_SNIPPET_CHARS
            ),
        )
