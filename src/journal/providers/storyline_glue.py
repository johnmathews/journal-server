"""Storyline curation-glue provider (Haiku, plain text).

Generates the 1-2-sentence transition prose between adjacent
verbatim excerpts in the curation panel. Examples of valid output:

    "Three days later:"
    "Two weeks on, with the same theme resurfacing:"
    "After a gap of a month:"

The glue's only job is to give the curation panel a sense of
narrative flow without committing to interpretation — the reader
gets the verbatim excerpts straight after.

We batch all transitions in one call rather than N-1 separate
calls — the model is cheap enough that the wins from caching the
batch don't outweigh the round-trip cost on a long storyline.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from journal.services import usage

if TYPE_CHECKING:
    from journal.models import DatedEntryExcerpt

log = logging.getLogger(__name__)


SYSTEM_PROMPT = """\
You write brief temporal transitions between excerpts of a journal. You will be
given a list of journal-entry pairs (the previous excerpt and the next excerpt)
with the gap in days between them, and you return one short transition phrase
for each pair.

Each transition must:

* Be one short phrase ending with a colon. Length: 2 to 8 words. Example: "Three
  days later:", "Two weeks on:", "After a month, the topic returns:".
* Reference the temporal gap accurately. Use "the next day", "two days later",
  "a week later", "after a fortnight", "later that month", "the following month"
  — whichever fits the gap in days.
* Be neutral. Do not interpret, judge, or summarize the excerpts. The reader
  already has the excerpts in front of them.
* Use the same voice/tense as the rest of the panel — descriptive, third-person
  framing of time passing.

Return your output as a JSON array of strings, one per input pair, in the same
order. No surrounding prose. No code fences. Example output:

    ["The next day:", "Three weeks on:", "A month later:"]
"""


@dataclass
class GlueResult:
    """One glue-generation call's output."""

    transitions: list[str] = field(default_factory=list)
    model_used: str = ""
    raw_usage: dict[str, Any] | None = None


@runtime_checkable
class StorylineGlueProtocol(Protocol):
    def generate_transitions(
        self,
        excerpts: list[DatedEntryExcerpt],
    ) -> GlueResult: ...


class AnthropicStorylineGlue:
    """Haiku-based transition generator for the curation panel."""

    def __init__(
        self,
        api_key: str,
        model: str = "claude-haiku-4-5",
        max_tokens: int = 1024,
        client: Any | None = None,
    ) -> None:
        if client is not None:
            self._client = client
        else:
            import anthropic
            self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model
        self._max_tokens = max_tokens

    @property
    def model(self) -> str:
        return self._model

    def generate_transitions(
        self,
        excerpts: list[DatedEntryExcerpt],
    ) -> GlueResult:
        """Return N-1 short transition phrases for N excerpts.

        On API failure or malformed response, returns a fallback
        list of deterministic gap-based phrases ("Two days later:")
        so the curation panel still renders something reasonable
        without leaving holes.
        """
        if len(excerpts) < 2:
            return GlueResult(model_used=self._model)

        pairs = _build_pair_descriptors(excerpts)
        user_text = (
            "Generate one short transition phrase for each of the "
            f"{len(pairs)} excerpt pairs below. Reply with a JSON array "
            "only.\n\n"
            f"Pairs (gap_days, previous_date, next_date):\n"
            + json.dumps(pairs, ensure_ascii=False)
        )

        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": user_text}],
            )
            usage.record_anthropic(self._model, response)
        except Exception:  # noqa: BLE001 — provider failure falls back
            log.exception("Storyline glue API call failed — using fallback")
            return GlueResult(
                transitions=_fallback_transitions(excerpts),
                model_used=self._model,
            )

        text = _extract_text(response)
        transitions = _parse_transitions(text, expected=len(pairs))
        if transitions is None:
            log.warning(
                "Glue response did not parse as JSON array of len %d — "
                "falling back to deterministic transitions",
                len(pairs),
            )
            transitions = _fallback_transitions(excerpts)

        return GlueResult(
            transitions=transitions,
            model_used=self._model,
            raw_usage=_extract_usage(response),
        )


def _build_pair_descriptors(
    excerpts: list[DatedEntryExcerpt],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    prev = excerpts[0]
    for nxt in excerpts[1:]:
        gap_days = _gap_days(prev.entry_date, nxt.entry_date)
        out.append({
            "gap_days": gap_days,
            "previous_date": prev.entry_date,
            "next_date": nxt.entry_date,
        })
        prev = nxt
    return out


def _gap_days(a: str, b: str) -> int:
    try:
        da = datetime.strptime(a, "%Y-%m-%d").date()
        db = datetime.strptime(b, "%Y-%m-%d").date()
    except ValueError:
        return 0
    return (db - da).days


def _fallback_transitions(
    excerpts: list[DatedEntryExcerpt],
) -> list[str]:
    """Deterministic gap-only transitions used when the LLM call
    fails or returns garbage. Not pretty but never wrong."""
    out: list[str] = []
    prev = excerpts[0]
    for nxt in excerpts[1:]:
        gap = _gap_days(prev.entry_date, nxt.entry_date)
        out.append(_describe_gap(gap))
        prev = nxt
    return out


def _describe_gap(days: int) -> str:
    if days <= 0:
        return "Later the same day:"
    if days == 1:
        return "The next day:"
    if days < 7:
        return f"{days} days later:"
    if days < 14:
        return "A week later:"
    if days < 30:
        return f"{days // 7} weeks later:"
    if days < 60:
        return "A month later:"
    months = days // 30
    return f"{months} months later:"


_TEXT_BLOCK_TYPES = ("text",)


def _extract_text(response: Any) -> str:  # noqa: ANN401
    content = getattr(response, "content", None) or []
    if not content and isinstance(response, dict):
        content = response.get("content", [])
    parts: list[str] = []
    for block in content:
        block_type = block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
        if block_type in _TEXT_BLOCK_TYPES:
            text = block.get("text") if isinstance(block, dict) else getattr(block, "text", "")
            parts.append(text or "")
    return "".join(parts)


_JSON_ARRAY_RE = re.compile(r"\[.*?\]", re.DOTALL)


def _parse_transitions(text: str, expected: int) -> list[str] | None:
    """Best-effort parse of the glue response into a list of strings.

    Accepts: plain JSON array, JSON array wrapped in code fences, or
    JSON array embedded in prose. Returns None if no array of the
    expected length can be extracted.
    """
    candidate = text.strip()
    # Strip ```json fences if present
    if candidate.startswith("```"):
        candidate = candidate.strip("`")
        candidate = candidate.lstrip("json").strip()
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        match = _JSON_ARRAY_RE.search(text)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    if not isinstance(parsed, list):
        return None
    if len(parsed) != expected:
        return None
    if not all(isinstance(x, str) for x in parsed):
        return None
    return parsed


def _extract_usage(response: Any) -> dict[str, Any] | None:  # noqa: ANN401
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")
    if usage is None:
        return None
    out: dict[str, Any] = {}
    for key in (
        "input_tokens", "output_tokens",
        "cache_creation_input_tokens", "cache_read_input_tokens",
    ):
        value = usage.get(key) if isinstance(usage, dict) else getattr(usage, key, None)
        if value is not None:
            out[key] = value
    return out or None


# Defensive: re-export `date` to silence unused-import linting when
# the file is read in isolation.
_ = date
