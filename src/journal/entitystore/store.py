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

from journal.models import Entity, EntityMention, EntityRelationship

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
        self, canonical_name: str, entity_type: str
    ) -> Entity | None: ...

    def find_by_alias(
        self, alias: str, entity_type: str
    ) -> Entity | None: ...

    def create_entity(
        self,
        entity_type: str,
        canonical_name: str,
        description: str,
        first_seen: str,
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
    ) -> list[Entity]: ...

    def list_entities_with_mention_counts(
        self,
        entity_type: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[tuple[Entity, int, str]]: ...

    def count_entities(self, entity_type: str | None = None) -> int: ...

    def get_entity(self, entity_id: int) -> Entity | None: ...

    def list_entities_of_type_with_embeddings(
        self, entity_type: str
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


def _normalise(s: str) -> str:
    return s.strip().lower()


def _row_to_entity(row: sqlite3.Row, aliases: list[str]) -> Entity:
    return Entity(
        id=row["id"],
        entity_type=row["entity_type"],
        canonical_name=row["canonical_name"],
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

    def get_entity(self, entity_id: int) -> Entity | None:
        row = self._conn.execute(
            "SELECT * FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        return self._hydrate(row) if row else None

    def get_entity_by_name(
        self, canonical_name: str, entity_type: str
    ) -> Entity | None:
        row = self._conn.execute(
            "SELECT * FROM entities"
            " WHERE entity_type = ? AND LOWER(canonical_name) = LOWER(?)",
            (entity_type, canonical_name.strip()),
        ).fetchone()
        return self._hydrate(row) if row else None

    def find_by_alias(
        self, alias: str, entity_type: str
    ) -> Entity | None:
        row = self._conn.execute(
            "SELECT e.* FROM entities e"
            " JOIN entity_aliases a ON a.entity_id = e.id"
            " WHERE e.entity_type = ? AND a.alias_normalised = ?"
            " LIMIT 1",
            (entity_type, _normalise(alias)),
        ).fetchone()
        return self._hydrate(row) if row else None

    def create_entity(
        self,
        entity_type: str,
        canonical_name: str,
        description: str,
        first_seen: str,
    ) -> Entity:
        cursor = self._conn.execute(
            "INSERT INTO entities"
            " (entity_type, canonical_name, description, first_seen)"
            " VALUES (?, ?, ?, ?)",
            (entity_type, canonical_name.strip(), description, first_seen),
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
        self, entity_type: str
    ) -> list[tuple[Entity, list[float]]]:
        rows = self._conn.execute(
            "SELECT * FROM entities"
            " WHERE entity_type = ? AND embedding_json IS NOT NULL",
            (entity_type,),
        ).fetchall()
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
    ) -> list[Entity]:
        sql = "SELECT * FROM entities"
        params: list[object] = []
        if entity_type:
            sql += " WHERE entity_type = ?"
            params.append(entity_type)
        sql += " ORDER BY entity_type, canonical_name LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = self._conn.execute(sql, params).fetchall()
        return [self._hydrate(r) for r in rows]

    def list_entities_with_mention_counts(
        self,
        entity_type: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[tuple[Entity, int, str]]:
        sql = (
            "SELECT e.*, COUNT(m.id) AS mention_count,"
            " MAX(ent.entry_date) AS last_seen"
            " FROM entities e"
            " LEFT JOIN entity_mentions m ON m.entity_id = e.id"
            " LEFT JOIN entries ent ON m.entry_id = ent.id"
        )
        params: list[object] = []
        if entity_type:
            sql += " WHERE e.entity_type = ?"
            params.append(entity_type)
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

    def count_entities(self, entity_type: str | None = None) -> int:
        sql = "SELECT COUNT(*) AS cnt FROM entities"
        params: list[object] = []
        if entity_type:
            sql += " WHERE entity_type = ?"
            params.append(entity_type)
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
