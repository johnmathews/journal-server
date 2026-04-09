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
class SearchResult:
    entry_id: int
    entry_date: str
    text: str
    score: float
    chunk_text: str = ""


@dataclass
class TopicFrequency:
    topic: str
    count: int
    entries: list[Entry] = field(default_factory=list)
