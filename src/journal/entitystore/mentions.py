"""Mentions + relationships mixin for ``SQLiteEntityStore``.

Holds every method whose primary table is ``entity_mentions`` or
``entity_relationships``, plus the entry-side lookups that join
those tables (``get_entities_for_entry``, ``mark_entry_extracted``).
Methods stay bound to ``self`` so they keep using ``self._conn`` and
``self._hydrate`` from the base store.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from journal.entitystore.protocol import _row_to_mention, _row_to_relationship

if TYPE_CHECKING:
    from journal.models import Entity, EntityMention, EntityRelationship


class _MentionsMixin:
    """Mentions + relationships + entry-side lookups."""

    # ---- mentions -----------------------------------------------------

    def create_mention(
        self,
        entity_id: int,
        entry_id: int,
        quote: str,
        confidence: float,
        extraction_run_id: str,
        match_source: str | None = None,
    ) -> EntityMention:
        cursor = self._conn.execute(  # type: ignore[attr-defined]
            "INSERT INTO entity_mentions"
            " (entity_id, entry_id, quote, confidence,"
            "  extraction_run_id, match_source)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                entity_id, entry_id, quote, confidence,
                extraction_run_id, match_source,
            ),
        )
        self._conn.commit()  # type: ignore[attr-defined]
        mention_id = cursor.lastrowid
        assert mention_id is not None
        row = self._conn.execute(  # type: ignore[attr-defined]
            "SELECT * FROM entity_mentions WHERE id = ?", (mention_id,),
        ).fetchone()
        return _row_to_mention(row)

    def get_mentions_for_entity(
        self,
        entity_id: int,
        limit: int = 50,
        offset: int = 0,
        user_id: int | None = None,
    ) -> list[EntityMention]:
        if user_id is not None:
            sql = (
                "SELECT m.* FROM entity_mentions m"
                " JOIN entries e ON e.id = m.entry_id"
                " WHERE m.entity_id = ? AND e.user_id = ?"
                " ORDER BY m.created_at DESC, m.id DESC LIMIT ? OFFSET ?"
            )
            params: tuple[object, ...] = (entity_id, user_id, limit, offset)
        else:
            sql = (
                "SELECT * FROM entity_mentions WHERE entity_id = ?"
                " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
            )
            params = (entity_id, limit, offset)
        rows = self._conn.execute(sql, params).fetchall()  # type: ignore[attr-defined]
        return [_row_to_mention(r) for r in rows]

    def get_mentions_for_entry(self, entry_id: int) -> list[EntityMention]:
        rows = self._conn.execute(  # type: ignore[attr-defined]
            "SELECT * FROM entity_mentions WHERE entry_id = ? ORDER BY id",
            (entry_id,),
        ).fetchall()
        return [_row_to_mention(r) for r in rows]

    def delete_mentions_for_entry(self, entry_id: int) -> int:
        cursor = self._conn.execute(  # type: ignore[attr-defined]
            "DELETE FROM entity_mentions WHERE entry_id = ?", (entry_id,),
        )
        self._conn.commit()  # type: ignore[attr-defined]
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
        cursor = self._conn.execute(  # type: ignore[attr-defined]
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
        self._conn.commit()  # type: ignore[attr-defined]
        rel_id = cursor.lastrowid
        assert rel_id is not None
        row = self._conn.execute(  # type: ignore[attr-defined]
            "SELECT * FROM entity_relationships WHERE id = ?", (rel_id,),
        ).fetchone()
        return _row_to_relationship(row)

    def get_relationships_for_entity(
        self, entity_id: int, user_id: int | None = None,
    ) -> tuple[list[EntityRelationship], list[EntityRelationship]]:
        if user_id is not None:
            # Filter to relationships whose entry belongs to this user.
            outgoing_rows = self._conn.execute(  # type: ignore[attr-defined]
                "SELECT r.* FROM entity_relationships r"
                " JOIN entries e ON e.id = r.entry_id"
                " WHERE r.subject_entity_id = ? AND e.user_id = ?"
                " ORDER BY r.id",
                (entity_id, user_id),
            ).fetchall()
            incoming_rows = self._conn.execute(  # type: ignore[attr-defined]
                "SELECT r.* FROM entity_relationships r"
                " JOIN entries e ON e.id = r.entry_id"
                " WHERE r.object_entity_id = ? AND e.user_id = ?"
                " ORDER BY r.id",
                (entity_id, user_id),
            ).fetchall()
        else:
            outgoing_rows = self._conn.execute(  # type: ignore[attr-defined]
                "SELECT * FROM entity_relationships"
                " WHERE subject_entity_id = ? ORDER BY id",
                (entity_id,),
            ).fetchall()
            incoming_rows = self._conn.execute(  # type: ignore[attr-defined]
                "SELECT * FROM entity_relationships"
                " WHERE object_entity_id = ? ORDER BY id",
                (entity_id,),
            ).fetchall()
        return (
            [_row_to_relationship(r) for r in outgoing_rows],
            [_row_to_relationship(r) for r in incoming_rows],
        )

    def get_relationships_for_entry(
        self, entry_id: int,
    ) -> list[EntityRelationship]:
        rows = self._conn.execute(  # type: ignore[attr-defined]
            "SELECT * FROM entity_relationships WHERE entry_id = ?"
            " ORDER BY id",
            (entry_id,),
        ).fetchall()
        return [_row_to_relationship(r) for r in rows]

    def delete_relationships_for_entry(self, entry_id: int) -> int:
        cursor = self._conn.execute(  # type: ignore[attr-defined]
            "DELETE FROM entity_relationships WHERE entry_id = ?",
            (entry_id,),
        )
        self._conn.commit()  # type: ignore[attr-defined]
        return cursor.rowcount

    # ---- per-entry lookups & stale flag -------------------------------

    def get_entities_for_entry(self, entry_id: int) -> list[Entity]:
        rows = self._conn.execute(  # type: ignore[attr-defined]
            "SELECT DISTINCT e.* FROM entities e"
            " JOIN entity_mentions m ON m.entity_id = e.id"
            " WHERE m.entry_id = ?"
            " ORDER BY e.entity_type, e.canonical_name",
            (entry_id,),
        ).fetchall()
        return [self._hydrate(r) for r in rows]  # type: ignore[attr-defined]

    def mark_entry_extracted(self, entry_id: int) -> None:
        self._conn.execute(  # type: ignore[attr-defined]
            "UPDATE entries SET entity_extraction_stale = 0 WHERE id = ?",
            (entry_id,),
        )
        self._conn.commit()  # type: ignore[attr-defined]
