"""Repository interface and SQLite implementation."""

import logging
import sqlite3
from typing import Protocol, runtime_checkable

from journal.models import Entry, MoodTrend, Statistics, TopicFrequency

log = logging.getLogger(__name__)


@runtime_checkable
class EntryRepository(Protocol):
    def create_entry(
        self, entry_date: str, source_type: str, raw_text: str, word_count: int
    ) -> Entry: ...

    def get_entry(self, entry_id: int) -> Entry | None: ...

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

    def get_statistics(
        self, start_date: str | None = None, end_date: str | None = None
    ) -> Statistics: ...

    def add_people(self, entry_id: int, names: list[str]) -> None: ...

    def add_places(self, entry_id: int, names: list[str]) -> None: ...

    def add_tags(self, entry_id: int, tags: list[str]) -> None: ...

    def add_mood_score(
        self, entry_id: int, dimension: str, score: float, confidence: float | None = None
    ) -> None: ...

    def get_mood_trends(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        granularity: str = "week",
    ) -> list[MoodTrend]: ...

    def get_topic_frequency(
        self, topic: str, start_date: str | None = None, end_date: str | None = None
    ) -> TopicFrequency: ...


def _row_to_entry(row: sqlite3.Row) -> Entry:
    return Entry(
        id=row["id"],
        entry_date=row["entry_date"],
        source_type=row["source_type"],
        raw_text=row["raw_text"],
        word_count=row["word_count"],
        language=row["language"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class SQLiteEntryRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def create_entry(
        self, entry_date: str, source_type: str, raw_text: str, word_count: int
    ) -> Entry:
        sql = (
            "INSERT INTO entries (entry_date, source_type, raw_text, word_count)"
            " VALUES (?, ?, ?, ?)"
        )
        cursor = self._conn.execute(sql, (entry_date, source_type, raw_text, word_count))
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

    def get_mood_trends(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        granularity: str = "week",
    ) -> list[MoodTrend]:
        if granularity == "day":
            period_expr = "e.entry_date"
        elif granularity == "month":
            period_expr = "strftime('%Y-%m', e.entry_date)"
        else:  # week
            period_expr = "strftime('%Y-W%W', e.entry_date)"

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

    def get_topic_frequency(
        self, topic: str, start_date: str | None = None, end_date: str | None = None
    ) -> TopicFrequency:
        entries = self.search_text(topic, start_date, end_date)
        return TopicFrequency(topic=topic, count=len(entries), entries=entries)
