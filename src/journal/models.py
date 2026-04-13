"""Shared data models."""

from dataclasses import dataclass, field
from typing import Any, Literal


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
class WritingFrequencyBin:
    """One time-bucket in the writing-frequency / word-count series.

    `bin_start` is the ISO-8601 date of the first day of the bucket
    (Monday for weeks, the 1st of the month for months, the 1st of
    Jan/Apr/Jul/Oct for quarters, Jan 1 for years). Callers render
    the timeseries by plotting `entry_count` or `total_words`
    against `bin_start`.
    """

    bin_start: str
    entry_count: int
    total_words: int


@dataclass
class IngestionStats:
    """Operational stats about the ingestion corpus, used by `/health`.

    All "last N days" windows are inclusive of the current day. Row
    counts are a mapping of table name → row count so the endpoint
    can surface schema growth without hardcoding a list on the
    caller side.
    """

    total_entries: int
    entries_last_7d: int
    entries_last_30d: int
    by_source_type: dict[str, int]
    avg_words_per_entry: float
    avg_chunks_per_entry: float
    last_ingestion_at: str | None
    total_chunks: int
    row_counts: dict[str, int]


@dataclass
class MoodTrend:
    period: str
    dimension: str
    avg_score: float
    entry_count: int


@dataclass(frozen=True)
class ChunkSpan:
    """A chunk of text with its position in the source text and token count.

    `char_start` and `char_end` are character offsets into the original
    input text passed to `ChunkingStrategy.chunk()`. `char_end` is
    exclusive — `source_text[char_start:char_end]` yields the range the
    chunk covers in the source. That range may contain slightly more
    whitespace than `text` does, because paragraph and sentence
    separators are normalised when building the chunk's rendered text
    (paragraphs joined with `\\n\\n`, sentences with a single space).

    `token_count` is the tiktoken `cl100k_base` token count of `text`,
    which matches the tokenizer used by `text-embedding-3-large`.
    """

    text: str
    char_start: int
    char_end: int
    token_count: int


@dataclass
class ChunkMatch:
    """A single chunk that matched a query, with its relevance score.

    `chunk_index` is the chunk's position within its parent entry's
    chunk list, as stored in `entry_chunks.chunk_index` and in the
    ChromaDB metadata. `char_start`/`char_end` are offsets into the
    parent entry's `final_text` (or `raw_text` fallback) — exactly the
    values stored in `entry_chunks`. All three fields are `None` for
    legacy entries that were ingested before chunk persistence shipped
    (migration 0003) and therefore have no `entry_chunks` rows to
    look up. Clients rendering overlays must be prepared for missing
    offsets on such entries.
    """

    text: str
    score: float
    chunk_index: int | None = None
    char_start: int | None = None
    char_end: int | None = None


@dataclass
class SearchResult:
    """One entry's contribution to a search result set.

    `text` is the full parent entry (`final_text or raw_text`).
    `matching_chunks` lists every chunk in the entry that scored above
    the vector store's similarity cutoff, sorted by score descending.
    `score` is the top (max) chunk score — used to rank entries against
    each other in the result list.

    `snippet` is populated only in keyword search mode. It is a
    substring of `final_text` with ASCII `\\x02` and `\\x03` control
    characters wrapping matched terms (FTS5's `snippet()` aux
    function). Semantic search leaves it as `None`; callers render
    highlights from `matching_chunks` instead.
    """

    entry_id: int
    entry_date: str
    text: str
    score: float
    matching_chunks: list[ChunkMatch] = field(default_factory=list)
    snippet: str | None = None


@dataclass
class TopicFrequency:
    topic: str
    count: int
    entries: list[Entry] = field(default_factory=list)


EntityType = Literal["person", "place", "activity", "organization", "topic", "other"]


@dataclass
class Entity:
    id: int
    entity_type: EntityType
    canonical_name: str
    description: str = ""
    aliases: list[str] = field(default_factory=list)
    first_seen: str = ""
    created_at: str = ""
    updated_at: str = ""


@dataclass
class EntityMention:
    id: int
    entity_id: int
    entry_id: int
    quote: str
    confidence: float
    extraction_run_id: str
    created_at: str = ""


@dataclass
class EntityRelationship:
    id: int
    subject_entity_id: int
    predicate: str
    object_entity_id: int
    quote: str
    entry_id: int
    confidence: float
    extraction_run_id: str
    created_at: str = ""


@dataclass
class MergeResult:
    survivor_id: int
    absorbed_ids: list[int]
    mentions_reassigned: int
    relationships_reassigned: int
    aliases_added: int


@dataclass
class MergeCandidate:
    id: int
    entity_a: Entity
    entity_b: Entity
    similarity: float
    status: str  # 'pending', 'accepted', 'dismissed'
    extraction_run_id: str
    created_at: str = ""


@dataclass
class ExtractionResult:
    entry_id: int
    extraction_run_id: str
    entities_created: int
    entities_matched: int
    mentions_created: int
    relationships_created: int
    warnings: list[str] = field(default_factory=list)


JobStatus = Literal["queued", "running", "succeeded", "failed"]
JobType = Literal["entity_extraction", "mood_backfill", "ingest_images", "mood_score_entry", "reprocess_embeddings"]


@dataclass
class Job:
    """One async batch job row.

    Mirrors the `jobs` table. `params` and `result` are the
    deserialised forms of `params_json` and `result_json` — the
    repository handles encoding on write and decoding on read, so
    callers only ever see Python dicts. `status` transitions are
    append-only in practice: queued -> running -> (succeeded | failed).
    Timestamps are ISO 8601 UTC strings.
    """

    id: str
    type: str
    status: str
    params: dict[str, Any]
    progress_current: int
    progress_total: int
    result: dict[str, Any] | None
    error_message: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None
