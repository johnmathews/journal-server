"""OCR Protocol and Anthropic adapter.

The Anthropic adapter supports optional "context priming" via static
markdown files. When `context_dir` is provided, every file in that
directory is loaded once at construction time and concatenated into the
system prompt. The resulting block is marked `cache_control` so
Anthropic can cache it across requests — cache hits are ~12.5× cheaper
than re-sending the context uncached.

The context is intended for proper-noun glossaries: family names,
place names, recurring topics — things that improve OCR accuracy on
handwritten text. See `docs/ocr-context.md` for the design rationale,
risks, and recommended content.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import anthropic
import tiktoken
from google import genai
from google.genai import types as genai_types

if TYPE_CHECKING:
    from pathlib import Path

    from journal.config import Config

logger = logging.getLogger(__name__)

# Sentinels the OCR model wraps around uncertain words or phrases.
# U+27EA / U+27EB (MATHEMATICAL LEFT/RIGHT DOUBLE ANGLE BRACKET) —
# chosen because they are extraordinarily unlikely to appear in real
# handwritten journal text, which makes the parser's single failure
# mode (treating a real bracket as a marker) practically impossible.
# If a user ever writes literal double angle brackets by hand, the
# parser will silently swallow them; that is an accepted tail risk.
UNCERTAIN_OPEN = "\u27EA"   # ⟪
UNCERTAIN_CLOSE = "\u27EB"  # ⟫

SYSTEM_PROMPT = (
    "You are an expert handwriting OCR system. Extract all text from the provided "
    "handwritten image as accurately as possible. Preserve paragraph breaks and line "
    "structure. Output only the extracted text with no commentary or preamble. "
    "When you are unsure about a word or short phrase — illegible strokes, ambiguous "
    "letters, a guess you cannot make with confidence — wrap that word or phrase in "
    "the sentinels \u27EA and \u27EB. Use the sentinels sparingly and only around the "
    "uncertain span itself, not around whole sentences. A span may cover one word or "
    "several consecutive words if they are jointly uncertain. Do not nest sentinels."
)


@dataclass(frozen=True)
class OCRResult:
    """Result of an OCR extraction.

    `text` is the clean extraction with all sentinels stripped.
    `uncertain_spans` is a list of `(char_start, char_end)` half-open
    offsets into `text` — each pair covers one contiguous region the
    model flagged as uncertain. Spans are sorted by `char_start` and
    do not overlap.
    """

    text: str
    uncertain_spans: list[tuple[int, int]] = field(default_factory=list)


def parse_uncertain_markers(raw: str) -> tuple[str, list[tuple[int, int]]]:
    """Strip ⟪/⟫ sentinels from `raw` and extract uncertain span offsets.

    Returns `(clean_text, spans)` where `spans` is a list of
    `(char_start, char_end)` half-open offsets into `clean_text`.

    The parser is deliberately forgiving — OCR output that arrives with
    unmatched, nested, or empty sentinels is parsed without raising.
    Specifically:

    - **Unmatched open** (`⟪` with no closing `⟫`): the open is dropped
      silently. The characters that were going to be in the span are
      still copied into `clean_text` exactly as they appeared.
    - **Unmatched close** (`⟫` with no preceding `⟪`): the close is
      dropped silently. No span is recorded.
    - **Nested sentinels** (`⟪foo ⟪bar⟫ baz⟫`): collapsed to the
      outermost pair. Only the outer span is recorded.
    - **Empty pair** (`⟪⟫`): dropped. No span is recorded.
    - **Whitespace-only pair** (`⟪   ⟫`): dropped. No span is recorded.
    - **Whitespace immediately inside** a pair is trimmed *out* of the
      span. The span points at letters, not padding.

    A single warning is logged per call if any sentinels were dropped,
    so malformed model output is visible in logs without being noisy.
    """
    clean: list[str] = []
    spans: list[tuple[int, int]] = []
    open_at: int | None = None
    depth = 0
    drops = 0
    for ch in raw:
        if ch == UNCERTAIN_OPEN:
            if depth == 0:
                open_at = len(clean)
            else:
                drops += 1  # nested open — collapse to outermost
            depth += 1
            continue
        if ch == UNCERTAIN_CLOSE:
            if depth == 0:
                drops += 1  # unmatched close
                continue
            depth -= 1
            if depth == 0 and open_at is not None:
                start = open_at
                end = len(clean)
                while start < end and clean[start].isspace():
                    start += 1
                while end > start and clean[end - 1].isspace():
                    end -= 1
                if end > start:
                    spans.append((start, end))
                else:
                    drops += 1  # empty or whitespace-only pair
                open_at = None
            continue
        clean.append(ch)

    if depth > 0:
        drops += 1  # unmatched open at end of input

    if drops:
        logger.warning(
            "OCR sentinel parser dropped %d malformed marker(s); "
            "text was preserved but some uncertainty spans may be missing",
            drops,
        )

    return "".join(clean), spans

# Minimum tokens for a cacheable block on Claude Opus 4.6. Below this
# the Anthropic API silently ignores cache_control and bills the block
# as a normal input token for every request. The provider logs a
# warning if the composed system text is smaller than this.
CACHEABLE_MINIMUM_TOKENS = 4096

# Instructions that always ride alongside the glossary. These exist
# primarily to defend against the "hallucinated substitution" failure
# mode — the model replacing an ambiguous scribble with a glossary
# entry that isn't actually what was written.
CONTEXT_USAGE_INSTRUCTIONS = (
    "\n\nThe sections below contain proper nouns (people, places, topics) "
    "that appear frequently in this author's handwritten journal. Use them "
    "as a candidate list ONLY — prefer a glossary spelling when the "
    "handwritten token is visually consistent with the entry, but do NOT "
    "substitute for the sake of matching. If a word is ambiguous AND does "
    "not match any glossary entry, transcribe exactly what you see, even "
    "if it looks like a typo. Never invent a glossary match that isn't "
    "supported by the pen strokes on the page."
)


def load_context_files(context_dir: Path | None) -> str:
    """Load and concatenate all markdown files in the context directory.

    Returns an empty string if `context_dir` is None, doesn't exist, or
    contains no `.md` files. Files are read in alphabetical order (so
    the composed blob is deterministic across restarts) and each one is
    prefixed with a `# <filename>` header so the model can tell them
    apart when they overlap (e.g. someone named after a place).

    Reads are best-effort: if any individual file is unreadable the
    error is logged and that file is skipped rather than failing the
    whole server startup.
    """
    if context_dir is None:
        return ""
    if not context_dir.exists() or not context_dir.is_dir():
        logger.warning(
            "OCR context dir %s does not exist — skipping context priming",
            context_dir,
        )
        return ""

    files = sorted(context_dir.glob("*.md"))
    if not files:
        logger.info(
            "OCR context dir %s has no *.md files — skipping context priming",
            context_dir,
        )
        return ""

    parts: list[str] = []
    for path in files:
        try:
            content = path.read_text(encoding="utf-8").strip()
        except OSError as e:
            logger.warning(
                "Failed to read OCR context file %s: %s — skipping", path, e
            )
            continue
        if not content:
            continue
        # Derive a heading from the filename stem so the model has a
        # category label for each section.
        heading = path.stem.replace("_", " ").replace("-", " ").strip()
        parts.append(f"# {heading}\n\n{content}")

    if not parts:
        return ""

    return "\n\n".join(parts)


def _build_cache_control(ttl: str) -> dict[str, str]:
    """Build a `cache_control` block from a TTL string.

    Anthropic supports two cache tiers: the default 5-minute ephemeral
    cache and an optional 1-hour cache. 1-hour is cheaper amortized
    when an ingestion session involves more than a handful of requests.
    """
    if ttl == "5m":
        return {"type": "ephemeral"}
    if ttl == "1h":
        return {"type": "ephemeral", "ttl": "1h"}
    raise ValueError(
        f"Invalid OCR context cache TTL {ttl!r} — must be '5m' or '1h'"
    )


@runtime_checkable
class OCRProvider(Protocol):
    """Protocol for OCR providers."""

    def extract(self, image_data: bytes, media_type: str) -> OCRResult: ...


class AnthropicOCRProvider:
    """OCR provider using Anthropic's Claude vision API."""

    def __init__(
        self,
        api_key: str,
        model: str,
        max_tokens: int,
        context_dir: Path | None = None,
        cache_ttl: str = "5m",
    ) -> None:
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model
        self._max_tokens = max_tokens
        self._cache_control = _build_cache_control(cache_ttl)

        # Compose the system text once at construction time. Startup is
        # the only time context files are read — restarting the server
        # is the intended way to reload context after editing it.
        context_text = load_context_files(context_dir)
        if context_text:
            self._system_text = (
                SYSTEM_PROMPT + CONTEXT_USAGE_INSTRUCTIONS + "\n\n" + context_text
            )
            logger.info(
                "OCR context loaded from %s (%d chars)",
                context_dir,
                len(context_text),
            )
        else:
            self._system_text = SYSTEM_PROMPT

        self._warn_if_below_cache_minimum()

    def _warn_if_below_cache_minimum(self) -> None:
        """Log a loud warning if the composed system text won't cache.

        Anthropic silently ignores cache_control on blocks below
        CACHEABLE_MINIMUM_TOKENS, so misconfigured context_dirs end up
        paying full per-request input cost with no user-visible error.
        The warning gives the user a single log line they can search
        for if their context-primed OCR becomes unexpectedly expensive.
        """
        try:
            encoder = tiktoken.encoding_for_model("gpt-4")
        except KeyError:
            encoder = tiktoken.get_encoding("cl100k_base")
        # cl100k_base is not Claude's actual tokenizer, but it is a
        # close-enough proxy for "is this block big enough to cache?".
        # Anthropic doesn't ship a Python tokenizer for Claude that
        # the server can use offline.
        token_count = len(encoder.encode(self._system_text))
        if token_count < CACHEABLE_MINIMUM_TOKENS:
            logger.warning(
                "OCR system text is %d tokens (approx) — below the %d-token "
                "cache minimum for %s. cache_control will be silently "
                "ignored and every request will pay full input price. "
                "Add more context files or increase their size to enable "
                "caching.",
                token_count,
                CACHEABLE_MINIMUM_TOKENS,
                self._model,
            )
        else:
            logger.info(
                "OCR system text is %d tokens — cache eligible on %s",
                token_count,
                self._model,
            )

    def extract(self, image_data: bytes, media_type: str) -> OCRResult:
        """Extract text from an image via Anthropic's vision API.

        The model is prompted to wrap uncertain words or phrases in
        ⟪/⟫ sentinels. This method strips the sentinels out of the
        response and returns an `OCRResult` carrying both the clean
        text and the list of uncertain span offsets (into the clean
        text). See `parse_uncertain_markers` for the parser's
        tolerance of malformed markers.
        """
        logger.info("Extracting text via Anthropic OCR (model=%s)", self._model)

        encoded_image = base64.standard_b64encode(image_data).decode("utf-8")

        message = self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=[
                {
                    "type": "text",
                    "text": self._system_text,
                    "cache_control": self._cache_control,
                }
            ],
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": encoded_image,
                            },
                        },
                        {
                            "type": "text",
                            "text": "Extract all handwritten text from this image.",
                        },
                    ],
                }
            ],
        )

        raw = message.content[0].text
        clean_text, spans = parse_uncertain_markers(raw)
        logger.info(
            "OCR extraction complete (%d characters, %d uncertain span(s))",
            len(clean_text),
            len(spans),
        )
        return OCRResult(text=clean_text, uncertain_spans=spans)

    def extract_text(self, image_data: bytes, media_type: str) -> str:
        """Backward-compatible wrapper returning only the clean text.

        Prefer `extract(...)` for new call sites — it exposes the
        uncertainty spans that drive the webapp's Review toggle. This
        wrapper stays available for simple callers (CLIs, one-off
        scripts) that only need a string.
        """
        return self.extract(image_data, media_type).text


