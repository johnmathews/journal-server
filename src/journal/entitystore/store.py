"""SQLite-backed implementation of the entity store.

The ``EntityStore`` Protocol and the row-converter helpers live in
``protocol.py``. Mention + relationship operations live in
``mentions.py``; merge / quarantine / merge-candidate operations
live in ``merge.py``. ``SQLiteEntityStore`` composes those mixins
and owns the entity-CRUD + listing + embedding methods directly.

``EntityStore`` is re-exported from this module so existing call
sites (``from journal.entitystore.store import EntityStore``) keep
working.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from journal.entitystore.mentions import _MentionsMixin
from journal.entitystore.merge import _MergeMixin
from journal.entitystore.protocol import (
    EntityStore,
    _normalise,
    _row_to_entity,
)
from journal.services.entity_naming import smart_title_case

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Mapping

    from journal.models import Entity

__all__ = ["EntityStore", "SQLiteEntityStore"]

log = logging.getLogger(__name__)


class SQLiteEntityStore(_MentionsMixin, _MergeMixin):
    """SQLite-backed implementation of the ``EntityStore`` Protocol.

    Owns entity-CRUD + listing + embeddings directly; mentions /
    relationships / merge / quarantine / merge-candidate operations
    are pulled in from ``mentions.py`` and ``merge.py`` mixins.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        casing_exceptions: Mapping[str, str] | None = None,
    ) -> None:
        self._conn = conn
        # Stored as a dict so the reload helper can rebind it in place by
        # calling `store.set_casing_exceptions(...)`. A None value at construction
        # time means "no exceptions" — the algorithm degrades to plain smart-title-case.
        self._casing_exceptions: dict[str, str] = (
            dict(casing_exceptions) if casing_exceptions else {}
        )

    def set_casing_exceptions(self, exceptions: Mapping[str, str]) -> None:
        """Swap in a fresh exceptions table. Called by the reload helper.

        Atomic from the caller's perspective: a single attribute write replaces
        the dict reference; in-flight ``create_entity`` calls already inside
        ``smart_title_case`` keep their existing reference until they finish.
        """
        self._casing_exceptions = dict(exceptions)

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
        normalised_name = smart_title_case(
            canonical_name, exceptions=self._casing_exceptions
        )
        cursor = self._conn.execute(
            "INSERT INTO entities"
            " (user_id, entity_type, canonical_name, description, first_seen)"
            " VALUES (?, ?, ?, ?, ?)",
            (user_id, entity_type, normalised_name, description, first_seen),
        )
        self._conn.commit()
        entity_id = cursor.lastrowid
        assert entity_id is not None
        log.info(
            "Created entity %d: %s (%s)", entity_id, normalised_name, entity_type
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

    def remove_alias(self, entity_id: int, alias: str) -> bool:
        normalised = _normalise(alias)
        if not normalised:
            return False
        cursor = self._conn.execute(
            "DELETE FROM entity_aliases"
            " WHERE entity_id = ? AND alias_normalised = ?",
            (entity_id, normalised),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def find_entity_by_alias_for_user(
        self, alias: str, user_id: int
    ) -> Entity | None:
        normalised = _normalise(alias)
        if not normalised:
            return None
        row = self._conn.execute(
            "SELECT e.* FROM entities e"
            " JOIN entity_aliases a ON a.entity_id = e.id"
            " WHERE a.alias_normalised = ? AND e.user_id = ?"
            " LIMIT 1",
            (normalised, user_id),
        ).fetchone()
        return self._hydrate(row) if row else None

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
        include_quarantined: bool = False,
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
        if not include_quarantined:
            conditions.append("is_quarantined = 0")
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
        search: str | None = None,
        include_quarantined: bool = False,
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
        if not include_quarantined:
            conditions.append("e.is_quarantined = 0")
        if search:
            needle = f"%{search.strip().lower()}%"
            conditions.append(
                "(LOWER(e.canonical_name) LIKE ?"
                " OR EXISTS ("
                "   SELECT 1 FROM entity_aliases a"
                "   WHERE a.entity_id = e.id AND LOWER(a.alias_normalised) LIKE ?"
                " ))"
            )
            params.extend([needle, needle])
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

    def count_entities(
        self,
        entity_type: str | None = None,
        user_id: int | None = None,
        search: str | None = None,
        include_quarantined: bool = False,
    ) -> int:
        sql = "SELECT COUNT(*) AS cnt FROM entities"
        params: list[object] = []
        conditions: list[str] = []
        if entity_type:
            conditions.append("entity_type = ?")
            params.append(entity_type)
        if user_id is not None:
            conditions.append("user_id = ?")
            params.append(user_id)
        if not include_quarantined:
            conditions.append("is_quarantined = 0")
        if search:
            needle = f"%{search.strip().lower()}%"
            conditions.append(
                "(LOWER(canonical_name) LIKE ?"
                " OR EXISTS ("
                "   SELECT 1 FROM entity_aliases a"
                "   WHERE a.entity_id = entities.id AND LOWER(a.alias_normalised) LIKE ?"
                " ))"
            )
            params.extend([needle, needle])
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        row = self._conn.execute(sql, params).fetchone()
        return int(row["cnt"])

    # ---- mentions -----------------------------------------------------

    # ---- relationships ------------------------------------------------

    # ---- per-entry lookups & stale flag -------------------------------

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
            params.append(
                smart_title_case(canonical_name, exceptions=self._casing_exceptions)
            )
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

    # ---- quarantine ------------------------------------------------------

    # ---- merge candidates -----------------------------------------------

    # ---- merge history ---------------------------------------------------

