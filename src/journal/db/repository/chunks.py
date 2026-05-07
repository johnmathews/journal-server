"""Entry chunk replacement, retrieval, and count updates.

Owns the ``entry_chunks`` table operations: bulk replace, fetch in
insertion order, and the chunk-count column on the parent entry.

Methods stay bound to ``self`` so they keep using ``self._conn``.
"""

import logging

from journal.models import ChunkSpan

log = logging.getLogger(__name__)


class _ChunksMixin:
    """Chunks methods on SQLiteEntryRepository."""

    def update_chunk_count(self, entry_id: int, chunk_count: int) -> None:
        self._conn.execute(
            "UPDATE entries SET chunk_count = ? WHERE id = ?",
            (chunk_count, entry_id),
        )
        self._conn.commit()

    def replace_chunks(self, entry_id: int, chunks: list[ChunkSpan]) -> None:
        """Replace all persisted chunks for an entry with the given list.

        Deletes any existing `entry_chunks` rows for `entry_id` and
        inserts the new ones in order. Intended to be called from the
        ingestion and rechunk paths so that stored chunks always reflect
        the most recent run of the chunker. Safe to call with an empty
        list — the entry simply ends up with no stored chunks.
        """
        with self._conn:
            self._conn.execute(
                "DELETE FROM entry_chunks WHERE entry_id = ?", (entry_id,)
            )
            if chunks:
                self._conn.executemany(
                    "INSERT INTO entry_chunks "
                    "(entry_id, chunk_index, chunk_text, char_start, char_end, token_count)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        (
                            entry_id,
                            i,
                            c.text,
                            c.char_start,
                            c.char_end,
                            c.token_count,
                        )
                        for i, c in enumerate(chunks)
                    ],
                )
        log.debug(
            "Replaced chunks for entry %d (%d rows)", entry_id, len(chunks)
        )

    def get_chunks(self, entry_id: int) -> list[ChunkSpan]:
        """Return persisted chunks for an entry in insertion order.

        Returns an empty list if the entry has no stored chunks — which
        is the case for entries ingested before migration 0003, or for
        entries whose chunking failed. Callers that need to distinguish
        "entry exists but no chunks" from "entry not found" should check
        entry existence separately.
        """
        rows = self._conn.execute(
            "SELECT chunk_index, chunk_text, char_start, char_end, token_count"
            " FROM entry_chunks WHERE entry_id = ? ORDER BY chunk_index",
            (entry_id,),
        ).fetchall()
        return [
            ChunkSpan(
                text=row["chunk_text"],
                char_start=row["char_start"],
                char_end=row["char_end"],
                token_count=row["token_count"],
            )
            for row in rows
        ]
