"""Corpus-level descriptive statistics.

Owns the read-only aggregations that summarise the journal as a whole
or a single entry:

- ``get_statistics`` — totals, dates, word counts, entries-per-month.
- ``count_entries`` — entry count in a date range.
- ``get_calendar_heatmap`` — daily entry counts + word totals.
- ``get_word_count_distribution`` — histogram + summary stats.
- ``get_ingestion_stats`` — corpus + per-table row counts for
  the ``/health`` endpoint. Uses the module-level
  ``_HEALTH_ROW_COUNT_TABLES`` whitelist so new migrations don't
  silently widen the health surface.
- ``get_entity_mention_count`` — single-entry mention count.
- ``get_page_count_for_entry``? lives in ``pages``.

Cross-axis analytics (entity / topic / mood joins, time-bucketed
trends) live in ``analytics.py``. The split keeps both modules under
the 500-line readable-context target.

Methods stay bound to ``self`` so they keep using ``self._conn``.
"""

from datetime import datetime

from journal.models import (
    CalendarDay,
    IngestionStats,
    Statistics,
    WordCountBucket,
    WordCountStats,
)

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


class _StatsMixin:
    """Stats methods on SQLiteEntryRepository."""

    def get_statistics(
        self, start_date: str | None = None, end_date: str | None = None,
        user_id: int | None = None,
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
        row = self._conn.execute(query, params).fetchone()

        # Calculate entries per month
        total_entries = row["total_entries"]
        entries_per_month = 0.0
        if total_entries > 0 and row["date_range_start"] and row["date_range_end"]:
            months_query = """
                SELECT COUNT(DISTINCT strftime('%Y-%m', entry_date)) as months
                FROM entries WHERE 1=1
            """
            months_params: list[str | int] = []
            if user_id is not None:
                months_query += " AND user_id = ?"
                months_params.append(user_id)
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

    def count_entries(
        self, start_date: str | None = None, end_date: str | None = None,
        user_id: int | None = None,
    ) -> int:
        query = "SELECT COUNT(*) as cnt FROM entries WHERE 1=1"
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
        row = self._conn.execute(query, params).fetchone()
        return row["cnt"]

    def get_calendar_heatmap(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        user_id: int | None = None,
    ) -> list[CalendarDay]:
        """Daily entry counts and total words for a calendar heatmap."""
        sql = """
            SELECT
                entry_date,
                COUNT(*) AS entry_count,
                COALESCE(SUM(word_count), 0) AS total_words
            FROM entries
            WHERE 1=1
        """
        params: list[str | int] = []
        if user_id is not None:
            sql += " AND user_id = ?"
            params.append(user_id)
        if start_date:
            sql += " AND entry_date >= ?"
            params.append(start_date)
        if end_date:
            sql += " AND entry_date <= ?"
            params.append(end_date)
        sql += " GROUP BY entry_date ORDER BY entry_date"
        rows = self._conn.execute(sql, params).fetchall()
        return [
            CalendarDay(
                date=row["entry_date"],
                entry_count=int(row["entry_count"]),
                total_words=int(row["total_words"]),
            )
            for row in rows
        ]

    def get_word_count_distribution(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        bucket_size: int = 100,
        user_id: int | None = None,
    ) -> tuple[list[WordCountBucket], WordCountStats]:
        """Histogram of entry word counts plus summary statistics.

        Returns ``(buckets, stats)``. Each bucket covers a half-open
        range ``[range_start, range_end)``.
        """
        # Histogram buckets.
        sql = """
            SELECT
                (word_count / ?) * ? AS range_start,
                COUNT(*) AS count
            FROM entries
            WHERE 1=1
        """
        params: list[str | int] = [bucket_size, bucket_size]
        if user_id is not None:
            sql += " AND user_id = ?"
            params.append(user_id)
        if start_date:
            sql += " AND entry_date >= ?"
            params.append(start_date)
        if end_date:
            sql += " AND entry_date <= ?"
            params.append(end_date)
        sql += " GROUP BY range_start ORDER BY range_start"
        rows = self._conn.execute(sql, params).fetchall()
        buckets = [
            WordCountBucket(
                range_start=int(row["range_start"]),
                range_end=int(row["range_start"]) + bucket_size,
                count=int(row["count"]),
            )
            for row in rows
        ]

        # Summary statistics.
        stats_sql = """
            SELECT
                COALESCE(MIN(word_count), 0) AS min_wc,
                COALESCE(MAX(word_count), 0) AS max_wc,
                COALESCE(AVG(word_count), 0) AS avg_wc,
                COUNT(*) AS total_entries
            FROM entries
            WHERE 1=1
        """
        stats_params: list[str | int] = []
        if user_id is not None:
            stats_sql += " AND user_id = ?"
            stats_params.append(user_id)
        if start_date:
            stats_sql += " AND entry_date >= ?"
            stats_params.append(start_date)
        if end_date:
            stats_sql += " AND entry_date <= ?"
            stats_params.append(end_date)
        stats_row = self._conn.execute(stats_sql, stats_params).fetchone()

        # Median via SQL: order by word_count and take the middle value(s).
        median_sql = """
            SELECT word_count FROM entries WHERE 1=1
        """
        median_params: list[str | int] = []
        if user_id is not None:
            median_sql += " AND user_id = ?"
            median_params.append(user_id)
        if start_date:
            median_sql += " AND entry_date >= ?"
            median_params.append(start_date)
        if end_date:
            median_sql += " AND entry_date <= ?"
            median_params.append(end_date)
        median_sql += " ORDER BY word_count"
        all_wc = self._conn.execute(median_sql, median_params).fetchall()
        if all_wc:
            n = len(all_wc)
            mid = n // 2
            if n % 2 == 1:
                median = float(all_wc[mid]["word_count"])
            else:
                median = (all_wc[mid - 1]["word_count"] + all_wc[mid]["word_count"]) / 2.0
        else:
            median = 0.0

        stats = WordCountStats(
            min=int(stats_row["min_wc"]),
            max=int(stats_row["max_wc"]),
            avg=round(float(stats_row["avg_wc"]), 1),
            median=round(median, 1),
            total_entries=int(stats_row["total_entries"]),
        )
        return buckets, stats

    def get_entity_mention_count(self, entry_id: int) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) as cnt FROM entity_mentions WHERE entry_id = ?",
            (entry_id,),
        ).fetchone()
        return row["cnt"]

    def get_ingestion_stats(self, now: datetime, user_id: int | None = None) -> IngestionStats:
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

        if user_id is not None:
            total_row = self._conn.execute(
                "SELECT COUNT(*) AS cnt, "
                "COALESCE(AVG(word_count), 0.0) AS avg_words, "
                "COALESCE(AVG(chunk_count), 0.0) AS avg_chunks, "
                "MAX(created_at) AS last_ingest "
                "FROM entries WHERE user_id = ?",
                (user_id,),
            ).fetchone()

            last_7d = self._conn.execute(
                "SELECT COUNT(*) AS cnt FROM entries WHERE user_id = ? AND entry_date >= ?",
                (user_id, cutoff_7d),
            ).fetchone()["cnt"]
            last_30d = self._conn.execute(
                "SELECT COUNT(*) AS cnt FROM entries WHERE user_id = ? AND entry_date >= ?",
                (user_id, cutoff_30d),
            ).fetchone()["cnt"]

            by_source: dict[str, int] = {}
            for row in self._conn.execute(
                "SELECT source_type, COUNT(*) AS cnt FROM entries "
                "WHERE user_id = ? GROUP BY source_type",
                (user_id,),
            ).fetchall():
                by_source[row["source_type"]] = row["cnt"]
        else:
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

            by_source = {}
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
        for table in _HEALTH_ROW_COUNT_TABLES:
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
