"""FTS5 keyword search over entries.

Owns ``search_text`` (entries matching), ``search_text_with_snippets``
(entries + highlighted snippets, paginated by FTS rank), and
``count_text_matches`` (hit count for badge/UX). All three go through
the ``entries_fts`` virtual table.

Methods stay bound to ``self`` so they keep using ``self._conn``.
"""

from journal.db.repository.protocol import _row_to_entry
from journal.models import Entry


class _SearchMixin:
    """Search methods on SQLiteEntryRepository."""

    def search_text(
        self, query: str, start_date: str | None = None, end_date: str | None = None,
        user_id: int | None = None,
    ) -> list[Entry]:
        sql = """
            SELECT e.* FROM entries_fts
            JOIN entries e ON e.id = entries_fts.rowid
            WHERE entries_fts MATCH ?
        """
        params: list[str | int] = [query]
        if user_id is not None:
            sql += " AND e.user_id = ?"
            params.append(user_id)
        if start_date:
            sql += " AND e.entry_date >= ?"
            params.append(start_date)
        if end_date:
            sql += " AND e.entry_date <= ?"
            params.append(end_date)
        sql += " ORDER BY rank"
        rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_entry(r) for r in rows]

    def search_text_with_snippets(
        self,
        query: str,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = 10,
        offset: int = 0,
        user_id: int | None = None,
    ) -> list[tuple[Entry, str]]:
        """FTS5 keyword search that returns a highlighted snippet per hit.

        Uses SQLite FTS5's `snippet()` aux function to produce a short
        excerpt around each match. Matched terms are wrapped in ASCII
        `\\x02` (STX) and `\\x03` (ETX) control characters — markers
        that never appear in normal journal text and survive JSON
        serialisation. Callers translate them to whatever highlight
        markup they need. Ellipsis for truncated context is the
        literal string `"…"` (U+2026).

        Results are ordered by FTS5's `rank` (best match first) and
        paginated via `limit` / `offset`.
        """
        sql = """
            SELECT
                e.*,
                snippet(entries_fts, 0, char(2), char(3), '…', 16) AS snippet
            FROM entries_fts
            JOIN entries e ON e.id = entries_fts.rowid
            WHERE entries_fts MATCH ?
        """
        params: list[str | int] = [query]
        if user_id is not None:
            sql += " AND e.user_id = ?"
            params.append(user_id)
        if start_date:
            sql += " AND e.entry_date >= ?"
            params.append(start_date)
        if end_date:
            sql += " AND e.entry_date <= ?"
            params.append(end_date)
        sql += " ORDER BY rank LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = self._conn.execute(sql, params).fetchall()
        return [(_row_to_entry(r), r["snippet"]) for r in rows]

    def count_text_matches(
        self,
        query: str,
        start_date: str | None = None,
        end_date: str | None = None,
        user_id: int | None = None,
    ) -> int:
        sql = """
            SELECT COUNT(*) AS cnt FROM entries_fts
            JOIN entries e ON e.id = entries_fts.rowid
            WHERE entries_fts MATCH ?
        """
        params: list[str | int] = [query]
        if user_id is not None:
            sql += " AND e.user_id = ?"
            params.append(user_id)
        if start_date:
            sql += " AND e.entry_date >= ?"
            params.append(start_date)
        if end_date:
            sql += " AND e.entry_date <= ?"
            params.append(end_date)
        row = self._conn.execute(sql, params).fetchone()
        return row["cnt"]
