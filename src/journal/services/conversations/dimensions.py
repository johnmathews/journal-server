"""Resolve a free-form mood-dimension string to a real facet key.

The intent classifier emits ``dimension`` as an LLM-authored string. It
is frequently a near-miss for the stored facet key ("energy" for
``energy_vigor``, "tiredness"/"fatigue" for the ``*_fatigue`` facets).
Matching it by exact equality against the loaded facet set silently
yields an empty trend series, so the answer degrades to "no data" even
when data exists.

`resolve_dimension` maps an emitted string to a canonical facet name
when it can do so unambiguously, and returns ``None`` (meaning "all
dimensions") when the string is empty, unrecognized, or ambiguous — so
the caller falls back to every dimension rather than returning nothing.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

#: Colloquial words → a canonical token that appears in a facet name.
#: Mapping is intentionally loose: a synonym that no longer matches any
#: facet (e.g. after a rename) simply produces zero candidates and the
#: caller degrades to "all dimensions", which is a safe outcome.
_SYNONYMS: dict[str, str] = {
    "tired": "fatigue",
    "tiredness": "fatigue",
    "exhausted": "fatigue",
    "exhaustion": "fatigue",
    "fatigued": "fatigue",
    "energetic": "energy",
    "vigour": "energy",
    "vigor": "energy",
    "sad": "sadness",
    "unhappy": "sadness",
    "depressed": "sadness",
    "happy": "joy",
    "happiness": "joy",
    "happier": "joy",
    "joyful": "joy",
    "anxious": "tension",
    "anxiety": "tension",
    "stress": "tension",
    "stressed": "tension",
    "tense": "tension",
    "relaxed": "calm",
    "frustrated": "frustration",
    "angry": "frustration",
    "connected": "connection",
    "lonely": "connection",
    "loneliness": "connection",
    "purpose": "fulfillment",
    "fulfilled": "fulfillment",
    "fulfilment": "fulfillment",
}


def _norm(value: str) -> str:
    """Lower-case and collapse any non-alphanumeric run to a single ``_``."""
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def resolve_dimension(raw: str | None, valid: Iterable[str]) -> str | None:
    """Map ``raw`` to a canonical facet name, or ``None`` for all dimensions.

    - Exact (case/separator-insensitive) match wins.
    - Otherwise a token-level match (with colloquial synonyms folded in)
      is used: if exactly one facet shares a token, that facet is
      returned; if several do (e.g. "fatigue" matching both
      ``physical_fatigue`` and ``mental_fatigue``) the result is
      ambiguous and ``None`` is returned.
    - Empty / unrecognized input also returns ``None``.
    """
    names = list(dict.fromkeys(valid))
    if not raw or not raw.strip():
        return None
    key = _norm(raw)
    if not key:
        return None

    by_norm = {_norm(n): n for n in names}
    if key in by_norm:
        return by_norm[key]

    raw_tokens = {_SYNONYMS.get(tok, tok) for tok in key.split("_") if tok}
    candidates: list[str] = []
    for name in names:
        name_tokens = set(_norm(name).split("_"))
        if raw_tokens & name_tokens:
            candidates.append(name)
    unique = list(dict.fromkeys(candidates))
    if len(unique) == 1:
        return unique[0]
    return None
