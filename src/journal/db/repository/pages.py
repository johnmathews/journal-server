"""Entry pages + uncertain OCR spans + verification.

Owns ``add_entry_page`` / ``get_entry_pages`` / ``get_page_count``
(per-entry page rows) plus ``add_uncertain_spans`` /
``get_uncertain_spans`` / ``get_uncertain_span_count`` /
``verify_doubts`` (OCR-uncertainty span tracking and the verified
flag that gates the count).

Methods route through ``self._conn()`` so each call gets the
appropriate connection — thread-local on the factory path, the
shared connection on the legacy path.
"""

import logging

from journal.models import EntryPage

log = logging.getLogger(__name__)


class _PagesMixin:
    """Pages methods on SQLiteEntryRepository."""

    def add_entry_page(
        self, entry_id: int, page_number: int, raw_text: str, source_file_id: int | None = None
    ) -> None:
        conn = self._conn()
        with conn:
            conn.execute(
                "INSERT INTO entry_pages (entry_id, page_number, raw_text, source_file_id)"
                " VALUES (?, ?, ?, ?)",
                (entry_id, page_number, raw_text, source_file_id),
            )
        log.info("Added page %d to entry %d", page_number, entry_id)

    def get_entry_pages(self, entry_id: int) -> list[EntryPage]:
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM entry_pages WHERE entry_id = ? ORDER BY page_number",
            (entry_id,),
        ).fetchall()
        return [
            EntryPage(
                id=row["id"],
                entry_id=row["entry_id"],
                page_number=row["page_number"],
                raw_text=row["raw_text"],
                source_file_id=row["source_file_id"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def get_page_count(self, entry_id: int) -> int:
        conn = self._conn()
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM entry_pages WHERE entry_id = ?",
            (entry_id,),
        ).fetchone()
        return row["cnt"]

    def add_uncertain_spans(
        self, entry_id: int, spans: list[tuple[int, int]]
    ) -> None:
        """Insert uncertain character spans for an entry.

        `spans` is a list of `(char_start, char_end)` half-open offsets
        into the entry's `raw_text`. A no-op when `spans` is empty.
        Intended to be called once during ingestion — the repo does
        not clear existing rows first, because `raw_text` is immutable
        and there is no re-OCR path in the codebase today. If that
        ever changes, the caller (ingestion service) is responsible
        for deleting stale spans before re-inserting.
        """
        if not spans:
            return
        conn = self._conn()
        with conn:
            conn.executemany(
                "INSERT INTO entry_uncertain_spans "
                "(entry_id, char_start, char_end) VALUES (?, ?, ?)",
                [(entry_id, start, end) for start, end in spans],
            )
        log.debug(
            "Stored %d uncertain spans for entry %d", len(spans), entry_id
        )

    def get_uncertain_spans(self, entry_id: int) -> list[tuple[int, int]]:
        """Return uncertain spans for an entry, sorted by char_start.

        Returns an empty list for entries with no recorded spans. No
        distinction between "the ingestion never ran the uncertainty
        pass" and "it ran and found zero uncertain words" — the webapp
        simply renders no highlight in either case.
        """
        conn = self._conn()
        rows = conn.execute(
            "SELECT char_start, char_end FROM entry_uncertain_spans "
            "WHERE entry_id = ? ORDER BY char_start ASC",
            (entry_id,),
        ).fetchall()
        return [(int(r["char_start"]), int(r["char_end"])) for r in rows]

    def get_uncertain_span_count(self, entry_id: int) -> int:
        """Return the number of uncertain spans for an entry.

        Returns 0 when the user has verified all doubts for this entry
        (``doubts_verified = 1``), even if span rows still exist in the
        database. The raw spans are preserved for future analysis.
        """
        conn = self._conn()
        row = conn.execute(
            "SELECT doubts_verified FROM entries WHERE id = ?",
            (entry_id,),
        ).fetchone()
        if row and row["doubts_verified"]:
            return 0
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM entry_uncertain_spans WHERE entry_id = ?",
            (entry_id,),
        ).fetchone()
        return row["cnt"]

    def verify_doubts(self, entry_id: int, user_id: int | None = None) -> bool:
        """Mark all doubts on an entry as verified.

        Sets ``doubts_verified = 1`` on the entry row. The underlying
        uncertain span rows are preserved for future use. Returns
        ``True`` if the entry exists, ``False`` otherwise.
        """
        conn = self._conn()
        with conn:
            if user_id is not None:
                cursor = conn.execute(
                    "UPDATE entries SET doubts_verified = 1 WHERE id = ? AND user_id = ?",
                    (entry_id, user_id),
                )
            else:
                cursor = conn.execute(
                    "UPDATE entries SET doubts_verified = 1 WHERE id = ?",
                    (entry_id,),
                )
        return cursor.rowcount > 0
