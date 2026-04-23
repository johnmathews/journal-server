"""Tests for the transcript paragraph formatter provider."""

from unittest.mock import MagicMock, patch

import pytest

from journal.providers.formatter import AnthropicFormatter, SYSTEM_PROMPT


@pytest.fixture
def mock_client():
    client = MagicMock()
    with patch("anthropic.Anthropic", return_value=client):
        yield client


class TestAnthropicFormatter:
    def test_calls_api_with_correct_params(self, mock_client):
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="Hello world.\n\nGoodbye world.")]
        mock_client.messages.create.return_value = mock_response

        formatter = AnthropicFormatter(api_key="test-key", model="claude-haiku-4-5")
        formatter.format_paragraphs("Hello world. Goodbye world.")

        mock_client.messages.create.assert_called_once()
        call_kwargs = mock_client.messages.create.call_args.kwargs
        assert call_kwargs["model"] == "claude-haiku-4-5"
        assert call_kwargs["system"] == SYSTEM_PROMPT
        assert call_kwargs["messages"] == [
            {"role": "user", "content": "Hello world. Goodbye world."}
        ]

    def test_returns_formatted_text_when_words_match(self, mock_client):
        original = "Hello world. Goodbye world."
        formatted = "Hello world.\n\nGoodbye world."
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=formatted)]
        mock_client.messages.create.return_value = mock_response

        formatter = AnthropicFormatter(api_key="test-key")
        result = formatter.format_paragraphs(original)
        assert result == formatted

    def test_returns_original_when_words_changed(self, mock_client):
        original = "Hello world. Goodbye world."
        bad_result = "Hello world.\n\nFarewell world."
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=bad_result)]
        mock_client.messages.create.return_value = mock_response

        formatter = AnthropicFormatter(api_key="test-key")
        result = formatter.format_paragraphs(original)
        assert result == original

    def test_returns_original_on_api_exception(self):
        formatter = AnthropicFormatter.__new__(AnthropicFormatter)
        formatter._client = MagicMock()
        formatter._model = "claude-haiku-4-5"
        formatter._max_tokens = 8192
        formatter._client.messages.create.side_effect = RuntimeError("API down")

        result = formatter.format_paragraphs("Some text here.")
        assert result == "Some text here."

    def test_empty_text_returned_as_is(self):
        formatter = AnthropicFormatter.__new__(AnthropicFormatter)
        formatter._client = MagicMock()
        formatter._model = "claude-haiku-4-5"
        formatter._max_tokens = 8192

        assert formatter.format_paragraphs("") == ""
        assert formatter.format_paragraphs("   ") == "   "
        formatter._client.messages.create.assert_not_called()

    def test_identical_text_returned_unchanged(self, mock_client):
        text = "Already good."
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=text)]
        mock_client.messages.create.return_value = mock_response

        formatter = AnthropicFormatter(api_key="test-key")
        assert formatter.format_paragraphs(text) == text
