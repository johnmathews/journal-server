"""ConversationService — start + reply (re-retrieve, persist on success)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from journal.db.conversation_repository import SQLiteConversationRepository
from journal.db.factory import ConnectionFactory
from journal.db.migrations import run_migrations
from journal.models import SearchResult
from journal.providers.answerer import (
    AnswerResult,
    AnswerUnavailable,
    ConversationTurn,
)
from journal.services.conversations import (
    ConversationNotFoundError,
    ConversationService,
)

USER_A = 1


def _repo(tmp_path: Path) -> SQLiteConversationRepository:
    factory = ConnectionFactory(tmp_path / "c.db")
    run_migrations(factory.get())
    return SQLiteConversationRepository(factory)


def _result(entry_id: int, date: str, text: str) -> SearchResult:
    return SearchResult(
        entry_id=entry_id, entry_date=date, text=text, score=1.0,
        matching_chunks=[], snippet=None,
    )


class _FakeQuery:
    def __init__(self, results: list[SearchResult]):
        self._results = results
        self.calls: list[dict] = []

    def search_entries(self, **kwargs):
        self.calls.append(kwargs)
        return self._results


class _FakeAnswerer:
    def __init__(self, result: AnswerResult | None = None, exc: Exception | None = None):
        self._result = result
        self._exc = exc
        self.history: list[ConversationTurn] | None = None

    def continue_conversation(self, history, passages):
        self.history = history
        if self._exc is not None:
            raise self._exc
        return self._result


def _service(repo, query, answerer, **kw) -> ConversationService:
    return ConversationService(
        repository=repo,
        query_service=query,
        answerer=answerer,
        model=kw.pop("model", "claude-sonnet-4-6"),
        context_entries=kw.pop("context_entries", 8),
        passage_chars=kw.pop("passage_chars", 800),
    )


def _seed(svc) -> int:
    conv = svc.start(
        USER_A,
        question="when did my back hurt?",
        answer="On 2026-02-14.",
        citations=[{"entry_id": 42, "entry_date": "2026-02-14", "snippet": "back"}],
    )
    return conv.id


def test_start_seeds_user_and_assistant_turns(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    svc = _service(repo, _FakeQuery([]), _FakeAnswerer())
    conv = svc.start(
        USER_A, question="x" * 200, answer="ans", citations=[],
    )
    assert len(conv.title) <= 120  # title trimmed
    assert [m.role for m in conv.messages] == ["user", "assistant"]
    assert conv.messages[1].content == "ans"


def test_reply_combines_query_and_persists_both_turns(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    query = _FakeQuery([_result(7, "2026-03-01", "Back better now.")])
    answerer = _FakeAnswerer(AnswerResult("Around 2026-03-01.", True, [7]))
    svc = _service(repo, query, answerer, context_entries=8)
    cid = _seed(svc)

    msg = svc.reply(USER_A, cid, "and when did it get better?")

    # combined query = original question + "\n" + follow-up
    assert query.calls[0]["query"] == (
        "when did my back hurt?\nand when did it get better?"
    )
    assert query.calls[0]["limit"] == 8
    assert query.calls[0]["user_id"] == USER_A
    # history passed to answerer ends with the new user turn
    assert answerer.history[-1].role == "user"
    assert answerer.history[-1].content == "and when did it get better?"
    # returned assistant message + resolved citation
    assert msg.role == "assistant"
    assert msg.content == "Around 2026-03-01."
    assert msg.citations[0]["entry_id"] == 7
    # both turns persisted
    reloaded = repo.get(cid, USER_A)
    assert [m.role for m in reloaded.messages] == [
        "user", "assistant", "user", "assistant"
    ]


def test_reply_drops_citations_not_in_results(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    query = _FakeQuery([_result(7, "2026-03-01", "t")])
    answerer = _FakeAnswerer(AnswerResult("x", True, [7, 999]))
    svc = _service(repo, query, answerer)
    cid = _seed(svc)
    msg = svc.reply(USER_A, cid, "follow up")
    assert [c["entry_id"] for c in msg.citations] == [7]


def test_reply_unavailable_persists_nothing(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    query = _FakeQuery([_result(7, "2026-03-01", "t")])
    answerer = _FakeAnswerer(exc=AnswerUnavailable("boom"))
    svc = _service(repo, query, answerer)
    cid = _seed(svc)
    with pytest.raises(AnswerUnavailable):
        svc.reply(USER_A, cid, "follow up")
    reloaded = repo.get(cid, USER_A)
    assert len(reloaded.messages) == 2  # unchanged — only the seed turns


def test_reply_missing_conversation_raises_not_found(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    svc = _service(repo, _FakeQuery([]), _FakeAnswerer())
    with pytest.raises(ConversationNotFoundError):
        svc.reply(USER_A, 99999, "hello?")
