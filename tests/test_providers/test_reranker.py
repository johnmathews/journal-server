"""Tests for the Reranker Protocol and adapters."""

import json
from unittest.mock import MagicMock, patch

import anthropic
import pytest

from journal.providers.reranker import (
    AnthropicReranker,
    NoopReranker,
    RerankCandidate,
    Reranker,
    build_reranker,
)


def _candidates(*texts: str) -> list[RerankCandidate]:
    return [RerankCandidate(id=str(i), text=t) for i, t in enumerate(texts, start=1)]


class TestNoopReranker:
    def test_implements_protocol(self) -> None:
        assert isinstance(NoopReranker(), Reranker)

    def test_empty_input_returns_empty(self) -> None:
        assert NoopReranker().rerank("q", [], top_k=10) == []

    def test_preserves_input_order(self) -> None:
        cands = _candidates("a", "b", "c")
        results = NoopReranker().rerank("q", cands, top_k=3)
        assert [r.id for r in results] == ["1", "2", "3"]

    def test_descending_scores(self) -> None:
        cands = _candidates("a", "b", "c", "d")
        results = NoopReranker().rerank("q", cands, top_k=4)
        scores = [r.score for r in results]
        # Strictly descending and bounded.
        assert scores == sorted(scores, reverse=True)
        assert scores[0] == 1.0
        assert 0.0 <= scores[-1] < 1.0

    def test_top_k_truncates(self) -> None:
        cands = _candidates("a", "b", "c", "d", "e")
        results = NoopReranker().rerank("q", cands, top_k=2)
        assert [r.id for r in results] == ["1", "2"]

    def test_single_candidate(self) -> None:
        results = NoopReranker().rerank("q", _candidates("only"), top_k=10)
        assert len(results) == 1
        assert results[0].score == 1.0
        assert results[0].reason is None


