"""Cross-axis analytics: entity / topic / mood joins + trends.

Owns the dashboard-feeding aggregations that join multiple tables or
slice results over a time bucket:

- ``get_entity_distribution`` — top-N entities by mention count.
- ``get_entity_trends`` — top-N entities, then per-bucket counts.
- ``get_topic_frequency`` — FTS-driven topic count (calls
  ``self.search_text(...)`` from ``_SearchMixin``; the cross-mixin
  call resolves through MRO).
- ``get_writing_frequency`` — entries per time bucket.
- ``get_mood_entity_correlation`` — avg mood-by-entity vs. overall.

Granularity-bucketed methods use ``_bin_start_sql`` from
``protocol``. Methods stay bound to ``self`` so they keep using
``self._conn``.
"""

from journal.db.repository.protocol import _SUPPORTED_BINS, _bin_start_sql
from journal.models import (
    EntityDistributionBin,
    EntityTrendBin,
    MoodEntityCorrelation,
    TopicFrequency,
    WritingFrequencyBin,
)


class _AnalyticsMixin:
    """Analytics methods on SQLiteEntryRepository."""

    def get_entity_distribution(
        self,
        entity_type: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = 50,
        user_id: int | None = None,
    ) -> list[EntityDistributionBin]:
        """Return mention counts grouped by entity, filtered by type and date."""
        sql = """
            SELECT
                en.canonical_name,
                en.entity_type,
                COUNT(m.id) AS mention_count
            FROM entity_mentions m
            JOIN entries e  ON e.id  = m.entry_id
            JOIN entities en ON en.id = m.entity_id
            WHERE en.is_quarantined = 0
        """
        params: list[str | int] = []
        if user_id is not None:
            sql += " AND e.user_id = ?"
            params.append(user_id)
        if entity_type is not None:
            sql += " AND en.entity_type = ?"
            params.append(entity_type)
        if start_date:
            sql += " AND e.entry_date >= ?"
            params.append(start_date)
        if end_date:
            sql += " AND e.entry_date <= ?"
            params.append(end_date)
        sql += " GROUP BY en.id ORDER BY mention_count DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [
            EntityDistributionBin(
                canonical_name=row["canonical_name"],
                entity_type=row["entity_type"],
                mention_count=int(row["mention_count"]),
            )
            for row in rows
        ]

    def get_entity_trends(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        granularity: str = "month",
        entity_type: str | None = None,
        limit: int = 8,
        user_id: int | None = None,
    ) -> tuple[list[str], list[EntityTrendBin]]:
        """Entity mention counts over time, bucketed by granularity.

        Returns ``(entity_names, bins)`` where ``entity_names`` is the
        ordered list of top-N entities by total mentions and ``bins``
        contains per-period counts for those entities only.
        """
        if granularity not in _SUPPORTED_BINS:
            raise ValueError(
                f"Unsupported granularity {granularity!r}; "
                f"must be one of {_SUPPORTED_BINS}"
            )

        # Step 1: find the top N entities by total mention count.
        top_sql = """
            SELECT en.canonical_name, COUNT(m.id) AS total
            FROM entity_mentions m
            JOIN entries e  ON e.id  = m.entry_id
            JOIN entities en ON en.id = m.entity_id
            WHERE en.is_quarantined = 0
        """
        params: list[str | int] = []
        if user_id is not None:
            top_sql += " AND e.user_id = ?"
            params.append(user_id)
        if entity_type is not None:
            top_sql += " AND en.entity_type = ?"
            params.append(entity_type)
        if start_date:
            top_sql += " AND e.entry_date >= ?"
            params.append(start_date)
        if end_date:
            top_sql += " AND e.entry_date <= ?"
            params.append(end_date)
        top_sql += " GROUP BY en.id ORDER BY total DESC LIMIT ?"
        params.append(limit)
        top_rows = self._conn.execute(top_sql, params).fetchall()
        entity_names = [row["canonical_name"] for row in top_rows]

        if not entity_names:
            return entity_names, []

        # Step 2: per-bin counts for the top entities.
        bin_expr = _bin_start_sql(granularity, column="e.entry_date")
        placeholders = ",".join("?" for _ in entity_names)
        bin_sql = f"""
            SELECT
                {bin_expr} AS period,
                en.canonical_name AS entity,
                COUNT(m.id) AS mention_count
            FROM entity_mentions m
            JOIN entries e  ON e.id  = m.entry_id
            JOIN entities en ON en.id = m.entity_id
            WHERE en.is_quarantined = 0
              AND en.canonical_name IN ({placeholders})
        """
        bin_params: list[str | int] = list(entity_names)
        if user_id is not None:
            bin_sql += " AND e.user_id = ?"
            bin_params.append(user_id)
        if entity_type is not None:
            bin_sql += " AND en.entity_type = ?"
            bin_params.append(entity_type)
        if start_date:
            bin_sql += " AND e.entry_date >= ?"
            bin_params.append(start_date)
        if end_date:
            bin_sql += " AND e.entry_date <= ?"
            bin_params.append(end_date)
        bin_sql += f" GROUP BY {bin_expr}, en.canonical_name ORDER BY period, entity"
        bin_rows = self._conn.execute(bin_sql, bin_params).fetchall()
        bins = [
            EntityTrendBin(
                period=row["period"],
                entity=row["entity"],
                mention_count=int(row["mention_count"]),
            )
            for row in bin_rows
        ]
        return entity_names, bins

    def get_topic_frequency(
        self, topic: str, start_date: str | None = None, end_date: str | None = None,
        user_id: int | None = None,
    ) -> TopicFrequency:
        entries = self.search_text(topic, start_date, end_date, user_id=user_id)
        return TopicFrequency(topic=topic, count=len(entries), entries=entries)

    def get_writing_frequency(
        self,
        start_date: str | None,
        end_date: str | None,
        granularity: str,
        user_id: int | None = None,
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

    def get_mood_entity_correlation(
        self,
        dimension: str,
        start_date: str | None = None,
        end_date: str | None = None,
        entity_type: str | None = None,
        limit: int = 10,
        user_id: int | None = None,
    ) -> tuple[float, list[MoodEntityCorrelation]]:
        """Average mood when specific entities are mentioned vs overall.

        Returns ``(overall_avg, items)`` where ``overall_avg`` is the
        global average score for the dimension across all entries and
        ``items`` lists per-entity averages sorted by entry_count desc.
        """
        # Overall average for this dimension.
        overall_sql = """
            SELECT COALESCE(AVG(m.score), 0) AS overall_avg
            FROM mood_scores m
            JOIN entries e ON e.id = m.entry_id
            WHERE m.dimension = ?
        """
        overall_params: list[str | int] = [dimension]
        if user_id is not None:
            overall_sql += " AND e.user_id = ?"
            overall_params.append(user_id)
        if start_date:
            overall_sql += " AND e.entry_date >= ?"
            overall_params.append(start_date)
        if end_date:
            overall_sql += " AND e.entry_date <= ?"
            overall_params.append(end_date)
        overall_row = self._conn.execute(overall_sql, overall_params).fetchone()
        overall_avg = round(float(overall_row["overall_avg"]), 4)

        # Per-entity averages.
        sql = """
            SELECT
                en.canonical_name AS entity,
                en.entity_type,
                AVG(ms.score)           AS avg_score,
                COUNT(DISTINCT e.id)    AS entry_count
            FROM entity_mentions em
            JOIN entries  e  ON e.id  = em.entry_id
            JOIN entities en ON en.id = em.entity_id
            JOIN mood_scores ms ON ms.entry_id = e.id AND ms.dimension = ?
            WHERE en.is_quarantined = 0
        """
        params: list[str | int] = [dimension]
        if user_id is not None:
            sql += " AND e.user_id = ?"
            params.append(user_id)
        if entity_type is not None:
            sql += " AND en.entity_type = ?"
            params.append(entity_type)
        if start_date:
            sql += " AND e.entry_date >= ?"
            params.append(start_date)
        if end_date:
            sql += " AND e.entry_date <= ?"
            params.append(end_date)
        sql += " GROUP BY en.id ORDER BY entry_count DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        items = [
            MoodEntityCorrelation(
                entity=row["entity"],
                entity_type=row["entity_type"],
                avg_score=round(float(row["avg_score"]), 4),
                entry_count=int(row["entry_count"]),
            )
            for row in rows
        ]
        return overall_avg, items
