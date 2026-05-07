"""Repository Protocol + shared row/SQL helpers.

Lives separately from ``store.py`` so the per-mixin modules
(``core``, ``pages``, ``chunks``, ``search``, ``mood``, ``stats``,
``analytics``) can import ``_row_to_entry`` and ``_bin_start_sql``
without a circular path through ``store``. Callers continue to use
``from journal.db.repository import EntryRepository`` via the
re-export in ``__init__.py``.
"""

import sqlite3
from datetime import datetime  # noqa: F401  (kept for Protocol type-eval)
from typing import Protocol, runtime_checkable

from journal.models import (
    CalendarDay,
    ChunkSpan,
    EntityDistributionBin,
    EntityTrendBin,
    Entry,
    EntryPage,
    IngestionStats,
    MoodDrilldownEntry,
    MoodEntityCorrelation,
    MoodScore,
    MoodTrend,
    Statistics,
    TopicFrequency,
    WordCountBucket,
    WordCountStats,
    WritingFrequencyBin,
)

_SUPPORTED_BINS: tuple[str, ...] = ("week", "month", "quarter", "year")

# `_bin_start_sql` accepts "day" because the same helper backs the
# mood-trend MCP tool, which exposes daily granularity to the LLM.
# Statistics endpoints clamp callers to the four-bin set above.
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
        user_id: int = 1,
    ) -> Entry: ...

    def get_entry(self, entry_id: int, user_id: int | None = None) -> Entry | None: ...

    def update_final_text(
        self, entry_id: int, final_text: str, word_count: int, chunk_count: int,
        user_id: int | None = None,
    ) -> Entry | None: ...

    def update_entry_date(
        self, entry_id: int, entry_date: str, user_id: int | None = None,
    ) -> Entry | None: ...

    def delete_entry(self, entry_id: int, user_id: int | None = None) -> bool: ...

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

    def verify_doubts(self, entry_id: int, user_id: int | None = None) -> bool: ...

    def get_entries_by_date(self, date: str, user_id: int | None = None) -> list[Entry]: ...

    def list_entries(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = 20,
        offset: int = 0,
        user_id: int | None = None,
    ) -> list[Entry]: ...

    def search_text(
        self, query: str, start_date: str | None = None, end_date: str | None = None,
        user_id: int | None = None,
    ) -> list[Entry]: ...

    def search_text_with_snippets(
        self,
        query: str,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = 10,
        offset: int = 0,
        user_id: int | None = None,
    ) -> list[tuple[Entry, str]]: ...

    def count_text_matches(
        self,
        query: str,
        start_date: str | None = None,
        end_date: str | None = None,
        user_id: int | None = None,
    ) -> int: ...

    def get_statistics(
        self, start_date: str | None = None, end_date: str | None = None,
        user_id: int | None = None,
    ) -> Statistics: ...

    def add_people(self, entry_id: int, names: list[str]) -> None: ...

    def add_places(self, entry_id: int, names: list[str]) -> None: ...

    def add_tags(self, entry_id: int, tags: list[str]) -> None: ...

    def add_mood_score(
        self, entry_id: int, dimension: str, score: float,
        confidence: float | None = None, rationale: str | None = None,
    ) -> None: ...

    def replace_mood_scores(
        self,
        entry_id: int,
        scores: list[tuple[str, float, float | None, str | None]],
    ) -> None: ...

    def get_mood_scores(self, entry_id: int) -> list[MoodScore]: ...

    def get_entries_missing_mood_scores(
        self, dimension_names: list[str], user_id: int | None = None,
    ) -> list[int]: ...

    def prune_retired_mood_scores(
        self, current_names: list[str]
    ) -> int: ...

    def get_mood_trends(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        granularity: str = "week",
        user_id: int | None = None,
    ) -> list[MoodTrend]: ...

    def get_mood_drilldown(
        self,
        dimension: str,
        period_start: str,
        period_end: str,
        user_id: int | None = None,
    ) -> list[MoodDrilldownEntry]: ...

    def get_entity_distribution(
        self,
        entity_type: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = 50,
        user_id: int | None = None,
    ) -> list[EntityDistributionBin]: ...

    def count_entries(
        self, start_date: str | None = None, end_date: str | None = None,
        user_id: int | None = None,
    ) -> int: ...

    def get_ingestion_stats(self, now: datetime, user_id: int | None = None) -> IngestionStats: ...

    def get_writing_frequency(
        self,
        start_date: str | None,
        end_date: str | None,
        granularity: str,
        user_id: int | None = None,
    ) -> list[WritingFrequencyBin]: ...

    def get_page_count(self, entry_id: int) -> int: ...

    def get_entity_mention_count(self, entry_id: int) -> int: ...

    def get_topic_frequency(
        self, topic: str, start_date: str | None = None, end_date: str | None = None,
        user_id: int | None = None,
    ) -> TopicFrequency: ...

    def get_calendar_heatmap(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        user_id: int | None = None,
    ) -> list[CalendarDay]: ...

    def get_entity_trends(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        granularity: str = "month",
        entity_type: str | None = None,
        limit: int = 8,
        user_id: int | None = None,
    ) -> tuple[list[str], list[EntityTrendBin]]: ...

    def get_mood_entity_correlation(
        self,
        dimension: str,
        start_date: str | None = None,
        end_date: str | None = None,
        entity_type: str | None = None,
        limit: int = 10,
        user_id: int | None = None,
    ) -> tuple[float, list[MoodEntityCorrelation]]: ...

    def get_word_count_distribution(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        bucket_size: int = 100,
        user_id: int | None = None,
    ) -> tuple[list[WordCountBucket], WordCountStats]: ...


def _row_to_entry(row: sqlite3.Row) -> Entry:
    return Entry(
        id=row["id"],
        entry_date=row["entry_date"],
        source_type=row["source_type"],
        raw_text=row["raw_text"],
        user_id=row["user_id"],
        final_text=row["final_text"] or row["raw_text"],
        word_count=row["word_count"],
        chunk_count=row["chunk_count"],
        language=row["language"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        doubts_verified=bool(row["doubts_verified"]),
    )
