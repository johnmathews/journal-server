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
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import anthropic
import tiktoken
from google import genai
from google.genai import types as genai_types

from journal.services import usage

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
    "handwritten image as accurately as possible.\n\n"
    "Output continuous prose. Do NOT preserve the physical line breaks from the "
    "page — when a sentence wraps to the next visual line because it ran out of "
    "horizontal space, join the lines with a single space. A line break in the "
    "image is an artifact of the page width, not part of the text.\n\n"
    "Use a paragraph break (two consecutive newlines) ONLY between distinct "
    "paragraphs, where the author left a clear visual gap, indented a new line, "
    "or started a new thought. When in doubt, prefer fewer paragraph breaks.\n\n"
    "If the author crossed out a word or phrase — a line struck through it — that "
    "text is a mistake the author deleted. Omit it entirely: do not transcribe "
    "crossed-out text and do not mark it with strikethrough. Keep only what the "
    "author left standing.\n\n"
    "Output plain text, not Markdown. Never escape punctuation with backslashes: "
    "if the page shows ***, output *** — not \\*\\*\\*. Reproduce every "
    "punctuation character exactly as the author wrote it.\n\n"
    "Output only the extracted text with no commentary or preamble. "
    "When you are unsure about a word or short phrase — illegible strokes, ambiguous "
    "letters, a guess you cannot make with confidence — wrap that word or phrase in "
    "the sentinels \u27EA and \u27EB. Use the sentinels sparingly and only around the "
    "uncertain span itself, not around whole sentences. A span may cover one word or "
    "several consecutive words if they are jointly uncertain. Do not nest sentinels."
)


class PageRole(StrEnum):
    """Where a page sits in a multi-page upload — drives the OCR prompt."""

    FIRST = "first"
    MIDDLE = "middle"
    LAST = "last"
    ONLY = "only"


ENTRY_BEGINS = "<<<ENTRY BEGINS>>>"
ENTRY_ENDS = "<<<ENTRY ENDS>>>"

_ROLE_CLAUSES: dict[PageRole, str] = {
    PageRole.FIRST: (
        "\n\nThis is the FIRST page of a journal entry that continues onto "
        "later pages. If text belonging to a PREVIOUS, already-finished entry "
        f"sits above this entry's first line, emit `{ENTRY_BEGINS}` on its own "
        "line immediately before this entry's first line. Never emit "
        f"`{ENTRY_ENDS}` — the entry continues past this page."
    ),
    PageRole.MIDDLE: (
        "\n\nThis is a MIDDLE page of a single ongoing entry — a pure "
        f"continuation. Do NOT emit `{ENTRY_BEGINS}` or `{ENTRY_ENDS}`."
    ),
    PageRole.LAST: (
        "\n\nThis is the LAST page of the entry; the entry ends on this page. "
        "If a DIFFERENT, new entry begins below where this entry ends (for "
        f"example a fresh date heading), emit `{ENTRY_ENDS}` on its own line "
        "immediately after this entry's last line. Never emit "
        f"`{ENTRY_BEGINS}`."
    ),
    PageRole.ONLY: (
        "\n\nThis image is a COMPLETE entry on a single page. If a previous "
        f"entry's tail sits above it, emit `{ENTRY_BEGINS}` on its own line "
        "immediately before this entry's first line. If a different, new entry "
        f"begins below it, emit `{ENTRY_ENDS}` on its own line immediately "
        "after this entry's last line. Emit each marker at most once."
    ),
}


def role_prompt_clause(role: PageRole | None) -> str:
    """Return the system-prompt addendum for a page role (``""`` if None)."""
    if role is None:
        return ""
    return _ROLE_CLAUSES[role]


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

