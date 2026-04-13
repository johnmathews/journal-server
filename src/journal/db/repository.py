"""Repository interface and SQLite implementation."""

import logging
import sqlite3
from datetime import datetime
from typing import Protocol, runtime_checkable

from journal.models import (
    ChunkSpan,
    Entry,
    EntryPage,
    IngestionStats,
    MoodScore,
    MoodTrend,
    Statistics,
    TopicFrequency,
    WritingFrequencyBin,
)

log = logging.getLogger(__name__)

# Hardcoded set of supported dashboard bin widths. Callers validate
# against this before any SQL runs. Adding a new granularity
# requires updates in this tuple AND the `_bin_start_sql` helper
# below — deliberately explicit so the contract never drifts.
_SUPPORTED_BINS: tuple[str, ...] = ("week", "month", "quarter", "year")

# Mood-trends supports the same set plus `day`, which predates the
# dashboard and is still exposed via the LLM-facing MCP tool.
_SUPPORTED_MOOD_BINS: tuple[str, ...] = (
    "day",
    "week",
    "month",
    "quarter",
    "year",
)


def _bin_start_sql(granularity: str, column: str = "entry_date") -> str:
    """SQL expression that returns the canonical bucket-start ISO
    date for a row's `entry_date` at the requested granularity.

    Raises `ValueError` on unsupported granularity. `column` lets
    callers that select from a JOINed table pass a qualified name
    like `e.entry_date` — the bin-start computation otherwise has
    no business knowing the join shape.

    - **day**: `entry_date` itself (identity).
    - **week**: Monday of the week containing entry_date. Uses
      `strftime('%w')` (0=Sunday..6=Saturday) and walks back
      `(weekday + 6) % 7` days, which lands on the preceding
      Monday for every day of the week.
    - **month**: 1st of the month.
    - **quarter**: 1st of Jan/Apr/Jul/Oct. Computed as "start of
      month, then minus `(month - 1) % 3` months".
    - **year**: January 1 of the year.
    """
    if granularity not in (*_SUPPORTED_MOOD_BINS,):
        raise ValueError(
            f"Unsupported granularity {granularity!r}; "
            f"must be one of {_SUPPORTED_MOOD_BINS}"
        )
    if granularity == "day":
        return column
    if granularity == "week":
        return (
            f"date({column}, "
            f"'-' || ((CAST(strftime('%w', {column}) AS INT) + 6) % 7) "
            f"|| ' days')"
        )
    if granularity == "month":
        return f"date({column}, 'start of month')"
    if granularity == "quarter":
        return (
            f"date({column}, 'start of month', "
            f"'-' || ((CAST(strftime('%m', {column}) AS INT) - 1) % 3) "
            f"|| ' months')"
        )
    # year
    return f"date({column}, 'start of year')"


