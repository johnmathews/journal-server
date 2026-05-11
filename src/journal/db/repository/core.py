"""Entry CRUD + listing methods for SQLiteEntryRepository.

Owns the core entry table operations: create, fetch, list (paginated
or by date), update text/word/chunk counts, update entry date,
delete.

Methods route through ``self._conn()`` so each call gets the
appropriate connection — thread-local on the factory path, the
shared connection on the legacy path.
"""

import logging

from journal.db.repository.protocol import _row_to_entry
from journal.models import Entry

log = logging.getLogger(__name__)


class _CoreMixin:
    """Core methods on SQLiteEntryRepository."""

    def create_entry(
        self, entry_date: str, source_type: str, raw_text: str, word_count: int,
        final_text: str | None = None,
        user_id: int = 1,
    ) -> Entry:
        actual_final = final_text if final_text is not None else raw_text
        sql = (
            "INSERT INTO entries"
            " (user_id, entry_date, source_type, raw_text, final_text, word_count)"
            " VALUES (?, ?, ?, ?, ?, ?)"
        )
        params = (user_id, entry_date, source_type, raw_text, actual_final, word_count)
        conn = self._conn()
        with conn:
            cursor = conn.execute(sql, params)
        entry_id = cursor.lastrowid
        log.info("Created entry %d for date %s", entry_id, entry_date)
        return self.get_entry(entry_id)  # type: ignore[return-value]

    def get_entry(self, entry_id: int, user_id: int | None = None) -> Entry | None:
        conn = self._conn()
        if user_id is not None:
            row = conn.execute(
                "SELECT * FROM entries WHERE id = ? AND user_id = ?", (entry_id, user_id)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM entries WHERE id = ?", (entry_id,)
            ).fetchone()
        return _row_to_entry(row) if row else None

    def get_entries_by_date(self, date: str, user_id: int | None = None) -> list[Entry]:
        conn = self._conn()
        if user_id is not None:
            rows = conn.execute(
                "SELECT * FROM entries WHERE entry_date = ? AND user_id = ? ORDER BY created_at",
                (date, user_id),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM entries WHERE entry_date = ? ORDER BY created_at", (date,)
            ).fetchall()
        return [_row_to_entry(r) for r in rows]

    def list_entries(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = 20,
        offset: int = 0,
        user_id: int | None = None,
    ) -> list[Entry]:
        query = "SELECT * FROM entries WHERE 1=1"
        params: list[str | int] = []
        if user_id is not None:
            query += " AND user_id = ?"
            params.append(user_id)
        if start_date:
            query += " AND entry_date >= ?"
            params.append(start_date)
        if end_date:
            query += " AND entry_date <= ?"
            params.append(end_date)
        query += " ORDER BY entry_date DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        conn = self._conn()
        rows = conn.execute(query, params).fetchall()
        return [_row_to_entry(r) for r in rows]

    def update_final_text(
        self, entry_id: int, final_text: str, word_count: int, chunk_count: int,
        user_id: int | None = None,
    ) -> Entry | None:
        conn = self._conn()
        with conn:
            if user_id is not None:
                conn.execute(
                    "UPDATE entries SET final_text = ?, word_count = ?, chunk_count = ?,"
                    " updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"
                    " WHERE id = ? AND user_id = ?",
                    (final_text, word_count, chunk_count, entry_id, user_id),
                )
            else:
                conn.execute(
                    "UPDATE entries SET final_text = ?, word_count = ?, chunk_count = ?,"
                    " updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?",
                    (final_text, word_count, chunk_count, entry_id),
                )
        log.info("Updated final_text for entry %d", entry_id)
        return self.get_entry(entry_id, user_id)

    def update_entry_date(
        self, entry_id: int, entry_date: str, user_id: int | None = None,
    ) -> Entry | None:
        conn = self._conn()
        with conn:
            if user_id is not None:
                conn.execute(
                    "UPDATE entries SET entry_date = ?,"
                    " updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"
                    " WHERE id = ? AND user_id = ?",
                    (entry_date, entry_id, user_id),
                )
            else:
                conn.execute(
                    "UPDATE entries SET entry_date = ?,"
                    " updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?",
                    (entry_date, entry_id),
                )
        log.info("Updated entry_date for entry %d to %s", entry_id, entry_date)
        return self.get_entry(entry_id, user_id)

    def delete_entry(self, entry_id: int, user_id: int | None = None) -> bool:
        """Delete an entry and all cascading rows. Returns True if a row was deleted."""
        conn = self._conn()
        with conn:
            if user_id is not None:
                cursor = conn.execute(
                    "DELETE FROM entries WHERE id = ? AND user_id = ?", (entry_id, user_id)
                )
            else:
                cursor = conn.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
        deleted = cursor.rowcount > 0
        if deleted:
            log.info("Deleted entry %d", entry_id)
        return deleted
