"""Stage-0 LLM-asserted match (WU4-D) — four-guard sanity check.

When the extraction LLM is given a known-entity catalog and emits
``matches_known_id`` for a mention, ``try_llm_asserted_match`` decides
whether to honour the assertion or reject it. Four guards run in order:

- **Guard A — ownership.** The asserted entity belongs to ``user_id``.
  Rejects cross-user references and hallucinated ids that don't exist.
- **Guard B — candidate-list membership.** The asserted id was in the
  candidate set we passed to the LLM. Anything outside the catalog is
  by definition a hallucination.
- **Guard C — type match.** The asserted entity's ``entity_type``
  matches what the LLM is claiming for this mention.
- **Guard D — cosine sanity.** Cosine similarity between the new
  mention's embedding (computed from canonical + description) and the
  asserted match's stored embedding is ≥ ``min_cosine``. Catches
  semantic drift / over-attribution where the LLM picks a known entity
  that is weakly related at best.

Each rejection logs the guard letter and the relevant inputs so the
threshold can be retuned from real data.
"""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from journal.entitystore.store import EntityStore
    from journal.providers.embeddings import EmbeddingsProvider

log = logging.getLogger(__name__)


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Return the cosine similarity of two equal-length float vectors.

    Returns 0.0 if either vector is empty or has zero magnitude. Does
    not pull in numpy — an entry-level extraction run compares at most
    a few hundred vectors, so a pure-Python loop is plenty fast.
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b, strict=True):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))


def try_llm_asserted_match(
    store: EntityStore,
    embeddings: EmbeddingsProvider,
    *,
    asserted_id: int,
    canonical: str,
    entity_type: str,
    description: str,
    user_id: int | None,
    justification: str | None,
    candidate_ids: set[int],
    candidate_embeddings: dict[int, list[float]],
    min_cosine: float,
) -> int | None:
    """Run the four-guard hybrid sanity check on an LLM-asserted match.

    Returns the entity id if accepted, ``None`` otherwise. Rejections
    are logged so the threshold can be retuned from real data.
    """
    # Guard A: ownership.
    target = store.get_entity(asserted_id, user_id=user_id)
    if target is None:
        log.info(
            "LLM-asserted match rejected (guard A: not found for "
            "user %s): id=%s, canonical=%r, justification=%r",
            user_id, asserted_id, canonical, justification,
        )
        return None

    # Guard B: candidate-list membership.
    if asserted_id not in candidate_ids:
        log.info(
            "LLM-asserted match rejected (guard B: not in candidate "
            "set): id=%s, canonical=%r, justification=%r",
            asserted_id, canonical, justification,
        )
        return None

    # Guard C: type match.
    if target.entity_type != entity_type:
        log.info(
            "LLM-asserted match rejected (guard C: type mismatch "
            "%s vs %s): id=%s, canonical=%r, justification=%r",
            target.entity_type, entity_type, asserted_id,
            canonical, justification,
        )
        return None

    # Guard D: cosine sanity.
    target_vec = candidate_embeddings.get(asserted_id)
    if target_vec is None:
        # No embedding for the candidate — fall back to the store
        # to avoid blanket-failing legitimate matches.
        target_vec = store.get_entity_embedding(asserted_id)
    if target_vec is None:
        log.info(
            "LLM-asserted match rejected (guard D: no stored "
            "embedding to compare against): id=%s, canonical=%r",
            asserted_id, canonical,
        )
        return None
    new_embedding = embeddings.embed_query(
        f"{canonical} {description}".strip()
    )
    cosine = cosine_similarity(new_embedding, target_vec)
    if cosine < min_cosine:
        log.info(
            "LLM-asserted match rejected (guard D: cosine %.3f < "
            "floor %.3f): id=%s, canonical=%r, justification=%r",
            cosine, min_cosine, asserted_id, canonical, justification,
        )
        return None

    log.info(
        "LLM-asserted match accepted: id=%s, canonical=%r, "
        "cosine=%.3f, justification=%r",
        asserted_id, canonical, cosine, justification,
    )
    return asserted_id
