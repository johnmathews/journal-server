"""Tests for the OpenAI transcription provider."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from journal.models import TranscriptionResult
from journal.providers.transcription import (
    OpenAITranscribeProvider,
    TranscriptionProvider,
    _logprobs_to_uncertain_spans,
    _supports_logprobs,
)


class TestOpenAITranscribeProvider:
    """Tests for OpenAITranscribeProvider."""

    def _make_provider(
        self, model: str = "gpt-4o-transcribe", threshold: float = -0.5,
    ) -> tuple[OpenAITranscribeProvider, MagicMock]:
        """Build the provider and return the fake SDK client too, so tests
        configure ``audio.transcriptions.create`` without reaching into
        ``provider._client``.
        """
        fake_client = MagicMock(name="openai.OpenAI")
        with patch(
            "journal.providers.transcription.openai.OpenAI",
            return_value=fake_client,
        ):
            provider = OpenAITranscribeProvider(
                api_key="test-key",
                model=model,
                confidence_threshold=threshold,
            )
        return provider, fake_client

    def test_implements_protocol(self) -> None:
        provider, _client = self._make_provider()
        assert isinstance(provider, TranscriptionProvider)

    def test_transcribe_returns_transcription_result(self) -> None:
        provider, client = self._make_provider()
        mock_transcript = MagicMock()
        mock_transcript.text = "Hello, this is a voice note."
        mock_transcript.logprobs = None
        client.audio.transcriptions.create.return_value = mock_transcript

        result = provider.transcribe(b"fake-audio-data", "audio/mpeg")

        assert isinstance(result, TranscriptionResult)
        assert result.text == "Hello, this is a voice note."
        assert result.uncertain_spans == []

    def test_transcribe_with_logprobs(self) -> None:
        provider, client = self._make_provider(threshold=-0.5)
        mock_transcript = MagicMock()
        mock_transcript.text = "Hello world"
        mock_transcript.logprobs = [
            SimpleNamespace(token="Hello", logprob=-0.1, bytes=[]),
            SimpleNamespace(token=" world", logprob=-0.8, bytes=[]),
        ]
        client.audio.transcriptions.create.return_value = mock_transcript

        result = provider.transcribe(b"fake-audio-data", "audio/mpeg")

        assert result.text == "Hello world"
        # " world" has logprob -0.8 < -0.5, whitespace stripped → "world" (6,11)
        assert result.uncertain_spans == [(6, 11)]

    def test_transcribe_passes_logprob_params_for_supported_model(self) -> None:
        provider, client = self._make_provider(model="gpt-4o-transcribe")
        mock_transcript = MagicMock()
        mock_transcript.text = "text"
        mock_transcript.logprobs = None
        client.audio.transcriptions.create.return_value = mock_transcript

        provider.transcribe(b"audio", "audio/mpeg")

        call_kwargs = client.audio.transcriptions.create.call_args[1]
        assert call_kwargs["response_format"] == "json"
        assert call_kwargs["include"] == ["logprobs"]

    def test_transcribe_skips_logprob_params_for_whisper(self) -> None:
        provider, client = self._make_provider(model="whisper-1")
        mock_transcript = MagicMock()
        mock_transcript.text = "text"
        client.audio.transcriptions.create.return_value = mock_transcript

        provider.transcribe(b"audio", "audio/mpeg")

        call_kwargs = client.audio.transcriptions.create.call_args[1]
        assert "response_format" not in call_kwargs
        assert "include" not in call_kwargs

    def test_whisper_returns_empty_uncertain_spans(self) -> None:
        provider, client = self._make_provider(model="whisper-1")
        mock_transcript = MagicMock()
        mock_transcript.text = "transcribed text"
        client.audio.transcriptions.create.return_value = mock_transcript

        result = provider.transcribe(b"audio", "audio/mpeg")

        assert isinstance(result, TranscriptionResult)
        assert result.uncertain_spans == []

    def test_temp_file_has_correct_extension(self) -> None:
        provider, client = self._make_provider()
        mock_transcript = MagicMock()
        mock_transcript.text = "transcribed text"
        mock_transcript.logprobs = None
        client.audio.transcriptions.create.return_value = mock_transcript

        with patch("journal.providers.transcription.tempfile.NamedTemporaryFile") as mock_tmp:
            mock_file = MagicMock()
            mock_file.__enter__ = MagicMock(return_value=mock_file)
            mock_file.__exit__ = MagicMock(return_value=False)
            mock_file.name = "/tmp/test.wav"
            mock_tmp.return_value = mock_file

            with patch("builtins.open", MagicMock()):
                provider.transcribe(b"fake-audio-data", "audio/wav")

            mock_tmp.assert_called_once_with(suffix=".wav", delete=True)

    def test_default_extension_for_unknown_media_type(self) -> None:
        provider, client = self._make_provider()
        mock_transcript = MagicMock()
        mock_transcript.text = "transcribed text"
        mock_transcript.logprobs = None
        client.audio.transcriptions.create.return_value = mock_transcript

        with patch("journal.providers.transcription.tempfile.NamedTemporaryFile") as mock_tmp:
            mock_file = MagicMock()
            mock_file.__enter__ = MagicMock(return_value=mock_file)
            mock_file.__exit__ = MagicMock(return_value=False)
            mock_file.name = "/tmp/test.mp3"
            mock_tmp.return_value = mock_file

            with patch("builtins.open", MagicMock()):
                provider.transcribe(b"fake-audio-data", "audio/unknown-format")

            mock_tmp.assert_called_once_with(suffix=".mp3", delete=True)


class TestSupportsLogprobs:
    def test_gpt4o_transcribe(self) -> None:
        assert _supports_logprobs("gpt-4o-transcribe") is True

    def test_gpt4o_mini_transcribe(self) -> None:
        assert _supports_logprobs("gpt-4o-mini-transcribe") is True

    def test_whisper_1(self) -> None:
        assert _supports_logprobs("whisper-1") is False

    def test_unknown_model(self) -> None:
        assert _supports_logprobs("some-future-model") is False


class TestLogprobsToUncertainSpans:
    """Unit tests for the logprob → uncertain-span conversion."""

    def _lp(self, token: str, logprob: float) -> SimpleNamespace:
        return SimpleNamespace(token=token, logprob=logprob, bytes=[])

    def test_empty_logprobs(self) -> None:
        assert _logprobs_to_uncertain_spans("hello", [], -0.5) == []

    def test_all_confident(self) -> None:
        logprobs = [self._lp("Hello", -0.1), self._lp(" world", -0.2)]
        assert _logprobs_to_uncertain_spans("Hello world", logprobs, -0.5) == []

    def test_single_uncertain_token(self) -> None:
        logprobs = [
            self._lp("Hello", -0.1),
            self._lp(" ", -0.01),
            self._lp("wrld", -0.8),
        ]
        text = "Hello wrld"
        spans = _logprobs_to_uncertain_spans(text, logprobs, -0.5)
        # "wrld" is uncertain, expanded to word boundary → (6, 10)
        assert spans == [(6, 10)]

    def test_adjacent_uncertain_tokens_merged(self) -> None:
        logprobs = [
            self._lp("un", -0.9),
            self._lp("certain", -0.7),
            self._lp(" ok", -0.1),
        ]
        text = "uncertain ok"
        spans = _logprobs_to_uncertain_spans(text, logprobs, -0.5)
        # "un" + "certain" merge to "uncertain" → (0, 9)
        assert spans == [(0, 9)]

    def test_word_boundary_expansion(self) -> None:
        logprobs = [
            self._lp("run", -0.1),
            self._lp("ning", -0.8),
        ]
        text = "running"
        spans = _logprobs_to_uncertain_spans(text, logprobs, -0.5)
        # "ning" is uncertain but part of "running", expand to full word
        assert spans == [(0, 7)]

    def test_multiple_separate_uncertain_words(self) -> None:
        logprobs = [
            self._lp("The", -0.05),
            self._lp(" cat", -0.9),
            self._lp(" sat", -0.1),
            self._lp(" on", -0.05),
            self._lp(" thee", -0.8),
            self._lp(" mat", -0.1),
        ]
        text = "The cat sat on thee mat"
        spans = _logprobs_to_uncertain_spans(text, logprobs, -0.5)
        # " cat" → "cat" (4,7), " thee" → "thee" (15,19)
        assert spans == [(4, 7), (15, 19)]

    def test_threshold_boundary(self) -> None:
        logprobs = [self._lp("exact", -0.5)]
        text = "exact"
        # logprob == threshold, NOT below → no span
        assert _logprobs_to_uncertain_spans(text, logprobs, -0.5) == []

        # Slightly below threshold → flagged
        logprobs = [self._lp("close", -0.501)]
        text = "close"
        assert _logprobs_to_uncertain_spans(text, logprobs, -0.5) == [(0, 5)]

    def test_expansion_merges_nearby_words(self) -> None:
        # Two adjacent uncertain tokens that expand into overlapping words
        logprobs = [
            self._lp("a", -0.8),
            self._lp("b", -0.8),
        ]
        text = "ab"
        spans = _logprobs_to_uncertain_spans(text, logprobs, -0.5)
        assert spans == [(0, 2)]