@runtime_checkable
class EntryRepository(Protocol):
    def create_entry(
        self, entry_date: str, source_type: str, raw_text: str, word_count: int,
        final_text: str | None = None,
    ) -> Entry: ...

    def get_entry(self, entry_id: int) -> Entry | None: ...

    def update_final_text(
        self, entry_id: int, final_text: str, word_count: int, chunk_count: int
    ) -> Entry | None: ...

    def update_entry_date(self, entry_id: int, entry_date: str) -> Entry | None: ...

    def delete_entry(self, entry_id: int) -> bool: ...

    def add_entry_page(
        self, entry_id: int, page_number: int, raw_text: str, source_file_id: int | None = None
    ) -> None: ...

    def get_entry_pages(self, entry_id: int) -> list[EntryPage]: ...

    def update_chunk_count(self, entry_id: int, chunk_count: int) -> None: ...

    def replace_chunks(self, entry_id: int, chunks: list[ChunkSpan]) -> None: ...

    def get_chunks(self, entry_id: int) -> list[ChunkSpan]: ...

    def add_uncertain_spans(
        self, entry_id: int, spans: list[tuple[int, int]]
    ) -> None: ...

    def get_uncertain_spans(self, entry_id: int) -> list[tuple[int, int]]: ...

    def get_uncertain_span_count(self, entry_id: int) -> int: ...

    def get_entries_by_date(self, date: str) -> list[Entry]: ...

    def list_entries(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[Entry]: ...

    def search_text(
        self, query: str, start_date: str | None = None, end_date: str | None = None
    ) -> list[Entry]: ...

    def search_text_with_snippets(
        self,
        query: str,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = 10,
        offset: int = 0,
    ) -> list[tuple[Entry, str]]: ...

    def count_text_matches(
        self,
        query: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> int: ...

    def get_statistics(
        self, start_date: str | None = None, end_date: str | None = None
    ) -> Statistics: ...

    def add_people(self, entry_id: int, names: list[str]) -> None: ...

    def add_places(self, entry_id: int, names: list[str]) -> None: ...

    def add_tags(self, entry_id: int, tags: list[str]) -> None: ...

    def add_mood_score(
        self, entry_id: int, dimension: str, score: float, confidence: float | None = None
    ) -> None: ...

    def replace_mood_scores(
        self,
        entry_id: int,
        scores: list[tuple[str, float, float | None]],
    ) -> None: ...

    def get_mood_scores(self, entry_id: int) -> list[MoodScore]: ...

    def get_entries_missing_mood_scores(
        self, dimension_names: list[str]
    ) -> list[int]: ...

    def prune_retired_mood_scores(
        self, current_names: list[str]
    ) -> int: ...

    def get_mood_trends(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        granularity: str = "week",
    ) -> list[MoodTrend]: ...

    def count_entries(
        self, start_date: str | None = None, end_date: str | None = None
    ) -> int: ...

    def get_ingestion_stats(self, now: datetime) -> IngestionStats: ...

    def get_writing_frequency(
        self,
        start_date: str | None,
        end_date: str | None,
        granularity: str,
    ) -> list[WritingFrequencyBin]: ...

    def get_page_count(self, entry_id: int) -> int: ...

    def get_topic_frequency(
        self, topic: str, start_date: str | None = None, end_date: str | None = None
    ) -> TopicFrequency: ...


def _row_to_entry(row: sqlite3.Row) -> Entry:
    return Entry(
        id=row["id"],
        entry_date=row["entry_date"],
        source_type=row["source_type"],
        raw_text=row["raw_text"],
        final_text=row["final_text"] or row["raw_text"],
        word_count=row["word_count"],
        chunk_count=row["chunk_count"],
        language=row["language"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class SQLiteEntryRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def create_entry(
        self, entry_date: str, source_type: str, raw_text: str, word_count: int,
        final_text: str | None = None,
    ) -> Entry:
        actual_final = final_text if final_text is not None else raw_text
        sql = (
            "INSERT INTO entries (entry_date, source_type, raw_text, final_text, word_count)"
            " VALUES (?, ?, ?, ?, ?)"
        )
        params = (entry_date, source_type, raw_text, actual_final, word_count)
        cursor = self._conn.execute(sql, params)
        self._conn.commit()
        entry_id = cursor.lastrowid
        log.info("Created entry %d for date %s", entry_id, entry_date)
        return self.get_entry(entry_id)  # type: ignore[return-value]

    def get_entry(self, entry_id: int) -> Entry | None:
        row = self._conn.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
        return _row_to_entry(row) if row else None

    def get_entries_by_date(self, date: str) -> list[Entry]:
        rows = self._conn.execute(
            "SELECT * FROM entries WHERE entry_date = ? ORDER BY created_at", (date,)
        ).fetchall()
        return [_row_to_entry(r) for r in rows]

    def list_entries(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[Entry]:
        query = "SELECT * FROM entries WHERE 1=1"
        params: list[str | int] = []
        if start_date:
            query += " AND entry_date >= ?"
            params.append(start_date)
        if end_date:
            query += " AND entry_date <= ?"
            params.append(end_date)
        query += " ORDER BY entry_date DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = self._conn.execute(query, params).fetchall()
        return [_row_to_entry(r) for r in rows]

    def search_text(
        self, query: str, start_date: str | None = None, end_date: str | None = None
    ) -> list[Entry]:
        sql = """
            SELECT e.* FROM entries_fts
            JOIN entries e ON e.id = entries_fts.rowid
            WHERE entries_fts MATCH ?
        """
        params: list[str] = [query]
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
    ) -> int:
        sql = """
            SELECT COUNT(*) AS cnt FROM entries_fts
            JOIN entries e ON e.id = entries_fts.rowid
            WHERE entries_fts MATCH ?
        """
        params: list[str] = [query]
        if start_date:
            sql += " AND e.entry_date >= ?"
            params.append(start_date)
        if end_date:
            sql += " AND e.entry_date <= ?"
            params.append(end_date)
        row = self._conn.execute(sql, params).fetchone()
        return row["cnt"]

    def update_final_text(
        self, entry_id: int, final_text: str, word_count: int, chunk_count: int
    ) -> Entry | None:
        self._conn.execute(
            "UPDATE entries SET final_text = ?, word_count = ?, chunk_count = ?,"
            " updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?",
            (final_text, word_count, chunk_count, entry_id),
        )
        self._conn.commit()
        log.info("Updated final_text for entry %d", entry_id)
        return self.get_entry(entry_id)

    def update_entry_date(self, entry_id: int, entry_date: str) -> Entry | None:
        self._conn.execute(
            "UPDATE entries SET entry_date = ?,"
            " updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?",
            (entry_date, entry_id),
        )
        self._conn.commit()
        log.info("Updated entry_date for entry %d to %s", entry_id, entry_date)
        return self.get_entry(entry_id)

    def delete_entry(self, entry_id: int) -> bool:
        """Delete an entry and all cascading rows. Returns True if a row was deleted."""
        cursor = self._conn.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
        self._conn.commit()
        deleted = cursor.rowcount > 0
        if deleted:
            log.info("Deleted entry %d", entry_id)
        return deleted

    def add_entry_page(
        self, entry_id: int, page_number: int, raw_text: str, source_file_id: int | None = None
    ) -> None:
        self._conn.execute(
            "INSERT INTO entry_pages (entry_id, page_number, raw_text, source_file_id)"
            " VALUES (?, ?, ?, ?)",
            (entry_id, page_number, raw_text, source_file_id),
        )
        self._conn.commit()
        log.info("Added page %d to entry %d", page_number, entry_id)

    def get_entry_pages(self, entry_id: int) -> list[EntryPage]:
        rows = self._conn.execute(
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
        with self._conn:
            self._conn.executemany(
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
        rows = self._conn.execute(
            "SELECT char_start, char_end FROM entry_uncertain_spans "
            "WHERE entry_id = ? ORDER BY char_start ASC",
            (entry_id,),
        ).fetchall()
        return [(int(r["char_start"]), int(r["char_end"])) for r in rows]

    def get_uncertain_span_count(self, entry_id: int) -> int:
        """Return the number of uncertain spans for an entry."""
        row = self._conn.execute(
            "SELECT COUNT(*) as cnt FROM entry_uncertain_spans WHERE entry_id = ?",
            (entry_id,),
        ).fetchone()
        return row["cnt"]

    def get_statistics(
        self, start_date: str | None = None, end_date: str | None = None
    ) -> Statistics:
        query = """
            SELECT
                COUNT(*) as total_entries,
                MIN(entry_date) as date_range_start,
                MAX(entry_date) as date_range_end,
                COALESCE(SUM(word_count), 0) as total_words,
                COALESCE(AVG(word_count), 0) as avg_words_per_entry
            FROM entries WHERE 1=1
        """
        params: list[str] = []
        if start_date:
            query += " AND entry_date >= ?"
            params.append(start_date)
        if end_date:
            query += " AND entry_date <= ?"
            params.append(end_date)
        row = self._conn.execute(query, params).fetchone()

        # Calculate entries per month
        total_entries = row["total_entries"]
        entries_per_month = 0.0
        if total_entries > 0 and row["date_range_start"] and row["date_range_end"]:
            months_query = """
                SELECT COUNT(DISTINCT strftime('%Y-%m', entry_date)) as months
                FROM entries WHERE 1=1
            """
            months_params: list[str] = []
            if start_date:
                months_query += " AND entry_date >= ?"
                months_params.append(start_date)
            if end_date:
                months_query += " AND entry_date <= ?"
                months_params.append(end_date)
            months_row = self._conn.execute(months_query, months_params).fetchone()
            months = months_row["months"] or 1
            entries_per_month = total_entries / months

        return Statistics(
            total_entries=total_entries,
            date_range_start=row["date_range_start"],
            date_range_end=row["date_range_end"],
            total_words=row["total_words"],
            avg_words_per_entry=row["avg_words_per_entry"],
            entries_per_month=entries_per_month,
        )

    def add_people(self, entry_id: int, names: list[str]) -> None:
        people_sql = (
            "INSERT OR IGNORE INTO people (name, first_seen)"
            " VALUES (?, (SELECT entry_date FROM entries WHERE id = ?))"
        )
        for name in names:
            self._conn.execute(people_sql, (name, entry_id))
            person_id = self._conn.execute(
                "SELECT id FROM people WHERE name = ?", (name,)
            ).fetchone()["id"]
            self._conn.execute(
                "INSERT OR IGNORE INTO entry_people (entry_id, person_id) VALUES (?, ?)",
                (entry_id, person_id),
            )
        self._conn.commit()

    def add_places(self, entry_id: int, names: list[str]) -> None:
        places_sql = (
            "INSERT OR IGNORE INTO places (name, first_seen)"
            " VALUES (?, (SELECT entry_date FROM entries WHERE id = ?))"
        )
        for name in names:
            self._conn.execute(places_sql, (name, entry_id))
            place_id = self._conn.execute(
                "SELECT id FROM places WHERE name = ?", (name,)
            ).fetchone()["id"]
            self._conn.execute(
                "INSERT OR IGNORE INTO entry_places (entry_id, place_id) VALUES (?, ?)",
                (entry_id, place_id),
            )
        self._conn.commit()

    def add_tags(self, entry_id: int, tags: list[str]) -> None:
        for tag in tags:
            self._conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (tag,))
            tag_id = self._conn.execute(
                "SELECT id FROM tags WHERE name = ?", (tag,)
            ).fetchone()["id"]
            self._conn.execute(
                "INSERT OR IGNORE INTO entry_tags (entry_id, tag_id) VALUES (?, ?)",
                (entry_id, tag_id),
            )
        self._conn.commit()

    def add_mood_score(
        self, entry_id: int, dimension: str, score: float, confidence: float | None = None
    ) -> None:
        self._conn.execute(
            "INSERT INTO mood_scores (entry_id, dimension, score, confidence) VALUES (?, ?, ?, ?)",
            (entry_id, dimension, score, confidence),
        )
        self._conn.commit()

    def replace_mood_scores(
        self,
        entry_id: int,
        scores: list[tuple[str, float, float | None]],
    ) -> None:
        """Idempotently write a set of mood scores for a single entry.

        `scores` is a list of `(dimension, score, confidence)` tuples.
        Delete-then-insert in a single transaction so a re-score is
        atomic — concurrent readers never see a partially-updated
        set. Intended for ingestion and the backfill CLI.

        Dimensions NOT included in `scores` but already present in
        the DB for this entry are **preserved** — callers can pass a
        subset to rewrite only some facets. The service layer passes
        the full current dimension set; backfill can target a subset
        if only some are stale.
        """
        if not scores:
            return
        dim_names = [s[0] for s in scores]
        placeholders = ",".join("?" for _ in dim_names)
        with self._conn:
            self._conn.execute(
                f"DELETE FROM mood_scores WHERE entry_id = ? "
                f"AND dimension IN ({placeholders})",
                (entry_id, *dim_names),
            )
            self._conn.executemany(
                "INSERT INTO mood_scores "
                "(entry_id, dimension, score, confidence) "
                "VALUES (?, ?, ?, ?)",
                [
                    (entry_id, name, score, confidence)
                    for name, score, confidence in scores
                ],
            )
        log.debug(
            "Replaced %d mood scores for entry %d", len(scores), entry_id
        )

    def get_mood_scores(self, entry_id: int) -> list[MoodScore]:
        """Return every mood score for a single entry, in dimension
        order. Used by `replace_mood_scores` callers for verification
        and by the backfill's `--stale-only` gate."""
        rows = self._conn.execute(
            "SELECT entry_id, dimension, score, confidence "
            "FROM mood_scores WHERE entry_id = ? ORDER BY dimension",
            (entry_id,),
        ).fetchall()
        return [
            MoodScore(
                entry_id=row["entry_id"],
                dimension=row["dimension"],
                score=row["score"],
                confidence=row["confidence"],
            )
            for row in rows
        ]

    def get_entries_missing_mood_scores(
        self, dimension_names: list[str]
    ) -> list[int]:
        """Return entry ids that are missing at least one of the
        listed dimensions in `mood_scores`. Drives the backfill
        CLI's `--stale-only` mode: we re-score every entry that
        doesn't already have a value for every current facet.

        Empty `dimension_names` returns an empty list — there's
        nothing to check against. An empty corpus also returns
        empty.
        """
        if not dimension_names:
            return []
        placeholders = ",".join("?" for _ in dimension_names)
        rows = self._conn.execute(
            f"""
            SELECT e.id AS id
            FROM entries e
            WHERE (
                SELECT COUNT(DISTINCT m.dimension)
                FROM mood_scores m
                WHERE m.entry_id = e.id
                  AND m.dimension IN ({placeholders})
            ) < ?
            ORDER BY e.entry_date ASC, e.id ASC
            """,
            (*dimension_names, len(dimension_names)),
        ).fetchall()
        return [int(r["id"]) for r in rows]

    def prune_retired_mood_scores(
        self, current_names: list[str]
    ) -> int:
        """Delete `mood_scores` rows whose dimension is NOT in
        `current_names` — used by the backfill CLI's
        `--prune-retired` flag. Returns the number of rows deleted.

        An empty `current_names` list is treated as "prune
        everything" (every stored dimension is, by definition,
        not in an empty current set). Callers should only pass
        an empty list if they really want to wipe `mood_scores`
        entirely.
        """
        if not current_names:
            cursor = self._conn.execute("DELETE FROM mood_scores")
            self._conn.commit()
            log.info(
                "Pruned ALL %d mood_scores rows (empty current set)",
                cursor.rowcount,
            )
            return cursor.rowcount
        placeholders = ",".join("?" for _ in current_names)
        cursor = self._conn.execute(
            f"DELETE FROM mood_scores "
            f"WHERE dimension NOT IN ({placeholders})",
            tuple(current_names),
        )
        self._conn.commit()
        log.info(
            "Pruned %d mood_scores rows with retired dimensions",
            cursor.rowcount,
        )
        return cursor.rowcount

    def get_mood_trends(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        granularity: str = "week",
    ) -> list[MoodTrend]:
        # Delegates bin-start computation to `_bin_start_sql` so the
        # supported granularity set and the SQL expression stay in
        # sync with `get_writing_frequency`. `day` is still supported
        # here for the LLM-facing MCP tool — the dashboard uses
        # week/month/quarter/year only. `period` is returned as a
        # canonical ISO date (e.g. "2026-03-02" for a week), not a
        # `%Y-%W`-style format string, so the webapp can plot it on
        # the same axis as the writing-frequency series.
        period_expr = _bin_start_sql(granularity, column="e.entry_date")

        query = f"""
            SELECT
                {period_expr} as period,
                m.dimension,
                AVG(m.score) as avg_score,
                COUNT(DISTINCT e.id) as entry_count
            FROM mood_scores m
            JOIN entries e ON e.id = m.entry_id
            WHERE 1=1
        """
        params: list[str] = []
        if start_date:
            query += " AND e.entry_date >= ?"
            params.append(start_date)
        if end_date:
            query += " AND e.entry_date <= ?"
            params.append(end_date)
        query += f" GROUP BY {period_expr}, m.dimension ORDER BY period"
        rows = self._conn.execute(query, params).fetchall()
        return [
            MoodTrend(
                period=row["period"],
                dimension=row["dimension"],
                avg_score=row["avg_score"],
                entry_count=row["entry_count"],
            )
            for row in rows
        ]

    def count_entries(
        self, start_date: str | None = None, end_date: str | None = None
    ) -> int:
        query = "SELECT COUNT(*) as cnt FROM entries WHERE 1=1"
        params: list[str] = []
        if start_date:
            query += " AND entry_date >= ?"
            params.append(start_date)
        if end_date:
            query += " AND entry_date <= ?"
            params.append(end_date)
        row = self._conn.execute(query, params).fetchone()
        return row["cnt"]

    def get_page_count(self, entry_id: int) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) as cnt FROM entry_pages WHERE entry_id = ?",
            (entry_id,),
        ).fetchone()
        return row["cnt"]

    # Fixed set of tables surfaced in `get_ingestion_stats().row_counts`.
    # Kept as an explicit contract so `/health` output is stable across
    # schema additions — when a new migration adds a table, add it here
    # deliberately rather than having it silently appear.
    _HEALTH_ROW_COUNT_TABLES: tuple[str, ...] = (
        "entries",
        "entry_pages",
        "entry_chunks",
        "mood_scores",
        "source_files",
        "entities",
        "entity_aliases",
        "entity_mentions",
        "entity_relationships",
    )

    def get_ingestion_stats(self, now: datetime) -> IngestionStats:
        """Aggregate corpus stats for the `/health` endpoint.

        `now` is injected rather than read from `datetime.now()` so
        tests can control the clock and assert on the 7d/30d windows
        deterministically. Windows are computed on `entry_date`, which
        is stored as a `YYYY-MM-DD` string — date arithmetic in Python
        is simpler and more portable than SQLite `date('now', '-7 days')`
        would be.
        """
        from datetime import timedelta

        cutoff_7d = (now.date() - timedelta(days=7)).isoformat()
        cutoff_30d = (now.date() - timedelta(days=30)).isoformat()

        total_row = self._conn.execute(
            "SELECT COUNT(*) AS cnt, "
            "COALESCE(AVG(word_count), 0.0) AS avg_words, "
            "COALESCE(AVG(chunk_count), 0.0) AS avg_chunks, "
            "MAX(created_at) AS last_ingest "
            "FROM entries"
        ).fetchone()

        last_7d = self._conn.execute(
            "SELECT COUNT(*) AS cnt FROM entries WHERE entry_date >= ?",
            (cutoff_7d,),
        ).fetchone()["cnt"]
        last_30d = self._conn.execute(
            "SELECT COUNT(*) AS cnt FROM entries WHERE entry_date >= ?",
            (cutoff_30d,),
        ).fetchone()["cnt"]

        by_source: dict[str, int] = {}
        for row in self._conn.execute(
            "SELECT source_type, COUNT(*) AS cnt FROM entries "
            "GROUP BY source_type"
        ).fetchall():
            by_source[row["source_type"]] = row["cnt"]

        total_chunks_row = self._conn.execute(
            "SELECT COALESCE(SUM(chunk_count), 0) AS total FROM entries"
        ).fetchone()
        total_chunks = int(total_chunks_row["total"] or 0)

        row_counts: dict[str, int] = {}
        for table in self._HEALTH_ROW_COUNT_TABLES:
            # Table names come from a hardcoded tuple — not user input —
            # so interpolation here is safe. SQLite rejects placeholders
            # for identifiers, so a PRAGMA or prepared statement would
            # not work even if we wanted one.
            cnt_row = self._conn.execute(
                f"SELECT COUNT(*) AS cnt FROM {table}"
            ).fetchone()
            row_counts[table] = int(cnt_row["cnt"])

        return IngestionStats(
            total_entries=int(total_row["cnt"]),
            entries_last_7d=int(last_7d),
            entries_last_30d=int(last_30d),
            by_source_type=by_source,
            avg_words_per_entry=round(float(total_row["avg_words"]), 2),
            avg_chunks_per_entry=round(float(total_row["avg_chunks"]), 2),
            last_ingestion_at=total_row["last_ingest"],
            total_chunks=total_chunks,
            row_counts=row_counts,
        )

    def get_writing_frequency(
        self,
        start_date: str | None,
        end_date: str | None,
        granularity: str,
    ) -> list[WritingFrequencyBin]:
        """Aggregate entries per time bucket for the dashboard charts.

        Returns one `WritingFrequencyBin` per non-empty bucket in
        the requested range, sorted by `bin_start` ascending. Empty
        buckets are omitted — callers that need a dense series
        (e.g. a continuous line chart over months with no entries)
        fill gaps client-side.

        `granularity` must be one of `week`, `month`, `quarter`,
        `year`. Invalid values raise `ValueError` before any SQL
        runs so the endpoint can surface a clean 400.

        **Bin start semantics** (canonical dates the frontend plots):

        - `week`:    the Monday of the ISO week. SQLite's
          `strftime('%w', ...)` returns 0 for Sunday..6 for
          Saturday, so we offset by `(weekday + 6) % 7` days to
          land on the preceding Monday.
        - `month`:   the 1st of the month.
        - `quarter`: the 1st of Jan/Apr/Jul/Oct. Computed
          explicitly because SQLite has no `%Q` format.
        - `year`:    January 1st of the year.
        """
        if granularity not in _SUPPORTED_BINS:
            raise ValueError(
                f"Unsupported granularity {granularity!r}; "
                f"must be one of {_SUPPORTED_BINS}"
            )

        bin_expr = _bin_start_sql(granularity, column="entry_date")
        sql = f"""
            SELECT
                {bin_expr} AS bin_start,
                COUNT(*) AS entry_count,
                COALESCE(SUM(word_count), 0) AS total_words
            FROM entries
            WHERE 1=1
        """
        params: list[str] = []
        if start_date:
            sql += " AND entry_date >= ?"
            params.append(start_date)
        if end_date:
            sql += " AND entry_date <= ?"
            params.append(end_date)
        sql += " GROUP BY bin_start ORDER BY bin_start ASC"

        rows = self._conn.execute(sql, params).fetchall()
        return [
            WritingFrequencyBin(
                bin_start=row["bin_start"],
                entry_count=int(row["entry_count"]),
                total_words=int(row["total_words"]),
            )
            for row in rows
        ]

    def get_topic_frequency(
        self, topic: str, start_date: str | None = None, end_date: str | None = None
    ) -> TopicFrequency:
        entries = self.search_text(topic, start_date, end_date)
        return TopicFrequency(topic=topic, count=len(entries), entries=entries)
