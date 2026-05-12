"""Extension classifier — does this entry extend an existing storyline?

Hybrid pipeline, applied per-(entry, storyline) pair:

1. **Entity overlap (deterministic).** If *any* of the storyline's
   anchor entity_ids appears in the entry's extracted entity
   mentions, return ``yes`` immediately. Zero cost, zero LLM calls.
2. **Surface form (deterministic).** If *any* anchor entity's
   ``canonical_name`` appears in the entry text (case-insensitive),
   fall through to the LLM decider — the surface form is present
   but the entity wasn't extracted, so pronominal references or
   extractor gaps are possible.
3. **Haiku decider.** Ask the model whether the entry meaningfully
   extends the storyline. Returns yes/no/maybe with one-sentence
   reasoning that the UI surfaces.

When neither (1) nor (2) fires, the classifier returns ``no``
without an LLM call — most ingested entries do not extend most
storylines, and surfacing every miss to Haiku is wasteful.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
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
    stage: str  # "entity_overlap" | "surface_form_llm" | "no_match"


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
    ) -> None:
        self._entity_store = entity_store
        self._entry_repository = entry_repository
        self._storyline_repository = storyline_repository
        self._decider = decider

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
        # storyline_extension_check fires AFTER entity extraction has
        # already run, so the mention rows are present.
        extracted_entity_ids = {
            e.id for e in self._entity_store.get_entities_for_entry(entry_id)
        }

        results: list[ExtensionResult] = []
        for storyline in storylines:
            results.append(
                self._classify_one(
                    storyline=storyline,
                    entry=entry,
                    entry_text_lower=entry_text_lower,
                    extracted_entity_ids=extracted_entity_ids,
                )
            )
            # Record the per-storyline check timestamp even when
            # decision is "no", so the UI can show "last checked".
            self._storyline_repository.record_extension_check(storyline.id)
        return results

    def _classify_one(
        self,
        *,
        storyline: Storyline,
        entry: Entry,
        entry_text_lower: str,
        extracted_entity_ids: set[int],
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

        # Stage 2: surface-form match on any anchor → Haiku decider
        surface_form_hits = False
        for entity_id in anchor_ids:
            entity = self._entity_store.get_entity(entity_id)
            if entity is None:
                continue
            if entity.canonical_name.lower() in entry_text_lower:
                surface_form_hits = True
                break
        if surface_form_hits:
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
                stage="surface_form_llm",
            )

        # No signal at all — definite no, no LLM call.
        return ExtensionResult(
            storyline_id=storyline.id,
            decision="no",
            reasoning=(
                "No entity or surface-form match between entry and storyline."
            ),
            stage="no_match",
        )
