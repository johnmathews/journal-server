"""Transcription Protocol and OpenAI Whisper adapter."""

import logging
import tempfile
from typing import Protocol, runtime_checkable

import openai

from journal.models import TranscriptionResult

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

# Models that support the ``include=["logprobs"]`` parameter.
_LOGPROB_MODELS = {"gpt-4o-transcribe", "gpt-4o-mini-transcribe"}


def _supports_logprobs(model: str) -> bool:
    """Return True if *model* supports per-token log-probabilities."""
    return model in _LOGPROB_MODELS


def _logprobs_to_uncertain_spans(
    text: str,
    logprobs: list[object],
    threshold: float,
) -> list[tuple[int, int]]:
    """Convert per-token logprobs to character-offset uncertain spans.

    Walks tokens in order, tracking character offset.  Tokens whose
    ``logprob`` is below *threshold* are flagged.  Adjacent flagged
    tokens are merged into a single span, then each span is expanded
    to the nearest word boundary so the UI highlights whole words.
    """
    if not logprobs:
        return []

    # Phase 1: collect raw (char_start, char_end) for low-confidence tokens.
    # Tokens may include leading/trailing whitespace (e.g., " world").
    # We strip that so the span covers only the word content — otherwise
    # word-boundary expansion in Phase 3 would pull in adjacent words.
    ws = frozenset(" \n\t\r")
    raw_spans: list[tuple[int, int]] = []
    char_offset = 0
    text_len = len(text)
    for entry in logprobs:
        token_text: str = entry.token  # type: ignore[union-attr]
        token_logprob: float = entry.logprob  # type: ignore[union-attr]
        token_len = len(token_text)
        if char_offset + token_len > text_len:
            # Token stream overflows text — stop to avoid IndexError.
            break
        if token_logprob < threshold:
            # Find the content (non-whitespace) portion of the token.
            content_start = char_offset
            content_end = char_offset + token_len
            while content_start < content_end and text[content_start] in ws:
                content_start += 1
            while content_end > content_start and text[content_end - 1] in ws:
                content_end -= 1
            if content_start < content_end:
                raw_spans.append((content_start, content_end))
        char_offset += token_len

    if not raw_spans:
        return []

    # Phase 2: merge adjacent / overlapping spans.
    merged: list[list[int]] = [list(raw_spans[0])]
    for start, end in raw_spans[1:]:
        prev = merged[-1]
        if start <= prev[1]:
            prev[1] = max(prev[1], end)
        else:
            merged.append([start, end])

    # Phase 3: expand to word boundaries for cleaner highlighting.
    expanded: list[tuple[int, int]] = []
    text_len = len(text)
    for span in merged:
        s, e = span
        while s > 0 and text[s - 1] not in ws:
            s -= 1
        while e < text_len and text[e] not in ws:
            e += 1
        expanded.append((s, e))

    # Phase 4: re-merge after expansion (expansion can cause overlap).
    expanded.sort()
    final: list[tuple[int, int]] = [expanded[0]]
    for s, e in expanded[1:]:
        ps, pe = final[-1]
        if s <= pe:
            final[-1] = (ps, max(pe, e))
        else:
            final.append((s, e))

    return final


@runtime_checkable
class TranscriptionProvider(Protocol):
    """Protocol for audio transcription providers."""

    def transcribe(
        self,
        audio_data: bytes,
        media_type: str,
        language: str = "en",
    ) -> TranscriptionResult: ...


class OpenAITranscriptionProvider:
    """Transcription provider using OpenAI's Whisper API."""

    def __init__(
        self,
        api_key: str,
        model: str,
        confidence_threshold: float = -0.5,
    ) -> None:
        self._client = openai.OpenAI(api_key=api_key)
        self._model = model
        self._confidence_threshold = confidence_threshold

    def transcribe(
        self,
        audio_data: bytes,
        media_type: str,
        language: str = "en",
    ) -> TranscriptionResult:
        """Transcribe audio data using OpenAI's transcription API."""
        logger.info(
            "Transcribing audio via OpenAI (model=%s, media_type=%s, language=%s)",
            self._model,
            media_type,
            language,
        )

        ext = MEDIA_TYPE_TO_EXT.get(media_type, ".mp3")
        use_logprobs = _supports_logprobs(self._model)

        with tempfile.NamedTemporaryFile(suffix=ext, delete=True) as tmp:
            tmp.write(audio_data)
            tmp.flush()

            with open(tmp.name, "rb") as audio_file:
                kwargs: dict = {
                    "model": self._model,
                    "file": audio_file,
                    "language": language,
                }
                if use_logprobs:
                    kwargs["response_format"] = "json"
                    kwargs["include"] = ["logprobs"]

                transcript = self._client.audio.transcriptions.create(**kwargs)

        text: str = transcript.text

        # Extract uncertain spans from logprobs when available.
        uncertain_spans: list[tuple[int, int]] = []
        if use_logprobs:
            logprobs = getattr(transcript, "logprobs", None)
            if logprobs:
                uncertain_spans = _logprobs_to_uncertain_spans(
                    text, logprobs, self._confidence_threshold,
                )
                logger.info(
                    "Identified %d uncertain span(s) from logprobs",
                    len(uncertain_spans),
                )

        logger.info("Transcription complete (%d characters)", len(text))
        return TranscriptionResult(text=text, uncertain_spans=uncertain_spans)
