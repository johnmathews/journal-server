"""Extension classifier — does this entry extend an existing storyline?

Hybrid pipeline, applied per-(entry, storyline) pair:

1. **Entity overlap (deterministic).** If *any* of the storyline's
   anchor entity_ids appears in the entry's extracted entity
   mentions, return ``yes`` immediately. Zero cost, zero LLM calls.
2. **Surface form (deterministic).** If *any* anchor entity's
   ``canonical_name`` appears in the entry text as a whole word
   (case-insensitive, word-boundary matched so "Ana" doesn't match
   "banana"), fall through to the LLM decider — the surface form is
   present but the entity wasn't extracted, so pronominal references
   or extractor gaps are possible.
2.5. **Semantic fallback (embedding).** If neither of the above fires
   but the entry's embedding is close enough to the storyline's
   *draft chapter* embedding, also fall through to the LLM decider.
3. **Haiku decider.** Ask the model whether the entry meaningfully
   extends the storyline. Returns yes/no/maybe with one-sentence
   reasoning that the UI surfaces.

When neither (1), (2), nor (2.5) fires, the classifier returns ``no``
without an LLM call — most ingested entries do not extend most
storylines, and surfacing every miss to Haiku is wasteful.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

from journal.services.entity_extraction.matching import cosine_similarity

if TYPE_CHECKING:
    from collections.abc import Callable

    from journal.db.repository.protocol import EntryRepository
    from journal.db.storyline_repository import SQLiteStorylineRepository
    from journal.entitystore.protocol import EntityStore
    from journal.models import Entry, Storyline
    from journal.providers.storyline_extension_decider import (
        StorylineExtensionDeciderProtocol,
    )

log = logging.getLogger(__name__)


Decision = Literal["yes", "no", "maybe"]


@dataclass
class ExtensionResult:
    """One classification verdict against one storyline."""

    storyline_id: int
    decision: Decision
    reasoning: str
    stage: str  # "entity_overlap" | "surface_form_llm" | "embedding_llm" | "no_match"


@runtime_checkable
class StorylineExtensionClassifierProtocol(Protocol):
    def classify_for_entry(
        self, entry_id: int, user_id: int,
    ) -> list[ExtensionResult]: ...


class StorylineExtensionClassifier:
    """Hybrid extension classifier (entity overlap + surface form + LLM).

    Iterates the user's active storylines; for each pair, returns a
    decision. The job worker (W7's
    ``run_storyline_extension_check``) uses these decisions to queue
    regeneration jobs for the ``yes`` storylines.
    """

    def __init__(
        self,
        *,
        entity_store: EntityStore,
        entry_repository: EntryRepository,
        storyline_repository: SQLiteStorylineRepository,
        decider: StorylineExtensionDeciderProtocol,
        embedder: Callable[[str], list[float]] | None = None,
        relevance_threshold: float = 0.5,
    ) -> None:
        self._entity_store = entity_store
        self._entry_repository = entry_repository
        self._storyline_repository = storyline_repository
        self._decider = decider
        # Optional semantic fallback (W6): when set, an entry that matches
        # neither an anchor entity nor a surface form is still escalated to
        # the decider if its embedding is close enough to the storyline's
        # *draft chapter* embedding (read live via
        # storyline_repository.get_draft — storylines no longer carry a
        # standalone summary_embedding, see Task 3). None → the fallback is
        # skipped entirely (behaviour identical to before W6).
        self._embedder = embedder
        self._relevance_threshold = relevance_threshold

    def classify_for_entry(
        self, entry_id: int, user_id: int,
    ) -> list[ExtensionResult]:
        entry = self._entry_repository.get_entry(entry_id)
        if entry is None:
            log.warning("Entry %d not found; no extension classifications", entry_id)
            return []

        storylines = self._storyline_repository.list_storylines(
            user_id=user_id, status="active",
        )
        if not storylines:
            return []

        entry_text = (entry.final_text or entry.raw_text or "")
        entry_text_lower = entry_text.lower()
        # Set of entity_ids the entity-extractor already linked to this
        # entry. The class doesn't repeat the LLM extraction itself —
        # storyline_extension_check is queued by the entity-extraction
        # worker only after it commits mentions, so the mention rows are
        # guaranteed present here (no longer a race — see W1).
        extracted_entity_ids = {
            e.id for e in self._entity_store.get_entities_for_entry(entry_id)
        }

        # Embed the entry once (not per storyline) for the semantic
        # fallback. Only when an embedder is wired and there is text to
        # embed; a failure here degrades gracefully to no fallback.
        entry_embedding: list[float] | None = None
        if self._embedder is not None and entry_text.strip():
            try:
                entry_embedding = self._embedder(entry_text)
            except Exception:  # noqa: BLE001 — fallback is best-effort
                log.warning(
                    "Failed to embed entry %d for storyline relevance "
                    "fallback; skipping semantic match.", entry_id,
                    exc_info=True,
                )

        results: list[ExtensionResult] = []
        for storyline in storylines:
            results.append(
                self._classify_one(
                    storyline=storyline,
                    entry=entry,
                    entry_text_lower=entry_text_lower,
                    extracted_entity_ids=extracted_entity_ids,
                    entry_embedding=entry_embedding,
                )
            )
            # Record the per-storyline check timestamp even when
            # decision is "no", so the UI can show "last checked".
            # record_extension_check commits per call already (it's a
            # single-row UPDATE), so there's no batching win from moving
            # this out of the loop — left per-storyline intentionally.
            self._storyline_repository.record_extension_check(storyline.id)
        return results

    def _classify_one(
        self,
        *,
        storyline: Storyline,
        entry: Entry,
        entry_text_lower: str,
        extracted_entity_ids: set[int],
        entry_embedding: list[float] | None = None,
    ) -> ExtensionResult:
        anchor_ids = self._storyline_repository.list_anchors(storyline.id)
        anchor_set = set(anchor_ids)

        # Stage 1: entity overlap on any anchor
        if anchor_set & extracted_entity_ids:
            return ExtensionResult(
                storyline_id=storyline.id,
                decision="yes",
                reasoning=(
                    "Entity overlap: an anchor entity of the storyline "
                    "appears in this entry's extracted mentions."
                ),
                stage="entity_overlap",
            )

        # Stage 2: surface-form match on any anchor → Haiku decider.
        # Word-boundary match (not a bare substring test) so a short
        # anchor name like "Ana" doesn't fire on "banana". Compiled per
        # anchor since canonical names vary per storyline/entity.
        surface_form_hits = False
        for entity_id in anchor_ids:
            entity = self._entity_store.get_entity(entity_id)
            if entity is None:
                continue
            pattern = re.compile(
                rf"\b{re.escape(entity.canonical_name)}\b", re.IGNORECASE,
            )
            if pattern.search(entry_text_lower):
                surface_form_hits = True
                break
        if surface_form_hits:
            return self._decide(storyline, entry, stage="surface_form_llm")

        # Stage 2.5 (W6): semantic fallback. When neither an anchor entity
        # nor a surface form matched, but the entry is semantically close
        # to the storyline's *draft chapter* embedding, escalate to the
        # decider rather than returning an outright "no". Catches entries
        # the extractor missed (pronouns, paraphrase) where the name never
        # appears verbatim. Storylines no longer carry their own embedding
        # (Task 3 dropped Storyline.summary_embedding) — the live draft
        # chapter's embedding is the current stand-in, and may be absent
        # (no draft yet, or a draft never narrated) in which case the
        # stage is skipped entirely.
        draft = self._storyline_repository.get_draft(storyline.id)
        draft_embedding = draft.draft_embedding if draft is not None else None
        if entry_embedding is not None and draft_embedding is not None:
            similarity = cosine_similarity(entry_embedding, draft_embedding)
            if similarity >= self._relevance_threshold:
                return self._decide(storyline, entry, stage="embedding_llm")

        # No signal at all — definite no, no LLM call.
        return ExtensionResult(
            storyline_id=storyline.id,
            decision="no",
            reasoning=(
                "No entity or surface-form match between entry and storyline."
            ),
            stage="no_match",
        )

    def _decide(
        self, storyline: Storyline, entry: Entry, *, stage: str,
    ) -> ExtensionResult:
        """Ask the Haiku decider whether this entry extends the storyline
        and wrap the verdict with the triggering ``stage``."""
        decision = self._decider.decide(
            storyline_name=storyline.name,
            storyline_description=storyline.description,
            entry_date=entry.entry_date,
            entry_text=(entry.final_text or entry.raw_text or ""),
        )
        return ExtensionResult(
            storyline_id=storyline.id,
            decision=decision.decision,
            reasoning=decision.reasoning,
            stage=stage,
        )
