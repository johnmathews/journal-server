"""Answer-synthesis service.

Orchestrates the opt-in `POST /api/search/answer` flow: reuse the hybrid
search to retrieve the top-N entries for the question, hand them to the
`Answerer` as dated passages, and resolve the cited entry ids back to
entries so the webapp can render clickable citations.

Retrieval reuses `QueryService.search_entries`, so the answer rides the
same BM25+dense+RRF+rerank pipeline (and result cache) as the list
endpoint — no separate retrieval path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from journal.providers.answerer import NO_MATCH_MESSAGE, AnswerPassage

if TYPE_CHECKING:
    from journal.providers.answerer import Answerer
    from journal.providers.query_classifier import QueryClassifier
    from journal.services.query import QueryService

#: Length of the citation preview snippet (chars of the entry text).
_SNIPPET_CHARS = 160


@dataclass(frozen=True)
class AnswerCitation:
    entry_id: int
    entry_date: str
    snippet: str


@dataclass(frozen=True)
class AnswerResponse:
    question: str
    answer: str
    answered: bool
    citations: list[AnswerCitation]
    model: str
    #: True when the query was classified as an answerable question.
    #: False means it was a plain keyword/entity search — no synthesis
    #: was attempted, `answer` is empty, and the client shows no answer.
    is_question: bool


class AnswerService:
    def __init__(
        self,
        query_service: QueryService,
        answerer: Answerer,
        classifier: QueryClassifier,
        *,
        model: str,
        context_entries: int = 8,
        passage_chars: int = 800,
    ) -> None:
        self._query = query_service
        self._answerer = answerer
        self._classifier = classifier
        self._model = model
        self._context_entries = context_entries
        self._passage_chars = passage_chars

    def answer_question(
        self,
        question: str,
        start_date: str | None = None,
        end_date: str | None = None,
        user_id: int | None = None,
    ) -> AnswerResponse:
        # Cheap intent gate first: a plain keyword/entity search never
        # pays for retrieval + synthesis here.
        if not self._classifier.is_question(question):
            return AnswerResponse(
                question=question,
                answer="",
                answered=False,
                citations=[],
                model=self._model,
                is_question=False,
            )

        results = self._query.search_entries(
            query=question,
            start_date=start_date,
            end_date=end_date,
            limit=self._context_entries,
            offset=0,
            user_id=user_id,
        )
        if not results:
            return AnswerResponse(
                question=question,
                answer=NO_MATCH_MESSAGE,
                answered=False,
                citations=[],
                model=self._model,
                is_question=True,
            )

        passages = [
            AnswerPassage(
                entry_id=r.entry_id,
                entry_date=r.entry_date,
                text=r.text[: self._passage_chars],
            )
            for r in results
        ]
        result = self._answerer.answer(question, passages)

        by_id = {r.entry_id: r for r in results}
        citations = [
            AnswerCitation(
                entry_id=eid,
                entry_date=by_id[eid].entry_date,
                snippet=by_id[eid].text[:_SNIPPET_CHARS].strip(),
            )
            for eid in result.cited_entry_ids
            if eid in by_id
        ]
        return AnswerResponse(
            question=question,
            answer=result.answer,
            answered=result.answered,
            citations=citations,
            model=self._model,
            is_question=True,
        )
