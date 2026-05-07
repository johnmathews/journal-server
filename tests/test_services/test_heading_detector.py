"""Tests for the date-heading detector service."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from journal.services.heading_detector import (
    SYSTEM_PROMPT,
    AnthropicHeadingDetector,
    HeadingDetectionResult,
    NullHeadingDetector,
)


def _llm_json(
    *,
    is_heading: bool,
    heading_text: str | None = None,
    source_phrase: str | None = None,
    iso_date: str | None = None,
) -> str:
    return json.dumps(
        {
            "is_heading": is_heading,
            "heading_text": heading_text,
            "source_phrase": source_phrase,
            "iso_date": iso_date,
        }
    )


@pytest.fixture
def mock_client():
    client = MagicMock()
    with patch("anthropic.Anthropic", return_value=client):
        yield client


def _set_response(client: MagicMock, text: str) -> None:
    response = MagicMock()
    response.content = [MagicMock(text=text)]
    client.messages.create.return_value = response


class TestHeadingDetectionResult:
    def test_no_heading_to_text_returns_body(self):
        result = HeadingDetectionResult(heading_text="", body="hello world")
        assert result.has_heading is False
        assert result.to_text() == "hello world"

    def test_heading_with_body(self):
        result = HeadingDetectionResult(
            heading_text="28 April 2026", body="Today I went out."
        )
        assert result.has_heading is True
        assert result.to_text() == "# 28 April 2026\n\nToday I went out."

    def test_heading_without_body(self):
        result = HeadingDetectionResult(heading_text="28 April 2026", body="")
        assert result.has_heading is True
        assert result.to_text() == "# 28 April 2026\n"


class TestNullHeadingDetector:
    def test_returns_text_unchanged(self):
        det = NullHeadingDetector()
        result = det.detect("April 28th. Today I went out.", entry_date="2026-04-28")
        assert result.has_heading is False
        assert result.body == "April 28th. Today I went out."
        assert result.to_text() == "April 28th. Today I went out."

    def test_empty_text(self):
        det = NullHeadingDetector()
        result = det.detect("", entry_date="2026-04-28")
        assert result.body == ""


class TestAnthropicHeadingDetector:
    def test_calls_api_with_system_prompt_and_entry_date(self, mock_client):
        _set_response(
            mock_client,
            _llm_json(
                is_heading=True,
                heading_text="28 April 2026",
                source_phrase="April 28th. ",
            ),
        )

        det = AnthropicHeadingDetector(api_key="k", model="claude-haiku-4-5")
        det.detect("April 28th. Today I went for a long run.", entry_date="2026-04-28")

        kwargs = mock_client.messages.create.call_args.kwargs
        assert kwargs["model"] == "claude-haiku-4-5"
        assert kwargs["system"] == SYSTEM_PROMPT
        assert kwargs["messages"][0]["role"] == "user"
        # entry_date is included in the user message so the model can resolve relative dates.
        assert "entry_date: 2026-04-28" in kwargs["messages"][0]["content"]
        assert "April 28th. Today I went for a long run." in kwargs["messages"][0]["content"]

    def test_user_message_omits_entry_date_when_not_given(self, mock_client):
        _set_response(mock_client, _llm_json(is_heading=False))

        det = AnthropicHeadingDetector(api_key="k")
        det.detect("Some plain text.")

        content = mock_client.messages.create.call_args.kwargs["messages"][0]["content"]
        assert content == "Some plain text."

    def test_numeric_date_with_topic_shift(self, mock_client):
        _set_response(
            mock_client,
            _llm_json(
                is_heading=True,
                heading_text="28 April 2026",
                source_phrase="April 28th. ",
            ),
        )

        det = AnthropicHeadingDetector(api_key="k")
        result = det.detect("April 28th. Today I went for a long run.")

        assert result.has_heading is True
        assert result.heading_text == "28 April 2026"
        assert result.body == "Today I went for a long run."
        assert result.to_text() == "# 28 April 2026\n\nToday I went for a long run."

    def test_relative_date_resolves_using_entry_date(self, mock_client):
        # The LLM is told entry_date=2026-04-28, so "Today" → "28 April 2026".
        _set_response(
            mock_client,
            _llm_json(
                is_heading=True,
                heading_text="28 April 2026",
                source_phrase="Today, ",
            ),
        )

        det = AnthropicHeadingDetector(api_key="k")
        result = det.detect("Today, I had breakfast at home.", entry_date="2026-04-28")

        assert result.heading_text == "28 April 2026"
        assert result.body == "I had breakfast at home."

    def test_date_with_time(self, mock_client):
        _set_response(
            mock_client,
            _llm_json(
                is_heading=True,
                heading_text="28 April 2026, 9am",
                source_phrase="28th April, 9am — ",
            ),
        )

        det = AnthropicHeadingDetector(api_key="k")
        result = det.detect("28th April, 9am — woke up early and read a book.")

        assert result.heading_text == "28 April 2026, 9am"
        assert result.body == "woke up early and read a book."

    def test_mid_sentence_date_returns_unchanged(self, mock_client):
        _set_response(mock_client, _llm_json(is_heading=False))

        det = AnthropicHeadingDetector(api_key="k")
        text = "I went to Berlin on April 28th."
        result = det.detect(text)

        assert result.has_heading is False
        assert result.body == text
        assert result.to_text() == text

    def test_already_a_heading_short_circuits(self, mock_client):
        det = AnthropicHeadingDetector(api_key="k")
        text = "# Existing Heading\n\nSome text."
        result = det.detect(text)

        assert result.has_heading is False
        assert result.body == text
        # No API call when text already looks like a heading.
        mock_client.messages.create.assert_not_called()

    def test_already_a_heading_with_leading_whitespace(self, mock_client):
        det = AnthropicHeadingDetector(api_key="k")
        result = det.detect("   # Heading\nbody")
        assert result.has_heading is False
        mock_client.messages.create.assert_not_called()

    def test_empty_text(self, mock_client):
        det = AnthropicHeadingDetector(api_key="k")
        assert det.detect("").body == ""
        assert det.detect("   ").body == "   "
        mock_client.messages.create.assert_not_called()

    def test_date_only_entry(self, mock_client):
        _set_response(
            mock_client,
            _llm_json(
                is_heading=True,
                heading_text="28 April 2026",
                source_phrase="April 28th 2026.",
            ),
        )

        det = AnthropicHeadingDetector(api_key="k")
        result = det.detect("April 28th 2026.")

        assert result.has_heading is True
        assert result.body == ""
        assert result.to_text() == "# 28 April 2026\n"

    def test_api_error_returns_unchanged(self, mock_client):
        mock_client.messages.create.side_effect = RuntimeError("API down")
        det = AnthropicHeadingDetector(api_key="k")

        result = det.detect("April 28th. Today I went out.")

        assert result.has_heading is False
        assert result.body == "April 28th. Today I went out."

    def test_invalid_json_returns_unchanged(self, mock_client):
        _set_response(mock_client, "not json at all")

        det = AnthropicHeadingDetector(api_key="k")
        result = det.detect("April 28th. Today I went out.")

        assert result.has_heading is False
        assert result.body == "April 28th. Today I went out."

    def test_no_braces_in_response_returns_unchanged(self, mock_client):
        _set_response(mock_client, "is_heading: true")

        det = AnthropicHeadingDetector(api_key="k")
        result = det.detect("April 28th. Today.")
        assert result.has_heading is False

    def test_json_in_markdown_fence_is_recovered(self, mock_client):
        # Even though we instruct "no markdown fences", be tolerant if the model wraps the
        # JSON anyway.
        _set_response(
            mock_client,
            "```json\n"
            + _llm_json(
                is_heading=True,
                heading_text="28 April 2026",
                source_phrase="April 28th. ",
            )
            + "\n```",
        )

        det = AnthropicHeadingDetector(api_key="k")
        result = det.detect("April 28th. Today I went out.")
        assert result.has_heading is True
        assert result.heading_text == "28 April 2026"

    def test_source_phrase_not_prefix_returns_unchanged(self, mock_client):
        # Model hallucinates a source_phrase that doesn't actually appear in the input.
        _set_response(
            mock_client,
            _llm_json(
                is_heading=True,
                heading_text="28 April 2026",
                source_phrase="something completely different",
            ),
        )

        det = AnthropicHeadingDetector(api_key="k")
        result = det.detect("April 28th. Today I went out.")

        assert result.has_heading is False
        assert result.body == "April 28th. Today I went out."

    def test_blank_heading_text_returns_unchanged(self, mock_client):
        _set_response(
            mock_client,
            _llm_json(is_heading=True, heading_text="   ", source_phrase="April 28th. "),
        )

        det = AnthropicHeadingDetector(api_key="k")
        result = det.detect("April 28th. Today I went out.")
        assert result.has_heading is False

    def test_missing_source_phrase_returns_unchanged(self, mock_client):
        _set_response(
            mock_client,
            '{"is_heading": true, "heading_text": "28 April 2026", "source_phrase": null}',
        )

        det = AnthropicHeadingDetector(api_key="k")
        result = det.detect("April 28th. Today I went out.")
        assert result.has_heading is False

    def test_leading_whitespace_in_input_is_stripped_from_body(self, mock_client):
        _set_response(
            mock_client,
            _llm_json(
                is_heading=True,
                heading_text="28 April 2026",
                source_phrase="April 28th. ",
            ),
        )

        det = AnthropicHeadingDetector(api_key="k")
        # Real OCR sometimes returns text with leading whitespace.
        result = det.detect("   April 28th. Today I went out.")

        assert result.has_heading is True
        assert result.body == "Today I went out."

    def test_only_first_window_chars_sent(self, mock_client):
        _set_response(mock_client, _llm_json(is_heading=False))

        det = AnthropicHeadingDetector(api_key="k")
        long_text = "Filler " * 100  # ~700 chars
        det.detect(long_text)

        sent = mock_client.messages.create.call_args.kwargs["messages"][0]["content"]
        # Default window is 300 chars; with no entry_date prefix the user message length
        # should be at most that.
        assert len(sent) <= 300

    def test_iso_date_returned_when_valid(self, mock_client):
        _set_response(
            mock_client,
            _llm_json(
                is_heading=True,
                heading_text="1 January 2026",
                source_phrase="The first of January twenty twenty six. ",
                iso_date="2026-01-01",
            ),
        )

        det = AnthropicHeadingDetector(api_key="k")
        result = det.detect(
            "The first of January twenty twenty six. I cleaned the kitchen.",
            entry_date="2026-05-04",
        )

        assert result.has_heading is True
        assert result.date_iso == "2026-01-01"

    def test_iso_date_absent_yields_none(self, mock_client):
        # Older / partial responses may omit iso_date entirely.
        _set_response(
            mock_client,
            '{"is_heading": true, "heading_text": "28 April 2026",'
            ' "source_phrase": "April 28th. "}',
        )

        det = AnthropicHeadingDetector(api_key="k")
        result = det.detect("April 28th. Today I went out.")

        assert result.has_heading is True
        assert result.date_iso is None

    def test_iso_date_malformed_yields_none(self, mock_client):
        _set_response(
            mock_client,
            _llm_json(
                is_heading=True,
                heading_text="28 April 2026",
                source_phrase="April 28th. ",
                iso_date="not-a-date",
            ),
        )

        det = AnthropicHeadingDetector(api_key="k")
        result = det.detect("April 28th. Today I went out.")

        # Heading still detected, but date_iso falls back to None — caller
        # uses other date sources.
        assert result.has_heading is True
        assert result.date_iso is None

    def test_iso_date_out_of_plausible_range_yields_none(self, mock_client):
        _set_response(
            mock_client,
            _llm_json(
                is_heading=True,
                heading_text="January 1, year 1",
                source_phrase="0001-01-01. ",
                iso_date="0001-01-01",
            ),
        )

        det = AnthropicHeadingDetector(api_key="k")
        result = det.detect("0001-01-01. Today I went out.")

        assert result.has_heading is True
        assert result.date_iso is None

    def test_iso_date_with_time_component_yields_none(self, mock_client):
        # The prompt forbids a time component but be defensive — datetime ISO
        # ("2026-04-28T09:00:00") shouldn't slip through into entry_date.
        _set_response(
            mock_client,
            _llm_json(
                is_heading=True,
                heading_text="28 April 2026, 9am",
                source_phrase="28 April 2026, 9am — ",
                iso_date="2026-04-28T09:00:00",
            ),
        )

        det = AnthropicHeadingDetector(api_key="k")
        result = det.detect("28 April 2026, 9am — woke up.")

        # date.fromisoformat accepts "YYYY-MM-DD" only. A datetime string
        # raises and we return None.
        assert result.has_heading is True
        assert result.date_iso is None
