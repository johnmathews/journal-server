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

from journal.services.conversations.passages import (
    build_citations,
    select_passages,
)

if TYPE_CHECKING:
    from journal.providers.answerer import Answerer, AnswerPassage, ConversationTurn  # noqa: F401
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
