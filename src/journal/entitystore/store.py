"""Entity store Protocol and SQLite implementation.

SQLite is the source of truth for extracted entities, their aliases,
mentions, and relationships. The `EntityStore` Protocol defines the
full surface that `EntityExtractionService` relies on so a graph-DB
implementation (e.g. Memgraph, LadybugDB) can be swapped in later
without touching the service layer.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from journal.models import Entity, EntityMention, EntityRelationship, MergeCandidate, MergeResult

if TYPE_CHECKING:
    import sqlite3

log = logging.getLogger(__name__)


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
    ) -> list[Entity]: ...

    def list_entities_with_mention_counts(
        self,
        entity_type: str | None = None,
        limit: int = 100,
        offset: int = 0,
        user_id: int | None = None,
    ) -> list[tuple[Entity, int, str]]: ...

    def count_entities(self, entity_type: str | None = None, user_id: int | None = None) -> int: ...

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
    ) -> EntityMention: ...

    def get_mentions_for_entity(
        self, entity_id: int, limit: int = 50, offset: int = 0
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
        self, entity_id: int
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

    # ---- merge candidates -----------------------------------------------

    def create_merge_candidate(
        self,
        entity_id_a: int,
        entity_id_b: int,
        similarity: float,
        extraction_run_id: str,
    ) -> None: ...

    def list_merge_candidates(
        self, status: str = "pending", limit: int = 50
    ) -> list[MergeCandidate]: ...

    def resolve_merge_candidate(
        self, candidate_id: int, status: str
    ) -> None: ...

    # ---- merge history ---------------------------------------------------

    def get_merge_history(
        self, entity_id: int
    ) -> list[dict[str, object]]: ...


def _normalise(s: str) -> str:
    return s.strip().lower()


def _row_to_entity(row: sqlite3.Row, aliases: list[str]) -> Entity:
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
    )


class SQLiteEntityStore:
    """SQLite-backed implementation of the `EntityStore` Protocol."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # ---- entity lookup and creation -----------------------------------

    def _load_aliases(self, entity_id: int) -> list[str]:
        rows = self._conn.execute(
            "SELECT alias_normalised FROM entity_aliases"
            " WHERE entity_id = ? ORDER BY alias_normalised",
            (entity_id,),
        ).fetchall()
        return [r["alias_normalised"] for r in rows]

    def _hydrate(self, row: sqlite3.Row) -> Entity:
        aliases = self._load_aliases(row["id"])
        return _row_to_entity(row, aliases)

    def get_entity(self, entity_id: int, user_id: int | None = None) -> Entity | None:
        sql = "SELECT * FROM entities WHERE id = ?"
        params: list[object] = [entity_id]
        if user_id is not None:
            sql += " AND user_id = ?"
            params.append(user_id)
        row = self._conn.execute(sql, params).fetchone()
        return self._hydrate(row) if row else None

    def get_entity_by_name(
        self, canonical_name: str, entity_type: str, user_id: int | None = None
    ) -> Entity | None:
        sql = (
            "SELECT * FROM entities"
            " WHERE entity_type = ? AND LOWER(canonical_name) = LOWER(?)"
        )
        params: list[object] = [entity_type, canonical_name.strip()]
        if user_id is not None:
            sql += " AND user_id = ?"
            params.append(user_id)
        row = self._conn.execute(sql, params).fetchone()
        return self._hydrate(row) if row else None

    def find_by_alias(
        self, alias: str, entity_type: str, user_id: int | None = None
    ) -> Entity | None:
        sql = (
            "SELECT e.* FROM entities e"
            " JOIN entity_aliases a ON a.entity_id = e.id"
            " WHERE e.entity_type = ? AND a.alias_normalised = ?"
        )
        params: list[object] = [entity_type, _normalise(alias)]
        if user_id is not None:
            sql += " AND e.user_id = ?"
            params.append(user_id)
        sql += " LIMIT 1"
        row = self._conn.execute(sql, params).fetchone()
        return self._hydrate(row) if row else None

    def create_entity(
        self,
        entity_type: str,
        canonical_name: str,
        description: str,
        first_seen: str,
        user_id: int = 1,
    ) -> Entity:
        cursor = self._conn.execute(
            "INSERT INTO entities"
            " (user_id, entity_type, canonical_name, description, first_seen)"
            " VALUES (?, ?, ?, ?, ?)",
            (user_id, entity_type, canonical_name.strip(), description, first_seen),
        )
        self._conn.commit()
        entity_id = cursor.lastrowid
        assert entity_id is not None
        log.info(
            "Created entity %d: %s (%s)", entity_id, canonical_name, entity_type
        )
        entity = self.get_entity(entity_id)
        assert entity is not None
        return entity

    def add_alias(self, entity_id: int, alias: str) -> None:
        normalised = _normalise(alias)
        if not normalised:
            return
        self._conn.execute(
            "INSERT OR IGNORE INTO entity_aliases"
            " (entity_id, alias_normalised) VALUES (?, ?)",
            (entity_id, normalised),
        )
        self._conn.commit()

    # ---- embeddings ---------------------------------------------------

    def get_entity_embedding(self, entity_id: int) -> list[float] | None:
        row = self._conn.execute(
            "SELECT embedding_json FROM entities WHERE id = ?",
            (entity_id,),
        ).fetchone()
        if row is None or row["embedding_json"] is None:
            return None
        parsed = json.loads(row["embedding_json"])
        return [float(x) for x in parsed]

    def set_entity_embedding(
        self, entity_id: int, embedding: list[float]
    ) -> None:
        self._conn.execute(
            "UPDATE entities SET embedding_json = ?,"
            " updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"
            " WHERE id = ?",
            (json.dumps(embedding), entity_id),
        )
        self._conn.commit()

    def list_entities_of_type_with_embeddings(
        self, entity_type: str, user_id: int | None = None
    ) -> list[tuple[Entity, list[float]]]:
        sql = (
            "SELECT * FROM entities"
            " WHERE entity_type = ? AND embedding_json IS NOT NULL"
        )
        params: list[object] = [entity_type]
        if user_id is not None:
            sql += " AND user_id = ?"
            params.append(user_id)
        rows = self._conn.execute(sql, params).fetchall()
        result: list[tuple[Entity, list[float]]] = []
        for row in rows:
            entity = self._hydrate(row)
            embedding_raw = json.loads(row["embedding_json"])
            embedding = [float(x) for x in embedding_raw]
            result.append((entity, embedding))
        return result

    # ---- list / count -------------------------------------------------

    def list_entities(
        self,
        entity_type: str | None = None,
        limit: int = 100,
        offset: int = 0,
        user_id: int | None = None,
    ) -> list[Entity]:
        sql = "SELECT * FROM entities"
        params: list[object] = []
        conditions: list[str] = []
        if entity_type:
            conditions.append("entity_type = ?")
            params.append(entity_type)
        if user_id is not None:
            conditions.append("user_id = ?")
            params.append(user_id)
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY entity_type, canonical_name LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = self._conn.execute(sql, params).fetchall()
        return [self._hydrate(r) for r in rows]

    def list_entities_with_mention_counts(
        self,
        entity_type: str | None = None,
        limit: int = 100,
        offset: int = 0,
        user_id: int | None = None,
    ) -> list[tuple[Entity, int, str]]:
        sql = (
            "SELECT e.*, COUNT(m.id) AS mention_count,"
            " MAX(ent.entry_date) AS last_seen"
            " FROM entities e"
            " LEFT JOIN entity_mentions m ON m.entity_id = e.id"
            " LEFT JOIN entries ent ON m.entry_id = ent.id"
        )
        params: list[object] = []
        conditions: list[str] = []
        if entity_type:
            conditions.append("e.entity_type = ?")
            params.append(entity_type)
        if user_id is not None:
            conditions.append("e.user_id = ?")
            params.append(user_id)
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += (
            " GROUP BY e.id"
            " ORDER BY e.entity_type, e.canonical_name"
            " LIMIT ? OFFSET ?"
        )
        params.extend([limit, offset])
        rows = self._conn.execute(sql, params).fetchall()
        return [
            (self._hydrate(r), int(r["mention_count"]), r["last_seen"] or "")
            for r in rows
        ]

    def count_entities(self, entity_type: str | None = None, user_id: int | None = None) -> int:
        sql = "SELECT COUNT(*) AS cnt FROM entities"
        params: list[object] = []
        conditions: list[str] = []
        if entity_type:
            conditions.append("entity_type = ?")
            params.append(entity_type)
        if user_id is not None:
            conditions.append("user_id = ?")
            params.append(user_id)
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        row = self._conn.execute(sql, params).fetchone()
        return int(row["cnt"])

    # ---- mentions -----------------------------------------------------

    def create_mention(
        self,
        entity_id: int,
        entry_id: int,
        quote: str,
        confidence: float,
        extraction_run_id: str,
    ) -> EntityMention:
        cursor = self._conn.execute(
            "INSERT INTO entity_mentions"
            " (entity_id, entry_id, quote, confidence, extraction_run_id)"
            " VALUES (?, ?, ?, ?, ?)",
            (entity_id, entry_id, quote, confidence, extraction_run_id),
        )
        self._conn.commit()
        mention_id = cursor.lastrowid
        assert mention_id is not None
        row = self._conn.execute(
            "SELECT * FROM entity_mentions WHERE id = ?", (mention_id,)
        ).fetchone()
        return _row_to_mention(row)

    def get_mentions_for_entity(
        self, entity_id: int, limit: int = 50, offset: int = 0
    ) -> list[EntityMention]:
        rows = self._conn.execute(
            "SELECT * FROM entity_mentions WHERE entity_id = ?"
            " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            (entity_id, limit, offset),
        ).fetchall()
        return [_row_to_mention(r) for r in rows]

    def get_mentions_for_entry(
        self, entry_id: int
    ) -> list[EntityMention]:
        rows = self._conn.execute(
            "SELECT * FROM entity_mentions WHERE entry_id = ?"
            " ORDER BY id",
            (entry_id,),
        ).fetchall()
        return [_row_to_mention(r) for r in rows]

    def delete_mentions_for_entry(self, entry_id: int) -> int:
        cursor = self._conn.execute(
            "DELETE FROM entity_mentions WHERE entry_id = ?", (entry_id,)
        )
        self._conn.commit()
        return cursor.rowcount

    # ---- relationships ------------------------------------------------

    def create_relationship(
        self,
        subject_id: int,
        predicate: str,
        object_id: int,
        quote: str,
        entry_id: int,
        confidence: float,
        extraction_run_id: str,
    ) -> EntityRelationship:
        cursor = self._conn.execute(
            "INSERT INTO entity_relationships"
            " (subject_entity_id, predicate, object_entity_id, quote,"
            " entry_id, confidence, extraction_run_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                subject_id,
                predicate,
                object_id,
                quote,
                entry_id,
                confidence,
                extraction_run_id,
            ),
        )
        self._conn.commit()
        rel_id = cursor.lastrowid
        assert rel_id is not None
        row = self._conn.execute(
            "SELECT * FROM entity_relationships WHERE id = ?", (rel_id,)
        ).fetchone()
        return _row_to_relationship(row)

    def get_relationships_for_entity(
        self, entity_id: int
    ) -> tuple[list[EntityRelationship], list[EntityRelationship]]:
        outgoing_rows = self._conn.execute(
            "SELECT * FROM entity_relationships"
            " WHERE subject_entity_id = ? ORDER BY id",
            (entity_id,),
        ).fetchall()
        incoming_rows = self._conn.execute(
            "SELECT * FROM entity_relationships"
            " WHERE object_entity_id = ? ORDER BY id",
            (entity_id,),
        ).fetchall()
        return (
            [_row_to_relationship(r) for r in outgoing_rows],
            [_row_to_relationship(r) for r in incoming_rows],
        )

    def get_relationships_for_entry(
        self, entry_id: int
    ) -> list[EntityRelationship]:
        rows = self._conn.execute(
            "SELECT * FROM entity_relationships WHERE entry_id = ?"
            " ORDER BY id",
            (entry_id,),
        ).fetchall()
        return [_row_to_relationship(r) for r in rows]

    def delete_relationships_for_entry(self, entry_id: int) -> int:
        cursor = self._conn.execute(
            "DELETE FROM entity_relationships WHERE entry_id = ?",
            (entry_id,),
        )
        self._conn.commit()
        return cursor.rowcount

    # ---- per-entry lookups & stale flag -------------------------------

    def get_entities_for_entry(self, entry_id: int) -> list[Entity]:
        rows = self._conn.execute(
            "SELECT DISTINCT e.* FROM entities e"
            " JOIN entity_mentions m ON m.entity_id = e.id"
            " WHERE m.entry_id = ?"
            " ORDER BY e.entity_type, e.canonical_name",
            (entry_id,),
        ).fetchall()
        return [self._hydrate(r) for r in rows]

    def mark_entry_extracted(self, entry_id: int) -> None:
        self._conn.execute(
            "UPDATE entries SET entity_extraction_stale = 0 WHERE id = ?",
            (entry_id,),
        )
        self._conn.commit()

    # ---- entity management (update / delete / merge) --------------------

    def update_entity(
        self,
        entity_id: int,
        *,
        canonical_name: str | None = None,
        entity_type: str | None = None,
        description: str | None = None,
        user_id: int | None = None,
    ) -> Entity:
        entity = self.get_entity(entity_id, user_id=user_id)
        if entity is None:
            raise ValueError(f"Entity {entity_id} not found")
        sets: list[str] = []
        params: list[object] = []
        if canonical_name is not None:
            sets.append("canonical_name = ?")
            params.append(canonical_name.strip())
        if entity_type is not None:
            sets.append("entity_type = ?")
            params.append(entity_type)
        if description is not None:
            sets.append("description = ?")
            params.append(description)
        if not sets:
            return entity
        sets.append("updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')")
        params.append(entity_id)
        self._conn.execute(
            f"UPDATE entities SET {', '.join(sets)} WHERE id = ?",
            params,
        )
        self._conn.commit()
        updated = self.get_entity(entity_id)
        assert updated is not None
        log.info("Updated entity %d: %s", entity_id, updated.canonical_name)
        return updated

    def delete_entity(self, entity_id: int, user_id: int | None = None) -> None:
        entity = self.get_entity(entity_id, user_id=user_id)
        if entity is None:
            raise ValueError(f"Entity {entity_id} not found")
        sql = "DELETE FROM entities WHERE id = ?"
        params: list[object] = [entity_id]
        if user_id is not None:
            sql += " AND user_id = ?"
            params.append(user_id)
        self._conn.execute(sql, params)
        self._conn.commit()
        log.info(
            "Deleted entity %d: %s (%s)",
            entity_id, entity.canonical_name, entity.entity_type,
        )

    def merge_entities(
        self, survivor_id: int, absorbed_ids: list[int]
    ) -> MergeResult:
        survivor = self.get_entity(survivor_id)
        if survivor is None:
            raise ValueError(f"Survivor entity {survivor_id} not found")

        total_mentions = 0
        total_relationships = 0
        total_aliases = 0

        for absorbed_id in absorbed_ids:
            absorbed = self.get_entity(absorbed_id)
            if absorbed is None:
                raise ValueError(f"Absorbed entity {absorbed_id} not found")
            if absorbed_id == survivor_id:
                raise ValueError("Cannot merge entity into itself")

            # Snapshot the absorbed entity for merge history
            self._conn.execute(
                "INSERT INTO entity_merge_history"
                " (survivor_id, absorbed_id, absorbed_name,"
                "  absorbed_type, absorbed_desc, absorbed_aliases)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    survivor_id,
                    absorbed_id,
                    absorbed.canonical_name,
                    absorbed.entity_type,
                    absorbed.description,
                    json.dumps(absorbed.aliases),
                ),
            )

            # Reassign mentions
            cursor = self._conn.execute(
                "UPDATE entity_mentions SET entity_id = ?"
                " WHERE entity_id = ?",
                (survivor_id, absorbed_id),
            )
            total_mentions += cursor.rowcount

            # Reassign relationships (both sides)
            cursor = self._conn.execute(
                "UPDATE entity_relationships SET subject_entity_id = ?"
                " WHERE subject_entity_id = ?",
                (survivor_id, absorbed_id),
            )
            total_relationships += cursor.rowcount
            cursor = self._conn.execute(
                "UPDATE entity_relationships SET object_entity_id = ?"
                " WHERE object_entity_id = ?",
                (survivor_id, absorbed_id),
            )
            total_relationships += cursor.rowcount

            # Copy aliases (including the absorbed entity's canonical name)
            for alias in [*absorbed.aliases, _normalise(absorbed.canonical_name)]:
                if alias and alias != _normalise(survivor.canonical_name):
                    self._conn.execute(
                        "INSERT OR IGNORE INTO entity_aliases"
                        " (entity_id, alias_normalised) VALUES (?, ?)",
                        (survivor_id, alias),
                    )
                    total_aliases += 1

            # Delete the absorbed entity (cascades aliases)
            self._conn.execute(
                "DELETE FROM entities WHERE id = ?", (absorbed_id,)
            )

            # Dismiss any pending merge candidates involving the absorbed entity
            self._conn.execute(
                "UPDATE entity_merge_candidates SET status = 'accepted',"
                " resolved_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"
                " WHERE status = 'pending'"
                " AND (entity_id_a = ? OR entity_id_b = ?)",
                (absorbed_id, absorbed_id),
            )

            log.info(
                "Merged entity %d (%s) into %d (%s)",
                absorbed_id, absorbed.canonical_name,
                survivor_id, survivor.canonical_name,
            )

        self._conn.commit()
        return MergeResult(
            survivor_id=survivor_id,
            absorbed_ids=absorbed_ids,
            mentions_reassigned=total_mentions,
            relationships_reassigned=total_relationships,
            aliases_added=total_aliases,
        )

    # ---- merge candidates -----------------------------------------------

    def create_merge_candidate(
        self,
        entity_id_a: int,
        entity_id_b: int,
        similarity: float,
        extraction_run_id: str,
    ) -> None:
        # Normalise order so (a, b) == (b, a)
        lo, hi = sorted([entity_id_a, entity_id_b])
        self._conn.execute(
            "INSERT OR IGNORE INTO entity_merge_candidates"
            " (entity_id_a, entity_id_b, similarity, extraction_run_id)"
            " VALUES (?, ?, ?, ?)",
            (lo, hi, similarity, extraction_run_id),
        )
        self._conn.commit()

    def list_merge_candidates(
        self, status: str = "pending", limit: int = 50
    ) -> list[MergeCandidate]:
        rows = self._conn.execute(
            "SELECT * FROM entity_merge_candidates"
            " WHERE status = ? ORDER BY similarity DESC LIMIT ?",
            (status, limit),
        ).fetchall()
        candidates: list[MergeCandidate] = []
        for row in rows:
            entity_a = self.get_entity(row["entity_id_a"])
            entity_b = self.get_entity(row["entity_id_b"])
            if entity_a is None or entity_b is None:
                continue
            candidates.append(MergeCandidate(
                id=row["id"],
                entity_a=entity_a,
                entity_b=entity_b,
                similarity=row["similarity"],
                status=row["status"],
                extraction_run_id=row["extraction_run_id"],
                created_at=row["created_at"],
            ))
        return candidates

    def resolve_merge_candidate(
        self, candidate_id: int, status: str
    ) -> None:
        if status not in ("accepted", "dismissed"):
            raise ValueError(f"Invalid status: {status}")
        self._conn.execute(
            "UPDATE entity_merge_candidates SET status = ?,"
            " resolved_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"
            " WHERE id = ?",
            (status, candidate_id),
        )
        self._conn.commit()

    # ---- merge history ---------------------------------------------------

    def get_merge_history(
        self, entity_id: int
    ) -> list[dict[str, object]]:
        rows = self._conn.execute(
            "SELECT * FROM entity_merge_history"
            " WHERE survivor_id = ? ORDER BY merged_at DESC",
            (entity_id,),
        ).fetchall()
        return [
            {
                "id": r["id"],
                "survivor_id": r["survivor_id"],
                "absorbed_id": r["absorbed_id"],
                "absorbed_name": r["absorbed_name"],
                "absorbed_type": r["absorbed_type"],
                "absorbed_desc": r["absorbed_desc"],
                "absorbed_aliases": json.loads(r["absorbed_aliases"]),
                "merged_at": r["merged_at"],
                "merged_by": r["merged_by"],
            }
            for r in rows
        ]


def _row_to_mention(row: sqlite3.Row) -> EntityMention:
    return EntityMention(
        id=row["id"],
        entity_id=row["entity_id"],
        entry_id=row["entry_id"],
        quote=row["quote"],
        confidence=row["confidence"],
        extraction_run_id=row["extraction_run_id"],
        created_at=row["created_at"],
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
