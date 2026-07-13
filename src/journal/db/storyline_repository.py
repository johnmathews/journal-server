"""SQLite-backed repository for storylines + draft/published chapters.

Standalone repository (not part of ``SQLiteEntryRepository``) because
storylines are a fresh resource: they don't read or write to the
``entries``/``entity_mentions`` tables, only reference them by id.
Mirrors the pattern of :mod:`journal.db.jobs_repository`.

Schema lives in ``db/migrations/0027_storylines.sql``,
``db/migrations/0028_storyline_entities.sql``, and
``db/migrations/0036_storylines_draft_published.sql``. Multi-entity
anchors live in the ``storyline_entities`` join table; storylines have
1..N anchors.

Each storyline has exactly one chapter in state ``'draft'`` at a time
(enforced by a partial unique index) plus zero or more ``'published'``
chapters. Entry membership lives in ``storyline_chapter_entries``
rather than on the chapter row, so ``list_chapters``/``get_chapter``
derive ``entry_count``/``first_entry_date``/``last_entry_date`` via a
join rather than storing them. Design notes in
``docs/superpowers/specs/2026-07-12-storylines-redesign-design.md``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from journal.models import DatedEntryExcerpt, Storyline, StorylineChapter

if TYPE_CHECKING:
    import sqlite3

    from journal.db.factory import ConnectionFactory

log = logging.getLogger(__name__)


@dataclass
class BootstrapChapterSpec:
    """One pre-narrated chapter for :meth:`replace_all_chapters` (bootstrap).

    Used by the one-time bootstrap sweep that seeds an existing
    storyline's chapters from scratch. ``entry_ids`` is the chapter's
    entry membership; ``mark_read`` lets the sweep mark pre-existing
    published content as already-read so it doesn't manufacture a wall
    of unread badges. Exactly the last spec in a ``replace_all_chapters``
    call may have ``state == 'draft'`` (a storyline always ends with a
    single trailing draft).
    """

    title: str
    state: str  # 'draft' | 'published'
    segments: list[dict[str, Any]]
    source_entry_ids: list[int]
    citation_count: int
    model_used: str
    entry_ids: list[int]
    mark_read: bool = False


def _row_to_storyline(row: sqlite3.Row) -> Storyline:
    return Storyline(
        id=row["id"],
        user_id=row["user_id"],
        name=row["name"],
        description=row["description"] or "",
        status=row["status"],
        last_extension_check_at=row["last_extension_check_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


_CHAPTER_SELECT = (
    "SELECT c.*, COUNT(ce.entry_id) AS entry_count,"
    "       MIN(e.entry_date) AS first_entry_date,"
    "       MAX(e.entry_date) AS last_entry_date"
    " FROM storyline_chapters c"
    " LEFT JOIN storyline_chapter_entries ce ON ce.chapter_id = c.id"
    " LEFT JOIN entries e ON e.id = ce.entry_id"
)


def _row_to_chapter(row: sqlite3.Row) -> StorylineChapter:
    embedding_raw = row["draft_embedding_json"]
    embedding = json.loads(embedding_raw) if embedding_raw else None
    return StorylineChapter(
        id=row["id"],
        storyline_id=row["storyline_id"],
        seq=row["seq"],
        title=row["title"] or "",
        state=row["state"],
        segments=list(json.loads(row["segments_json"] or "[]")),
        source_entry_ids=[
            int(x) for x in json.loads(row["source_entry_ids_json"] or "[]")
        ],
        citation_count=int(row["citation_count"]),
        model_used=row["model_used"] or "",
        generated_at=row["generated_at"],
        published_at=row["published_at"],
        read_at=row["read_at"],
        addenda=list(json.loads(row["addenda_json"] or "[]")),
        draft_embedding=[float(x) for x in embedding] if embedding else None,
        entry_count=int(row["entry_count"]),
        first_entry_date=row["first_entry_date"],
        last_entry_date=row["last_entry_date"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class SQLiteStorylineRepository:
    """SQLite-backed CRUD for storylines, anchors, and draft/published chapters.

    Constructed with a :class:`ConnectionFactory`; every method
    routes through ``self._factory.get()`` so each thread gets its
    own connection (per the SQLite threading invariant).
    """

    def __init__(self, factory: ConnectionFactory) -> None:
        self._factory = factory

    def _conn(self) -> sqlite3.Connection:
        return self._factory.get()

    def _now(self, conn: sqlite3.Connection) -> str:
        row = conn.execute(
            "SELECT strftime('%Y-%m-%dT%H:%M:%SZ', 'now') AS ts",
        ).fetchone()
        return str(row["ts"])

    def _require_state(self, chapter_id: int, expected: str) -> StorylineChapter:
        ch = self.get_chapter(chapter_id)
        if ch is None:
            raise ValueError(f"Chapter {chapter_id} not found")
        if ch.state != expected:
            raise ValueError(
                f"Chapter {chapter_id} is {ch.state}; operation requires {expected}"
            )
        return ch

    # ── storylines ──────────────────────────────────────────────

    def create_storyline(
        self,
        user_id: int,
        entity_ids: list[int],
        name: str,
        description: str = "",
    ) -> Storyline:
        """Create a storyline anchored on one or more entities.

        ``entity_ids`` must be non-empty; the cap is enforced at the
        service layer (``MAX_ANCHORS``). Anchor rows and the seq-1
        draft chapter are written in the same transaction as the
        parent ``storylines`` row.
        """
        if not entity_ids:
            raise ValueError("create_storyline requires at least one entity_id")
        unique_ids = sorted(set(entity_ids))

        conn = self._conn()
        try:
            cursor = conn.execute(
                "INSERT INTO storylines (user_id, name, description)"
                " VALUES (?, ?, ?)",
                (user_id, name.strip(), description),
            )
            storyline_id = cursor.lastrowid
            assert storyline_id is not None
            conn.executemany(
                "INSERT INTO storyline_entities (storyline_id, entity_id)"
                " VALUES (?, ?)",
                [(storyline_id, eid) for eid in unique_ids],
            )
            conn.execute(
                "INSERT INTO storyline_chapters (storyline_id, seq, state)"
                " VALUES (?, 1, 'draft')",
                (storyline_id,),
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
        mirroring ``create_storyline``. Chapters are untouched — a
        rename is metadata-only and never triggers a regeneration.
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

    def record_extension_check(self, storyline_id: int) -> None:
        conn = self._conn()
        conn.execute(
            "UPDATE storylines"
            " SET last_extension_check_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"
            " WHERE id = ?",
            (storyline_id,),
        )
        conn.commit()

    def unread_counts(self, user_id: int) -> dict[int, int]:
        """Return ``{storyline_id: unread_published_chapter_count}``.

        Only storylines with at least one unread published chapter
        appear in the result — callers should default to 0 for
        storylines not present.
        """
        rows = self._conn().execute(
            "SELECT c.storyline_id, COUNT(*) AS cnt"
            " FROM storyline_chapters c"
            " JOIN storylines s ON s.id = c.storyline_id"
            " WHERE s.user_id = ? AND c.state = 'published' AND c.read_at IS NULL"
            " GROUP BY c.storyline_id",
            (user_id,),
        ).fetchall()
        return {int(r["storyline_id"]): int(r["cnt"]) for r in rows}

    def chapter_counts(self, user_id: int) -> dict[int, int]:
        """Chapter count per storyline for this user (one query, no JSON).

        Returns ``{storyline_id: total_chapter_count}`` for all storylines
        belonging to ``user_id``. Counts both draft and published chapters.
        Storylines with no chapters (shouldn't exist, but graceful default) are
        omitted — callers should default to 0 for missing storylines.
        """
        rows = self._conn().execute(
            "SELECT c.storyline_id, COUNT(*) AS cnt"
            " FROM storyline_chapters c"
            " JOIN storylines s ON s.id = c.storyline_id"
            " WHERE s.user_id = ?"
            " GROUP BY c.storyline_id",
            (user_id,),
        ).fetchall()
        return {int(r["storyline_id"]): int(r["cnt"]) for r in rows}

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

    # ── chapters ─────────────────────────────────────────────────

    def list_chapters(self, storyline_id: int) -> list[StorylineChapter]:
        rows = self._conn().execute(
            _CHAPTER_SELECT
            + " WHERE c.storyline_id = ? GROUP BY c.id ORDER BY c.seq ASC",
            (storyline_id,),
        ).fetchall()
        return [_row_to_chapter(r) for r in rows]

    def get_chapter(self, chapter_id: int) -> StorylineChapter | None:
        row = self._conn().execute(
            _CHAPTER_SELECT + " WHERE c.id = ? GROUP BY c.id",
            (chapter_id,),
        ).fetchone()
        return _row_to_chapter(row) if row else None

    def get_draft(self, storyline_id: int) -> StorylineChapter | None:
        """Return the storyline's single draft chapter, or None."""
        row = self._conn().execute(
            _CHAPTER_SELECT
            + " WHERE c.storyline_id = ? AND c.state = 'draft' GROUP BY c.id",
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
            " SET title = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"
            " WHERE id = ?",
            (title.strip(), chapter_id),
        )
        conn.commit()
        if cursor.rowcount == 0:
            return None
        return self.get_chapter(chapter_id)

    def set_read(self, chapter_id: int, read: bool) -> StorylineChapter | None:
        """Mark a published chapter read/unread. Raises if not published."""
        self._require_state(chapter_id, "published")
        conn = self._conn()
        conn.execute(
            "UPDATE storyline_chapters"
            " SET read_at = CASE WHEN ? THEN"
            "     strftime('%Y-%m-%dT%H:%M:%SZ', 'now') ELSE NULL END,"
            "     updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"
            " WHERE id = ?",
            (1 if read else 0, chapter_id),
        )
        conn.commit()
        return self.get_chapter(chapter_id)

    # ── membership ───────────────────────────────────────────────

    def assigned_entry_ids(self, storyline_id: int) -> set[int]:
        """Return every entry_id assigned to any chapter of this storyline."""
        rows = self._conn().execute(
            "SELECT ce.entry_id FROM storyline_chapter_entries ce"
            " JOIN storyline_chapters c ON c.id = ce.chapter_id"
            " WHERE c.storyline_id = ?",
            (storyline_id,),
        ).fetchall()
        return {int(r["entry_id"]) for r in rows}

    def find_storyline_ids_for_entry(self, entry_id: int) -> list[int]:
        """Distinct storylines whose chapters (draft or published) contain
        the entry — the reverse lookup used by date-edit propagation
        (spec 2026-07-13). Served by idx_storyline_chapter_entries_entry.
        """
        rows = self._conn().execute(
            "SELECT DISTINCT c.storyline_id"
            " FROM storyline_chapter_entries ce"
            " JOIN storyline_chapters c ON c.id = ce.chapter_id"
            " WHERE ce.entry_id = ?"
            " ORDER BY c.storyline_id ASC",
            (entry_id,),
        ).fetchall()
        return [int(r["storyline_id"]) for r in rows]

    def chapter_entry_ids(self, chapter_id: int) -> list[int]:
        rows = self._conn().execute(
            "SELECT ce.entry_id FROM storyline_chapter_entries ce"
            " JOIN entries e ON e.id = ce.entry_id"
            " WHERE ce.chapter_id = ?"
            " ORDER BY e.entry_date ASC, ce.entry_id ASC",
            (chapter_id,),
        ).fetchall()
        return [int(r["entry_id"]) for r in rows]

    def _reject_cross_chapter_membership(
        self,
        storyline_id: int,
        entry_ids: list[int],
        *,
        exclude_chapter_id: int | None = None,
    ) -> None:
        """Raise ``ValueError`` if any of ``entry_ids`` is already a member
        of a DIFFERENT chapter of ``storyline_id`` (spec §1: an entry
        belongs to exactly one chapter of a storyline at a time).

        ``exclude_chapter_id`` lets a caller check against every OTHER
        chapter while assigning into a chapter that may already hold
        some of the same ids (e.g. re-adding an already-assigned id to
        its own draft is a no-op via ``INSERT OR IGNORE``, not a
        conflict). One query; the message names the offending entry id
        and the chapter it's already in.
        """
        if not entry_ids:
            return
        placeholders = ", ".join("?" for _ in entry_ids)
        sql = (
            "SELECT ce.entry_id, ce.chapter_id FROM storyline_chapter_entries ce"
            " JOIN storyline_chapters c ON c.id = ce.chapter_id"
            f" WHERE c.storyline_id = ? AND ce.entry_id IN ({placeholders})"
        )
        params: list[object] = [storyline_id, *entry_ids]
        if exclude_chapter_id is not None:
            sql += " AND ce.chapter_id != ?"
            params.append(exclude_chapter_id)
        sql += " LIMIT 1"
        row = self._conn().execute(sql, params).fetchone()
        if row is not None:
            raise ValueError(
                f"Entry {row['entry_id']} is already a member of chapter "
                f"{row['chapter_id']} in storyline {storyline_id}; an "
                "entry can only belong to one chapter at a time."
            )

    def add_entries_to_draft(self, chapter_id: int, entry_ids: list[int]) -> None:
        """Assign entries to a draft chapter. Raises if not draft, or if
        any entry id already belongs to another chapter of the same
        storyline (membership uniqueness guard, spec §1)."""
        chapter = self._require_state(chapter_id, "draft")
        if not entry_ids:
            return
        self._reject_cross_chapter_membership(
            chapter.storyline_id, entry_ids, exclude_chapter_id=chapter_id,
        )
        conn = self._conn()
        try:
            conn.executemany(
                "INSERT OR IGNORE INTO storyline_chapter_entries"
                " (chapter_id, entry_id) VALUES (?, ?)",
                [(chapter_id, eid) for eid in entry_ids],
            )
            conn.execute(
                "UPDATE storyline_chapters"
                " SET updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"
                " WHERE id = ?",
                (chapter_id,),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    # ── pending (matched-but-unassigned) entries ────────────────

    def add_pending_entry(self, storyline_id: int, entry_id: int) -> None:
        """Record a matched-but-unassigned entry. Idempotent."""
        conn = self._conn()
        conn.execute(
            "INSERT OR IGNORE INTO storyline_pending_entries"
            " (storyline_id, entry_id) VALUES (?, ?)",
            (storyline_id, entry_id),
        )
        conn.commit()

    def list_pending_entries(self, storyline_id: int) -> list[int]:
        rows = self._conn().execute(
            "SELECT entry_id FROM storyline_pending_entries"
            " WHERE storyline_id = ? ORDER BY entry_id ASC",
            (storyline_id,),
        ).fetchall()
        return [int(r["entry_id"]) for r in rows]

    def clear_pending_entries(self, storyline_id: int, entry_ids: list[int]) -> None:
        if not entry_ids:
            return
        conn = self._conn()
        placeholders = ", ".join("?" for _ in entry_ids)
        conn.execute(
            "DELETE FROM storyline_pending_entries"
            f" WHERE storyline_id = ? AND entry_id IN ({placeholders})",
            (storyline_id, *entry_ids),
        )
        conn.commit()

    def find_entries_mentioning(
        self, user_id: int, name: str,
    ) -> list[DatedEntryExcerpt]:
        """Plain ``LIKE`` scan over entry text for a literal surface form.

        Sparse-storyline recall fallback (spec §3, used by the engine's
        ``_candidate_entries`` when an anchor's entity-mention union has
        fewer than a handful of rows): catches pronominal references or
        extractor misses that ``entity_mentions`` never recorded. Fully
        parameterised (``name`` only ever appears as a bound ``?``
        substitution inside the ``%...%`` pattern) — no SQL injection
        surface. ``%`` and ``_`` in ``name`` are backslash-escaped
        (``ESCAPE '\\'``) before being wrapped so a literal entity name
        like ``"100%"`` is matched as text, not as a LIKE wildcard.
        Returns excerpts with no ``quotes`` (there is no
        ``entity_mentions`` row to quote from on this path).
        """
        escaped_name = name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped_name}%"
        rows = self._conn().execute(
            "SELECT id AS entry_id, entry_date,"
            "  COALESCE(NULLIF(final_text, ''), raw_text) AS body_text"
            " FROM entries"
            " WHERE user_id = ?"
            "   AND date_confirmed = 1"
            "   AND (final_text LIKE ? ESCAPE '\\' OR raw_text LIKE ? ESCAPE '\\')"
            " ORDER BY entry_date ASC, id ASC",
            (user_id, pattern, pattern),
        ).fetchall()
        return [
            DatedEntryExcerpt(
                entry_id=int(row["entry_id"]),
                entry_date=row["entry_date"],
                final_text=row["body_text"] or "",
                quotes=[],
            )
            for row in rows
        ]

    # ── narrative writes ─────────────────────────────────────────

    def set_draft_narrative(
        self,
        chapter_id: int,
        *,
        segments: list[dict[str, Any]],
        source_entry_ids: list[int],
        citation_count: int,
        model_used: str,
        embedding: list[float] | None,
    ) -> None:
        """Persist a fresh narrative onto a draft chapter. Raises if not draft."""
        self._require_state(chapter_id, "draft")
        conn = self._conn()
        conn.execute(
            "UPDATE storyline_chapters"
            " SET segments_json = ?, source_entry_ids_json = ?,"
            "     citation_count = ?, model_used = ?,"
            "     generated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),"
            "     draft_embedding_json = ?,"
            "     updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"
            " WHERE id = ?",
            (
                json.dumps(segments),
                json.dumps(source_entry_ids),
                int(citation_count),
                model_used,
                json.dumps(embedding) if embedding is not None else None,
                chapter_id,
            ),
        )
        conn.commit()

    def append_addendum(
        self,
        chapter_id: int,
        *,
        segments: list[dict[str, Any]],
        entry_ids: list[int],
    ) -> None:
        """Append an addendum to a published chapter; clears ``read_at``.

        Raises if the chapter is not published. Membership rows for
        ``entry_ids`` are inserted (or updated) with ``added_late = 1``
        so the UI can flag them as folded in after the fact.
        """
        self._require_state(chapter_id, "published")
        conn = self._conn()
        try:
            now = self._now(conn)
            row = conn.execute(
                "SELECT addenda_json FROM storyline_chapters WHERE id = ?",
                (chapter_id,),
            ).fetchone()
            addenda = list(json.loads(row["addenda_json"] or "[]"))
            addenda.append(
                {"added_at": now, "segments": segments, "entry_ids": entry_ids},
            )
            conn.execute(
                "UPDATE storyline_chapters"
                " SET addenda_json = ?, read_at = NULL,"
                "     updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"
                " WHERE id = ?",
                (json.dumps(addenda), chapter_id),
            )
            conn.executemany(
                "INSERT INTO storyline_chapter_entries"
                " (chapter_id, entry_id, added_late) VALUES (?, ?, 1)"
                " ON CONFLICT(chapter_id, entry_id) DO UPDATE SET added_late = 1",
                [(chapter_id, eid) for eid in entry_ids],
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    # ── lifecycle transactions ──────────────────────────────────

    def publish_draft(
        self,
        storyline_id: int,
        *,
        title: str,
        segments: list[dict[str, Any]],
        source_entry_ids: list[int],
        citation_count: int,
        model_used: str,
        new_draft_entry_ids: list[int],
    ) -> tuple[StorylineChapter, StorylineChapter]:
        """Publish the storyline's draft and seed the next draft, atomically.

        Raises ``ValueError`` if the storyline has no draft chapter, or
        if any of ``new_draft_entry_ids`` already belongs to another
        chapter of this storyline (membership uniqueness guard, spec
        §1) — the new draft chapter doesn't exist yet, so this checks
        against every existing chapter, no exclusion.
        """
        draft = self.get_draft(storyline_id)
        if draft is None:
            raise ValueError(f"Storyline {storyline_id} has no draft chapter")
        self._reject_cross_chapter_membership(storyline_id, new_draft_entry_ids)
        conn = self._conn()
        try:
            conn.execute(
                "UPDATE storyline_chapters SET state='published', title=?,"
                " segments_json=?, source_entry_ids_json=?, citation_count=?,"
                " model_used=?, generated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now'),"
                " published_at=strftime('%Y-%m-%dT%H:%M:%SZ','now'),"
                " read_at=NULL, draft_embedding_json=NULL,"
                " updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')"
                " WHERE id=?",
                (
                    title.strip(), json.dumps(segments),
                    json.dumps(source_entry_ids), int(citation_count),
                    model_used, draft.id,
                ),
            )
            cursor = conn.execute(
                "INSERT INTO storyline_chapters (storyline_id, seq, state)"
                " VALUES (?, ?, 'draft')",
                (storyline_id, draft.seq + 1),
            )
            new_id = cursor.lastrowid
            conn.executemany(
                "INSERT OR IGNORE INTO storyline_chapter_entries (chapter_id, entry_id)"
                " VALUES (?, ?)",
                [(new_id, eid) for eid in new_draft_entry_ids],
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        published = self.get_chapter(draft.id)
        new_draft = self.get_chapter(new_id)
        assert published is not None and new_draft is not None
        return published, new_draft

    def unpublish_newest(self, storyline_id: int) -> StorylineChapter:
        """Fold the newest published chapter back into the draft, atomically.

        The draft's stale narrative is cleared (the caller queues a
        re-narration job). Raises ``ValueError`` if the storyline has
        no draft (should not happen — every storyline always has one)
        or no published chapter to fold back.
        """
        draft = self.get_draft(storyline_id)
        if draft is None:
            raise ValueError(f"Storyline {storyline_id} has no draft chapter")
        chapters = self.list_chapters(storyline_id)
        published = [c for c in chapters if c.state == "published"]
        if not published:
            raise ValueError(f"Storyline {storyline_id} has no published chapter")
        newest = published[-1]
        conn = self._conn()
        try:
            conn.execute(
                "UPDATE OR IGNORE storyline_chapter_entries SET chapter_id=?"
                " WHERE chapter_id=?", (draft.id, newest.id),
            )
            conn.execute(  # duplicates skipped by IGNORE above are deleted with the row
                "DELETE FROM storyline_chapters WHERE id=?", (newest.id,),
            )
            conn.execute(
                "UPDATE storyline_chapters SET seq=?, segments_json='[]',"
                " source_entry_ids_json='[]', citation_count=0,"
                " model_used='', draft_embedding_json=NULL,"
                " updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?",
                (newest.seq, draft.id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        merged = self.get_chapter(draft.id)
        assert merged is not None
        return merged

    def replace_all_chapters(
        self, storyline_id: int, chapters: list[BootstrapChapterSpec],
    ) -> list[StorylineChapter]:
        """Atomically replace a storyline's entire chapter set (bootstrap).

        ``chapters`` is the complete, seq-ordered desired chapter list.
        Exactly one spec may have ``state == 'draft'`` and it must be
        the last one — every storyline must end with a single trailing
        draft. Existing chapters (and their membership, via cascade)
        are dropped and replaced in one transaction.
        """
        draft_positions = [i for i, s in enumerate(chapters) if s.state == "draft"]
        if len(draft_positions) != 1 or draft_positions[0] != len(chapters) - 1:
            raise ValueError(
                "replace_all_chapters: exactly one draft must be the final chapter"
            )
        conn = self._conn()
        try:
            conn.execute(
                "DELETE FROM storyline_chapters WHERE storyline_id = ?",
                (storyline_id,),
            )
            for seq, spec in enumerate(chapters, start=1):
                generated_at = None
                published_at = None
                read_at = None
                if spec.state == "published":
                    now = self._now(conn)
                    generated_at = now
                    published_at = now
                    if spec.mark_read:
                        read_at = now
                cursor = conn.execute(
                    "INSERT INTO storyline_chapters"
                    " (storyline_id, seq, title, state, segments_json,"
                    "  source_entry_ids_json, citation_count, model_used,"
                    "  generated_at, published_at, read_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        storyline_id, seq, spec.title.strip(), spec.state,
                        json.dumps(spec.segments), json.dumps(spec.source_entry_ids),
                        int(spec.citation_count), spec.model_used,
                        generated_at, published_at, read_at,
                    ),
                )
                chapter_id = cursor.lastrowid
                conn.executemany(
                    "INSERT OR IGNORE INTO storyline_chapter_entries"
                    " (chapter_id, entry_id) VALUES (?, ?)",
                    [(chapter_id, eid) for eid in spec.entry_ids],
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return self.list_chapters(storyline_id)
