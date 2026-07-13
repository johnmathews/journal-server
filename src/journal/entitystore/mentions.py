"""Mentions + relationships mixin for ``SQLiteEntityStore``.

Holds every method whose primary table is ``entity_mentions`` or
``entity_relationships``, plus the entry-side lookups that join
those tables (``get_entities_for_entry``, ``mark_entry_extracted``).
Methods route through ``self._conn()`` (defined on the base store)
so each call gets the appropriate connection — thread-local on the
factory path, the shared connection on the legacy path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from journal.entitystore.protocol import _row_to_mention, _row_to_relationship

if TYPE_CHECKING:
    from journal.models import (
        DatedEntryExcerpt,
        Entity,
        EntityMention,
        EntityRelationship,
    )


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
        conn = self._conn()  # type: ignore[attr-defined]
        cursor = conn.execute(
            "INSERT INTO entity_mentions"
            " (entity_id, entry_id, quote, confidence,"
            "  extraction_run_id, match_source)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                entity_id, entry_id, quote, confidence,
                extraction_run_id, match_source,
            ),
        )
        conn.commit()
        mention_id = cursor.lastrowid
        assert mention_id is not None
        row = conn.execute(
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
        conn = self._conn()  # type: ignore[attr-defined]
        rows = conn.execute(sql, params).fetchall()
        return [_row_to_mention(r) for r in rows]

    def get_dated_entity_excerpts(
        self,
        entity_id: int,
        user_id: int,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[DatedEntryExcerpt]:
        """Return one row per entry that mentions ``entity_id``, in
        chronological order by ``entries.entry_date``, with each
        entry's verbatim quotes from ``entity_mentions.quote``
        aggregated into a list.

        Used by the storyline generation service: the curation panel
        iterates ``quotes`` for source-cited excerpts, and the
        narrative panel passes ``final_text`` to Opus via the
        Citations API as one custom-content document block per entry.

        ``user_id`` is required (storylines are user-scoped). Date
        bounds are inclusive ISO 8601 dates (e.g. ``"2026-02-12"``);
        either may be ``None`` for an open bound.
        """
        from journal.models import DatedEntryExcerpt  # local import to avoid cycle

        sql = (
            "SELECT e.id AS entry_id, e.entry_date,"
            "  COALESCE(NULLIF(e.final_text, ''), e.raw_text) AS body_text,"
            "  m.quote"
            " FROM entity_mentions m"
            " JOIN entries e ON e.id = m.entry_id"
            " WHERE m.entity_id = ? AND e.user_id = ?"
            # Quarantined entries (unconfirmed date) never reach the
            # storyline corpus (spec 2026-07-13).
            " AND e.date_confirmed = 1"
        )
        params: list[object] = [entity_id, user_id]
        if start_date is not None:
            sql += " AND e.entry_date >= ?"
            params.append(start_date)
        if end_date is not None:
            sql += " AND e.entry_date <= ?"
            params.append(end_date)
        sql += " ORDER BY e.entry_date ASC, e.id ASC, m.id ASC"

        conn = self._conn()  # type: ignore[attr-defined]
        rows = conn.execute(sql, params).fetchall()

        excerpts: list[DatedEntryExcerpt] = []
        current: DatedEntryExcerpt | None = None
        for row in rows:
            entry_id_val = int(row["entry_id"])
            if current is None or current.entry_id != entry_id_val:
                current = DatedEntryExcerpt(
                    entry_id=entry_id_val,
                    entry_date=row["entry_date"],
                    final_text=row["body_text"] or "",
                    quotes=[],
                )
                excerpts.append(current)
            quote = row["quote"] or ""
            if quote:
                current.quotes.append(quote)
        return excerpts

    def get_mentions_for_entry(self, entry_id: int) -> list[EntityMention]:
        conn = self._conn()  # type: ignore[attr-defined]
        rows = conn.execute(
            "SELECT * FROM entity_mentions WHERE entry_id = ? ORDER BY id",
            (entry_id,),
        ).fetchall()
        return [_row_to_mention(r) for r in rows]

    def delete_mentions_for_entry(self, entry_id: int) -> int:
        conn = self._conn()  # type: ignore[attr-defined]
        cursor = conn.execute(
            "DELETE FROM entity_mentions WHERE entry_id = ?", (entry_id,),
        )
        conn.commit()
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
        conn = self._conn()  # type: ignore[attr-defined]
        cursor = conn.execute(
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
        conn.commit()
        rel_id = cursor.lastrowid
        assert rel_id is not None
        row = conn.execute(
            "SELECT * FROM entity_relationships WHERE id = ?", (rel_id,),
        ).fetchone()
        return _row_to_relationship(row)

    def get_relationships_for_entity(
        self, entity_id: int, user_id: int | None = None,
    ) -> tuple[list[EntityRelationship], list[EntityRelationship]]:
        conn = self._conn()  # type: ignore[attr-defined]
        if user_id is not None:
            # Filter to relationships whose entry belongs to this user.
            outgoing_rows = conn.execute(
                "SELECT r.* FROM entity_relationships r"
                " JOIN entries e ON e.id = r.entry_id"
                " WHERE r.subject_entity_id = ? AND e.user_id = ?"
                " ORDER BY r.id",
                (entity_id, user_id),
            ).fetchall()
            incoming_rows = conn.execute(
                "SELECT r.* FROM entity_relationships r"
                " JOIN entries e ON e.id = r.entry_id"
                " WHERE r.object_entity_id = ? AND e.user_id = ?"
                " ORDER BY r.id",
                (entity_id, user_id),
            ).fetchall()
        else:
            outgoing_rows = conn.execute(
                "SELECT * FROM entity_relationships"
                " WHERE subject_entity_id = ? ORDER BY id",
                (entity_id,),
            ).fetchall()
            incoming_rows = conn.execute(
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
        conn = self._conn()  # type: ignore[attr-defined]
        rows = conn.execute(
            "SELECT * FROM entity_relationships WHERE entry_id = ?"
            " ORDER BY id",
            (entry_id,),
        ).fetchall()
        return [_row_to_relationship(r) for r in rows]

    def delete_relationships_for_entry(self, entry_id: int) -> int:
        conn = self._conn()  # type: ignore[attr-defined]
        cursor = conn.execute(
            "DELETE FROM entity_relationships WHERE entry_id = ?",
            (entry_id,),
        )
        conn.commit()
        return cursor.rowcount

    # ---- per-entry lookups & stale flag -------------------------------

    def get_entities_for_entry(self, entry_id: int) -> list[Entity]:
        conn = self._conn()  # type: ignore[attr-defined]
        rows = conn.execute(
            "SELECT DISTINCT e.* FROM entities e"
            " JOIN entity_mentions m ON m.entity_id = e.id"
            " WHERE m.entry_id = ?"
            " ORDER BY e.entity_type, e.canonical_name",
            (entry_id,),
        ).fetchall()
        return [self._hydrate(r) for r in rows]  # type: ignore[attr-defined]

    def mark_entry_extracted(self, entry_id: int) -> None:
        conn = self._conn()  # type: ignore[attr-defined]
        conn.execute(
            "UPDATE entries SET entity_extraction_stale = 0 WHERE id = ?",
            (entry_id,),
        )
        conn.commit()