# Crossed-out handwriting (a word with a line struck through it) is a
# mistake the author deleted, so it must not reach the entry text. The
# OCR model represents such words with Markdown strikethrough (~~word~~).
# This regex matches one strikethrough span. It is deliberately:
#   * non-greedy (``.+?``) so ``~~a~~b~~c~~`` removes the two struck
#     words and keeps ``b`` between them, rather than swallowing ``b``;
#   * single-line (``.`` excludes ``\n``) so an unmatched ``~~`` cannot
#     eat across a paragraph break. A struck phrase that wraps across a
#     physical line is the accepted tail risk — the system prompt also
#     instructs the model to omit crossed-out text, so the stripper is a
#     safety net rather than the sole mechanism.
_STRIKETHROUGH_RE = re.compile(r"~~.+?~~")


def strip_strikethrough(text: str) -> str:
    """Remove Markdown strikethrough (``~~crossed out~~``) spans from *text*.

    Words the author struck through are mistakes they deleted; they must
    not appear in the saved entry. The OCR model emits them as Markdown
    strikethrough, which this function removes along with the whitespace
    the removal would otherwise strand:

    - runs of spaces/tabs left behind collapse to a single space
      (``"to  the"`` → ``"to the"``);
    - a space stranded before sentence punctuation is dropped
      (``"happy ."`` → ``"happy."``);
    - leading/trailing horizontal whitespace on each line is trimmed,
      and the whole result is stripped.

    Paragraph breaks (``\\n``) are preserved. A lone, unmatched ``~~`` is
    not a strikethrough span and is left untouched.

    Run this on the raw model output **before** ``parse_uncertain_markers``
    so the uncertain-span character offsets are computed against the
    already-stripped text and stay valid.
    """
    without = _STRIKETHROUGH_RE.sub("", text)
    without = re.sub(r"[ \t]{2,}", " ", without)
    without = re.sub(r" +([,.;:!?])", r"\1", without)
    without = re.sub(r"[ \t]+\n", "\n", without)
    without = re.sub(r"\n[ \t]+", "\n", without)
    return without.strip()


# The OCR models are trained on Markdown, so punctuation that has Markdown
# meaning comes back backslash-escaped: a *** divider centered on the page
# arrives as \*\*\* (a "protected" thematic break), snake_case as
# snake\_case, and so on. Entry text is plain text — every such escape is
# a character the author never wrote. The character class is CommonMark's
# escapable set: exactly the ASCII punctuation range.
_MARKDOWN_ESCAPE_RE = re.compile(r"\\([!-/:-@\[-`{-~])")


def strip_markdown_escapes(text: str) -> str:
    """Remove Markdown backslash escapes (``\\*`` → ``*``) from *text*.

    Applies to every CommonMark-escapable character (ASCII punctuation),
    including ``\\\\`` → ``\\`` — so an author's literal backslash the
    model escaped round-trips correctly. Backslashes before letters,
    digits, or non-ASCII punctuation are not Markdown escapes and are
    left untouched.

    Run this on the raw model output **before** ``parse_uncertain_markers``
    (it changes character counts) and **after** ``strip_strikethrough``
    (so a handwritten literal ``\\~\\~`` unescapes to ``~~`` only after
    the strikethrough stripper has run, and survives).
    """
    return _MARKDOWN_ESCAPE_RE.sub(r"\1", text)


