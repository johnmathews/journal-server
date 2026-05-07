"""Entity-store Protocol + shared row-conversion helpers.

Lives separately from ``store.py`` so the per-mixin modules
(``mentions.py``, ``merge.py``) can import ``_row_to_mention`` /
``_row_to_relationship`` / ``_normalise`` without a circular import
through ``store``. Callers that previously imported ``EntityStore``
from ``journal.entitystore.store`` still get it through the
re-export at the end of ``store.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from journal.models import (
    Entity,
    EntityMention,
    EntityRelationship,
    MergeCandidate,
    MergeResult,
)

if TYPE_CHECKING:
    import sqlite3


def _normalise(s: str) -> str:
    return s.strip().lower()


def _row_to_entity(row: sqlite3.Row, aliases: list[str]) -> Entity:
    # `is_quarantined`/`quarantine_reason`/`quarantined_at` arrive on the row
    # via migration 0018. They're read with sqlite3.Row's mapping access using
    # `.keys()` so older fixtures that bypass the migration runner don't
    # explode — quarantine simply degrades to "off" for them.
    keys = row.keys()
    is_quarantined = bool(row["is_quarantined"]) if "is_quarantined" in keys else False
    quarantine_reason = (
        row["quarantine_reason"] if "quarantine_reason" in keys else ""
    ) or ""
    quarantined_at = (
        row["quarantined_at"] if "quarantined_at" in keys else ""
    ) or ""
    return Entity(
        id=row["id"],
        entity_type=row["entity_type"],
        canonical_name=row["canonical_name"],
        user_id=row["user_id"],
        description=row["description"] or "",
        aliases=aliases,
        first_seen=row["first_seen"] or "",
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        is_quarantined=is_quarantined,
        quarantine_reason=quarantine_reason,
        quarantined_at=quarantined_at,
    )


def _row_to_mention(row: sqlite3.Row) -> EntityMention:
    # match_source arrives via migration 0020. Fall back to None for
    # rows from fixtures that bypass the migration runner.
    keys = row.keys()
    match_source = row["match_source"] if "match_source" in keys else None
    return EntityMention(
        id=row["id"],
        entity_id=row["entity_id"],
        entry_id=row["entry_id"],
        quote=row["quote"],
        confidence=row["confidence"],
        extraction_run_id=row["extraction_run_id"],
        created_at=row["created_at"],
        match_source=match_source,
    )


def _row_to_relationship(row: sqlite3.Row) -> EntityRelationship:
    return EntityRelationship(
        id=row["id"],
        subject_entity_id=row["subject_entity_id"],
        predicate=row["predicate"],
        object_entity_id=row["object_entity_id"],
        quote=row["quote"],
        entry_id=row["entry_id"],
        confidence=row["confidence"],
        extraction_run_id=row["extraction_run_id"],
        created_at=row["created_at"],
    )


@runtime_checkable
class EntityStore(Protocol):
    """Storage surface for entity tracking.

    Every method used by `EntityExtractionService` must be declared
    here. Future graph-DB implementations should satisfy the same
    shape so the service layer can swap backends transparently.
    """

    def get_entity_by_name(
        self, canonical_name: str, entity_type: str, user_id: int | None = None
    ) -> Entity | None: ...

    def find_by_alias(
        self, alias: str, entity_type: str, user_id: int | None = None
    ) -> Entity | None: ...

    def create_entity(
        self,
        entity_type: str,
        canonical_name: str,
        description: str,
        first_seen: str,
        user_id: int = 1,
    ) -> Entity: ...

    def add_alias(self, entity_id: int, alias: str) -> None: ...

    def remove_alias(self, entity_id: int, alias: str) -> bool: ...

    def find_entity_by_alias_for_user(
        self, alias: str, user_id: int
    ) -> Entity | None: ...

    def get_entity_embedding(self, entity_id: int) -> list[float] | None: ...

    def set_entity_embedding(
        self, entity_id: int, embedding: list[float]
    ) -> None: ...

    def list_entities(
        self,
        entity_type: str | None = None,
        limit: int = 100,
        offset: int = 0,
        user_id: int | None = None,
        include_quarantined: bool = False,
    ) -> list[Entity]: ...

    def list_entities_with_mention_counts(
        self,
        entity_type: str | None = None,
        limit: int = 100,
        offset: int = 0,
        user_id: int | None = None,
        search: str | None = None,
        include_quarantined: bool = False,
    ) -> list[tuple[Entity, int, str]]: ...

    def list_quarantined_entities(self, user_id: int) -> list[Entity]: ...

    def quarantine_entity(self, entity_id: int, reason: str) -> None: ...

    def release_quarantine(self, entity_id: int) -> None: ...

    def count_entities(
        self,
        entity_type: str | None = None,
        user_id: int | None = None,
        search: str | None = None,
        include_quarantined: bool = False,
    ) -> int: ...

    def get_entity(self, entity_id: int, user_id: int | None = None) -> Entity | None: ...

    def list_entities_of_type_with_embeddings(
        self, entity_type: str, user_id: int | None = None
    ) -> list[tuple[Entity, list[float]]]: ...

    def create_mention(
        self,
        entity_id: int,
        entry_id: int,
        quote: str,
        confidence: float,
        extraction_run_id: str,
        match_source: str | None = None,
    ) -> EntityMention: ...

    def get_mentions_for_entity(
        self, entity_id: int, limit: int = 50, offset: int = 0,
        user_id: int | None = None,
    ) -> list[EntityMention]: ...

    def get_mentions_for_entry(
        self, entry_id: int
    ) -> list[EntityMention]: ...

    def delete_mentions_for_entry(self, entry_id: int) -> int: ...

    def create_relationship(
        self,
        subject_id: int,
        predicate: str,
        object_id: int,
        quote: str,
        entry_id: int,
        confidence: float,
        extraction_run_id: str,
    ) -> EntityRelationship: ...

    def get_relationships_for_entity(
        self, entity_id: int, user_id: int | None = None,
    ) -> tuple[list[EntityRelationship], list[EntityRelationship]]: ...

    def get_relationships_for_entry(
        self, entry_id: int
    ) -> list[EntityRelationship]: ...

    def delete_relationships_for_entry(self, entry_id: int) -> int: ...

    def get_entities_for_entry(self, entry_id: int) -> list[Entity]: ...

    def mark_entry_extracted(self, entry_id: int) -> None: ...

    # ---- entity management (update / delete / merge) --------------------

    def update_entity(
        self,
        entity_id: int,
        *,
        canonical_name: str | None = None,
        entity_type: str | None = None,
        description: str | None = None,
        user_id: int | None = None,
    ) -> Entity: ...

    def delete_entity(self, entity_id: int, user_id: int | None = None) -> None: ...

    def merge_entities(
        self, survivor_id: int, absorbed_ids: list[int]
    ) -> MergeResult: ...

    def delete_orphaned_entities(self, entity_ids: list[int]) -> int:
        """Delete entities from *entity_ids* that have zero remaining mentions.

        Returns the number of entities deleted."""
        ...

    # ---- merge candidates -----------------------------------------------

    def create_merge_candidate(
        self,
        entity_id_a: int,
        entity_id_b: int,
        similarity: float,
        extraction_run_id: str,
    ) -> None: ...

    def list_merge_candidates(
        self, status: str = "pending", limit: int = 50,
        user_id: int | None = None,
    ) -> list[MergeCandidate]: ...

    def resolve_merge_candidate(
        self, candidate_id: int, status: str
    ) -> None: ...

    # ---- merge history ---------------------------------------------------

    def get_merge_history(
        self, entity_id: int
    ) -> list[dict[str, object]]: ...
