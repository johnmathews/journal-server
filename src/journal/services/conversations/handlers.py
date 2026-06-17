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
from journal.services.conversations.passages import (
    build_citations,
    select_passages,
    window_passage,
)

if TYPE_CHECKING:
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
