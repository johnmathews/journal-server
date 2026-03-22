"""Transcription Protocol and OpenAI Whisper adapter."""

import logging
import tempfile
from typing import Protocol, runtime_checkable

import openai

logger = logging.getLogger(__name__)

MEDIA_TYPE_TO_EXT: dict[str, str] = {
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/mp4": ".mp4",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/webm": ".webm",
    "audio/ogg": ".ogg",
    "audio/flac": ".flac",
    "audio/x-m4a": ".m4a",
    "audio/m4a": ".m4a",
}


@runtime_checkable
class TranscriptionProvider(Protocol):
    """Protocol for audio transcription providers."""

    def transcribe(self, audio_data: bytes, media_type: str, language: str = "en") -> str: ...


class OpenAITranscriptionProvider:
    """Transcription provider using OpenAI's Whisper API."""

    def __init__(self, api_key: str, model: str) -> None:
        self._client = openai.OpenAI(api_key=api_key)
        self._model = model

    def transcribe(self, audio_data: bytes, media_type: str, language: str = "en") -> str:
        """Transcribe audio data using OpenAI's transcription API."""
        logger.info(
            "Transcribing audio via OpenAI (model=%s, media_type=%s, language=%s)",
            self._model,
            media_type,
            language,
        )

        ext = MEDIA_TYPE_TO_EXT.get(media_type, ".mp3")

        with tempfile.NamedTemporaryFile(suffix=ext, delete=True) as tmp:
            tmp.write(audio_data)
            tmp.flush()

            with open(tmp.name, "rb") as audio_file:
                transcript = self._client.audio.transcriptions.create(
                    model=self._model,
                    file=audio_file,
                    language=language,
                )

        text = transcript.text
        logger.info("Transcription complete (%d characters)", len(text))
        return text
