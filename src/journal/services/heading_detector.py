"""Date-heading detector — promotes a leading date into a markdown heading."""

from __future__ import annotations

import datetime
import json
import logging
import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

log = logging.getLogger(__name__)

# Detector inspects only the leading slice of text — anything past this point can't be the
# heading anyway, and limiting input keeps LLM cost predictable.
_DETECTION_WINDOW_CHARS = 300

# Plausible journal-date range. The LLM occasionally hallucinates dates like
# "0001-01-01" or "9999-12-31"; reject anything outside a generous window
# around the present so a parser glitch can't silently file an entry under
# the year 1 or year 9999.
_MIN_PLAUSIBLE_YEAR = 1900
_MAX_PLAUSIBLE_YEAR = 2100


def _validate_iso_date(raw: object) -> str | None:
    """Return the value if it parses as an ISO 8601 date in a plausible range.

    Returns None when ``raw`` is missing, the wrong type, malformed, or
    outside ``[_MIN_PLAUSIBLE_YEAR, _MAX_PLAUSIBLE_YEAR]``. Used to guard
    the LLM's ``iso_date`` field — see ``AnthropicHeadingDetector.detect``.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.date.fromisoformat(raw.strip())
    except ValueError:
        log.warning("Heading detector iso_date is not a valid ISO date: %r", raw[:40])
        return None
    if not (_MIN_PLAUSIBLE_YEAR <= parsed.year <= _MAX_PLAUSIBLE_YEAR):
        log.warning("Heading detector iso_date out of range: %r", parsed.isoformat())
        return None
    return parsed.isoformat()

SYSTEM_PROMPT = """\
You normalize the start of a journal entry. Users often dictate or write a date (sometimes
followed by a time) at the very start, before describing their day.

Given text from the start of a journal entry, decide whether it begins with a date that
should be lifted into a markdown heading.

These COUNT as a heading:
- Numeric: "28 April 2026", "April 28th, 2026", "April 28th"
- Spelled-out: "the twenty-eighth of April two thousand and twenty-six"
- Relative: "today", "yesterday", "tomorrow"
- With a time: "28 April 2026, 9am" or "Today at 9.30am"

These do NOT count as a heading — return is_heading=false:
- A date that appears mid-sentence: "I went to Berlin on April 28th"
- Text that already starts with a markdown heading (`# ...`)
- Any text where the date is not the very first content

If entry_date is provided in the user message (ISO 8601), use it to resolve relative phrases
("today", "yesterday") into the canonical absolute form.

Respond with ONLY a JSON object on a single line, no other text, no markdown fences. The
shape is one of these (formatted here across lines for clarity — your output stays on one
line):

{
  "is_heading": true,
  "heading_text": "28 April 2026",
  "iso_date": "2026-04-28",
  "source_phrase": "April 28th. "
}

{
  "is_heading": false,
  "heading_text": null,
  "iso_date": null,
  "source_phrase": null
}

Where:
- heading_text: the canonical form to use as the heading (e.g. "28 April 2026" or
  "28 April 2026, 9am"). Use a clean, consistent format. No trailing punctuation.
  Day-month-year order. Lowercase the time-of-day suffix ("9am", not "9AM").
- iso_date: the same date in ISO 8601 form ("YYYY-MM-DD"). REQUIRED when is_heading=true.
  Resolve relative phrases against entry_date if given. If the year is missing from the
  input AND entry_date is not provided, return is_heading=false instead of guessing.
  Never include the time component — the date is calendar-day only.
- source_phrase: the EXACT verbatim substring from the start of the input text that became
  the heading, INCLUDING any trailing punctuation and whitespace. This is used as a
  bounds check (it must be a verbatim prefix of the input) — it is NOT removed from the
  body, which keeps the date phrase intact as the user wrote it.
  Example — input "April 28th. Today I went...", source_phrase is "April 28th. "
  (eleven characters plus a trailing space, total 12 chars).
