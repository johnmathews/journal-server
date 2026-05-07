"""Transcription Protocol and OpenAI Whisper adapter."""

from __future__ import annotations

import logging
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from io import BytesIO
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import httpx
import openai
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from pydantic import BaseModel, Field

from journal.models import TranscriptionResult
from journal.services.transcription_context import (
    build_full_context_instruction,
    build_whisper_prompt,
)

if TYPE_CHECKING:
    from pathlib import Path

    from journal.config import Config

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


class OpenAITranscribeProvider:
    """Transcription provider using OpenAI's `/audio/transcriptions` endpoint.

    Supports ``whisper-1``, ``gpt-4o-transcribe``, and ``gpt-4o-mini-transcribe``
    via the ``model`` parameter. The endpoint contract is the same across all
    three; the gpt-4o variants additionally accept ``include=["logprobs"]``,
    which we use to derive ``uncertain_spans``.

    The ``context_prompt`` is a Whisper-style spelling bias (capped at ~200
    tokens — see ``services.transcription_context.build_whisper_prompt``).
    OpenAI does not currently expose a system-instruction parameter on this
    endpoint, so the prompt is not full instruction-following — for that, use
    ``GeminiTranscribeProvider``.
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        confidence_threshold: float = -0.5,
        context_prompt: str = "",
    ) -> None:
        self._client = openai.OpenAI(api_key=api_key)
        self._model = model
        self._confidence_threshold = confidence_threshold
        # Optional context prompt (up to ~200 tokens of names/places/jargon)
        # passed to Whisper to bias toward correct spellings. Empty string
        # means "no prompt" — preserves prior behaviour.
        self._context_prompt = context_prompt

    @property
    def model(self) -> str:
        return self._model

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
                if self._context_prompt:
                    kwargs["prompt"] = self._context_prompt

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


# ---------------------------------------------------------------------------
# Gemini transcription provider
# ---------------------------------------------------------------------------


_GEMINI_DEFAULT_SYSTEM_INSTRUCTION = (
    "You are a careful transcription engine. Transcribe the audio accurately. "
    "Do not invent words."
)

# 20 MB — Gemini's documented inline-bytes ceiling. Larger payloads must be
# uploaded via the Files API and referenced by handle.
_GEMINI_INLINE_LIMIT_BYTES = 20 * 1024 * 1024

# Permissive set; if the model rejects a media_type we let its error
# propagate rather than maintaining a parallel allowlist that drifts.
_GEMINI_SUPPORTED_AUDIO_TYPES = frozenset({
    "audio/mp3",
    "audio/mpeg",
    "audio/wav",
    "audio/x-wav",
    "audio/ogg",
    "audio/flac",
    "audio/aac",
    "audio/aiff",
    "audio/m4a",
    "audio/x-m4a",
    "audio/webm",
    "audio/mp4",
})


class _GeminiUncertainPhrase(BaseModel):
    phrase: str = Field(
        description="The exact phrase from the transcript that you're uncertain about.",
    )
    reason: str = Field(
        default="",
        description="Why uncertain — e.g., 'unclear audio', 'unfamiliar name'.",
    )


class _GeminiTranscriptionResponse(BaseModel):
    text: str
    uncertain_phrases: list[_GeminiUncertainPhrase] = Field(default_factory=list)


def _phrases_to_uncertain_spans(
    text: str, phrases: list[_GeminiUncertainPhrase],
) -> list[tuple[int, int]]:
    """Locate each uncertain phrase in *text* and return merged spans.

    For phrases that appear more than once, only the first occurrence is
    used. Phrases not found in the text are skipped with a warning —
    this happens when the model paraphrases the uncertain content
    instead of quoting it verbatim.
    """
    raw_spans: list[tuple[int, int]] = []
    for entry in phrases:
        phrase = entry.phrase
        if not phrase:
            continue
        idx = text.find(phrase)
        if idx < 0:
            logger.warning(
                "Gemini uncertain phrase %r not found in transcript — skipping",
                phrase,
            )
            continue
        raw_spans.append((idx, idx + len(phrase)))

    if not raw_spans:
        return []

    raw_spans.sort()
    merged: list[tuple[int, int]] = [raw_spans[0]]
    for start, end in raw_spans[1:]:
        prev_start, prev_end = merged[-1]
        if start <= prev_end:
            merged[-1] = (prev_start, max(prev_end, end))
        else:
            merged.append((start, end))
    return merged


class GeminiTranscribeProvider:
    """Transcription provider using Google's Gemini multimodal API.

    Unlike OpenAI's /audio/transcriptions endpoint, Gemini accepts a full
    instruction-following system prompt and structured output schemas. We
    exploit this to ask the model to return both the transcript text and a
    list of phrases it's uncertain about, in one call.

    Uncertain spans are derived by string-locating each reported phrase in
    the transcript text. This is model-introspective (not mechanically
    grounded like logprobs) and should be eval'd before being trusted as
    a primary signal.
    """

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-2.5-pro",
        context_dir: Path | None = None,
    ) -> None:
        self._client = genai.Client(api_key=api_key)
        self._model = model
        instruction = build_full_context_instruction(context_dir)
        self._system_instruction = instruction or _GEMINI_DEFAULT_SYSTEM_INSTRUCTION

    @property
    def model(self) -> str:
        return self._model

    def transcribe(
        self,
        audio_data: bytes,
        media_type: str,
        language: str = "en",
    ) -> TranscriptionResult:
        if not audio_data:
            raise ValueError("Audio data is empty")

        if media_type not in _GEMINI_SUPPORTED_AUDIO_TYPES:
            logger.warning(
                "Media type %r is not in the known Gemini audio set — "
                "passing through and letting the API decide",
                media_type,
            )

        logger.info(
            "Transcribing audio via Gemini (model=%s, media_type=%s, language=%s, %d bytes)",
            self._model,
            media_type,
            language,
            len(audio_data),
        )

        if len(audio_data) <= _GEMINI_INLINE_LIMIT_BYTES:
            audio_part: object = genai_types.Part.from_bytes(
                data=audio_data, mime_type=media_type,
            )
        else:
            logger.info(
                "Audio exceeds %d bytes — uploading via Files API",
                _GEMINI_INLINE_LIMIT_BYTES,
            )
            audio_part = self._client.files.upload(
                file=BytesIO(audio_data),
                config=genai_types.UploadFileConfig(mime_type=media_type),
            )

        prompt = (
            f"Transcribe this audio. The speaker uses primarily {language}. "
            "Return JSON conforming to the schema. List uncertain phrases — "
            "names you're not sure of, words you couldn't make out. Do not "
            "invent words."
        )

        response = self._client.models.generate_content(
            model=self._model,
            contents=[audio_part, prompt],
            config=genai_types.GenerateContentConfig(
                system_instruction=self._system_instruction,
                response_mime_type="application/json",
                response_schema=_GeminiTranscriptionResponse,
                temperature=0.0,
            ),
        )

        parsed = getattr(response, "parsed", None)
        if parsed is None:
            raw_text = getattr(response, "text", None)
            if not raw_text:
                raise RuntimeError(
                    "Gemini transcription returned neither parsed object nor text",
                )
            parsed = _GeminiTranscriptionResponse.model_validate_json(raw_text)

        spans = _phrases_to_uncertain_spans(parsed.text, parsed.uncertain_phrases)
        logger.info(
            "Gemini transcription complete (%d characters, %d uncertain span(s))",
            len(parsed.text),
            len(spans),
        )
        return TranscriptionResult(text=parsed.text, uncertain_spans=spans)


# ---------------------------------------------------------------------------
# Retrying / fallback wrapper
# ---------------------------------------------------------------------------


class PrimaryExhaustedError(RuntimeError):
    """Raised when the primary provider exhausts its retry budget without a fallback."""

    def __init__(self, attempts: int, last_error: Exception) -> None:
        self.attempts = attempts
        self.last_error = last_error
        super().__init__(
            f"Primary transcription provider exhausted after {attempts} attempt(s); "
            f"last error: {type(last_error).__name__}: {last_error}",
        )


def _is_transient(exc: Exception) -> bool:
    """Return True if *exc* should be retried, False otherwise."""
    # OpenAI transient: timeout, connection, rate-limit, 5xx.
    if isinstance(exc, (
        openai.APITimeoutError,
        openai.APIConnectionError,
        openai.RateLimitError,
        openai.InternalServerError,
    )):
        return True
    # OpenAI non-transient — explicit list to make intent clear.
    if isinstance(exc, (
        openai.AuthenticationError,
        openai.PermissionDeniedError,
        openai.NotFoundError,
        openai.BadRequestError,
        openai.UnprocessableEntityError,
    )):
        return False

    # Gemini ServerError = 5xx → transient.
    if isinstance(exc, genai_errors.ServerError):
        return True
    # Gemini ClientError → only 429 (rate limit) is transient.
    if isinstance(exc, genai_errors.ClientError):
        code = getattr(exc, "code", None)
        return code == 429

    # httpx low-level: Gemini surfaces these for timeouts/connection issues.
    return isinstance(exc, (httpx.TimeoutException, httpx.ConnectError))


class RetryingTranscriptionProvider:
    """Wrapper that retries transient errors then falls through to a fallback provider."""

    def __init__(
        self,
        primary: TranscriptionProvider,
        fallback: TranscriptionProvider | None = None,
        max_attempts: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 30.0,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._max_attempts = max_attempts
        self._base_delay = base_delay
        self._max_delay = max_delay

    @property
    def primary(self) -> TranscriptionProvider:
        return self._primary

    @property
    def fallback(self) -> TranscriptionProvider | None:
        return self._fallback

    def transcribe(
        self,
        audio_data: bytes,
        media_type: str,
        language: str = "en",
    ) -> TranscriptionResult:
        last_error: Exception | None = None
        for attempt in range(self._max_attempts):
            try:
                return self._primary.transcribe(audio_data, media_type, language)
            except Exception as exc:
                if not _is_transient(exc):
                    raise
                last_error = exc
                if attempt < self._max_attempts - 1:
                    delay = min(self._base_delay * (2 ** attempt), self._max_delay)
                    logger.warning(
                        "Primary transcription provider raised transient error "
                        "(%s: %s); attempt %d/%d, sleeping %.2fs before retry",
                        type(exc).__name__,
                        exc,
                        attempt + 1,
                        self._max_attempts,
                        delay,
                    )
                    time.sleep(delay)
                else:
                    logger.warning(
                        "Primary transcription provider exhausted after %d attempt(s) "
                        "(last error: %s: %s)",
                        self._max_attempts,
                        type(exc).__name__,
                        exc,
                    )

        assert last_error is not None  # noqa: S101 — loop ran at least once
        if self._fallback is not None:
            logger.warning(
                "Falling back to secondary transcription provider after primary exhaustion",
            )
            return self._fallback.transcribe(audio_data, media_type, language)

        raise PrimaryExhaustedError(attempts=self._max_attempts, last_error=last_error)


# ---------------------------------------------------------------------------
# Shadow wrapper — run primary + shadow in parallel and log a diff
# ---------------------------------------------------------------------------


def _word_diff(primary: str, shadow: str) -> list[dict[str, str]]:
    """Return only the disagreeing word-level chunks."""
    p_words = primary.split()
    s_words = shadow.split()
    matcher = SequenceMatcher(None, p_words, s_words)
    diffs: list[dict[str, str]] = []
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op == "equal":
            continue
        diffs.append({
            "op": op,
            "primary": " ".join(p_words[i1:i2]),
            "shadow": " ".join(s_words[j1:j2]),
        })
    return diffs


class ShadowTranscriptionProvider:
    """Run primary + shadow in parallel, return primary, log diff (no full transcripts)."""

    def __init__(
        self,
        primary: TranscriptionProvider,
        shadow: TranscriptionProvider,
        shadow_label: str = "shadow",
    ) -> None:
        self._primary = primary
        self._shadow = shadow
        self._shadow_label = shadow_label

    @property
    def primary(self) -> TranscriptionProvider:
        return self._primary

    @property
    def shadow(self) -> TranscriptionProvider:
        return self._shadow

    def transcribe(
        self,
        audio_data: bytes,
        media_type: str,
        language: str = "en",
    ) -> TranscriptionResult:
        with ThreadPoolExecutor(max_workers=2) as pool:
            primary_future = pool.submit(
                self._primary.transcribe, audio_data, media_type, language,
            )
            shadow_future = pool.submit(
                self._shadow.transcribe, audio_data, media_type, language,
            )
            primary_result = primary_future.result()
            try:
                shadow_result = shadow_future.result()
            except Exception as exc:
                logger.warning(
                    "Shadow transcription (%s) failed: %s",
                    self._shadow_label,
                    exc,
                )
                return primary_result

        self._log_diff(primary_result, shadow_result)
        return primary_result

    def _log_diff(
        self,
        primary: TranscriptionResult,
        shadow: TranscriptionResult,
    ) -> None:
        similarity = SequenceMatcher(None, primary.text, shadow.text).ratio()
        diffs = _word_diff(primary.text, shadow.text)
        logger.info(
            "transcription_shadow_diff",
            extra={
                "primary_chars": len(primary.text),
                "shadow_chars": len(shadow.text),
                "similarity_ratio": round(similarity, 3),
                "primary_uncertain_count": len(primary.uncertain_spans),
                "shadow_uncertain_count": len(shadow.uncertain_spans),
                "diffs": diffs,
                "shadow_label": self._shadow_label,
            },
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


_DEFAULT_TRANSCRIPTION_MODELS: dict[str, str] = {
    "openai": "gpt-4o-transcribe",
    "gemini": "gemini-2.5-pro",
}


def _resolve_model(provider: str, configured: str) -> str:
    """Return the configured model if compatible with *provider*, else the default.

    Logs an INFO when an override happens — this protects against an OpenAI
    default leaking into a Gemini run (or vice versa) when the user changes
    `TRANSCRIPTION_PROVIDER` without also changing `TRANSCRIPTION_MODEL`.
    """
    default = _DEFAULT_TRANSCRIPTION_MODELS[provider]
    if not configured:
        return default
    if provider == "gemini" and (
        configured.startswith("gpt-") or configured.startswith("whisper")
    ):
        logger.info(
            "TRANSCRIPTION_MODEL=%r is not compatible with provider=gemini; "
            "using default %r",
            configured,
            default,
        )
        return default
    if provider == "openai" and configured.startswith("gemini"):
        logger.info(
            "TRANSCRIPTION_MODEL=%r is not compatible with provider=openai; "
            "using default %r",
            configured,
            default,
        )
        return default
    return configured


def _build_primary(
    provider: str,
    model: str,
    config: Config,
) -> TranscriptionProvider:
    if provider == "openai":
        return OpenAITranscribeProvider(
            api_key=config.openai_api_key,
            model=model,
            confidence_threshold=config.transcription_confidence_threshold,
            context_prompt=(
                build_whisper_prompt(config.ocr_context_dir)
                if config.transcription_context_enabled
                else ""
            ),
        )
    if provider == "gemini":
        return GeminiTranscribeProvider(
            api_key=config.google_api_key,
            model=model,
            context_dir=(
                config.ocr_context_dir
                if config.transcription_context_enabled
                else None
            ),
        )
    raise ValueError(
        f"Unknown transcription provider {provider!r} — must be 'openai' or 'gemini'"
    )


def _describe_stack(provider: TranscriptionProvider) -> str:
    """Return a short string describing the wrapper chain.

    Examples:
        ``openai/gpt-4o-transcribe``
        ``retrying(openai/gpt-4o-transcribe, fb=whisper-1)``
        ``shadow(retrying(openai/gpt-4o-transcribe, fb=whisper-1), gemini/gemini-2.5-pro)``
    """
    if isinstance(provider, ShadowTranscriptionProvider):
        inner = _describe_stack(provider._primary)
        shadow = _describe_stack(provider._shadow)
        return f"shadow({inner}, {shadow})"
    if isinstance(provider, RetryingTranscriptionProvider):
        inner = _describe_stack(provider._primary)
        if provider._fallback is not None:
            fb = _describe_stack(provider._fallback)
            return f"retrying({inner}, fb={fb})"
        return f"retrying({inner})"
    if isinstance(provider, OpenAITranscribeProvider):
        return f"openai/{provider._model}"
    if isinstance(provider, GeminiTranscribeProvider):
        return f"gemini/{provider._model}"
    return type(provider).__name__


def build_transcription_provider(config: Config) -> TranscriptionProvider:
    """Build the transcription provider stack from *config*.

    Composition (innermost → outermost):
        primary → optional retry+fallback wrapper → optional shadow wrapper.
    """
    primary_name = config.transcription_provider
    primary_model = _resolve_model(primary_name, config.transcription_model)
    provider: TranscriptionProvider = _build_primary(
        primary_name, primary_model, config,
    )

    if config.transcription_fallback_enabled:
        fallback_model = config.transcription_fallback_model
        # Avoid wrapping an OpenAI provider with an identical OpenAI fallback.
        if primary_name == "openai" and primary_model == fallback_model:
            logger.info(
                "Skipping fallback wrapper — primary and fallback both "
                "openai/%s",
                primary_model,
            )
        else:
            fallback = OpenAITranscribeProvider(
                api_key=config.openai_api_key,
                model=fallback_model,
                confidence_threshold=config.transcription_confidence_threshold,
                context_prompt="",
            )
            provider = RetryingTranscriptionProvider(
                primary=provider,
                fallback=fallback,
                max_attempts=config.transcription_retry_max_attempts,
                base_delay=config.transcription_retry_base_delay,
                max_delay=config.transcription_retry_max_delay,
            )

    shadow_name = config.transcription_shadow_provider
    if shadow_name:
        shadow_model = _resolve_model(
            shadow_name, config.transcription_shadow_model,
        )
        shadow_adapter = _build_primary(shadow_name, shadow_model, config)
        provider = ShadowTranscriptionProvider(
            primary=provider,
            shadow=shadow_adapter,
            shadow_label=f"{shadow_name}/{shadow_model}",
        )

    logger.info("Transcription stack: %s", _describe_stack(provider))
    return provider