def reflow_paragraphs(text: str) -> str:
    """Replace hard line breaks within paragraphs with spaces.

    Some OCR models (notably Gemini) preserve the physical line breaks
    from the handwritten page, producing many short lines. This function
    collapses single newlines into spaces while preserving paragraph
    breaks (two or more consecutive newlines).

    Because each ``\\n`` is replaced with a single space (1-for-1), the
    character count is unchanged and any character-offset spans (e.g.
    uncertain_spans) remain valid without adjustment.
    """
    return re.sub(r"(?<!\n)\n(?!\n)", " ", text)


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

    def extract(
        self, image_data: bytes, media_type: str, page_role: PageRole | None = None
    ) -> OCRResult: ...


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

    @property
    def model(self) -> str:
        return self._model

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

    def extract(
        self, image_data: bytes, media_type: str, page_role: PageRole | None = None
    ) -> OCRResult:
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

        user_text = "Extract all handwritten text from this image."
        user_text += role_prompt_clause(page_role)

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
                            "text": user_text,
                        },
                    ],
                }
            ],
        )

        usage.record_anthropic(self._model, message)

        raw = message.content[0].text
        # Drop crossed-out (struck-through) words before parsing sentinels
        # so the uncertain-span offsets are anchored to the final text.
        raw = strip_strikethrough(raw)
        # Undo Markdown backslash escapes (\*\*\* → ***) — also before the
        # sentinel parser, because unescaping changes character counts.
        raw = strip_markdown_escapes(raw)
        clean_text, spans = parse_uncertain_markers(raw)
        # The OCR prompt asks the model to reflow line wraps as a single
        # space, but Anthropic (like Gemini) occasionally preserves a
        # physical line break from the page. Reflow as a safety net —
        # single \n → space, \n\n+ kept. Character offsets are unchanged
        # so uncertain_spans don't need re-anchoring.
        clean_text = reflow_paragraphs(clean_text)
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

    @property
    def model(self) -> str:
        return self._model

    def extract(
        self, image_data: bytes, media_type: str, page_role: PageRole | None = None
    ) -> OCRResult:
        """Extract text from an image via Google's Gemini vision API.

        Uses the same system prompt, context glossary, and ⟪/⟫ uncertainty
        sentinels as the Anthropic provider so the downstream pipeline
        (sentinel parser, uncertain_spans, webapp Review toggle) works
        identically.
        """
        logger.info("Extracting text via Gemini OCR (model=%s)", self._model)

        user_text = "Extract all handwritten text from this image."
        user_text += role_prompt_clause(page_role)

        response = self._client.models.generate_content(
            model=self._model,
            contents=[
                genai_types.Part.from_bytes(data=image_data, mime_type=media_type),
                user_text,
            ],
            config=genai_types.GenerateContentConfig(
                system_instruction=self._system_text,
            ),
        )

        usage.record_gemini(self._model, response)

        raw = response.text
        # Drop crossed-out (struck-through) words before parsing sentinels
        # so the uncertain-span offsets are anchored to the final text.
        raw = strip_strikethrough(raw)
        # Undo Markdown backslash escapes (\*\*\* → ***) — also before the
        # sentinel parser, because unescaping changes character counts.
        raw = strip_markdown_escapes(raw)
        clean_text, spans = parse_uncertain_markers(raw)
        # Gemini preserves physical line breaks from the handwritten page.
        # Reflow into natural paragraphs — single \n → space, \n\n+ kept.
        clean_text = reflow_paragraphs(clean_text)
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


# ---------------------------------------------------------------------------
# Dual-pass OCR: reconciliation and composite provider
# ---------------------------------------------------------------------------


def _tokenize_with_positions(text: str) -> list[tuple[str, int, int]]:
    """Split *text* on whitespace, returning ``(word, char_start, char_end)``."""
    tokens: list[tuple[str, int, int]] = []
    i = 0
    n = len(text)
    while i < n:
        # skip whitespace
        while i < n and text[i].isspace():
            i += 1
        if i >= n:
            break
        start = i
        while i < n and not text[i].isspace():
            i += 1
        tokens.append((text[start:i], start, i))
    return tokens