class GeminiOCRProvider:
    """OCR provider using Google's Gemini vision API."""

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-2.5-pro",
        context_dir: Path | None = None,
    ) -> None:
        self._client = genai.Client(api_key=api_key)
        self._model = model

        context_text = load_context_files(context_dir)
        if context_text:
            self._system_text = (
                SYSTEM_PROMPT + CONTEXT_USAGE_INSTRUCTIONS + "\n\n" + context_text
            )
            logger.info(
                "Gemini OCR context loaded from %s (%d chars)",
                context_dir,
                len(context_text),
            )
        else:
            self._system_text = SYSTEM_PROMPT

    def extract(self, image_data: bytes, media_type: str) -> OCRResult:
        """Extract text from an image via Google's Gemini vision API.

        Uses the same system prompt, context glossary, and ⟪/⟫ uncertainty
        sentinels as the Anthropic provider so the downstream pipeline
        (sentinel parser, uncertain_spans, webapp Review toggle) works
        identically.
        """
        logger.info("Extracting text via Gemini OCR (model=%s)", self._model)

        response = self._client.models.generate_content(
            model=self._model,
            contents=[
                genai_types.Part.from_bytes(data=image_data, mime_type=media_type),
                "Extract all handwritten text from this image.",
            ],
            config=genai_types.GenerateContentConfig(
                system_instruction=self._system_text,
            ),
        )

        raw = response.text
        clean_text, spans = parse_uncertain_markers(raw)
        logger.info(
            "OCR extraction complete (%d characters, %d uncertain span(s))",
            len(clean_text),
            len(spans),
        )
        return OCRResult(text=clean_text, uncertain_spans=spans)

    def extract_text(self, image_data: bytes, media_type: str) -> str:
        """Backward-compatible wrapper returning only the clean text."""
        return self.extract(image_data, media_type).text


_DEFAULT_MODELS: dict[str, str] = {
    "anthropic": "claude-opus-4-6",
    "gemini": "gemini-2.5-pro",
}


def build_ocr_provider(config: Config) -> OCRProvider:
    """Build the OCR provider specified by config.ocr_provider."""
    provider_name = config.ocr_provider
    model = config.ocr_model or _DEFAULT_MODELS.get(provider_name, "")
    if provider_name == "anthropic":
        return AnthropicOCRProvider(
            api_key=config.anthropic_api_key,
            model=model,
            max_tokens=config.ocr_max_tokens,
            context_dir=config.ocr_context_dir,
            cache_ttl=config.ocr_context_cache_ttl,
        )
    if provider_name == "gemini":
        return GeminiOCRProvider(
            api_key=config.google_api_key,
            model=model,
            context_dir=config.ocr_context_dir,
        )
    raise ValueError(
        f"Unknown OCR provider {provider_name!r} — must be 'anthropic' or 'gemini'"
    )
