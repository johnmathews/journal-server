"""Smart title-casing for entity canonical names.

Applied at write time in EntityStore.create_entity. Reads the operator-managed exception
list from config/entity-casing-exceptions.toml. Hot-reloadable via services.reload.

Algorithm summary:
    1. Strip + collapse whitespace.
    2. Case-insensitive lookup in the exceptions table -> exact preserved-case value.
    3. If the input has any uppercase character at index >= 1 (e.g. ``iOS``,
       ``GitHub``, ``FC Barcelona``), assume deliberate casing and return verbatim.
    4. Otherwise word-by-word title-case. Articles/prepositions and Dutch particles
       are lowercased in non-leading positions. Hyphen-segments are individually
       title-cased. Apostrophe-suffixes (``'s``) keep the trailing letter lower.

The function is idempotent: ``smart_title_case(smart_title_case(x)) == smart_title_case(x)``.
"""

from __future__ import annotations

import logging
import re
import tomllib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# Articles / prepositions that are lowercased in non-leading positions.
_LOWERCASE_ARTICLES: frozenset[str] = frozenset({
    "of", "the", "and", "for", "in", "on", "at", "to", "with", "or", "by", "a", "an",
})

# Dutch place-name particles. Lowercased in non-leading positions.
_DUTCH_PARTICLES: frozenset[str] = frozenset({
    "van", "der", "de", "den", "het", "'t", "ten", "ter", "op", "aan",
})

# All particles checked in the algorithm.
_NON_LEADING_LOWERCASE: frozenset[str] = _LOWERCASE_ARTICLES | _DUTCH_PARTICLES


def _has_midword_uppercase(s: str) -> bool:
    """Return True if any character at index >= 1 is uppercase.

    Used to detect deliberately-cased input like 'iOS', 'iPhone', 'FC Barcelona'.
    """
    return any(c.isupper() for c in s[1:])


def _capitalize_word(word: str) -> str:
    """Capitalize the first alphabetic char of a word, lowercase the rest.

    Hyphen-aware: each hyphen-separated segment is capitalized independently.
    Apostrophe-aware: ``john's`` becomes ``John's`` because we just upper-case the
    first character and lower-case the rest — the apostrophe is preserved in place.
    """
    if not word:
        return word
    if "-" in word:
        return "-".join(_capitalize_word(seg) for seg in word.split("-"))
    return word[0].upper() + word[1:].lower()


def smart_title_case(name: str, exceptions: dict[str, str] | None = None) -> str:
    """Apply smart title-casing to an entity name.

    See module docstring for the algorithm summary.

    Args:
        name: The raw canonical name as supplied by the LLM / caller.
        exceptions: Optional mapping of ``lowercased name -> preserved-case form``.
            Lookup is case-insensitive; the value is returned verbatim when matched.
            ``None`` is treated as an empty mapping.

    Returns:
        The normalized canonical name. Empty / whitespace-only input returns ``""``.
    """
    if exceptions is None:
        exceptions = {}
    if not name:
        return ""
    trimmed = re.sub(r"\s+", " ", name.strip())
    if not trimmed:
        return ""

    lower_key = trimmed.lower()
    if lower_key in exceptions:
        return exceptions[lower_key]

    if _has_midword_uppercase(trimmed):
        # Deliberate mixed-case input — pass through verbatim.
        return trimmed

    words = trimmed.split(" ")
    out: list[str] = []
    for idx, word in enumerate(words):
        if not word:
            continue
        word_lower = word.lower()
        if idx > 0 and word_lower in _NON_LEADING_LOWERCASE:
            out.append(word_lower)
        else:
            out.append(_capitalize_word(word))
    return " ".join(out)


def load_entity_casing_exceptions(path: Path) -> dict[str, str]:
    """Load the exceptions TOML. Returns ``{lower_key: preserved_case_value}``.

    Returns an empty dict and logs a warning if the file doesn't exist or can't be
    parsed. The algorithm degrades gracefully without exceptions.
    """
    if not path.exists():
        logger.warning("Entity casing exceptions file not found: %s", path)
        return {}
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, ValueError) as e:
        logger.warning("Failed to load entity casing exceptions from %s: %s", path, e)
        return {}
    raw = data.get("exceptions", {})
    if not isinstance(raw, dict):
        logger.warning(
            "Entity casing exceptions [exceptions] is not a table in %s", path
        )
        return {}
    return {str(k).lower(): str(v) for k, v in raw.items()}
