"""Mood scoring Protocol and Anthropic adapter.

The adapter asks Claude (Sonnet 4.5 by default, env-overridable)
to score a journal entry against a runtime-loaded set of
`MoodDimension` facets. Structured output is forced via the
Anthropic Messages tool-use API: the adapter builds a single
`record_mood_scores` tool whose JSON schema mirrors the current
facets — each facet is a required property with its own
`minimum`/`maximum` based on `scale_type` (bipolar `[-1, +1]` or
unipolar `[0, +1]`).

Editing `config/mood-dimensions.toml` and restarting the server
changes the tool schema at the next call — no code or prompt
edits required.

The adapter deliberately does **not** cache the system prompt
block: journal entries are short and the prompt rebuilds from
the current facets, so the cache-hit rate would be marginal and
the added complexity isn't worth it for a single-user tool.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import anthropic

if TYPE_CHECKING:
    from journal.services.mood_dimensions import MoodDimension

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RawMoodScore:
    """One score as returned by the scorer, before any persistence
    normalisation. `value` is clamped into the dimension's declared
    range by the adapter; `confidence` is optional (the model may
    omit it and the service stores NULL)."""

    dimension_name: str
    value: float
    confidence: float | None = None


def build_system_prompt(dimensions: tuple[MoodDimension, ...]) -> str:
    """Compose the scoring system prompt from the loaded facets.

    Each facet's `notes` are inlined verbatim so `journal
    backfill-mood --force` reinterpretation is deterministic given
    the same config file. The prompt explicitly tells the model
    that unipolar facets use `0` as absence (not neutral) so scores
    at the bottom of the range mean something different than for
    bipolar facets.
    """
    lines = [
        "You are a mood-scoring assistant for a personal journal.",
        "",
        (
            "Given a single journal entry, you will call the "
            "`record_mood_scores` tool exactly once with a score for "
            "every facet listed below. Each score must be a single "
            "floating-point number."
        ),
        "",
        (
            "Two scale types are in use. Read each facet's scale "
            "type before scoring."
        ),
        "",
        (
            "- `bipolar` facets range from -1.0 (negative pole) to "
            "+1.0 (positive pole), with 0.0 meaning neither pole "
            "clearly dominates. A calm, flat entry scores 0 on a "
            "bipolar facet."
        ),
        "- `unipolar` facets range from 0.0 (absence of the positive",
        "  pole) to +1.0 (strong presence of the positive pole).",
        "  0.0 on a unipolar facet does NOT mean neutral — it means",
        "  the named feeling is absent from the entry.",
        "",
        "Score conservatively. Reserve extreme values (±1.0, 1.0) for",
        "entries that unmistakably exhibit the feeling; use small",
        "non-zero values (±0.1 to ±0.3) for mild traces.",
        "",
        "Facets to score:",
        "",
    ]
    for i, d in enumerate(dimensions, start=1):
        lines.append(
            f"{i}. `{d.name}` ({d.scale_type}, "
            f"range [{d.score_min:+.1f}, {d.score_max:+.1f}]): "
            f"{d.negative_pole} → {d.positive_pole}."
        )
        for note_line in d.notes.splitlines():
            note_line = note_line.strip()
            if note_line:
                lines.append(f"   {note_line}")
        lines.append("")

    lines.append(
        "If the entry is too short or uninformative to score a "
        "particular facet, include it anyway with the neutral "
        "value (0.0 for bipolar, 0.0 for unipolar) and a low "
        "confidence."
    )
    return "\n".join(lines)


def build_tool_schema(
    dimensions: tuple[MoodDimension, ...],
) -> dict[str, Any]:
    """Build the `record_mood_scores` tool definition from the
    current facets.

    Each facet becomes a required sub-object with its own
    `minimum`/`maximum` bounds and an optional per-facet
    confidence. Using per-facet bounds (rather than one global
    `minimum: -1, maximum: 1`) lets unipolar facets fail schema
    validation if the model tries to return a negative score —
    the model is told the bounds up front, and wrong-sign values
    are a prompt-engineering bug to fix rather than silently
    clamp.
    """
    properties: dict[str, Any] = {}
    required: list[str] = []
    for d in dimensions:
        properties[d.name] = {
            "type": "object",
            "description": (
                f"{d.negative_pole} ({d.score_min:+.1f}) → "
                f"{d.positive_pole} ({d.score_max:+.1f})"
            ),
            "required": ["value"],
            "properties": {
                "value": {
                    "type": "number",
                    "minimum": d.score_min,
                    "maximum": d.score_max,
                    "description": (
                        f"Score for {d.name} in "
                        f"[{d.score_min:+.1f}, {d.score_max:+.1f}]"
                    ),
                },
                "confidence": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 1.0,
                    "description": (
                        "Optional per-facet confidence in [0, 1]"
                    ),
                },
            },
        }
        required.append(d.name)

    return {
        "name": "record_mood_scores",
        "description": (
            "Record one score per mood facet for a journal entry. "
            "Every facet in the schema is required."
        ),
        "input_schema": {
            "type": "object",
            "required": required,
            "properties": properties,
        },
    }


@runtime_checkable
class MoodScorer(Protocol):
    """Protocol for mood scoring providers.

    Implementations take an entry's text and the current
    dimension set and return one `RawMoodScore` per dimension.
    Implementations MAY raise on upstream errors; callers
    (`MoodScoringService`) are responsible for logging and
    swallowing failures so ingestion is never broken by a scoring
    hiccup.
    """

    def score(
        self,
        entry_text: str,
        dimensions: tuple[MoodDimension, ...],
    ) -> list[RawMoodScore]: ...


class AnthropicMoodScorer:
    """Mood scorer using Anthropic's Messages tool-use API.

    Default model is Claude Sonnet 4.5 — slightly more expensive
    than Haiku but still ~$0.005/entry and noticeably better at
    subjective calibration on short texts. Overridable via
    `MOOD_SCORER_MODEL` env var (wired through `config.py`).
    """

    def __init__(
        self,
        api_key: str,
        model: str = "claude-sonnet-4-5",
        max_tokens: int = 1024,
    ) -> None:
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model
        self._max_tokens = max_tokens

    def score(
        self,
        entry_text: str,
        dimensions: tuple[MoodDimension, ...],
    ) -> list[RawMoodScore]:
        if not dimensions:
            return []

        system_text = build_system_prompt(dimensions)
        tool = build_tool_schema(dimensions)
        log.info(
            "Scoring mood via Anthropic (model=%s, dims=%d, chars=%d)",
            self._model,
            len(dimensions),
            len(entry_text),
        )

        message = self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=system_text,
            tools=[tool],
            tool_choice={"type": "tool", "name": "record_mood_scores"},
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Journal entry to score:\n\n"
                                f"{entry_text}"
                            ),
                        }
                    ],
                }
            ],
        )

        return _parse_tool_response(message, dimensions)


def _parse_tool_response(
    message: Any,
    dimensions: tuple[MoodDimension, ...],
) -> list[RawMoodScore]:
    """Pull the scores out of the tool_use block.

    Two fallback paths so a degenerate response doesn't crash the
    scoring service:

    1. If no `tool_use` block is present, scan the text blocks for
       the first JSON object and parse it as the tool input.
    2. If a facet is missing from the payload, it's silently
       skipped. The service logs a warning with the missing names
       so prompt drift is visible.
    """
    if message is None:
        return []

    content = getattr(message, "content", None) or []

    tool_input: dict[str, Any] | None = None
    for block in content:
        if getattr(block, "type", None) == "tool_use":
            raw_input = getattr(block, "input", None)
            if isinstance(raw_input, dict):
                tool_input = raw_input
                break

    if tool_input is None:
        # Fall back to the first JSON object in the text blocks.
        for block in content:
            if getattr(block, "type", None) == "text":
                text = getattr(block, "text", "") or ""
                parsed = _extract_first_json_object(text)
                if parsed is not None:
                    tool_input = parsed
                    break

    if not isinstance(tool_input, dict):
        log.warning(
            "Mood scorer returned no parseable tool_use or JSON block"
        )
        return []

    results: list[RawMoodScore] = []
    missing: list[str] = []
    for d in dimensions:
        raw = tool_input.get(d.name)
        if not isinstance(raw, dict):
            missing.append(d.name)
            continue
        value = raw.get("value")
        if not isinstance(value, int | float):
            missing.append(d.name)
            continue
        # Clamp defensively — the schema already constrains this
        # range but Anthropic's validation is best-effort on the
        # wire, and a bad score should not break persistence.
        clamped = _clamp(float(value), d.score_min, d.score_max)
        confidence_raw = raw.get("confidence")
        confidence: float | None
        if isinstance(confidence_raw, int | float):
            confidence = _clamp(float(confidence_raw), 0.0, 1.0)
        else:
            confidence = None
        results.append(
            RawMoodScore(
                dimension_name=d.name,
                value=clamped,
                confidence=confidence,
            )
        )

    if missing:
        log.warning(
            "Mood scorer response omitted facets: %s",
            ", ".join(missing),
        )

    return results


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _extract_first_json_object(text: str) -> dict[str, Any] | None:
    """Walk `text` looking for the first complete `{...}` object
    and parse it. Returns None on failure.

    Used as a last-ditch fallback when the model responds with
    prose instead of a tool call. Not robust to nested strings
    containing `{` / `}`, but good enough for a recovery path.
    """
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth == 0:
                continue
            depth -= 1
            if depth == 0 and start >= 0:
                candidate = text[start : i + 1]
                try:
                    parsed = json.loads(candidate)
                except json.JSONDecodeError:
                    start = -1
                    continue
                if isinstance(parsed, dict):
                    return parsed
                start = -1
    return None
