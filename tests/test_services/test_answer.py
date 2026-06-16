"""Tests for the answer-synthesis service."""

from journal.models import SearchResult
from journal.providers.answerer import (
    NO_MATCH_MESSAGE,
    AnswerPassage,
    AnswerResult,
)
from journal.services.answer import AnswerService


class _FakeQuery:
    def __init__(self, results: list[SearchResult]):
        self._results = results
        self.calls: list[dict] = []

    def search_entries(self, **kwargs):
        self.calls.append(kwargs)
        return self._results


class _FakeAnswerer:
    def __init__(self, result: AnswerResult):
        self._result = result
        self.passages: list[AnswerPassage] | None = None

    def answer(self, question, passages):
        self.passages = passages
        return self._result


def _result(entry_id: int, date: str, text: str) -> SearchResult:
    return SearchResult(
        entry_id=entry_id, entry_date=date, text=text, score=1.0,
        matching_chunks=[], snippet=None,
    )


def test_no_results_short_circuits_without_calling_answerer():
    answerer = _FakeAnswerer(AnswerResult("should not be used", True, [1]))
    svc = AnswerService(_FakeQuery([]), answerer, model="claude-sonnet-4-6")
    resp = svc.answer_question("anything?")
    assert resp.answered is False
    assert resp.answer == NO_MATCH_MESSAGE
    assert resp.citations == []
    assert answerer.passages is None  # answerer never called


def test_builds_passages_and_resolves_citations():
    results = [
        _result(42, "2026-02-14", "My lower back started hurting today."),
        _result(7, "2026-03-01", "Back still sore."),
    ]
    answerer = _FakeAnswerer(
        AnswerResult("Your back pain began on 2026-02-14.", True, [42])
    )
    svc = AnswerService(
        _FakeQuery(results), answerer, model="claude-sonnet-4-6",
        context_entries=8,
    )
    resp = svc.answer_question("when did my back start hurting?")

    assert svc._query.calls[0] == {
        "query": "when did my back start hurting?",
        "start_date": None,
        "end_date": None,
        "limit": 8,
        "offset": 0,
        "user_id": None,
    }
    assert [p.entry_id for p in answerer.passages] == [42, 7]
    assert resp.answered is True
    assert len(resp.citations) == 1
    assert resp.citations[0].entry_id == 42
    assert resp.citations[0].entry_date == "2026-02-14"
    assert "back" in resp.citations[0].snippet.lower()
    assert resp.model == "claude-sonnet-4-6"


def test_forwards_date_and_user_filters():
    results = [_result(1, "2026-02-14", "back")]
    answerer = _FakeAnswerer(AnswerResult("ok", True, [1]))
    svc = AnswerService(_FakeQuery(results), answerer, model="m", context_entries=3)
    svc.answer_question(
        "q?", start_date="2026-01-01", end_date="2026-03-01", user_id=99
    )
    assert svc._query.calls[0] == {
        "query": "q?",
        "start_date": "2026-01-01",
        "end_date": "2026-03-01",
        "limit": 3,
        "offset": 0,
        "user_id": 99,
    }
