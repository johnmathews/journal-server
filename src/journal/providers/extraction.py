"""Entity extraction Protocol and Anthropic adapter.

The adapter asks Claude to identify named entities and relationships
in a journal entry via the tool-use API, which forces structured JSON
output. The system prompt enumerates the supported entity types,
provides a preferred-predicate list for relationships, and tells the
model the author's name so first-person statements ("I visited Blue
Bottle") can be turned into ("<author>", "visited", "Blue Bottle")
triples.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import anthropic

logger = logging.getLogger(__name__)


ENTITY_TYPES = (
    "person",
    "place",
    "activity",
    "organization",
    "topic",
    "other",
)

PREFERRED_PREDICATES = (
    "at",
    "visited",
    "works_for",
    "knows",
    "plays",
    "attended",
    "mentioned",
    "part_of",
    "located_in",
)


def build_system_prompt(author_name: str) -> str:
    """Compose the extraction system prompt for a specific author.

    The author's name is inlined so the model can use it as the
    subject of first-person relationships. The prompt is conservative
    on purpose — extracting noise is worse than missing signal.
    """
    return (
        "You are an information extraction system for a personal journal.\n"
        "Given a single journal entry, identify named entities and the\n"
        "relationships between them.\n\n"
        f"The journal's author is named {author_name}. First-person\n"
        "actions (\"I went to the gym\", \"I played squash\") should be\n"
        f"recorded as relationships where the subject is {author_name!r}.\n"
        "If the author is not already in the entity list, add them as a\n"
        "'person' entity with canonical_name exactly equal to the author\n"
        "name above.\n\n"
        "Entity types (pick the best fit for each):\n"
        "  - person: a named individual, pet, or character\n"
        "  - place: a city, venue, building, region, or address\n"
        "  - activity: a verb-ish noun (squash, climbing, journaling)\n"
        "  - organization: a company, club, team, or institution\n"
        "  - topic: a subject or concept the author is thinking about\n"
        "  - other: only when none of the above fit\n\n"
        "Preferred predicates for relationships (use free text when none\n"
        "of these fit):\n"
        "  " + ", ".join(PREFERRED_PREDICATES) + "\n\n"
        "Rules:\n"
        "  - Be conservative. Only extract named or strongly-implied\n"
        "    entities. Do NOT invent generic nouns (e.g. 'the meeting').\n"
        "  - Every entity needs a verbatim quote from the entry that\n"
        "    supports the mention.\n"
        "  - Every relationship needs both subject and object to appear\n"
        "    as entities in the same response.\n"
        "  - If nothing is found, return empty arrays for entities and\n"
        "    relationships.\n"
        "  - Confidence is a number in [0.0, 1.0]: 1.0 = completely\n"
        "    certain, 0.5 = plausible guess, <0.3 = probably skip.\n\n"
        "Call the `record_entities` tool exactly once with your findings."
    )


ENTITY_EXTRACTION_TOOL: dict[str, Any] = {
    "name": "record_entities",
    "description": (
        "Record every named entity and relationship found in the entry."
    ),
    "input_schema": {
        "type": "object",
        "required": ["entities", "relationships"],
        "properties": {
            "entities": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": [
                        "entity_type",
                        "canonical_name",
                        "quote",
                        "confidence",
                    ],
                    "properties": {
                        "entity_type": {
                            "type": "string",
                            "enum": list(ENTITY_TYPES),
                        },
                        "canonical_name": {"type": "string"},
                        "description": {"type": "string"},
                        "aliases": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "quote": {"type": "string"},
                        "confidence": {
                            "type": "number",
                            "minimum": 0.0,
                            "maximum": 1.0,
                        },
                    },
                },
            },
            "relationships": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": [
                        "subject",
                        "predicate",
                        "object",
                        "quote",
                        "confidence",
                    ],
                    "properties": {
                        "subject": {"type": "string"},
                        "predicate": {"type": "string"},
                        "object": {"type": "string"},
                        "quote": {"type": "string"},
                        "confidence": {
                            "type": "number",
                            "minimum": 0.0,
                            "maximum": 1.0,
                        },
                    },
                },
            },
        },
    },
}


@dataclass
class RawExtractionResult:
    """Unprocessed output from an extraction provider.

    `entities` and `relationships` are lists of dicts matching the keys
    in the tool schema. Normalisation, dedup, and persistence happen in
    `EntityExtractionService`.
    """

    entities: list[dict[str, Any]] = field(default_factory=list)
    relationships: list[dict[str, Any]] = field(default_factory=list)


@runtime_checkable
class ExtractionProvider(Protocol):
    """Protocol for entity extraction providers."""

    def extract_entities(
        self,
        entry_text: str,
        entry_date: str,
        author_name: str,
    ) -> RawExtractionResult: ...


class AnthropicExtractionProvider:
    """Extraction provider using Anthropic's tool-use API."""

    def __init__(
        self,
        api_key: str,
        model: str,
        max_tokens: int = 4096,
    ) -> None:
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model
        self._max_tokens = max_tokens

    def extract_entities(
        self,
        entry_text: str,
        entry_date: str,
        author_name: str,
    ) -> RawExtractionResult:
        """Call Claude to extract entities and relationships.

        The tool_choice parameter forces the model to return its answer
        as a tool call rather than prose, so we can parse
        `message.content[0].input` as a structured dict.
        """
        logger.info(
            "Extracting entities via Anthropic (model=%s, date=%s, chars=%d)",
            self._model,
            entry_date,
            len(entry_text),
        )

        system_text = build_system_prompt(author_name)
        user_text = (
            f"Entry date: {entry_date}\n\n"
            f"Entry text:\n{entry_text}"
        )

        message = self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=[
                {
                    "type": "text",
                    "text": system_text,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=[ENTITY_EXTRACTION_TOOL],
            tool_choice={"type": "tool", "name": "record_entities"},
            messages=[
                {
                    "role": "user",
                    "content": [{"type": "text", "text": user_text}],
                }
            ],
        )

        return _parse_tool_response(message)


_PUNCT_TO_STRIP = ",.;:!?\"'()[]{}"

# Suffixes that turn a noun into a possessive or plural. If the only
# difference between the LLM's canonical_name and a longer token in the
# quote is one of these, the LLM had it right — the longer form is the
# inflected form, not a clipped canonical. Without this guard the
# repair logic happily promoted "Hermione" -> "Hermione's", "Daniel"
# -> "Daniels", etc. across most entities in the corpus on the first
# real-data run.
_INFLECTION_SUFFIXES = ("'s", "s'", "s")


def _is_inflection_of(name_lower: str, token_lower: str) -> bool:
    """True if ``token_lower`` is just an inflected form of ``name_lower``
    (possessive or plural). Used both to trust the canonical when the
    token is its inflected form, and to reject inflected forms as
    repair candidates."""
    if not token_lower.startswith(name_lower):
        return False
    extra = token_lower[len(name_lower):]
    return extra in _INFLECTION_SUFFIXES


def _repair_canonical_name(
    canonical_name: str, quote: str,
) -> tuple[str, bool]:
    """Defend against LLM-clipped ``canonical_name`` values.

    Returns ``(repaired_name, was_repaired)``.

    The model occasionally returns a ``canonical_name`` that is one or
    two characters shorter than the form actually in the source text
    — e.g. ``"Nautilin"`` for a quote ``"Nautiline, the iOS app..."``.
    Operates at the token level (a naive substring check is not enough:
    the clipped name is itself a substring of the longer token).

    Trust rules — if any of these match, the LLM had it right:

    1. Some whitespace-separated token in ``quote`` (after stripping
       surrounding punctuation) **equals** ``canonical_name``
       case-insensitively. Protects deliberate short canonicals like
       ``"Bob"`` for a quote ``"Robert 'Bob' Smith"``.
    2. Some token is an inflection of ``canonical_name`` — the same
       name with a possessive (``'s``, ``s'``) or plural (``s``)
       suffix. The LLM picked the bare canonical and was right; the
       longer form is just an inflected reference. This is the common
       case for proper nouns and prevents false repairs like
       ``"Hermione" -> "Hermione's"`` or ``"Daniel" -> "Daniels"``.

    Repair rule:

    3. Otherwise, if ``canonical_name`` is a strict prefix of some
       longer token in the quote AND the extra characters are not an
       inflection suffix, return that longer token. Catches clipped-
       trailing-character LLM bugs (``"Nautilin"`` -> ``"Nautiline"``)
       without false-positive-ing on inflections.

    Anything else is left alone with a warning logged by the caller.
    Returned token preserves the original casing from the quote.
    """
    if not canonical_name or not quote:
        return canonical_name, False

    name_lower = canonical_name.lower()
    repair_candidate: str | None = None
    for raw_token in quote.split():
        token = raw_token.strip(_PUNCT_TO_STRIP)
        if not token:
            continue
        token_lower = token.lower()
        if token_lower == name_lower:
            # canonical_name is genuinely present as a token.
            return canonical_name, False
        if _is_inflection_of(name_lower, token_lower):
            # Token is "<canonical>'s" / "<canonical>s'" / "<canonical>s".
            # The LLM correctly picked the bare canonical.
            return canonical_name, False
        if (
            len(token) > len(canonical_name)
            and token_lower.startswith(name_lower)
            and repair_candidate is None
        ):
            repair_candidate = token

    if repair_candidate is not None:
        return repair_candidate, True
    return canonical_name, False


# Minimum length (in characters) of a canonical substring we will accept
# as a longest-substring repair. Below this, the match is too short to
# carry meaning — we'd rather flag the whole thing for quarantine.
_MIN_SUBSTRING_REPAIR_LEN = 3


def _longest_canonical_substring_in_quote(
    canonical: str, quote: str,
) -> str | None:
    """Return the longest token-aligned substring of ``canonical`` that
    appears in ``quote``, or None if nothing of length ≥ 3 chars matches.

    Comparison is **case-insensitive** and **whitespace-tolerant**: both
    sides have whitespace runs collapsed to a single space before the
    substring check. The returned string preserves the **canonical's
    original casing** (so the post-WU2 smart-title-cased canonical isn't
    re-cased from the quote).

    Token-aligned: candidates are produced by joining contiguous
    space-separated tokens of ``canonical``. We never return mid-token
    fragments like ``"C Zui"`` from ``"Zij Kanaal C Zuid"``.

    Used by the entity-extraction provider to repair hallucinated
    canonical names — when the LLM emits ``"Zij Kanaal C Zuid"`` for a
    quote containing only ``"Zij Kanaal C"``, this returns
    ``"Zij Kanaal C"``. If the longest matchable substring is too short
    (< 3 chars), the result is None so the caller can soft-quarantine
    the new entity instead of fabricating a tiny rebound.
    """
    if not canonical or not quote:
        return None

    canonical_collapsed = re.sub(r"\s+", " ", canonical.strip())
    quote_collapsed = re.sub(r"\s+", " ", quote.strip())
    canonical_lower = canonical_collapsed.lower()
    quote_lower = quote_collapsed.lower()

    if not canonical_collapsed or not quote_collapsed:
        return None

    # Whole canonical present (modulo case + whitespace) — return as-is,
    # subject to the minimum-length floor (a 1-char canonical is too
    # short to be meaningful).
    if (
        canonical_lower in quote_lower
        and len(canonical_collapsed) >= _MIN_SUBSTRING_REPAIR_LEN
    ):
        return canonical

    # Token-aligned: try every contiguous span of canonical's tokens,
    # longest first. Returns the first substring whose lower form is
    # in the quote and is at least _MIN_SUBSTRING_REPAIR_LEN chars long.
    tokens = canonical_collapsed.split()
    n = len(tokens)
    for length in range(n, 0, -1):
        for start in range(0, n - length + 1):
            candidate = " ".join(tokens[start : start + length])
            if len(candidate) < _MIN_SUBSTRING_REPAIR_LEN:
                continue
            cand_lower = candidate.lower()
            if cand_lower in quote_lower:
                return candidate
    return None


def _parse_tool_response(message: Any) -> RawExtractionResult:
    """Extract entities/relationships from an Anthropic tool-use response.

    Handles minor defensive cases so a malformed or empty response
    produces an empty `RawExtractionResult` instead of crashing the
    batch. The tool_choice parameter should guarantee a `tool_use`
    block, but we still scan the whole content list for robustness.
    """
    if message is None:
        return RawExtractionResult()

    content = getattr(message, "content", None)
    if not content:
        return RawExtractionResult()

    tool_block: Any = None
    for block in content:
        if getattr(block, "type", None) == "tool_use":
            tool_block = block
            break
    if tool_block is None:
        # Fall back to the first block — FastMCP test stubs sometimes
        # just set `.input` on a MagicMock without a `type` attribute.
        tool_block = content[0]

    payload = getattr(tool_block, "input", None) or {}
    entities_raw = payload.get("entities") or []
    relationships_raw = payload.get("relationships") or []

    entities: list[dict[str, Any]] = []
    for item in entities_raw:
        if not isinstance(item, dict):
            continue
        canonical_name = item.get("canonical_name", "").strip()
        quote = item.get("quote", "") or ""
        repaired_name, was_repaired = _repair_canonical_name(
            canonical_name, quote,
        )
        pending_quarantine_reason = ""
        if was_repaired:
            logger.warning(
                "Repaired clipped canonical_name from LLM: %r -> %r "
                "(quote: %r)",
                canonical_name, repaired_name, quote,
            )
        elif (
            canonical_name
            and quote
            and canonical_name.lower() not in quote.lower()
        ):
            # The token-prefix repair didn't catch anything. Try a
            # longest-token-substring repair against the quote — this is
            # the WU4 path that handles LLM hallucinations like
            # "Zij Kanaal C Zuid" for a quote containing only
            # "Zij Kanaal C". If a substring matches we rename;
            # otherwise we keep the original name (for audit) and flag
            # the result so the calling extraction service can
            # soft-quarantine the entity it creates.
            longest_substring = _longest_canonical_substring_in_quote(
                canonical_name, quote,
            )
            if longest_substring is not None and (
                longest_substring.lower() != canonical_name.lower()
            ):
                logger.info(
                    "Renamed canonical_name from %r to %r "
                    "(longest-substring of quote %r)",
                    canonical_name, longest_substring, quote,
                )
                repaired_name = longest_substring
            else:
                pending_quarantine_reason = (
                    f"canonical_name {canonical_name!r} not found in "
                    f"source quote {quote!r}"
                )
                logger.warning(
                    "LLM returned canonical_name %r that does not appear "
                    "in its quote %r — flagging for quarantine",
                    canonical_name, quote,
                )
        entities.append(
            {
                "entity_type": item.get("entity_type", "other"),
                "canonical_name": repaired_name,
                "description": item.get("description", "") or "",
                "aliases": list(item.get("aliases") or []),
                "quote": quote,
                "confidence": float(item.get("confidence", 0.0) or 0.0),
                "pending_quarantine_reason": pending_quarantine_reason,
            }
        )

    relationships: list[dict[str, Any]] = []
    for item in relationships_raw:
        if not isinstance(item, dict):
            continue
        relationships.append(
            {
                "subject": item.get("subject", "").strip(),
                "predicate": item.get("predicate", "").strip(),
                "object": item.get("object", "").strip(),
                "quote": item.get("quote", "") or "",
                "confidence": float(item.get("confidence", 0.0) or 0.0),
            }
        )

    return RawExtractionResult(entities=entities, relationships=relationships)