class TestAnthropicReranker:
    def _make(self) -> tuple[AnthropicReranker, MagicMock]:
        """Build the reranker with the SDK class patched, and return the
        fake client alongside it so tests can configure ``messages.create``
        without reaching into ``rr._client``.
        """
        fake_client = MagicMock(name="anthropic.Anthropic")
        with patch(
            "journal.providers.reranker.anthropic.Anthropic",
            return_value=fake_client,
        ):
            rr = AnthropicReranker(api_key="test-key", model="claude-haiku-4-5")
        return rr, fake_client

    def _mock_response(self, payload: dict) -> MagicMock:
        response = MagicMock()
        block = MagicMock()
        block.text = json.dumps(payload)
        response.content = [block]
        return response

    def test_implements_protocol(self) -> None:
        rr, _client = self._make()
        assert isinstance(rr, Reranker)

    def test_empty_candidates_returns_empty(self) -> None:
        rr, client = self._make()
        client.messages.create.assert_not_called()
        assert rr.rerank("q", [], top_k=5) == []
        # Still should not have called the API.
        client.messages.create.assert_not_called()

    def test_zero_top_k_returns_empty(self) -> None:
        rr, client = self._make()
        assert rr.rerank("q", _candidates("a"), top_k=0) == []
        client.messages.create.assert_not_called()

    def test_parses_well_formed_response(self) -> None:
        rr, client = self._make()
        client.messages.create.return_value = self._mock_response(
            {
                "ranking": [
                    {"index": 2, "score": 0.91, "reason": "directly answers"},
                    {"index": 1, "score": 0.4, "reason": "tangential"},
                    {"index": 3, "score": 0.1, "reason": "off topic"},
                ]
            }
        )
        cands = _candidates("alpha", "bravo", "charlie")
        results = rr.rerank("query", cands, top_k=2)
        assert [r.id for r in results] == ["2", "1"]
        assert results[0].score == pytest.approx(0.91)
        assert results[0].reason == "directly answers"

    def test_passes_query_and_candidates_to_api(self) -> None:
        rr, client = self._make()
        client.messages.create.return_value = self._mock_response(
            {"ranking": [{"index": 1, "score": 0.5, "reason": "match"}]}
        )
        rr.rerank("vienna trip", _candidates("trip to vienna in march"), top_k=1)
        kwargs = client.messages.create.call_args.kwargs
        assert kwargs["model"] == "claude-haiku-4-5"
        # System prompt is passed as a list with cache_control set.
        assert isinstance(kwargs["system"], list)
        assert kwargs["system"][0]["cache_control"]["type"] == "ephemeral"
        # User message contains the query and the numbered candidate.
        user_msg = kwargs["messages"][0]["content"]
        assert "vienna trip" in user_msg
        assert "[1] trip to vienna in march" in user_msg

    def test_ignores_indices_out_of_range(self) -> None:
        rr, client = self._make()
        client.messages.create.return_value = self._mock_response(
            {
                "ranking": [
                    {"index": 99, "score": 1.0, "reason": "fake"},
                    {"index": 0, "score": 1.0, "reason": "fake"},
                    {"index": 1, "score": 0.7, "reason": "real"},
                ]
            }
        )
        results = rr.rerank("q", _candidates("only one"), top_k=5)
        assert [r.id for r in results] == ["1"]

    def test_ignores_duplicate_indices(self) -> None:
        rr, client = self._make()
        client.messages.create.return_value = self._mock_response(
            {
                "ranking": [
                    {"index": 1, "score": 0.9, "reason": "first"},
                    {"index": 1, "score": 0.5, "reason": "dup"},
                    {"index": 2, "score": 0.4, "reason": "second"},
                ]
            }
        )
        cands = _candidates("a", "b")
        results = rr.rerank("q", cands, top_k=5)
        assert [r.id for r in results] == ["1", "2"]

    def test_falls_back_to_noop_on_api_error(self) -> None:
        rr, client = self._make()
        client.messages.create.side_effect = anthropic.APIError(
            request=MagicMock(), message="boom", body=None,
        )
        cands = _candidates("a", "b")
        results = rr.rerank("q", cands, top_k=2)
        # Noop fallback preserves input order.
        assert [r.id for r in results] == ["1", "2"]

    def test_falls_back_on_malformed_json(self) -> None:
        rr, client = self._make()
        bad = MagicMock()
        bad_block = MagicMock()
        bad_block.text = "not json at all"
        bad.content = [bad_block]
        client.messages.create.return_value = bad
        results = rr.rerank("q", _candidates("a", "b"), top_k=2)
        assert [r.id for r in results] == ["1", "2"]

    def test_falls_back_when_ranking_missing(self) -> None:
        rr, client = self._make()
        client.messages.create.return_value = self._mock_response({"items": []})
        results = rr.rerank("q", _candidates("a", "b"), top_k=2)
        assert [r.id for r in results] == ["1", "2"]

    def test_tolerates_prose_around_json(self) -> None:
        # Models occasionally wrap output despite the system prompt.
        rr, client = self._make()
        bad = MagicMock()
        block = MagicMock()
        block.text = (
            "Sure! Here is the ranking:\n"
            '{"ranking": [{"index": 1, "score": 0.8, "reason": "ok"}]}\n'
            "Hope that helps!"
        )
        bad.content = [block]
        client.messages.create.return_value = bad
        results = rr.rerank("q", _candidates("a"), top_k=1)
        assert [r.id for r in results] == ["1"]
        assert results[0].score == pytest.approx(0.8)

    def test_truncates_long_candidate_text(self) -> None:
        rr, client = self._make()
        client.messages.create.return_value = self._mock_response(
            {"ranking": [{"index": 1, "score": 0.5, "reason": "ok"}]}
        )
        long_text = "x" * 5000
        rr.rerank("q", [RerankCandidate(id="1", text=long_text)], top_k=1)
        user_msg = client.messages.create.call_args.kwargs["messages"][0][
            "content"
        ]
        # The full 5000 chars must not appear verbatim — truncated with ellipsis.
        assert long_text not in user_msg
        assert "…" in user_msg


class TestBuildReranker:
    def test_none_returns_noop(self) -> None:
        rr = build_reranker("none")
        assert isinstance(rr, NoopReranker)

    def test_noop_alias(self) -> None:
        assert isinstance(build_reranker("noop"), NoopReranker)

    def test_anthropic_requires_api_key(self) -> None:
        with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
            build_reranker("anthropic", anthropic_api_key="")

    def test_anthropic_builds_with_key(self) -> None:
        with patch("journal.providers.reranker.anthropic.Anthropic"):
            rr = build_reranker(
                "anthropic", anthropic_api_key="test-key", model="claude-haiku-4-5"
            )
        assert isinstance(rr, AnthropicReranker)
        assert rr.model == "claude-haiku-4-5"

    def test_unknown_name_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown reranker"):
            build_reranker("voyage")
