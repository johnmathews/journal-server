"""String-signature heuristic for entity dedup.

Produces merge-candidate suggestions that pure embedding distance misses —
near-duplicate names that differ only in case, whitespace, trivial
punctuation, or short trailing/leading qualifiers. Examples that should
collapse to a match:

- ``Zij Kanaal`` ↔ ``Zijkanaal``     (whitespace)
- ``St. Mary``  ↔ ``St Mary``        (punctuation)
- ``Zij Kanaal C Weg`` ↔ ``Zij Kanaal C Zuid`` (short divergent tail)

All public functions in this module are pure — they take strings and
return strings/floats/booleans. ``find_signature_matches`` is the only
function with a service dependency: it needs an ``EntityStore`` to scan
existing same-type entities.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from journal.entitystore.store import EntityStore

# Synthetic similarity scores for the heuristic. The dedup pipeline uses
# these as the "score" column on merge-candidate rows so the signature
# branch and the embedding branch share a single ranking schema. Exact
# normalised match outranks a short-difference match — the latter has
# more room for false positives (short qualifiers that genuinely
# distinguish near-duplicate place names that the embedding distance
# happens to miss).
_SIGNATURE_EXACT_MATCH_SCORE = 1.0
_SIGNATURE_SHORT_DIFF_SCORE = 0.95

_VOWELS = frozenset("aeiou")


def _is_likely_word_tail(
    tail: str, *, allow_short_words: bool = False,
) -> bool:
    """Whether a divergent tail is more likely a real word/qualifier
    than an OCR/typo artifact.

    Tails that look like real words point at semantically distinct
    entities ("John Mathews" vs "John Mathews' mother", "Bible" vs
    "Bible study") rather than near-duplicates of the same entity.

    Triggers:

    - **Possessive markers** (``'`` / ``’``) — virtually always a
      relational suffix (``"X's mother"``).
    - **Purely numeric tails** — qualifying specifiers like "Psalms 63"
      or "Highway 5".
    - **Multi-character vowel-bearing tails** — likely real words. The
      length threshold defaults to 3 (strict) but rises to 5 when the
      caller passes ``allow_short_words=True``, which preserves the
      common Dutch place-qualifier pattern ("Weg", "Zuid", "Noord")
      that the heuristic was built to catch.
    """
    if not tail:
        return False
    t = tail.lower()
    if t.startswith("'") or t.startswith("’"):
        return True
    if t.isdigit():
        return True
    threshold = 5 if allow_short_words else 3
    return len(t) >= threshold and any(c in _VOWELS for c in t)


def _normalized_signature(name: str) -> str:
    """Lowercase, strip whitespace runs, drop trivial punctuation.

    Designed so OCR-driven splits like ``Zij Kanaal`` vs ``Zijkanaal``
    or ``St. Mary`` vs ``St Mary`` collapse to the same string. We keep
    this conservative — only commas, periods, and hyphens are stripped
    so names that intentionally contain other punctuation aren't merged
    by accident.
    """
    s = name.lower().strip()
    s = re.sub(r"\s+", "", s)
    s = re.sub(r"[\.,\-]", "", s)
    return s


def _is_short_difference(longer: str, shorter: str) -> bool:
    """Return True when ``shorter`` is a substring of ``longer`` and the
    leftover after removing it is small enough — and word-shaped enough —
    to suggest a near-duplicate rather than a semantically related but
    distinct entity.

    Empty leftover is always a match (the strings differ only in
    case/whitespace/punctuation, already collapsed before this check).
    Wordy leftovers — possessives, numerics, real words — indicate the
    longer name is a more specific concept ("X's mother", "Psalms 63",
    "Bible study") and are rejected. Short non-word leftovers (``"s"``,
    ``"St "``) are accepted to preserve OCR/typo recall.
    """
    if shorter not in longer:
        return False
    leftover = longer.replace(shorter, "", 1).strip()
    if not leftover:
        return True
    if _is_likely_word_tail(leftover):
        return False
    return len(leftover) <= 6


def _common_prefix_len(a: str, b: str) -> int:
    """Length of the longest common prefix of ``a`` and ``b``."""
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def _common_suffix_len(a: str, b: str) -> int:
    """Length of the longest common suffix of ``a`` and ``b``."""
    n = min(len(a), len(b))
    i = 0
    while i < n and a[-1 - i] == b[-1 - i]:
        i += 1
    return i


def _is_short_tail(tail: str) -> bool:
    """Whether a divergent tail string counts as 'short' for the
    common-prefix/suffix heuristic.

    A tail of ≤ 6 characters is always short. Longer tails are only
    accepted when they're empty or a single token — but since we've
    already collapsed whitespace, this collapses to the length check.
    """
    return len(tail) <= 6


def _is_signature_match(name_a: str, name_b: str) -> bool:
    """True if two entity names should be flagged as merge candidates by
    the relaxed string-signature heuristic.

    Three cases trigger a match:
      1. Normalized signatures (lowercased, whitespace-stripped, trivial
         punctuation removed) are identical.
      2. One signature is a substring of the other and the leftover is
         short (≤ 6 chars or a single token).
      3. The two signatures share a long common prefix or suffix
         (≥ 60% of the shorter name and ≥ 4 chars) and each unique tail
         is short (≤ 6 chars or a single token).

    Case 3 catches near-duplicates whose trailing/leading qualifiers
    differ (e.g. ``Zij Kanaal C Weg`` vs ``Zij Kanaal C Zuid``) which
    pure substring containment misses.

    The caller is responsible for filtering out same-id pairs and
    enforcing same-``entity_type``.
    """
    # Degenerate inputs: skip to avoid false positives on empty strings
    # or single-character names that would substring-match anything.
    if not name_a.strip() or not name_b.strip():
        return False
    if min(len(name_a.strip()), len(name_b.strip())) < 2:
        return False

    sig_a = _normalized_signature(name_a)
    sig_b = _normalized_signature(name_b)
    if not sig_a or not sig_b:
        return False
    if sig_a == sig_b:
        return True

    # Case 2: substring + short-leftover. Compare the
    # whitespace-collapsed but case-preserved variants so the leftover
    # length reflects the original strings, not the case-folded
    # signatures.
    collapsed_a = re.sub(r"\s+", "", name_a.strip())
    collapsed_b = re.sub(r"\s+", "", name_b.strip())
    if sig_b in sig_a and _is_short_difference(collapsed_a, collapsed_b):
        return True
    if sig_a in sig_b and _is_short_difference(collapsed_b, collapsed_a):
        return True

    # Case 3: long common prefix or suffix with short divergent tails.
    # Operate on the signatures so case/whitespace differences don't
    # truncate the common region. We require:
    #   - the common region to be ≥ 8 chars (avoids "Amsterdam" /
    #     "Rotterdam" sharing a 6-char "terdam" suffix);
    #   - the common region to be at least twice the max tail length
    #     (so the shared portion clearly dominates the divergence);
    #   - both divergent tails to be short (≤ 6 chars).
    #
    # Plus a tail-shape filter: tails that look like real words point
    # at semantically distinct entities ("Chaos" / "Data" sharing the
    # "Engineering" suffix) rather than near-duplicates. The PREFIX
    # branch (divergent suffix tails) is lenient — short Dutch place
    # qualifiers like "Weg" / "Zuid" should still match. The SUFFIX
    # branch (divergent prefix tails) is strict — a different qualifier
    # at the start nearly always means a different entity.
    prefix = _common_prefix_len(sig_a, sig_b)
    if prefix >= 8:
        tail_a = sig_a[prefix:]
        tail_b = sig_b[prefix:]
        max_tail = max(len(tail_a), len(tail_b))
        if (
            _is_short_tail(tail_a)
            and _is_short_tail(tail_b)
            and prefix >= 2 * max_tail
            and not _is_likely_word_tail(tail_a, allow_short_words=True)
            and not _is_likely_word_tail(tail_b, allow_short_words=True)
        ):
            return True

    suffix = _common_suffix_len(sig_a, sig_b)
    if suffix >= 8:
        tail_a = sig_a[: len(sig_a) - suffix]
        tail_b = sig_b[: len(sig_b) - suffix]
        max_tail = max(len(tail_a), len(tail_b))
        if (
            _is_short_tail(tail_a)
            and _is_short_tail(tail_b)
            and suffix >= 2 * max_tail
            and not _is_likely_word_tail(tail_a)
            and not _is_likely_word_tail(tail_b)
        ):
            return True

    return False


def _signature_match_score(name_a: str, name_b: str) -> float | None:
    """Return a synthetic similarity score for the heuristic, or None.

    ``_SIGNATURE_EXACT_MATCH_SCORE`` for identical signatures (case /
    whitespace / trivial-punctuation differences only),
    ``_SIGNATURE_SHORT_DIFF_SCORE`` for short-substring near-duplicates.
    """
    if not _is_signature_match(name_a, name_b):
        return None
    if _normalized_signature(name_a) == _normalized_signature(name_b):
        return _SIGNATURE_EXACT_MATCH_SCORE
    return _SIGNATURE_SHORT_DIFF_SCORE


def find_signature_matches(
    store: EntityStore,
    canonical: str,
    entity_type: str,
    *,
    user_id: int | None = None,
) -> list[tuple[int, float]]:
    """Scan existing same-type entities for string-signature matches.

    Returns a list of ``(entity_id, score)`` for every existing entity
    whose canonical name pairs with ``canonical`` under
    ``_is_signature_match``. Empty when nothing matches.

    Pulls entity rows via ``list_entities`` (not the embedding variant)
    so the heuristic still works for entities that were somehow stored
    without an embedding. The list is bounded to a large but finite
    limit — same-type collections are small in practice.
    """
    if not canonical or not canonical.strip():
        return []
    # 5000 is well above any realistic same-type cardinality but caps
    # us in case of pathological data.
    existing = store.list_entities(
        entity_type=entity_type, limit=5000, user_id=user_id,
    )
    matches: list[tuple[int, float]] = []
    for candidate in existing:
        if candidate.canonical_name == canonical:
            # Same name — that's stage-a territory, not a candidate.
            continue
        score = _signature_match_score(canonical, candidate.canonical_name)
        if score is not None:
            matches.append((candidate.id, score))
    return matches
