"""SQLite-backed repository for storylines + storyline panels.

Standalone repository (not part of ``SQLiteEntryRepository``) because
storylines are a fresh resource: they don't read or write to the
``entries``/``entity_mentions`` tables, only reference them by id.
Mirrors the pattern of :mod:`journal.db.jobs_repository`.

Schema lives in ``db/migrations/0027_storylines.sql`` and
``db/migrations/0028_storyline_entities.sql``. Multi-entity anchors
live in the ``storyline_entities`` join table; storylines have 1..N
anchors. Design notes in ``docs/storylines-plan.md``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date as _date
from datetime import timedelta as _timedelta
from typing import TYPE_CHECKING, Any

from journal.models import Storyline, StorylineChapter, StorylinePanel

if TYPE_CHECKING:
    import sqlite3

    from journal.db.factory import ConnectionFactory

log = logging.getLogger(__name__)


# Offset applied to existing seqs during an atomic rebuild so the final
# 1..N assignment can never collide with a still-present old seq. The
# rebuild only ever holds a handful of chapters, so 100000 is comfortably
# above any real seq.
_REBUILD_SEQ_OFFSET = 100_000


@dataclass
class ChapterSpec:
    """One desired chapter in an atomic :meth:`rebuild_chapters` call.

    Two flavors, discriminated by ``preserve_id``:

    * **preserve** — ``preserve_id`` is the id of an existing chapter row
      to keep untouched (row + its panels survive). Only its ``seq`` and
      ``state`` are re-derived during the rebuild. Used for
      ``boundary_locked`` anchor chapters.
    * **new** — ``preserve_id is None``; the spec carries the column
      values to INSERT a fresh chapter (``title``, ``start_date``,
      ``end_date``, ``title_locked``, ``boundary_locked``,
      ``narrative_word_count``). The caller writes panels afterward.

    ``state`` is the desired final state; the rebuilder enforces the
    single-open invariant by deferring the lone ``open`` assignment to
    the very last statement, so callers may pass ``state='open'`` on the
    final-in-time spec freely.
    """

    state: str = "closed"
    preserve_id: int | None = None
    title: str = ""
    start_date: str | None = None
    end_date: str | None = None
    title_locked: bool = False
    boundary_locked: bool = False
    narrative_word_count: int = 0


def _day_before(iso: str) -> str:
    """ISO day immediately before ``iso`` (inclusive-window math)."""
    return (_date.fromisoformat(iso) - _timedelta(days=1)).isoformat()


def _day_after(iso: str) -> str:
    """ISO day immediately after ``iso`` (inclusive-window math)."""
    return (_date.fromisoformat(iso) + _timedelta(days=1)).isoformat()


def _row_to_storyline(row: sqlite3.Row) -> Storyline:
    summary_raw = row["summary_embedding_json"]
    summary = json.loads(summary_raw) if summary_raw else None
    return Storyline(
        id=row["id"],
        user_id=row["user_id"],
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


def _row_to_chapter(row: sqlite3.Row) -> StorylineChapter:
    summary_raw = row["summary_embedding_json"]
    summary = json.loads(summary_raw) if summary_raw else None
    return StorylineChapter(
        id=row["id"],
        storyline_id=row["storyline_id"],
        seq=row["seq"],
        title=row["title"] or "",
        start_date=row["start_date"],
        end_date=row["end_date"],
        state=row["state"],
        last_generated_at=row["last_generated_at"],
        summary_embedding=[float(x) for x in summary] if summary else None,
        title_locked=bool(row["title_locked"]),
        boundary_locked=bool(row["boundary_locked"]),
        narrative_word_count=int(row["narrative_word_count"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_panel(row: sqlite3.Row) -> StorylinePanel:
    segments_raw = json.loads(row["segments_json"] or "[]")
    source_ids_raw = json.loads(row["source_entry_ids_json"] or "[]")
    return StorylinePanel(
        id=row["id"],
        chapter_id=row["chapter_id"],
        panel_kind=row["panel_kind"],
        segments=list(segments_raw),
        source_entry_ids=[int(x) for x in source_ids_raw],
        citation_count=int(row["citation_count"]),
        model_used=row["model_used"] or "",
        generated_at=row["generated_at"],
    )


class SQLiteStorylineRepository:
    """SQLite-backed CRUD for storylines, anchors, and panels.

    Constructed with a :class:`ConnectionFactory`; every method
    routes through ``self._factory.get()`` so each thread gets its
    own connection (per the SQLite threading invariant).
    """

    def __init__(self, factory: ConnectionFactory) -> None:
        self._factory = factory

    def _conn(self) -> sqlite3.Connection:
        return self._factory.get()

    def _shift_seqs(
        self,
        conn: sqlite3.Connection,
        storyline_id: int,
        from_seq: int,
        delta: int,
    ) -> None:
        """Shift seq by ``delta`` for chapters with seq >= ``from_seq``.

        Two-pass via a negative offset so we never collide with the
        UNIQUE(storyline_id, seq) index mid-update.

        For negative ``delta``, any row(s) whose seq would be overwritten
        must already be deleted before calling — callers delete or merge
        those rows first, then call this method to resequence the tail.
        """
        assert delta != 0
        conn.execute(
            "UPDATE storyline_chapters SET seq = -(seq + ?)"
            " WHERE storyline_id = ? AND seq >= ?",
            (delta, storyline_id, from_seq),
        )
        conn.execute(
            "UPDATE storyline_chapters SET seq = -seq"
            " WHERE storyline_id = ? AND seq < 0",
            (storyline_id,),
        )

    # ── storylines ──────────────────────────────────────────────

    def create_storyline(
        self,
        user_id: int,
        entity_ids: list[int],
        name: str,
        description: str = "",
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> Storyline:
        """Create a storyline anchored on one or more entities.

        ``entity_ids`` must be non-empty; the cap is enforced at the
        service layer (``MAX_ANCHORS``). Anchor rows are written in
        the same transaction as the parent ``storylines`` row.
        """
        if not entity_ids:
            raise ValueError("create_storyline requires at least one entity_id")
        unique_ids = sorted(set(entity_ids))

        conn = self._conn()
        try:
            cursor = conn.execute(
                "INSERT INTO storylines"
                " (user_id, name, description, start_date, end_date)"
                " VALUES (?, ?, ?, ?, ?)",
                (user_id, name.strip(), description, start_date, end_date),
            )
            storyline_id = cursor.lastrowid
            assert storyline_id is not None
            conn.executemany(
                "INSERT INTO storyline_entities (storyline_id, entity_id)"
                " VALUES (?, ?)",
                [(storyline_id, eid) for eid in unique_ids],
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        log.info(
            "Created storyline %d: %s (anchors=%s, user_id=%d)",
            storyline_id, name, unique_ids, user_id,
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

    def update_storyline_name(
        self, storyline_id: int, name: str, user_id: int,
    ) -> Storyline | None:
        """Rename a storyline.

        Returns the updated row, or ``None`` if no storyline with that
        id belongs to ``user_id``. The name is trimmed before storage,
        mirroring ``create_storyline``. Panels are untouched — a rename
        is metadata-only and never triggers a regeneration.
        """
        conn = self._conn()
        cursor = conn.execute(
            "UPDATE storylines"
            " SET name = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"
            " WHERE id = ? AND user_id = ?",
            (name.strip(), storyline_id, user_id),
        )
        conn.commit()
        if cursor.rowcount == 0:
            return None
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

    # ── anchors (storyline_entities) ────────────────────────────

    def list_anchors(self, storyline_id: int) -> list[int]:
        """Return the entity_ids anchored on this storyline, sorted ASC."""
        rows = self._conn().execute(
            "SELECT entity_id FROM storyline_entities"
            " WHERE storyline_id = ? ORDER BY entity_id ASC",
            (storyline_id,),
        ).fetchall()
        return [int(r["entity_id"]) for r in rows]

    def set_anchors(
        self, storyline_id: int, entity_ids: list[int],
    ) -> list[int]:
        """Replace the anchor set atomically. Returns the new anchor list."""
        if not entity_ids:
            raise ValueError("set_anchors requires at least one entity_id")
        unique_ids = sorted(set(entity_ids))
        conn = self._conn()
        try:
            conn.execute(
                "DELETE FROM storyline_entities WHERE storyline_id = ?",
                (storyline_id,),
            )
            conn.executemany(
                "INSERT INTO storyline_entities (storyline_id, entity_id)"
                " VALUES (?, ?)",
                [(storyline_id, eid) for eid in unique_ids],
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return unique_ids

    def add_anchor(self, storyline_id: int, entity_id: int) -> None:
        """Add a single anchor. Idempotent (no-op if pair already exists)."""
        conn = self._conn()
        conn.execute(
            "INSERT OR IGNORE INTO storyline_entities (storyline_id, entity_id)"
            " VALUES (?, ?)",
            (storyline_id, entity_id),
        )
        conn.commit()

    def remove_anchor(self, storyline_id: int, entity_id: int) -> bool:
        """Remove a single anchor. Returns True if a row was deleted."""
        conn = self._conn()
        cursor = conn.execute(
            "DELETE FROM storyline_entities"
            " WHERE storyline_id = ? AND entity_id = ?",
            (storyline_id, entity_id),
        )
        conn.commit()
        return cursor.rowcount > 0

    def find_by_anchor_set(
        self,
        user_id: int,
        entity_ids: list[int],
        name: str,
    ) -> Storyline | None:
        """Return the first storyline matching (user, name, exact anchor set).

        Used by ``POST /api/storylines`` to detect "already exists" and
        return 409 instead of creating a duplicate. Order of
        ``entity_ids`` does not matter (set comparison).
        """
        if not entity_ids:
            return None
        unique_ids = sorted(set(entity_ids))
        target_count = len(unique_ids)
        placeholders = ", ".join("?" for _ in unique_ids)
        # Match storylines that:
        #   - belong to this user with the given name,
        #   - have AT LEAST these entity_ids in storyline_entities,
        #   - have EXACTLY this many anchors total (no extras).
        sql = (
            "SELECT s.* FROM storylines s"
            " WHERE s.user_id = ? AND s.name = ?"
            "   AND (SELECT COUNT(*) FROM storyline_entities se"
            "        WHERE se.storyline_id = s.id) = ?"
            "   AND (SELECT COUNT(*) FROM storyline_entities se"
            f"        WHERE se.storyline_id = s.id AND se.entity_id IN ({placeholders})) = ?"
            " ORDER BY s.id LIMIT 1"
        )
        params: list[object] = [user_id, name.strip(), target_count]
        params.extend(unique_ids)
        params.append(target_count)
        row = self._conn().execute(sql, params).fetchone()
        return _row_to_storyline(row) if row else None

    def list_storylines_with_anchor(
        self,
        user_id: int,
        entity_id: int,
        status: str | None = None,
    ) -> list[Storyline]:
        """Return all storylines for this user that have the given entity
        as one of their anchors.

        Used by the extension classifier to enumerate candidate
        storylines when a new entry mentions an entity.
        """
        sql = (
            "SELECT s.* FROM storylines s"
            " JOIN storyline_entities se ON se.storyline_id = s.id"
            " WHERE s.user_id = ? AND se.entity_id = ?"
        )
        params: list[object] = [user_id, entity_id]
        if status is not None:
            sql += " AND s.status = ?"
            params.append(status)
        sql += " ORDER BY s.id ASC"
        rows = self._conn().execute(sql, params).fetchall()
        return [_row_to_storyline(r) for r in rows]

    # ── panels ──────────────────────────────────────────────────

    def upsert_panel(
        self,
        chapter_id: int,
        panel_kind: str,
        segments: list[dict[str, Any]],
        source_entry_ids: list[int],
        citation_count: int,
        model_used: str,
    ) -> StorylinePanel:
        conn = self._conn()
        conn.execute(
            "INSERT INTO storyline_panels"
            " (chapter_id, panel_kind, segments_json,"
            "  source_entry_ids_json, citation_count, model_used,"
            "  generated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))"
            " ON CONFLICT(chapter_id, panel_kind) DO UPDATE SET"
            "  segments_json = excluded.segments_json,"
            "  source_entry_ids_json = excluded.source_entry_ids_json,"
            "  citation_count = excluded.citation_count,"
            "  model_used = excluded.model_used,"
            "  generated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')",
            (
                chapter_id,
                panel_kind,
                json.dumps(segments),
                json.dumps(source_entry_ids),
                int(citation_count),
                model_used,
            ),
        )
        conn.commit()
        panel = self.get_panel(chapter_id, panel_kind)
        assert panel is not None
        return panel

    def get_panel(
        self, chapter_id: int, panel_kind: str,
    ) -> StorylinePanel | None:
        row = self._conn().execute(
            "SELECT * FROM storyline_panels"
            " WHERE chapter_id = ? AND panel_kind = ?",
            (chapter_id, panel_kind),
        ).fetchone()
        return _row_to_panel(row) if row else None

    def list_panels(self, chapter_id: int) -> list[StorylinePanel]:
        rows = self._conn().execute(
            "SELECT * FROM storyline_panels"
            " WHERE chapter_id = ? ORDER BY panel_kind",
            (chapter_id,),
        ).fetchall()
        return [_row_to_panel(r) for r in rows]

    # ── chapters ─────────────────────────────────────────────────

    def create_chapter(
        self,
        storyline_id: int,
        seq: int,
        title: str = "",
        start_date: str | None = None,
        end_date: str | None = None,
        state: str = "open",
    ) -> StorylineChapter:
        """Create one chapter of a storyline and return it populated."""
        conn = self._conn()
        cursor = conn.execute(
            "INSERT INTO storyline_chapters"
            " (storyline_id, seq, title, start_date, end_date, state)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (storyline_id, seq, title.strip(), start_date, end_date, state),
        )
        conn.commit()
        chapter_id = cursor.lastrowid
        assert chapter_id is not None
        ch = self.get_chapter(chapter_id)
        assert ch is not None
        return ch

    def get_chapter(self, chapter_id: int) -> StorylineChapter | None:
        row = self._conn().execute(
            "SELECT * FROM storyline_chapters WHERE id = ?", (chapter_id,),
        ).fetchone()
        return _row_to_chapter(row) if row else None

    def list_chapters(self, storyline_id: int) -> list[StorylineChapter]:
        rows = self._conn().execute(
            "SELECT * FROM storyline_chapters"
            " WHERE storyline_id = ? ORDER BY seq ASC",
            (storyline_id,),
        ).fetchall()
        return [_row_to_chapter(r) for r in rows]

    def get_open_chapter(self, storyline_id: int) -> StorylineChapter | None:
        """Return the storyline's single open chapter, or None."""
        row = self._conn().execute(
            "SELECT * FROM storyline_chapters"
            " WHERE storyline_id = ? AND state = 'open'"
            " ORDER BY seq DESC LIMIT 1",
            (storyline_id,),
        ).fetchone()
        return _row_to_chapter(row) if row else None

    def rename_chapter(
        self, chapter_id: int, title: str,
    ) -> StorylineChapter | None:
        """Rename a chapter; returns the updated row or None if absent."""
        conn = self._conn()
        cursor = conn.execute(
            "UPDATE storyline_chapters"
            " SET title = ?, title_locked = 1,"
            "     updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"
            " WHERE id = ?",
            (title.strip(), chapter_id),
        )
        conn.commit()
        if cursor.rowcount == 0:
            return None
        return self.get_chapter(chapter_id)

    def record_chapter_generation_complete(self, chapter_id: int) -> None:
        """Stamp ``last_generated_at`` after a chapter's panels are written."""
        conn = self._conn()
        conn.execute(
            "UPDATE storyline_chapters"
            " SET last_generated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),"
            "     updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"
            " WHERE id = ?",
            (chapter_id,),
        )
        conn.commit()

    def update_chapter_summary_embedding(
        self, chapter_id: int, embedding: list[float] | None,
    ) -> None:
        """Persist (or clear) a chapter's narrative summary embedding."""
        conn = self._conn()
        conn.execute(
            "UPDATE storyline_chapters"
            " SET summary_embedding_json = ?,"
            "     updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"
            " WHERE id = ?",
            (json.dumps(embedding) if embedding is not None else None, chapter_id),
        )
        conn.commit()

    def set_chapter_word_count(self, chapter_id: int, count: int) -> None:
        """Persist the cached narrative word count for a chapter."""
        conn = self._conn()
        conn.execute(
            "UPDATE storyline_chapters"
            " SET narrative_word_count = ?,"
            "     updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"
            " WHERE id = ?",
            (int(count), chapter_id),
        )
        conn.commit()

    def merge_chapters(self, chapter_ids: list[int]) -> StorylineChapter:
        """Merge a contiguous run of chapters into the lowest-seq one.

        The survivor is the lowest-seq row; its window becomes
        ``start = min(start)``, ``end = max(end)`` (NULL if any input was
        open); state is ``open`` if any input was open, else ``closed``.
        The survivor's title is kept. Non-survivor rows are deleted, then
        the tail (chapters after the run) is shifted DOWN by
        ``len(ids) - 1``.

        Raises ``ValueError`` for fewer than 2 ids, non-contiguous seqs,
        chapters belonging to different storylines, or missing chapters.
        """
        if len(chapter_ids) < 2:
            raise ValueError("merge requires at least two chapters")
        chapters = [self.get_chapter(cid) for cid in chapter_ids]
        if any(c is None for c in chapters):
            raise ValueError("one or more chapters not found")
        chapters = sorted(chapters, key=lambda c: c.seq)  # type: ignore[union-attr]
        sid = chapters[0].storyline_id
        if any(c.storyline_id != sid for c in chapters):
            raise ValueError("chapters belong to different storylines")
        seqs = [c.seq for c in chapters]
        if seqs != list(range(seqs[0], seqs[0] + len(seqs))):
            raise ValueError("chapters to merge must be adjacent (contiguous seq)")
        survivor = chapters[0]
        starts = [c.start_date for c in chapters if c.start_date is not None]
        is_open = any(c.state == "open" for c in chapters)
        new_start = min(starts) if starts else None
        ends = [c.end_date for c in chapters if c.end_date is not None]
        new_end = None if is_open else (max(ends) if ends else None)
        new_state = "open" if is_open else "closed"
        conn = self._conn()
        try:
            # Delete non-survivors first so the partial unique index
            # (at most one open chapter per storyline) is never violated
            # when we set the survivor's state to 'open'.
            for c in chapters[1:]:
                conn.execute("DELETE FROM storyline_chapters WHERE id = ?", (c.id,))
            conn.execute(
                "UPDATE storyline_chapters SET start_date = ?, end_date = ?, state = ?,"
                " updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id = ?",
                (new_start, new_end, new_state, survivor.id),
            )
            self._shift_seqs(conn, sid, chapters[-1].seq + 1, -(len(chapters) - 1))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        merged = self.get_chapter(survivor.id)
        assert merged is not None
        return merged

    def add_chapter(
        self,
        storyline_id: int,
        start_date: str,
        end_date: str | None = None,
    ) -> StorylineChapter:
        """Add a chapter: new-latest (end_date None) or ranged (end_date set).

        New-latest flavor:
            Close the current open chapter at _day_before(start_date) and
            append a new open chapter [start_date, NULL) at max(seq)+1.
            Requires start_date strictly after the open chapter's start_date
            (if an open chapter exists).

        Ranged flavor:
            Insert a closed chapter [start_date, end_date] into a free range.
            Rejects if the new chapter overlaps any existing chapter (open
            chapter's end treated as +infinity). seq is assigned by date order;
            later chapters shift up by 1.
        """
        conn = self._conn()
        existing = self.list_chapters(storyline_id)
        if end_date is None:
            open_ch = self.get_open_chapter(storyline_id)
            if (
                open_ch is not None
                and open_ch.start_date is not None
                and start_date <= open_ch.start_date
            ):
                raise ValueError(
                    "new chapter must start after the current open chapter"
                )
            new_seq = max((c.seq for c in existing), default=0) + 1
            try:
                if open_ch is not None:
                    conn.execute(
                        "UPDATE storyline_chapters"
                        " SET end_date = ?, state = 'closed',"
                        " updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')"
                        " WHERE id = ?",
                        (_day_before(start_date), open_ch.id),
                    )
                cursor = conn.execute(
                    "INSERT INTO storyline_chapters"
                    " (storyline_id, seq, title, start_date, end_date, state,"
                    "  boundary_locked)"
                    " VALUES (?, ?, '', ?, NULL, 'open', 1)",
                    (storyline_id, new_seq, start_date),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            ch = self.get_chapter(cursor.lastrowid)
            assert ch is not None
            return ch
        # Ranged flavor
        if end_date < start_date:
            raise ValueError("end_date must be on or after start_date")
        for c in existing:
            c_end = c.end_date if c.end_date is not None else "9999-12-31"
            c_start = c.start_date if c.start_date is not None else "0000-01-01"
            if start_date <= c_end and end_date >= c_start:
                raise ValueError("new chapter overlaps an existing chapter")
        # NULL start == open-start (−∞): such a chapter is never "later" than
        # the new range, matching the overlap check's −∞ treatment above.
        later = [c for c in existing if (c.start_date or "0000-01-01") > end_date]
        insert_seq = min((c.seq for c in later), default=len(existing) + 1)
        try:
            self._shift_seqs(conn, storyline_id, insert_seq, 1)
            cursor = conn.execute(
                "INSERT INTO storyline_chapters"
                " (storyline_id, seq, title, start_date, end_date, state,"
                "  boundary_locked)"
                " VALUES (?, ?, '', ?, ?, 'closed', 1)",
                (storyline_id, insert_seq, start_date, end_date),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        ch = self.get_chapter(cursor.lastrowid)
        assert ch is not None
        return ch

    def _assert_no_overlap(
        self, storyline_id: int, conn: sqlite3.Connection,
    ) -> None:
        """Raise ValueError if any two chapters' windows overlap."""
        rows = conn.execute(
            "SELECT start_date, end_date FROM storyline_chapters"
            " WHERE storyline_id = ? ORDER BY seq ASC",
            (storyline_id,),
        ).fetchall()
        prev_end: str | None = None
        for r in rows:
            start = r["start_date"] or "0000-01-01"
            if prev_end is not None and start <= prev_end:
                raise ValueError("chapter windows overlap")
            prev_end = r["end_date"] or "9999-12-31"

    def update_chapter_window(
        self,
        chapter_id: int,
        start_date: str | None,
        end_date: str | None,
        allow_gap: bool = False,
    ) -> list[StorylineChapter]:
        """Move a chapter's boundaries, rippling neighbors by default.

        By default, the shared edge of the touching neighbor ripples to
        stay contiguous: changing a chapter's ``start`` sets the previous
        chapter's ``end`` to ``_day_before(new_start)``; changing a
        chapter's ``end`` sets the next chapter's ``start`` to
        ``_day_after(new_end)``.  With ``allow_gap=True`` neighbors are
        left alone (a gap is allowed).

        Raises ``ValueError`` if:
        - the chapter is not found,
        - ``end_date`` is set for an open chapter,
        - ``end_date < start_date``, or
        - any two chapters overlap after applying the change.

        Returns the list of chapters that changed (the edited one plus any
        rippled neighbor).
        """
        target = self.get_chapter(chapter_id)
        if target is None:
            raise ValueError(f"Chapter {chapter_id} not found")
        if target.state == "open" and end_date is not None:
            raise ValueError("the open chapter's end cannot be set")
        eff_start = start_date if start_date is not None else target.start_date
        eff_end = end_date if end_date is not None else target.end_date
        if eff_start is not None and eff_end is not None and eff_end < eff_start:
            raise ValueError("end_date must be on or after start_date")
        chapters = self.list_chapters(target.storyline_id)
        idx = next(i for i, c in enumerate(chapters) if c.id == chapter_id)
        changed_ids: set[int] = {chapter_id}
        conn = self._conn()
        try:
            conn.execute(
                "UPDATE storyline_chapters SET start_date = ?, end_date = ?,"
                " boundary_locked = 1,"
                " updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id = ?",
                (start_date, end_date, chapter_id),
            )
            if not allow_gap:
                if start_date is not None and idx > 0:
                    prev = chapters[idx - 1]
                    conn.execute(
                        "UPDATE storyline_chapters SET end_date = ?,"
                        " updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id = ?",
                        (_day_before(start_date), prev.id),
                    )
                    changed_ids.add(prev.id)
                if end_date is not None and idx < len(chapters) - 1:
                    nxt = chapters[idx + 1]
                    conn.execute(
                        "UPDATE storyline_chapters SET start_date = ?,"
                        " updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id = ?",
                        (_day_after(end_date), nxt.id),
                    )
                    changed_ids.add(nxt.id)
            self._assert_no_overlap(target.storyline_id, conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return [c for c in self.list_chapters(target.storyline_id) if c.id in changed_ids]

    def delete_chapter(
        self, chapter_id: int, allow_gap: bool = False,
    ) -> list[int]:
        """Delete a chapter; absorb its range into a neighbor by default.

        Raises ``ValueError`` if the chapter is not found or is the storyline's
        only chapter.

        Default (``allow_gap=False``):
        - If there is a previous neighbor, it absorbs the deleted chapter's
          window.  If the deleted chapter was ``open``, the previous neighbor is
          promoted to ``open`` (``end_date → NULL``, ``state → 'open'``);
          otherwise the previous neighbor's ``end_date`` extends to the deleted
          chapter's ``end_date``.
        - If there is NO previous neighbor (deleting the first chapter), the
          next neighbor's ``start_date`` extends back to the deleted chapter's
          ``start_date``.
        - Returns the list of chapter ids whose windows changed.

        With ``allow_gap=True`` no neighbor is modified; returns ``[]``.

        Ordering invariant: the target row is DELETED first, then the neighbor
        UPDATE runs (so promoting a neighbor to ``open`` never collides with the
        still-present open row), then ``_shift_seqs`` resequences the tail
        (negative delta is safe because the target is already gone).
        """
        target = self.get_chapter(chapter_id)
        if target is None:
            raise ValueError(f"Chapter {chapter_id} not found")
        chapters = self.list_chapters(target.storyline_id)
        if len(chapters) == 1:
            raise ValueError("cannot delete a storyline's only chapter")
        idx = next(i for i, c in enumerate(chapters) if c.id == chapter_id)
        affected: list[int] = []
        conn = self._conn()
        try:
            conn.execute(
                "DELETE FROM storyline_chapters WHERE id = ?", (chapter_id,),
            )
            if not allow_gap:
                if idx > 0:
                    prev = chapters[idx - 1]
                    if target.state == "open":
                        conn.execute(
                            "UPDATE storyline_chapters"
                            " SET end_date = NULL, state = 'open',"
                            " updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')"
                            " WHERE id = ?",
                            (prev.id,),
                        )
                    else:
                        conn.execute(
                            "UPDATE storyline_chapters"
                            " SET end_date = ?,"
                            " updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')"
                            " WHERE id = ?",
                            (target.end_date, prev.id),
                        )
                    affected.append(prev.id)
                else:
                    nxt = chapters[idx + 1]
                    conn.execute(
                        "UPDATE storyline_chapters"
                        " SET start_date = ?,"
                        " updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')"
                        " WHERE id = ?",
                        (target.start_date, nxt.id),
                    )
                    affected.append(nxt.id)
            self._shift_seqs(conn, target.storyline_id, target.seq + 1, -1)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return affected

    def split_chapter(
        self, chapter_id: int, date: str,
    ) -> tuple[StorylineChapter, StorylineChapter]:
        """Split a chapter at ``date`` into a left + right pair.

        Left keeps the existing row (same seq, same start) with
        ``end_date = _day_before(date)``; right is a new row at ``seq+1``
        with ``start_date = date`` and the original ``end_date``.

        If the source was ``open``, left becomes ``closed`` and right
        stays ``open``; otherwise both are ``closed``.

        ``date`` must satisfy ``start_date < date`` and, when the source
        has an end, ``date <= end_date``.
        """
        conn = self._conn()
        src = self.get_chapter(chapter_id)
        if src is None:
            raise ValueError(f"Chapter {chapter_id} not found")
        if src.start_date is not None and date <= src.start_date:
            raise ValueError("split date must be after the chapter start")
        if src.end_date is not None and date > src.end_date:
            raise ValueError("split date must be on or before the chapter end")
        right_state = "open" if src.state == "open" else "closed"
        try:
            self._shift_seqs(conn, src.storyline_id, src.seq + 1, 1)
            conn.execute(
                "UPDATE storyline_chapters SET end_date = ?, state = 'closed',"
                " boundary_locked = 1,"
                " updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id = ?",
                (_day_before(date), chapter_id),
            )
            cursor = conn.execute(
                "INSERT INTO storyline_chapters"
                " (storyline_id, seq, title, start_date, end_date, state,"
                "  boundary_locked)"
                " VALUES (?, ?, ?, ?, ?, ?, 1)",
                (src.storyline_id, src.seq + 1, "", date, src.end_date, right_state),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        left = self.get_chapter(chapter_id)
        right = self.get_chapter(cursor.lastrowid)
        assert left is not None and right is not None
        return left, right

    def rebuild_chapters(
        self, storyline_id: int, specs: list[ChapterSpec],
    ) -> list[StorylineChapter]:
        """Atomically replace a storyline's chapter set with ``specs``.

        ``specs`` is the complete, date-ordered desired chapter list. Each
        spec is either a *preserve* (an existing chapter id whose row +
        panels survive) or a *new* (a fresh INSERT). Final ``seq`` values
        are assigned 1..N in list order, so the caller must pass specs in
        the order they should tile the timeline.

        Atomicity + invariant handling — the whole rebuild runs in ONE
        transaction and is ordered so SQLite's per-statement unique-index
        checks are NEVER transiently violated:

        1. **Force every existing row of this storyline to ``state =
           'closed'``.** Zero open chapters is allowed by the partial
           unique index ``idx_storyline_chapters_one_open``; this clears
           the way to reshuffle without two rows momentarily both 'open'.
        2. **Offset every existing seq by ``_REBUILD_SEQ_OFFSET``.** Moves
           all current rows out of the 1..N target range so neither the
           preserved-row UPDATEs nor the new-row INSERTs collide with a
           stale ``UNIQUE(storyline_id, seq)`` value.
        3. **DELETE the existing rows that are NOT preserved.** Their
           panels cascade away (they are regenerated by the caller).
        4. **Place each spec at its final seq** (1..N): preserved rows via
           UPDATE (seq + window/title metadata left as-is except seq), new
           rows via INSERT — all still ``state = 'closed'``.
        5. **Promote exactly the final-in-order chapter to ``open``** as
           the LAST statement, iff that spec's desired state is 'open'.
           Doing this last guarantees the single-open index sees zero→one
           open, never two.

        Returns the resulting chapters in seq order so the service can
        write panels for the new ones.
        """
        preserve_ids = {s.preserve_id for s in specs if s.preserve_id is not None}
        existing = self.list_chapters(storyline_id)
        existing_ids = {c.id for c in existing}
        for pid in preserve_ids:
            if pid not in existing_ids:
                raise ValueError(
                    f"rebuild_chapters: preserve id {pid} not in storyline "
                    f"{storyline_id}"
                )
        # Defence-in-depth: never commit an inverted window. A spec with
        # end_date < start_date would corrupt the chapter set silently
        # (the index/seq invariants would still hold). Reject it loudly so
        # a window-derivation bug surfaces as a failed job, not bad data.
        for i, s in enumerate(specs):
            if (
                s.start_date is not None
                and s.end_date is not None
                and s.end_date < s.start_date
            ):
                raise ValueError(
                    f"rebuild_chapters: spec {i} has end_date "
                    f"{s.end_date!r} < start_date {s.start_date!r}"
                )

        conn = self._conn()
        try:
            # 1. Zero out open state (zero-open is allowed by the index).
            conn.execute(
                "UPDATE storyline_chapters SET state = 'closed'"
                " WHERE storyline_id = ?",
                (storyline_id,),
            )
            # 2. Offset existing seqs out of the target range.
            conn.execute(
                "UPDATE storyline_chapters SET seq = seq + ?"
                " WHERE storyline_id = ?",
                (_REBUILD_SEQ_OFFSET, storyline_id),
            )
            # 3. Delete the rows we are not preserving.
            for c in existing:
                if c.id not in preserve_ids:
                    conn.execute(
                        "DELETE FROM storyline_chapters WHERE id = ?", (c.id,),
                    )
            # 4. Place every spec at its final seq, all closed for now.
            placed_ids: list[int] = []
            for final_seq, spec in enumerate(specs, start=1):
                if spec.preserve_id is not None:
                    conn.execute(
                        "UPDATE storyline_chapters SET seq = ?, state = 'closed',"
                        " updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')"
                        " WHERE id = ?",
                        (final_seq, spec.preserve_id),
                    )
                    placed_ids.append(spec.preserve_id)
                else:
                    cursor = conn.execute(
                        "INSERT INTO storyline_chapters"
                        " (storyline_id, seq, title, start_date, end_date,"
                        "  state, title_locked, boundary_locked,"
                        "  narrative_word_count)"
                        " VALUES (?, ?, ?, ?, ?, 'closed', ?, ?, ?)",
                        (
                            storyline_id, final_seq, spec.title.strip(),
                            spec.start_date, spec.end_date,
                            1 if spec.title_locked else 0,
                            1 if spec.boundary_locked else 0,
                            int(spec.narrative_word_count),
                        ),
                    )
                    new_id = cursor.lastrowid
                    assert new_id is not None
                    placed_ids.append(new_id)
            # 5. Promote exactly the final chapter to open, last of all.
            if specs and specs[-1].state == "open":
                conn.execute(
                    "UPDATE storyline_chapters SET state = 'open',"
                    " updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')"
                    " WHERE id = ?",
                    (placed_ids[-1],),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return self.list_chapters(storyline_id)
