"""Conversation intent handlers."""

from __future__ import annotations

from journal.models import SearchResult
from journal.providers.answerer import AnswerResult, ConversationTurn
from journal.providers.intent_classifier import IntentResult
from journal.services.conversations.handlers import (
    LookupHandler,
    ReplyOutcome,
)


def _sr(entry_id: int, text: str, score: float = 1.0) -> SearchResult:
    return SearchResult(
        entry_id=entry_id, entry_date="2026-03-01", text=text, score=score,
        matching_chunks=[], snippet=None,
    )


class _Query:
    def __init__(self, **returns):
        self.returns = returns
        self.calls: list[tuple[str, dict]] = []

    def search_entries(self, **kw):
        self.calls.append(("search_entries", kw))
        return self.returns.get("search_entries", [])

    def get_topic_frequency(self, topic, start_date=None, end_date=None, user_id=None):
        self.calls.append(("get_topic_frequency", {"topic": topic}))
        return self.returns["get_topic_frequency"]

    def get_mood_trends(self, start_date=None, end_date=None, granularity="week", user_id=None):
        self.calls.append(("get_mood_trends", {}))
        return self.returns.get("get_mood_trends", [])


class _Answerer:
    def __init__(self, result: AnswerResult):
        self._result = result
        self.last_kwargs = None

    def continue_conversation(self, history, passages, *, context_note=None, retrieve=None):
        self.last_kwargs = {
            "passages": passages,
            "context_note": context_note,
            "retrieve": retrieve,
        }
        return self._result


def _history() -> list[ConversationTurn]:
    return [ConversationTurn(role="user", content="follow up")]


def test_lookup_handler_passes_retrieve_and_resolves_citations() -> None:
    query = _Query(search_entries=[_sr(7, "Back better now.")])
    answerer = _Answerer(AnswerResult("Around March.", True, [7]))
    handler = LookupHandler(query, answerer, passage_chars=800)
    intent = IntentResult(intent="lookup", search_query="back pain better")

    out = handler.handle(_history(), intent, user_id=1)

    assert isinstance(out, ReplyOutcome)
    assert out.answer == "Around March."
    assert out.citations[0]["entry_id"] == 7
    # lookup gives the answerer a one-hop retrieve callback
    assert answerer.last_kwargs["retrieve"] is not None
    # retrieval used the condensed search_query, larger candidate pool
    assert query.calls[0][1]["query"] == "back pain better"
    assert query.calls[0][1]["limit"] >= 15
