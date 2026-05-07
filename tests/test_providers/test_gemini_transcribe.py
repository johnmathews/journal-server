"""Tests for the Gemini transcription provider."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from journal.models import TranscriptionResult
from journal.providers.transcription import (
    GeminiTranscribeProvider,
    TranscriptionProvider,
    _GeminiTranscriptionResponse,
    _GeminiUncertainPhrase,
    _phrases_to_uncertain_spans,
)

if TYPE_CHECKING:
    from pathlib import Path


def _mock_response(parsed: _GeminiTranscriptionResponse | None, text: str = "") -> MagicMock:
    response = MagicMock()
    response.parsed = parsed
    response.text = text
    return response


def _make_provider(context_dir: Path | None = None) -> tuple[GeminiTranscribeProvider, MagicMock]:
    """Build the provider; return the fake genai client instance the
    constructor wired up so tests configure ``models.generate_content``
    without reaching into ``provider._client``.
    """
    fake_client = MagicMock(name="genai.Client")
    with patch(
        "journal.providers.transcription.genai.Client",
        return_value=fake_client,
    ):
        provider = GeminiTranscribeProvider(
            api_key="test-key",
            model="gemini-2.5-pro",
            context_dir=context_dir,
        )
    return provider, fake_client


class TestGeminiTranscribeProvider:
    def test_implements_protocol(self) -> None:
        provider, client = _make_provider()
        assert isinstance(provider, TranscriptionProvider)

    def test_returns_transcription_result_happy_path(self) -> None:
        provider, client = _make_provider()
        text = "I went hiking with Saoirse and saw the Cuillin ridge."
        parsed = _GeminiTranscriptionResponse(
            text=text,
            uncertain_phrases=[
                _GeminiUncertainPhrase(phrase="Saoirse", reason="unfamiliar name"),
                _GeminiUncertainPhrase(phrase="Cuillin", reason="unclear audio"),
            ],
        )
        client.models.generate_content.return_value = _mock_response(parsed)

        result = provider.transcribe(b"fake-audio", "audio/mpeg")

        assert isinstance(result, TranscriptionResult)
        assert result.text == text
        s1 = text.index("Saoirse")
        s2 = text.index("Cuillin")
        assert (s1, s1 + len("Saoirse")) in result.uncertain_spans
        assert (s2, s2 + len("Cuillin")) in result.uncertain_spans
        assert len(result.uncertain_spans) == 2

    def test_no_uncertain_phrases(self) -> None:
        provider, client = _make_provider()
        parsed = _GeminiTranscriptionResponse(text="A clean transcript.", uncertain_phrases=[])
        client.models.generate_content.return_value = _mock_response(parsed)

        result = provider.transcribe(b"fake-audio", "audio/mpeg")

        assert result.text == "A clean transcript."
        assert result.uncertain_spans == []

    def test_phrase_not_found_in_text_skipped(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        provider, client = _make_provider()
        text = "Hello world."
        parsed = _GeminiTranscriptionResponse(
            text=text,
            uncertain_phrases=[
                _GeminiUncertainPhrase(phrase="Hello"),
                _GeminiUncertainPhrase(phrase="totally-absent-phrase"),
            ],
        )
        client.models.generate_content.return_value = _mock_response(parsed)

        with caplog.at_level(logging.WARNING, logger="journal.providers.transcription"):
            result = provider.transcribe(b"fake-audio", "audio/mpeg")

        assert result.uncertain_spans == [(0, 5)]
        assert any("totally-absent-phrase" in r.message for r in caplog.records)

    def test_phrase_appears_multiple_times_first_wins(self) -> None:
        provider, client = _make_provider()
        text = "cat dog cat dog"
        parsed = _GeminiTranscriptionResponse(
            text=text,
            uncertain_phrases=[_GeminiUncertainPhrase(phrase="cat")],
        )
        client.models.generate_content.return_value = _mock_response(parsed)

        result = provider.transcribe(b"fake-audio", "audio/mpeg")

        assert result.uncertain_spans == [(0, 3)]

    def test_overlapping_spans_merged(self) -> None:
        # Two uncertain phrases whose located ranges abut/overlap — they
        # should collapse into a single span.
        text = "alpha beta gamma"
        parsed = _GeminiTranscriptionResponse(
            text=text,
            uncertain_phrases=[
                _GeminiUncertainPhrase(phrase="alpha beta"),
                _GeminiUncertainPhrase(phrase="beta gamma"),
            ],
        )
        provider, client = _make_provider()
        client.models.generate_content.return_value = _mock_response(parsed)

        result = provider.transcribe(b"fake-audio", "audio/mpeg")

        assert result.uncertain_spans == [(0, len(text))]

    def test_empty_audio_raises(self) -> None:
        provider, client = _make_provider()
        with pytest.raises(ValueError, match="Audio data is empty"):
            provider.transcribe(b"", "audio/mpeg")

    def test_full_context_instruction_passed_when_context_dir_set(
        self, tmp_path: Path,
    ) -> None:
        ctx_file = tmp_path / "people.md"
        ctx_file.write_text("# people\n\n- **Saoirse Ronan** — actress\n")

        provider, client = _make_provider(context_dir=tmp_path)

        parsed = _GeminiTranscriptionResponse(text="ok", uncertain_phrases=[])
        client.models.generate_content.return_value = _mock_response(parsed)
        provider.transcribe(b"fake-audio", "audio/mpeg")

        call_kwargs = client.models.generate_content.call_args.kwargs
        config = call_kwargs["config"]
        assert "Saoirse Ronan" in config.system_instruction

    def test_default_system_used_when_no_context(self) -> None:
        provider, client = _make_provider(context_dir=None)
        parsed = _GeminiTranscriptionResponse(text="ok", uncertain_phrases=[])
        client.models.generate_content.return_value = _mock_response(parsed)
        provider.transcribe(b"fake-audio", "audio/mpeg")

        call_kwargs = client.models.generate_content.call_args.kwargs
        config = call_kwargs["config"]
        assert "careful transcription engine" in config.system_instruction.lower()

    def test_audio_over_20mb_uses_files_api(self) -> None:
        provider, client = _make_provider()
        big_audio = b"\x00" * (21 * 1024 * 1024)
        parsed = _GeminiTranscriptionResponse(text="ok", uncertain_phrases=[])
        client.models.generate_content.return_value = _mock_response(parsed)
        client.files.upload.return_value = MagicMock(name="UploadedFile")

        provider.transcribe(big_audio, "audio/mpeg")

        client.files.upload.assert_called_once()
        upload_kwargs = client.files.upload.call_args.kwargs
        assert "file" in upload_kwargs
        assert upload_kwargs["config"].mime_type == "audio/mpeg"

    def test_falls_back_to_response_text_when_parsed_is_none(self) -> None:
        provider, client = _make_provider()
        client.models.generate_content.return_value = _mock_response(
            parsed=None,
            text='{"text": "hi", "uncertain_phrases": []}',
        )

        result = provider.transcribe(b"fake-audio", "audio/mpeg")

        assert result.text == "hi"
        assert result.uncertain_spans == []

    def test_raises_when_parsed_and_text_both_unusable(self) -> None:
        provider, client = _make_provider()
        client.models.generate_content.return_value = _mock_response(
            parsed=None, text="",
        )

        with pytest.raises(RuntimeError, match="neither parsed object nor text"):
            provider.transcribe(b"fake-audio", "audio/mpeg")


class TestPhrasesToUncertainSpans:
    def test_empty(self) -> None:
        assert _phrases_to_uncertain_spans("hello", []) == []

    def test_skips_empty_phrase(self) -> None:
        spans = _phrases_to_uncertain_spans(
            "hello world",
            [_GeminiUncertainPhrase(phrase="")],
        )
        assert spans == []
