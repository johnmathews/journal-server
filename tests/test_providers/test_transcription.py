"""Tests for the OpenAI transcription provider."""

from unittest.mock import MagicMock, patch

from journal.providers.transcription import (
    OpenAITranscriptionProvider,
    TranscriptionProvider,
)


class TestOpenAITranscriptionProvider:
    """Tests for OpenAITranscriptionProvider."""

    def _make_provider(self) -> OpenAITranscriptionProvider:
        with patch("journal.providers.transcription.openai.OpenAI"):
            provider = OpenAITranscriptionProvider(
                api_key="test-key",
                model="gpt-4o-transcribe",
            )
        return provider

    def test_implements_protocol(self) -> None:
        with patch("journal.providers.transcription.openai.OpenAI"):
            provider = OpenAITranscriptionProvider(api_key="test-key", model="gpt-4o-transcribe")
        assert isinstance(provider, TranscriptionProvider)

    def test_transcribe_success(self) -> None:
        provider = self._make_provider()
        mock_transcript = MagicMock()
        mock_transcript.text = "Hello, this is a voice note."
        provider._client.audio.transcriptions.create.return_value = mock_transcript

        result = provider.transcribe(b"fake-audio-data", "audio/mpeg")

        assert result == "Hello, this is a voice note."
        provider._client.audio.transcriptions.create.assert_called_once()

    def test_temp_file_has_correct_extension(self) -> None:
        provider = self._make_provider()
        mock_transcript = MagicMock()
        mock_transcript.text = "transcribed text"
        provider._client.audio.transcriptions.create.return_value = mock_transcript

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
        provider = self._make_provider()
        mock_transcript = MagicMock()
        mock_transcript.text = "transcribed text"
        provider._client.audio.transcriptions.create.return_value = mock_transcript

        with patch("journal.providers.transcription.tempfile.NamedTemporaryFile") as mock_tmp:
            mock_file = MagicMock()
            mock_file.__enter__ = MagicMock(return_value=mock_file)
            mock_file.__exit__ = MagicMock(return_value=False)
            mock_file.name = "/tmp/test.mp3"
            mock_tmp.return_value = mock_file

            with patch("builtins.open", MagicMock()):
                provider.transcribe(b"fake-audio-data", "audio/unknown-format")

            mock_tmp.assert_called_once_with(suffix=".mp3", delete=True)
