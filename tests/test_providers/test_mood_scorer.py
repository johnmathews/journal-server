"""Tests for the Anthropic mood scorer adapter."""

from __future__ import annotations

import dataclasses
from typing import Any
from unittest.mock import MagicMock

import pytest

from journal.providers.mood_scorer import (
    AnthropicMoodScorer,
    RawMoodScore,
    _extract_first_json_object,
    build_system_prompt,
    build_tool_schema,
)
from journal.services.mood_dimensions import MoodDimension


@pytest.fixture
def dims() -> tuple[MoodDimension, ...]:
    return (
        MoodDimension(
            name="joy_sadness",
            positive_pole="joy",
            negative_pole="sadness",
            scale_type="bipolar",
            notes="Joyful vs sad",
        ),
        MoodDimension(
            name="agency",
            positive_pole="agency",
            negative_pole="apathy",
            scale_type="unipolar",
            notes="Agency vs absence of agency",
        ),
    )


def _tool_block(payload: dict[str, Any]) -> MagicMock:
    block = MagicMock()
    block.type = "tool_use"
    block.input = payload
    return block


def _text_block(text: str) -> MagicMock:
    block = MagicMock()
    block.type = "text"
    block.text = text
    return block


class TestBuildSystemPrompt:
    def test_includes_every_dimension(self, dims) -> None:
        prompt = build_system_prompt(dims)
        assert "joy_sadness" in prompt
        assert "agency" in prompt
        assert "bipolar" in prompt
        assert "unipolar" in prompt

    def test_inlines_notes_verbatim(self, dims) -> None:
        prompt = build_system_prompt(dims)
        assert "Joyful vs sad" in prompt

    def test_explains_unipolar_semantics(self, dims) -> None:
        prompt = build_system_prompt(dims)
        # Must flag that unipolar 0 is absence, not neutral.
        assert "absence" in prompt.lower()


class TestBuildToolSchema:
    def test_every_dimension_is_required(self, dims) -> None:
        tool = build_tool_schema(dims)
        assert tool["name"] == "record_mood_scores"
        schema = tool["input_schema"]
        assert set(schema["required"]) == {"joy_sadness", "agency"}

    def test_bipolar_bounds(self, dims) -> None:
        tool = build_tool_schema(dims)
        value_schema = tool["input_schema"]["properties"]["joy_sadness"][
            "properties"
        ]["value"]
        assert value_schema["minimum"] == -1.0
        assert value_schema["maximum"] == 1.0

    def test_unipolar_bounds(self, dims) -> None:
        tool = build_tool_schema(dims)
        value_schema = tool["input_schema"]["properties"]["agency"][
            "properties"
        ]["value"]
        assert value_schema["minimum"] == 0.0
        assert value_schema["maximum"] == 1.0

    def test_confidence_is_optional(self, dims) -> None:
        tool = build_tool_schema(dims)
        facet = tool["input_schema"]["properties"]["joy_sadness"]
        # `value` is required, `confidence` is NOT.
        assert facet["required"] == ["value"]
        assert "confidence" in facet["properties"]


class TestScoreHappyPath:
    def test_parses_full_tool_response(self, dims) -> None:
        client = MagicMock()
        client.messages.create.return_value = MagicMock(
            content=[
                _tool_block(
                    {
                        "joy_sadness": {
                            "value": 0.6,
                            "confidence": 0.9,
                        },
                        "agency": {
                            "value": 0.8,
                            "confidence": 0.85,
                        },
                    }
                )
            ]
        )
        scorer = AnthropicMoodScorer(api_key="test-key")
        scorer._client = client

        results = scorer.score("Today was pretty good.", dims)

        assert len(results) == 2
        by_name = {r.dimension_name: r for r in results}
        assert by_name["joy_sadness"].value == 0.6
        assert by_name["joy_sadness"].confidence == 0.9
        assert by_name["agency"].value == 0.8
        assert by_name["agency"].confidence == 0.85

    def test_missing_confidence_yields_none(self, dims) -> None:
        client = MagicMock()
        client.messages.create.return_value = MagicMock(
            content=[
                _tool_block(
                    {
                        "joy_sadness": {"value": 0.2},
                        "agency": {"value": 0.5},
                    }
                )
            ]
        )
        scorer = AnthropicMoodScorer(api_key="test-key")
        scorer._client = client

        results = scorer.score("...", dims)

        for r in results:
            assert r.confidence is None

    def test_clamps_out_of_range_values(self, dims) -> None:
        client = MagicMock()
        client.messages.create.return_value = MagicMock(
            content=[
                _tool_block(
                    {
                        "joy_sadness": {"value": 2.5},  # beyond +1
                        "agency": {"value": -0.3},      # beyond 0
                    }
                )
            ]
        )
        scorer = AnthropicMoodScorer(api_key="test-key")
        scorer._client = client

        results = scorer.score("...", dims)
        by_name = {r.dimension_name: r for r in results}
        # Bipolar clamped to +1, unipolar clamped to 0.
        assert by_name["joy_sadness"].value == 1.0
        assert by_name["agency"].value == 0.0