def _merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Sort spans by start and merge overlapping/adjacent ones."""
    if not spans:
        return []
    sorted_spans = sorted(spans)
    merged: list[tuple[int, int]] = [sorted_spans[0]]
    for start, end in sorted_spans[1:]:
        prev_start, prev_end = merged[-1]
        if start <= prev_end:
            merged[-1] = (prev_start, max(prev_end, end))
        else:
            merged.append((start, end))
    return merged


def _any_span_overlap(
    spans: list[tuple[int, int]], start: int, end: int,
) -> bool:
    """Return True if any span in *spans* overlaps the half-open range [start, end)."""
    return any(s < end and e > start for s, e in spans)


def reconcile_ocr_results(
    primary: OCRResult,
    secondary: OCRResult,
) -> OCRResult:
    """Reconcile two OCR results using sentinel-based confidence signals.

    Uses a word-level diff to align the two texts, then decides per-block:

    * **Equal blocks** — both models agree on the words.  Uncertain spans
      from either provider are carried forward as doubts.
    * **Disagreement blocks** — the two models read different words.
      The decision depends on which model flagged sentinels:

      - *Primary uncertain, secondary confident* → substitute the
        secondary text into the output and mark it as a doubt.
      - *Secondary uncertain, primary confident* → keep primary text,
        mark as a doubt.
      - *Both uncertain* → keep primary text, mark as a doubt.
      - *Neither uncertain* → keep primary text, **no doubt**.  Both
        models are confident; trust the primary.

    The output text may therefore differ from ``primary.text`` when
    secondary text is substituted into uncertain primary regions.
    """
    from difflib import SequenceMatcher

    primary_tokens = _tokenize_with_positions(primary.text)
    secondary_tokens = _tokenize_with_positions(secondary.text)

    if not primary_tokens:
        return OCRResult(
            text=primary.text,
            uncertain_spans=list(primary.uncertain_spans),
        )

    primary_words = [t[0] for t in primary_tokens]
    secondary_words = [t[0] for t in secondary_tokens]

    matcher = SequenceMatcher(
        None, primary_words, secondary_words, autojunk=False,
    )

    # Build output text and doubt spans in a single pass.
    parts: list[str] = []
    out_pos = 0          # running character position in output
    pri_cursor = 0       # how far we have consumed primary.text
    doubt_spans: list[tuple[int, int]] = []

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            if i1 >= i2:
                continue
            block_end = primary_tokens[i2 - 1][2]
            chunk = primary.text[pri_cursor:block_end]
            parts.append(chunk)

            # Carry primary uncertain spans that overlap this chunk.
            for sp_s, sp_e in primary.uncertain_spans:
                if sp_s < block_end and sp_e > pri_cursor:
                    adj_s = out_pos + max(0, sp_s - pri_cursor)
                    adj_e = out_pos + min(len(chunk), sp_e - pri_cursor)
                    if adj_e > adj_s:
                        doubt_spans.append((adj_s, adj_e))

            # Map secondary uncertain spans to output coordinates.
            for sp_s, sp_e in secondary.uncertain_spans:
                for sec_idx in range(j1, j2):
                    _, s_start, s_end = secondary_tokens[sec_idx]
                    if s_start >= sp_e or s_end <= sp_s:
                        continue
                    pri_idx = i1 + (sec_idx - j1)
                    _, p_start, p_end = primary_tokens[pri_idx]
                    adj_s = out_pos + (p_start - pri_cursor)
                    adj_e = out_pos + (p_end - pri_cursor)
                    doubt_spans.append((adj_s, adj_e))

            out_pos += len(chunk)
            pri_cursor = block_end

        else:
            # Disagreement block.
            if i1 >= i2:
                # Secondary-only insertion — nothing to emit.
                continue

            pri_block_start = primary_tokens[i1][1]
            pri_block_end = primary_tokens[i2 - 1][2]

            # Emit any whitespace gap between the last consumed
            # position and the start of this block.
            if pri_cursor < pri_block_start:
                ws = primary.text[pri_cursor:pri_block_start]
                parts.append(ws)
                out_pos += len(ws)

            # Check sentinel coverage in both providers.
            pri_uncertain = _any_span_overlap(
                primary.uncertain_spans, pri_block_start, pri_block_end,
            )

            sec_uncertain = False
            sec_text = ""
            if j1 < j2:
                sec_start = secondary_tokens[j1][1]
                sec_end = secondary_tokens[j2 - 1][2]
                sec_uncertain = _any_span_overlap(
                    secondary.uncertain_spans, sec_start, sec_end,
                )
                sec_text = secondary.text[sec_start:sec_end]

            if pri_uncertain and not sec_uncertain and j1 < j2:
                # Primary uncertain, secondary confident → substitute.
                parts.append(sec_text)
                doubt_spans.append((out_pos, out_pos + len(sec_text)))
                out_pos += len(sec_text)
            elif pri_uncertain or sec_uncertain:
                # At least one model uncertain → keep primary, doubt.
                pri_text = primary.text[pri_block_start:pri_block_end]
                parts.append(pri_text)
                doubt_spans.append((out_pos, out_pos + len(pri_text)))
                out_pos += len(pri_text)
            else:
                # Neither uncertain → keep primary, no doubt.
                pri_text = primary.text[pri_block_start:pri_block_end]
                parts.append(pri_text)
                out_pos += len(pri_text)

            pri_cursor = pri_block_end

    # Trailing text after the last token (e.g. trailing newline).
    if pri_cursor < len(primary.text):
        parts.append(primary.text[pri_cursor:])

    return OCRResult(
        text="".join(parts),
        uncertain_spans=_merge_spans(doubt_spans),
    )


class DualPassOCRProvider:
    """OCR provider that runs two providers concurrently and reconciles.

    Implements the ``OCRProvider`` Protocol. Uses each model's own
    sentinel-based confidence signal: only regions where at least one
    model is uncertain become doubts.  When the primary is uncertain but
    the secondary is confident, the secondary's text is substituted in.
    Confident disagreements (neither model used sentinels) are resolved
    silently in favour of the primary.
    """

    def __init__(self, primary: OCRProvider, secondary: OCRProvider) -> None:
        self._primary = primary
        self._secondary = secondary

    @property
    def primary(self) -> OCRProvider:
        return self._primary

    @property
    def secondary(self) -> OCRProvider:
        return self._secondary

    def extract(
        self, image_data: bytes, media_type: str, page_role: PageRole | None = None
    ) -> OCRResult:
        import contextvars
        from concurrent.futures import ThreadPoolExecutor

        # copy_context().run propagates the active usage_scope collector
        # (services/usage.py) into the two sub-threads so their token
        # counts land on the parent job — a thread-local would miss them.
        with ThreadPoolExecutor(max_workers=2) as pool:
            primary_future = pool.submit(
                contextvars.copy_context().run,
                self._primary.extract, image_data, media_type, page_role,
            )
            secondary_future = pool.submit(
                contextvars.copy_context().run,
                self._secondary.extract, image_data, media_type, page_role,
            )
            primary_result = primary_future.result()
            secondary_result = secondary_future.result()

        reconciled = reconcile_ocr_results(primary_result, secondary_result)
        logger.info(
            "Dual-pass OCR: primary %d chars / %d spans, "
            "secondary %d chars / %d spans → reconciled %d spans",
            len(primary_result.text),
            len(primary_result.uncertain_spans),
            len(secondary_result.text),
            len(secondary_result.uncertain_spans),
            len(reconciled.uncertain_spans),
        )
        return reconciled


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def _build_single_provider(config: Config) -> OCRProvider:
    """Build the single OCR provider specified by ``config.ocr_provider``."""
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


def _build_dual_pass_provider(config: Config) -> DualPassOCRProvider:
    """Build a dual-pass provider: Anthropic primary, Gemini secondary.

    ``config.ocr_model`` is ignored — each provider always uses its own
    default because a single model override cannot apply to both APIs.
    """
    primary = AnthropicOCRProvider(
        api_key=config.anthropic_api_key,
        model=_DEFAULT_MODELS["anthropic"],
        max_tokens=config.ocr_max_tokens,
        context_dir=config.ocr_context_dir,
        cache_ttl=config.ocr_context_cache_ttl,
    )
    secondary = GeminiOCRProvider(
        api_key=config.google_api_key,
        model=_DEFAULT_MODELS["gemini"],
        context_dir=config.ocr_context_dir,
    )
    return DualPassOCRProvider(primary=primary, secondary=secondary)


def build_ocr_provider(config: Config) -> OCRProvider:
    """Build the OCR provider specified by config.

    When ``config.ocr_dual_pass`` is true, both Anthropic and Gemini
    providers are instantiated and wrapped in a ``DualPassOCRProvider``.
    """
    if config.ocr_dual_pass:
        return _build_dual_pass_provider(config)
    return _build_single_provider(config)
