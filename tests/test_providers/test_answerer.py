"""Tests for the answer-synthesis provider."""

from unittest.mock import MagicMock

import anthropic
import pytest

from journal.providers.answerer import (
    NO_MATCH_MESSAGE,
    AnswerPassage,
    AnswerUnavailable,
    AnthropicAnswerer,
    NoopAnswerer,
    build_answerer,
)


class _FakeMessages:
    def __init__(self, raw: str | None = None, exc: Exception | None = None):
        self._raw = raw
        self._exc = exc
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._exc is not None:
            raise self._exc

        class _Block:
            text = self._raw

        class _Resp:
            content = [_Block()]

        return _Resp()


def _answerer(raw: str | None = None, exc: Exception | None = None) -> AnthropicAnswerer:
    a = AnthropicAnswerer(api_key="test", model="claude-sonnet-4-6")
    a._client.messages = _FakeMessages(raw=raw, exc=exc)  # type: ignore[assignment]
    return a


PASSAGES = [
    AnswerPassage(entry_id=42, entry_date="2026-02-14", text="My lower back started hurting."),
    AnswerPassage(entry_id=7, entry_date="2026-03-01", text="Back still sore after the gym."),
]


def test_parses_answer_and_filters_invented_ids():
    raw = (
        '{"answer": "Your back pain began on 2026-02-14.", "answered": true,'
        ' "cited_entry_ids": [42, 999]}'
    )
    result = _answerer(raw=raw).answer("when did my back start hurting?", PASSAGES)
    assert result.answered is True
    assert "2026-02-14" in result.answer
    # 999 was never a candidate — it must be dropped.
    assert result.cited_entry_ids == [42]


def test_answered_false_passthrough():
    raw = f'{{"answer": "{NO_MATCH_MESSAGE}", "answered": false, "cited_entry_ids": []}}'
    result = _answerer(raw=raw).answer("did I go to Mars?", PASSAGES)
    assert result.answered is False
    assert result.cited_entry_ids == []


def test_malformed_output_raises():
    with pytest.raises(AnswerUnavailable):
        _answerer(raw="not json at all").answer("q", PASSAGES)


def test_api_error_raises():
    exc = anthropic.APIError("boom", request=MagicMock(), body=None)
    with pytest.raises(AnswerUnavailable):
        _answerer(exc=exc).answer("q", PASSAGES)


def test_noop_answerer_is_not_answered():
    result = NoopAnswerer().answer("q", PASSAGES)
    assert result.answered is False
    assert result.cited_entry_ids == []


def test_empty_passages_returns_no_match():
    result = _answerer().answer("q", [])
    assert result.answered is False
    assert result.answer == NO_MATCH_MESSAGE


def test_string_entry_ids_are_coerced():
    raw = '{"answer": "ok", "answered": true, "cited_entry_ids": ["42", 7]}'
    result = _answerer(raw=raw).answer("q", PASSAGES)
    assert result.cited_entry_ids == [42, 7]


def test_build_answerer_selects_adapter():
    assert isinstance(build_answerer("none"), NoopAnswerer)
    assert isinstance(
        build_answerer("anthropic", anthropic_api_key="k", model="claude-sonnet-4-6"),
        AnthropicAnswerer,
    )
    with pytest.raises(ValueError):
        build_answerer("anthropic", anthropic_api_key="")
    with pytest.raises(ValueError):
        build_answerer("bogus")
