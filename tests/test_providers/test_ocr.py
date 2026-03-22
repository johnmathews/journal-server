"""Tests for the Anthropic OCR provider."""

import base64
from unittest.mock import MagicMock, patch

from journal.providers.ocr import SYSTEM_PROMPT, AnthropicOCRProvider, OCRProvider


class TestAnthropicOCRProvider:
    """Tests for AnthropicOCRProvider."""

    def _make_provider(self) -> AnthropicOCRProvider:
        with patch("journal.providers.ocr.anthropic.Anthropic"):
            provider = AnthropicOCRProvider(
                api_key="test-key",
                model="claude-opus-4-6",
                max_tokens=4096,
            )
        return provider

    def test_implements_protocol(self) -> None:
        with patch("journal.providers.ocr.anthropic.Anthropic"):
            provider = AnthropicOCRProvider(
                api_key="test-key", model="claude-opus-4-6", max_tokens=4096
            )
        assert isinstance(provider, OCRProvider)

    def test_extract_text_success(self) -> None:
        provider = self._make_provider()
        mock_message = MagicMock()
        mock_message.content = [MagicMock(text="Hello world from handwriting")]
        provider._client.messages.create.return_value = mock_message

        result = provider.extract_text(b"fake-image-data", "image/png")

        assert result == "Hello world from handwriting"
        provider._client.messages.create.assert_called_once()

    def test_system_prompt_included(self) -> None:
        provider = self._make_provider()
        mock_message = MagicMock()
        mock_message.content = [MagicMock(text="extracted")]
        provider._client.messages.create.return_value = mock_message

        provider.extract_text(b"fake-image-data", "image/jpeg")

        call_kwargs = provider._client.messages.create.call_args.kwargs
        system = call_kwargs["system"]
        assert len(system) == 1
        assert system[0]["text"] == SYSTEM_PROMPT
        assert system[0]["cache_control"] == {"type": "ephemeral"}

    def test_image_is_base64_encoded(self) -> None:
        provider = self._make_provider()
        mock_message = MagicMock()
        mock_message.content = [MagicMock(text="extracted")]
        provider._client.messages.create.return_value = mock_message

        image_data = b"fake-image-data"
        provider.extract_text(image_data, "image/png")

        call_kwargs = provider._client.messages.create.call_args.kwargs
        messages = call_kwargs["messages"]
        image_block = messages[0]["content"][0]
        expected_b64 = base64.standard_b64encode(image_data).decode("utf-8")
        assert image_block["source"]["data"] == expected_b64
        assert image_block["source"]["media_type"] == "image/png"
