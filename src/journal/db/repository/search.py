"""FTS5 keyword search over entries.

Owns ``search_text`` (entries matching), ``search_text_with_snippets``
(entries + highlighted snippets, paginated by FTS rank), and
``count_text_matches`` (hit count for badge/UX). All three go through
the ``entries_fts`` virtual table.

Methods route through ``self._conn()`` so each call gets the
appropriate connection — thread-local on the factory path, the
shared connection on the legacy path.
"""

import re

from journal.db.repository.protocol import _row_to_entry
from journal.models import Entry

# Matches a single word character (letter/digit/underscore), Unicode-aware.
# Used to decide whether a whitespace token carries any searchable content.
_WORD_RE = re.compile(r"\w", re.UNICODE)


def _to_fts_match_query(query: str) -> str | None:
    """Turn free-form user text into a safe FTS5 ``MATCH`` expression.

    Search queries are natural language ("when did my back start
    hurting?"). FTS5's MATCH grammar treats ``?``, double quotes,
    ``-``, ``:``, ``*``, ``(`` / ``)`` and the bare booleans
    ``AND`` / ``OR`` / ``NOT`` as operators, so handing the raw query
    to MATCH raises ``sqlite3.OperationalError`` on anything that isn't
    plain barewords.

    We tokenise on whitespace, drop tokens with no word characters
    (pure punctuation like a lone ``?``), and wrap each remaining token
    in double quotes — escaping any embedded quote by doubling it — so
    every token is matched as a literal phrase and nothing is parsed as
    an operator. Tokens are space-joined, which FTS5 reads as implicit
    AND, preserving the previous bareword semantics for ordinary
    keyword queries.

    Returns ``None`` when the query has no searchable tokens (e.g. it
    was all punctuation); callers short-circuit to an empty result set
    rather than passing an empty MATCH string to FTS5 (which would
    itself be a syntax error).
    """
    tokens: list[str] = []
    for raw in query.split():
        if not _WORD_RE.search(raw):
            continue
        escaped = raw.replace('"', '""')
        tokens.append(f'"{escaped}"')
    if not tokens:
        return None
    return " ".join(tokens)


class _SearchMixin:
    """Search methods on SQLiteEntryRepository."""

    def search_text(
        self, query: str, start_date: str | None = None, end_date: str | None = None,
        user_id: int | None = None,
    ) -> list[Entry]:
        match = _to_fts_match_query(query)
        if match is None:
            return []
        sql = """
            SELECT e.* FROM entries_fts
            JOIN entries e ON e.id = entries_fts.rowid
            WHERE entries_fts MATCH ?
              AND e.date_confirmed = 1
        """
        params: list[str | int] = [match]
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
        conn = self._conn()
        rows = conn.execute(sql, params).fetchall()
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
        match = _to_fts_match_query(query)
        if match is None:
            return []
        sql = """
            SELECT
                e.*,
                snippet(entries_fts, 0, char(2), char(3), '…', 16) AS snippet
            FROM entries_fts
            JOIN entries e ON e.id = entries_fts.rowid
            WHERE entries_fts MATCH ?
              AND e.date_confirmed = 1
        """
        params: list[str | int] = [match]
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
        conn = self._conn()
        rows = conn.execute(sql, params).fetchall()
        return [(_row_to_entry(r), r["snippet"]) for r in rows]

    def count_text_matches(
        self,
        query: str,
        start_date: str | None = None,
        end_date: str | None = None,
        user_id: int | None = None,
    ) -> int:
        match = _to_fts_match_query(query)
        if match is None:
            return 0
        sql = """
            SELECT COUNT(*) AS cnt FROM entries_fts
            JOIN entries e ON e.id = entries_fts.rowid
            WHERE entries_fts MATCH ?
              AND e.date_confirmed = 1
        """
        params: list[str | int] = [match]
        if user_id is not None:
            sql += " AND e.user_id = ?"
            params.append(user_id)
        if start_date:
            sql += " AND e.entry_date >= ?"
            params.append(start_date)
        if end_date:
            sql += " AND e.entry_date <= ?"
            params.append(end_date)
        conn = self._conn()
        row = conn.execute(sql, params).fetchone()
        return row["cnt"]
