"""Mood score CRUD + trends + drilldowns + dimension hygiene.

Owns the ``mood_scores`` table operations:

- Per-entry CRUD: ``add_mood_score``, ``replace_mood_scores``,
  ``get_mood_scores``.
- Backfill helpers: ``get_entries_missing_mood_scores`` (staleness
  detection), ``prune_retired_mood_scores`` (delete obsolete dims).
- Time-bucketed analytics: ``get_mood_trends`` (aggregate by
  granularity), ``get_mood_drilldown`` (per-entry within a window).

Methods route through ``self._conn()`` so each call gets the
appropriate connection — thread-local on the factory path, the
shared connection on the legacy path.
"""

import logging

from journal.db.repository.protocol import _bin_start_sql
from journal.models import MoodDrilldownEntry, MoodScore, MoodTrend

log = logging.getLogger(__name__)


class _MoodMixin:
    """Mood methods on SQLiteEntryRepository."""

    def add_mood_score(
        self, entry_id: int, dimension: str, score: float,
        confidence: float | None = None, rationale: str | None = None,
    ) -> None:
        conn = self._conn()
        with conn:
            conn.execute(
                "INSERT INTO mood_scores (entry_id, dimension, score, confidence, rationale)"
                " VALUES (?, ?, ?, ?, ?)",
                (entry_id, dimension, score, confidence, rationale),
            )

    def replace_mood_scores(
        self,
        entry_id: int,
        scores: list[tuple[str, float, float | None, str | None]],
    ) -> None:
        """Idempotently write a set of mood scores for a single entry.

        `scores` is a list of `(dimension, score, confidence, rationale)`
        tuples. Delete-then-insert in a single transaction so a re-score
        is atomic — concurrent readers never see a partially-updated
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
        conn = self._conn()
        with conn:
            conn.execute(
                f"DELETE FROM mood_scores WHERE entry_id = ? "
                f"AND dimension IN ({placeholders})",
                (entry_id, *dim_names),
            )
            conn.executemany(
                "INSERT INTO mood_scores "
                "(entry_id, dimension, score, confidence, rationale) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (entry_id, name, score, confidence, rationale)
                    for name, score, confidence, rationale in scores
                ],
            )
        log.debug(
            "Replaced %d mood scores for entry %d", len(scores), entry_id
        )

    def get_mood_scores(self, entry_id: int) -> list[MoodScore]:
        """Return every mood score for a single entry, in dimension
        order. Used by `replace_mood_scores` callers for verification
        and by the backfill's `--stale-only` gate."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT entry_id, dimension, score, confidence, rationale "
            "FROM mood_scores WHERE entry_id = ? ORDER BY dimension",
            (entry_id,),
        ).fetchall()
        return [
            MoodScore(
                entry_id=row["entry_id"],
                dimension=row["dimension"],
                score=row["score"],
                confidence=row["confidence"],
                rationale=row["rationale"],
            )
            for row in rows
        ]

    def get_entries_missing_mood_scores(
        self, dimension_names: list[str], user_id: int | None = None,
    ) -> list[int]:
        """Return entry ids that need mood (re-)scoring.

        An entry is considered stale when **either** condition holds:

        1. It is missing at least one of the listed dimensions in
           ``mood_scores`` (the original check).
        2. Its ``updated_at`` timestamp is newer than the most recent
           ``mood_scores.created_at`` for that entry — meaning the
           text was edited after the last scoring run.

        This drives the backfill CLI's ``--stale-only`` mode: we
        (re-)score every entry that doesn't already have up-to-date
        values for every current facet.

        Empty `dimension_names` returns an empty list — there's
        nothing to check against. An empty corpus also returns
        empty.
        """
        if not dimension_names:
            return []
        placeholders = ",".join("?" for _ in dimension_names)
        user_filter = ""
        user_params: tuple[int, ...] = ()
        if user_id is not None:
            user_filter = " AND e.user_id = ?"
            user_params = (user_id,)
        conn = self._conn()
        rows = conn.execute(
            f"""
            SELECT e.id AS id
            FROM entries e
            WHERE e.date_confirmed = 1
              AND (
                (SELECT COUNT(DISTINCT m.dimension)
                 FROM mood_scores m
                 WHERE m.entry_id = e.id
                   AND m.dimension IN ({placeholders})
                ) < ?
                OR e.updated_at > (
                    SELECT MAX(m2.created_at)
                    FROM mood_scores m2
                    WHERE m2.entry_id = e.id
                )
            ){user_filter}
            ORDER BY e.entry_date ASC, e.id ASC
            """,
            (*dimension_names, len(dimension_names), *user_params),
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
        conn = self._conn()
        if not current_names:
            with conn:
                cursor = conn.execute("DELETE FROM mood_scores")
            log.info(
                "Pruned ALL %d mood_scores rows (empty current set)",
                cursor.rowcount,
            )
            return cursor.rowcount
        placeholders = ",".join("?" for _ in current_names)
        with conn:
            cursor = conn.execute(
                f"DELETE FROM mood_scores "
                f"WHERE dimension NOT IN ({placeholders})",
                tuple(current_names),
            )
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
        user_id: int | None = None,
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
                COUNT(DISTINCT e.id) as entry_count,
                MIN(m.score) as score_min,
                MAX(m.score) as score_max
            FROM mood_scores m
            JOIN entries e ON e.id = m.entry_id
            WHERE 1=1
        """
        params: list[str | int] = []
        if user_id is not None:
            query += " AND e.user_id = ?"
            params.append(user_id)
        if start_date:
            query += " AND e.entry_date >= ?"
            params.append(start_date)
        if end_date:
            query += " AND e.entry_date <= ?"
            params.append(end_date)
        query += f" GROUP BY {period_expr}, m.dimension ORDER BY period"
        conn = self._conn()
        rows = conn.execute(query, params).fetchall()
        return [
            MoodTrend(
                period=row["period"],
                dimension=row["dimension"],
                avg_score=row["avg_score"],
                entry_count=row["entry_count"],
                score_min=row["score_min"],
                score_max=row["score_max"],
            )
            for row in rows
        ]

    def get_mood_drilldown(
        self,
        dimension: str,
        period_start: str,
        period_end: str,
        user_id: int | None = None,
    ) -> list[MoodDrilldownEntry]:
        """Return per-entry scores for one dimension within a date window."""
        sql = """
            SELECT
                e.id       AS entry_id,
                e.entry_date,
                m.score,
                m.confidence,
                m.rationale
            FROM mood_scores m
            JOIN entries e ON e.id = m.entry_id
            WHERE m.dimension = ?
              AND e.entry_date >= ?
              AND e.entry_date <= ?
        """
        params: list[str | int] = [dimension, period_start, period_end]
        if user_id is not None:
            sql += " AND e.user_id = ?"
            params.append(user_id)
        sql += " ORDER BY e.entry_date ASC, e.id ASC"
        conn = self._conn()
        rows = conn.execute(sql, params).fetchall()
        return [
            MoodDrilldownEntry(
                entry_id=int(row["entry_id"]),
                entry_date=row["entry_date"],
                score=float(row["score"]),
                confidence=float(row["confidence"]) if row["confidence"] is not None else None,
                rationale=row["rationale"],
            )
            for row in rows
        ]