"""


@dataclass(frozen=True)
class HeadingDetectionResult:
    """Outcome of running the heading detector on a piece of text.

    `heading_text` is the canonical heading form (e.g. ``"28 April 2026"``) when a heading
    was detected, otherwise the empty string. `body` is the input with heading-area leading
    whitespace dropped — the date phrase itself is left in place. When no heading is
    detected, `body` is the original input verbatim.

    `date_iso` is the detected date in ISO 8601 ``YYYY-MM-DD`` form when the LLM resolved
    one (including relative phrases like "today" / "yesterday" against the entry_date hint).
    Ingestion uses it to set the entry's ``entry_date`` so a backdated dictation that begins
    with the actual date doesn't end up filed under "today". ``None`` when no heading was
    detected, when the LLM didn't return a usable iso_date, or for the null detector.
    """

    heading_text: str
    body: str
    date_iso: str | None = None

    @property
    def has_heading(self) -> bool:
        return bool(self.heading_text)


@runtime_checkable
class HeadingDetector(Protocol):
    """Protocol for the date-heading detection step."""

    def detect(
        self, text: str, entry_date: str | None = None
    ) -> HeadingDetectionResult: ...


class NullHeadingDetector:
    """No-op detector — every input is returned with no heading."""

    def detect(
        self, text: str, entry_date: str | None = None
    ) -> HeadingDetectionResult:
        return HeadingDetectionResult(heading_text="", body=text)


class AnthropicHeadingDetector:
    """Lifts a leading date in *text* into a markdown heading using Anthropic Haiku.

    Fails safe — any error in the LLM call, response parsing, or sanity checks results
    in a no-heading result with the original text as the body. The pipelines that consume
    this detector therefore never need to wrap the call in their own try/except.
    """

    def __init__(
        self,
        api_key: str,
        model: str = "claude-haiku-4-5",
        max_tokens: int = 256,
    ) -> None:
        import anthropic

        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model
        self._max_tokens = max_tokens

    @property
    def model(self) -> str:
        return self._model

    def detect(
        self, text: str, entry_date: str | None = None
    ) -> HeadingDetectionResult:
        # Empty / whitespace-only — nothing to do.
        if not text or not text.strip():
            return HeadingDetectionResult(heading_text="", body=text)
        # Already a heading — caller authored this on purpose.
        if text.lstrip().startswith("#"):
            return HeadingDetectionResult(heading_text="", body=text)

        # Drop leading whitespace before reasoning about the start of the entry.
        # We never want it back in the body either way.
        stripped = text.lstrip()
        window = stripped[:_DETECTION_WINDOW_CHARS]

        user_content = (
            f"entry_date: {entry_date}\n\n{window}" if entry_date else window
        )

        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_content}],
            )
            raw = response.content[0].text.strip()
        except Exception:
            log.warning(
                "Heading detector API call failed — returning text unchanged",
                exc_info=True,
            )
            return HeadingDetectionResult(heading_text="", body=text)

        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            log.warning("Heading detector returned no JSON object: %r", raw[:200])
            return HeadingDetectionResult(heading_text="", body=text)
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError:
            log.warning("Heading detector returned invalid JSON: %r", raw[:200])
            return HeadingDetectionResult(heading_text="", body=text)

        if not payload.get("is_heading"):
            return HeadingDetectionResult(heading_text="", body=text)

        heading_text = payload.get("heading_text")
        source_phrase = payload.get("source_phrase")
        iso_date_raw = payload.get("iso_date")

        if not isinstance(heading_text, str) or not heading_text.strip():
            return HeadingDetectionResult(heading_text="", body=text)
        if not isinstance(source_phrase, str) or not source_phrase:
            return HeadingDetectionResult(heading_text="", body=text)

        # The source_phrase must be a verbatim prefix of the (lstripped) input. This is
        # the bulletproof check against the model hallucinating an offset.
        if not window.startswith(source_phrase):
            log.warning(
                "Heading detector source_phrase %r is not a prefix of input — refusing",
                source_phrase[:80],
            )
            return HeadingDetectionResult(heading_text="", body=text)

        # iso_date is optional in the response — if absent, malformed, or out of
        # plausible range, we keep the heading detection but leave date_iso=None
        # so callers fall back to other date sources (regex, caller-provided).
        date_iso = _validate_iso_date(iso_date_raw)

        # body keeps the source_phrase intact — the title is driven by
        # heading_text / date_iso, but the body is left as the user wrote
        # it (date as the first line, then whatever followed). Only the
        # leading whitespace before the heading is dropped (via `stripped`);
        # the source_phrase prefix check above guarantees we still know
        # where the date phrase begins for the rare caller that needs it.
        return HeadingDetectionResult(
            heading_text=heading_text.strip(), body=stripped, date_iso=date_iso,
        )
