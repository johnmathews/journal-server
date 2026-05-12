"""SQLite-backed repository for storylines + storyline panels.

Standalone repository (not part of ``SQLiteEntryRepository``) because
storylines are a fresh resource: they don't read or write to the
``entries``/``entity_mentions`` tables, only reference them by id.
Mirrors the pattern of :mod:`journal.db.jobs_repository`.

Schema lives in ``db/migrations/0027_storylines.sql``. Design notes
in ``docs/storylines-plan.md``.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from journal.models import Storyline, StorylinePanel

if TYPE_CHECKING:
    import sqlite3

    from journal.db.factory import ConnectionFactory

log = logging.getLogger(__name__)


def _row_to_storyline(row: sqlite3.Row) -> Storyline:
    summary_raw = row["summary_embedding_json"]
    summary = json.loads(summary_raw) if summary_raw else None
    return Storyline(
        id=row["id"],
        user_id=row["user_id"],
        entity_id=row["entity_id"],
        name=row["name"],
        description=row["description"] or "",
        start_date=row["start_date"],
        end_date=row["end_date"],
        status=row["status"],
        last_generated_at=row["last_generated_at"],
        last_extension_check_at=row["last_extension_check_at"],
        summary_embedding=[float(x) for x in summary] if summary else None,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_panel(row: sqlite3.Row) -> StorylinePanel:
    segments_raw = json.loads(row["segments_json"] or "[]")
    source_ids_raw = json.loads(row["source_entry_ids_json"] or "[]")
    return StorylinePanel(
        id=row["id"],
        storyline_id=row["storyline_id"],
        panel_kind=row["panel_kind"],
        segments=list(segments_raw),
        source_entry_ids=[int(x) for x in source_ids_raw],
        citation_count=int(row["citation_count"]),
        model_used=row["model_used"] or "",
        generated_at=row["generated_at"],
    )


class SQLiteStorylineRepository:
    """SQLite-backed CRUD for storylines and their panels.

    Constructed with a :class:`ConnectionFactory`; every method
    routes through ``self._factory.get()`` so each thread gets its
    own connection (per the SQLite threading invariant).
    """

    def __init__(self, factory: ConnectionFactory) -> None:
        self._factory = factory

    def _conn(self) -> sqlite3.Connection:
        return self._factory.get()

    # ── storylines ──────────────────────────────────────────────

    def create_storyline(
        self,
        user_id: int,
        entity_id: int,
        name: str,
        description: str = "",
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> Storyline:
        conn = self._conn()
        cursor = conn.execute(
            "INSERT INTO storylines"
            " (user_id, entity_id, name, description, start_date, end_date)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, entity_id, name.strip(), description, start_date, end_date),
        )
        conn.commit()
        storyline_id = cursor.lastrowid
        assert storyline_id is not None
        log.info(
            "Created storyline %d: %s (entity_id=%d, user_id=%d)",
            storyline_id, name, entity_id, user_id,
        )
        storyline = self.get_storyline(storyline_id, user_id=user_id)
        assert storyline is not None
        return storyline

    def get_storyline(
        self, storyline_id: int, user_id: int | None = None,
    ) -> Storyline | None:
        sql = "SELECT * FROM storylines WHERE id = ?"
        params: list[object] = [storyline_id]
        if user_id is not None:
            sql += " AND user_id = ?"
            params.append(user_id)
        row = self._conn().execute(sql, params).fetchone()
        return _row_to_storyline(row) if row else None

    def find_by_entity(
        self,
        user_id: int,
        entity_id: int,
        name: str | None = None,
    ) -> Storyline | None:
        """Return the first storyline matching (user, entity[, name]).

        Used by `POST /api/storylines` to detect "already exists" and
        return 409 instead of letting the UNIQUE constraint trip.
        """
        sql = "SELECT * FROM storylines WHERE user_id = ? AND entity_id = ?"
        params: list[object] = [user_id, entity_id]
        if name is not None:
            sql += " AND name = ?"
            params.append(name.strip())
        sql += " ORDER BY id LIMIT 1"
        row = self._conn().execute(sql, params).fetchone()
        return _row_to_storyline(row) if row else None

    def list_storylines(
        self,
        user_id: int,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Storyline]:
        sql = "SELECT * FROM storylines WHERE user_id = ?"
        params: list[object] = [user_id]
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = self._conn().execute(sql, params).fetchall()
        return [_row_to_storyline(r) for r in rows]

    def count_storylines(
        self, user_id: int, status: str | None = None,
    ) -> int:
        sql = "SELECT COUNT(*) AS cnt FROM storylines WHERE user_id = ?"
        params: list[object] = [user_id]
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        row = self._conn().execute(sql, params).fetchone()
        return int(row["cnt"])

    def update_storyline_status(
        self, storyline_id: int, status: str, user_id: int,
    ) -> Storyline | None:
        conn = self._conn()
        conn.execute(
            "UPDATE storylines"
            " SET status = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"
            " WHERE id = ? AND user_id = ?",
            (status, storyline_id, user_id),
        )
        conn.commit()
        return self.get_storyline(storyline_id, user_id=user_id)

    def delete_storyline(self, storyline_id: int, user_id: int) -> bool:
        conn = self._conn()
        cursor = conn.execute(
            "DELETE FROM storylines WHERE id = ? AND user_id = ?",
            (storyline_id, user_id),
        )
        conn.commit()
        return cursor.rowcount > 0

    def record_generation_complete(self, storyline_id: int) -> None:
        conn = self._conn()
        conn.execute(
            "UPDATE storylines"
            " SET last_generated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),"
            "     updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"
            " WHERE id = ?",
            (storyline_id,),
        )
        conn.commit()

    def record_extension_check(self, storyline_id: int) -> None:
        conn = self._conn()
        conn.execute(
            "UPDATE storylines"
            " SET last_extension_check_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"
            " WHERE id = ?",
            (storyline_id,),
        )
        conn.commit()

    def update_summary_embedding(
        self, storyline_id: int, embedding: list[float] | None,
    ) -> None:
        conn = self._conn()
        conn.execute(
            "UPDATE storylines"
            " SET summary_embedding_json = ?,"
            "     updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"
            " WHERE id = ?",
            (json.dumps(embedding) if embedding is not None else None, storyline_id),
        )
        conn.commit()

    # ── panels ──────────────────────────────────────────────────

    def upsert_panel(
        self,
        storyline_id: int,
        panel_kind: str,
        segments: list[dict[str, Any]],
        source_entry_ids: list[int],
        citation_count: int,
        model_used: str,
    ) -> StorylinePanel:
        conn = self._conn()
        conn.execute(
            "INSERT INTO storyline_panels"
            " (storyline_id, panel_kind, segments_json,"
            "  source_entry_ids_json, citation_count, model_used,"
            "  generated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))"
            " ON CONFLICT(storyline_id, panel_kind) DO UPDATE SET"
            "  segments_json = excluded.segments_json,"
            "  source_entry_ids_json = excluded.source_entry_ids_json,"
            "  citation_count = excluded.citation_count,"
            "  model_used = excluded.model_used,"
            "  generated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')",
            (
                storyline_id,
                panel_kind,
                json.dumps(segments),
                json.dumps(source_entry_ids),
                int(citation_count),
                model_used,
            ),
        )
        conn.commit()
        panel = self.get_panel(storyline_id, panel_kind)
        assert panel is not None
        return panel

    def get_panel(
        self, storyline_id: int, panel_kind: str,
    ) -> StorylinePanel | None:
        row = self._conn().execute(
            "SELECT * FROM storyline_panels"
            " WHERE storyline_id = ? AND panel_kind = ?",
            (storyline_id, panel_kind),
        ).fetchone()
        return _row_to_panel(row) if row else None

    def list_panels(self, storyline_id: int) -> list[StorylinePanel]:
        rows = self._conn().execute(
            "SELECT * FROM storyline_panels"
            " WHERE storyline_id = ? ORDER BY panel_kind",
            (storyline_id,),
        ).fetchall()
        return [_row_to_panel(r) for r in rows]
