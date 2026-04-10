"""Shared data models."""

from dataclasses import dataclass, field


@dataclass
class Entry:
    id: int
    entry_date: str
    source_type: str
    raw_text: str
    final_text: str = ""
    word_count: int = 0
    chunk_count: int = 0
    language: str = "en"
    created_at: str = ""
    updated_at: str = ""


@dataclass
class EntryPage:
    id: int
    entry_id: int
    page_number: int
    raw_text: str
    source_file_id: int | None = None
    created_at: str = ""


@dataclass
class MoodScore:
    entry_id: int
    dimension: str
    score: float
    confidence: float | None = None


@dataclass
class Statistics:
    total_entries: int
    date_range_start: str | None
    date_range_end: str | None
    total_words: int
    avg_words_per_entry: float
    entries_per_month: float


@dataclass
class MoodTrend:
    period: str
    dimension: str
    avg_score: float
    entry_count: int


@dataclass
class ChunkMatch:
    """A single chunk that matched a query, with its relevance score."""

    text: str
    score: float


@dataclass
class SearchResult:
    """One entry's contribution to a search result set.

    `text` is the full parent entry (`final_text or raw_text`).
    `matching_chunks` lists every chunk in the entry that scored above
    the vector store's similarity cutoff, sorted by score descending.
    `score` is the top (max) chunk score — used to rank entries against
    each other in the result list.
    """

    entry_id: int
    entry_date: str
    text: str
    score: float
    matching_chunks: list[ChunkMatch] = field(default_factory=list)


@dataclass
class TopicFrequency:
    topic: str
    count: int
    entries: list[Entry] = field(default_factory=list)