class TestScoreEdgeCases:
    def test_empty_dimensions_returns_empty_without_calling_api(
        self,
    ) -> None:
        client = MagicMock()
        scorer = AnthropicMoodScorer(api_key="test-key")
        scorer._client = client
        assert scorer.score("text", ()) == []
        client.messages.create.assert_not_called()

    def test_response_with_no_tool_use_returns_empty(self, dims) -> None:
        client = MagicMock()
        client.messages.create.return_value = MagicMock(content=[])
        scorer = AnthropicMoodScorer(api_key="test-key")
        scorer._client = client
        assert scorer.score("text", dims) == []

    def test_response_with_none_message_returns_empty(self, dims) -> None:
        client = MagicMock()
        client.messages.create.return_value = None
        scorer = AnthropicMoodScorer(api_key="test-key")
        scorer._client = client
        assert scorer.score("text", dims) == []

    def test_missing_facet_is_skipped_with_warning(
        self, dims, caplog
    ) -> None:
        client = MagicMock()
        client.messages.create.return_value = MagicMock(
            content=[_tool_block({"joy_sadness": {"value": 0.5}})]
        )
        scorer = AnthropicMoodScorer(api_key="test-key")
        scorer._client = client

        with caplog.at_level("WARNING"):
            results = scorer.score("text", dims)
        assert len(results) == 1
        assert results[0].dimension_name == "joy_sadness"
        assert any("agency" in m for m in caplog.messages)

    def test_non_numeric_value_is_skipped(self, dims) -> None:
        client = MagicMock()
        client.messages.create.return_value = MagicMock(
            content=[
                _tool_block(
                    {
                        "joy_sadness": {"value": "happy"},
                        "agency": {"value": 0.4},
                    }
                )
            ]
        )
        scorer = AnthropicMoodScorer(api_key="test-key")
        scorer._client = client

        results = scorer.score("text", dims)
        assert len(results) == 1
        assert results[0].dimension_name == "agency"


class TestJSONFallback:
    def test_fallback_parses_json_from_text_block(self, dims) -> None:
        """If the model returns prose with an embedded JSON object
        instead of using the tool, we still try to recover."""
        client = MagicMock()
        client.messages.create.return_value = MagicMock(
            content=[
                _text_block(
                    'Here are the scores: {"joy_sadness": {"value": '
                    '0.4}, "agency": {"value": 0.6}} — hope this helps.'
                )
            ]
        )
        scorer = AnthropicMoodScorer(api_key="test-key")
        scorer._client = client

        results = scorer.score("text", dims)
        assert len(results) == 2
        by_name = {r.dimension_name: r for r in results}
        assert by_name["joy_sadness"].value == 0.4
        assert by_name["agency"].value == 0.6

    def test_fallback_ignores_unparseable_text(self, dims) -> None:
        client = MagicMock()
        client.messages.create.return_value = MagicMock(
            content=[_text_block("No JSON here sorry.")]
        )
        scorer = AnthropicMoodScorer(api_key="test-key")
        scorer._client = client
        assert scorer.score("text", dims) == []


class TestExtractFirstJsonObject:
    def test_finds_object_in_middle_of_prose(self) -> None:
        obj = _extract_first_json_object(
            'pre {"a": 1} post'
        )
        assert obj == {"a": 1}

    def test_returns_none_on_garbage(self) -> None:
        assert _extract_first_json_object("nope") is None

    def test_returns_none_on_mismatched_braces(self) -> None:
        assert _extract_first_json_object("{ not valid") is None

    def test_skips_invalid_first_object(self) -> None:
        """Broken-JSON-then-valid-JSON should return the second."""
        obj = _extract_first_json_object(
            '{not valid} then {"ok": true}'
        )
        assert obj == {"ok": True}


class TestRawMoodScoreDataclass:
    def test_frozen(self) -> None:
        s = RawMoodScore("x", 0.5, 0.9)
        with pytest.raises(dataclasses.FrozenInstanceError):
            s.value = 0.8  # type: ignore[misc]
