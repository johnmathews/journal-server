"""Tests for the query intent classifier."""

from unittest.mock import MagicMock

import anthropic
import pytest

from journal.providers.query_classifier import (
    AnthropicQueryClassifier,
    HeuristicQueryClassifier,
    build_query_classifier,
)


class _FakeMessages:
    def __init__(self, text: str | None = None, exc: Exception | None = None):
        self._text = text
        self._exc = exc

    def create(self, **kwargs):
        if self._exc is not None:
            raise self._exc

        class _Block:
            text = self._text

        class _Resp:
            content = [_Block()]

        return _Resp()


def _clf(text: str | None = None, exc: Exception | None = None):
    c = AnthropicQueryClassifier(api_key="test", model="claude-haiku-4-5")
    c._client.messages = _FakeMessages(text=text, exc=exc)  # type: ignore[assignment]
    return c


def test_classifies_question_true():
    assert _clf(text="QUESTION").is_question("when did my back start hurting?") is True


def test_classifies_search_false():
    assert _clf(text="SEARCH").is_question("vienna atlas") is False


def test_unparseable_reply_falls_back_to_heuristic():
    # Model returns junk → fall back to the offline heuristic.
    assert _clf(text="banana").is_question("when did it start?") is True
    assert _clf(text="banana").is_question("vienna atlas") is False


def test_api_error_falls_back_to_heuristic():
    exc = anthropic.APIError("boom", request=MagicMock(), body=None)
    assert _clf(exc=exc).is_question("how often do I run?") is True
    assert _clf(exc=exc).is_question("atlas tennis") is False


def test_empty_query_is_not_a_question():
    assert _clf(text="QUESTION").is_question("   ") is False


def test_heuristic_classifier():
    h = HeuristicQueryClassifier()
    assert h.is_question("what did I do?") is True
    assert h.is_question("how often do I run") is True  # wh-word, no '?'
    assert h.is_question("vienna") is False
    assert h.is_question("") is False


def test_build_query_classifier_selects_adapter():
    assert isinstance(build_query_classifier("none"), HeuristicQueryClassifier)
    assert isinstance(
        build_query_classifier("anthropic", anthropic_api_key="k"),
        AnthropicQueryClassifier,
    )
    with pytest.raises(ValueError):
        build_query_classifier("anthropic", anthropic_api_key="")
    with pytest.raises(ValueError):
        build_query_classifier("bogus")
