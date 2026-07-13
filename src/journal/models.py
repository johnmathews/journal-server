"""Shared data models."""

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class TranscriptionResult:
    """Result from a transcription provider.

    ``text`` is the transcribed text.  ``uncertain_spans`` lists
    character-offset ranges ``(char_start, char_end)`` where the
    transcription model had low confidence.  The spans use the same
    convention as ``entry_uncertain_spans`` — half-open intervals into
    ``text``.  An empty list means either no low-confidence regions
    were detected or the model does not support confidence data.
    """

    text: str
    uncertain_spans: list[tuple[int, int]] = field(default_factory=list)


@dataclass
class Entry:
    id: int
    entry_date: str
    source_type: str
    raw_text: str
    user_id: int = 0
    final_text: str = ""
    word_count: int = 0
    chunk_count: int = 0
    language: str = "en"
    created_at: str = ""
    updated_at: str = ""
    doubts_verified: bool = False
    date_confirmed: bool = True
    content_start_char: int | None = None
    content_end_char: int | None = None


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
    rationale: str | None = None


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
    score_min: float | None = None
    score_max: float | None = None


@dataclass
class MoodDrilldownEntry:
    """One entry's contribution to a mood drill-down result."""

    entry_id: int
    entry_date: str
    score: float
    confidence: float | None
    rationale: str | None


@dataclass
class EntityDistributionBin:
    """Mention count for one entity within a date-filtered window."""

    canonical_name: str
    entity_type: str
    mention_count: int


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
    user_id: int = 0
    description: str = ""
    aliases: list[str] = field(default_factory=list)
    first_seen: str = ""
    created_at: str = ""
    updated_at: str = ""
    is_quarantined: bool = False
    quarantine_reason: str = ""
    quarantined_at: str = ""


@dataclass
class EntityMention:
    id: int
    entity_id: int
    entry_id: int
    quote: str
    confidence: float
    extraction_run_id: str
    created_at: str = ""
    # NULL for mentions that created a new entity; otherwise one of
    # "stage_a" (exact canonical name), "stage_b" (alias), "stage_c"
    # (embedding similarity), or "llm_asserted" (matches_known_id
    # supplied by the extraction LLM, validated through the four
    # guards in `_resolve_entity`).
    match_source: str | None = None


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
class PairDecision:
    """A persisted "not a duplicate" decision for a pair of entities.

    Survives across extraction runs so the same pair is not re-suggested
    after the user has rejected it. Stored normalised (entity_a.id <
    entity_b.id) by the repository layer.
    """

    id: int
    user_id: int
    entity_a: Entity
    entity_b: Entity
    decision: str  # currently always 'rejected'
    decided_at: str


@dataclass
class ExtractionResult:
    entry_id: int
    extraction_run_id: str
    entities_created: int
    entities_matched: int
    mentions_created: int
    relationships_created: int
    warnings: list[str] = field(default_factory=list)
    # Number of entities that became orphaned (zero mentions across all
    # entries) as a result of this extraction and were auto-removed by
    # the orphan-cleanup pass.  Always 0 for batch/multi-entry runs;
    # only meaningful for single-entry re-extractions triggered by an
    # edit-save pipeline.
    entities_deleted: int = 0


JobStatus = Literal["queued", "running", "succeeded", "failed"]
JobType = Literal[
    "entity_extraction",
    "mood_backfill",
    "ingest_images",
    "ingest_audio",
    "mood_score_entry",
    "reprocess_embeddings",
    "fitness_sync_strava",
    "fitness_sync_garmin",
    "storyline_update",
    "storyline_extension_check",
]


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
    status_detail: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None
    user_id: int | None = None
    # Per-job LLM token usage + dollar cost (W2). NULL for legacy rows and
    # jobs that made no LLM calls; cost_usd stays NULL until W3 wires pricing.
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None


@dataclass
class CalendarDay:
    """One day in the calendar heatmap."""

    date: str
    entry_count: int
    total_words: int


@dataclass
class EntityTrendBin:
    """One time bucket for one entity in the entity-trends series."""

    period: str
    entity: str
    mention_count: int


@dataclass
class MoodEntityCorrelation:
    """Average mood score when a specific entity is mentioned."""

    entity: str
    entity_type: str
    avg_score: float
    entry_count: int


@dataclass
class WordCountBucket:
    """One bucket in a word-count histogram."""

    range_start: int
    range_end: int
    count: int


@dataclass
class WordCountStats:
    """Aggregate statistics for the word-count distribution."""

    min: int
    max: int
    avg: float
    median: float
    total_entries: int


@dataclass
class User:
    id: int
    email: str
    display_name: str
    is_admin: bool = False
    is_active: bool = True
    email_verified: bool = False
    created_at: str = ""
    updated_at: str = ""


@dataclass
class ApiKeyInfo:
    """API key metadata returned to the user. Never includes the hash."""

    id: int
    user_id: int
    key_prefix: str
    name: str
    created_at: str = ""
    expires_at: str | None = None
    last_used_at: str | None = None
    revoked_at: str | None = None


# ── Storylines ──────────────────────────────────────────────────────
#
# Persistence dataclasses for the storylines redesign. Schema lives in
# migrations 0027 and 0036; design rationale in docs/storylines-redesign.md
# (2026-07-12 spec).

StorylineStatus = Literal["active", "archived"]
StorylineChapterState = Literal["draft", "published"]


@dataclass
class Storyline:
    """One named, entity-anchored synthesized narrative.

    A storyline is anchored on 1..N entities. The anchor set lives in
    ``storyline_entities`` and is loaded by the repository's
    ``list_anchors`` / ``with_anchors`` helpers; this dataclass keeps
    only the ``storylines`` row fields. Callers that need the anchors
    fetch them explicitly to avoid surprise queries.
    """

    id: int
    user_id: int
    name: str
    description: str = ""
    status: str = "active"
    last_extension_check_at: str | None = None
    created_at: str = ""
    updated_at: str = ""


@dataclass
class StorylineChapter:
    """One time-windowed chapter of a storyline.

    Each chapter progresses through ``draft`` (being written) to
    ``published`` (finalized). Draft chapters accumulate ``addenda``
    (updates after publishing a non-leaf chapter) and include
    ``draft_embedding`` for the current narrative. Published chapters
    have ``published_at`` and read tracking via ``read_at``.

    Addendum dict shape (stored in ``addenda_json``):
    ``{"added_at": str, "segments": list[segment], "entry_ids": list[int]}``.
    """

    id: int
    storyline_id: int
    seq: int
    title: str = ""
    state: str = "draft"
    segments: list[dict[str, Any]] = field(default_factory=list)
    source_entry_ids: list[int] = field(default_factory=list)
    citation_count: int = 0
    model_used: str = ""
    generated_at: str | None = None
    published_at: str | None = None
    read_at: str | None = None
    addenda: list[dict[str, Any]] = field(default_factory=list)
    draft_embedding: list[float] | None = None
    # Derived from membership by the repository (not columns):
    entry_count: int = 0
    first_entry_date: str | None = None
    last_entry_date: str | None = None
    created_at: str = ""
    updated_at: str = ""


@dataclass
class DatedEntryExcerpt:
    """One entry's contribution to a storyline's source corpus.

    Produced by the `get_dated_entity_excerpts` query on the entity
    store. `quotes` is the verbatim list of `entity_mentions.quote`
    rows for the (entity, entry) pair — used by the curation panel.
    `final_text` is the full entry text — used by the narrative panel
    via the Citations API as one document block.
    """

    entry_id: int
    entry_date: str
    final_text: str
    quotes: list[str] = field(default_factory=list)


# ── Fitness pipeline ────────────────────────────────────────────────
#
# Persistence dataclasses for the fitness integration. Schema lives in
# migrations 0023/0024/0025; design rationale in
# docs/fitness-integration-plan.md and docs/fitness-schema.md.

FitnessSource = Literal["strava", "garmin"]
FitnessAuthStatus = Literal["unknown", "ok", "broken"]
FitnessSyncStatus = Literal[
    "running", "success", "auth_broken", "transient_failure", "normalize_drift",
]
FitnessActivityType = Literal[
    "run", "ride", "swim", "walk", "hike", "strength", "other",
]


@dataclass
class FitnessAuthState:
    """Per-user, per-source auth tokens + status for the fitness pipeline.

    `extra_state_json` is the catch-all for source-specific token blobs
    (garth's OAuth1 + OAuth2 pair, Strava's redirect-state nonce, etc.)
    that don't fit the explicit columns.
    """

    user_id: int
    source: str
    access_token: str | None = None
    refresh_token: str | None = None
    token_expires_at: str | None = None
    extra_state: dict[str, Any] = field(default_factory=dict)
    last_successful_login_at: str | None = None
    last_refresh_at: str | None = None
    auth_status: str = "unknown"
    auth_broken_since: str | None = None
    id: int | None = None
    created_at: str = ""
    updated_at: str = ""


@dataclass
class FitnessSyncRun:
    """One scheduled fetch invocation for one source.

    ``rows_fetched`` / ``rows_normalized`` are the totals the legacy UI read.
    The ``*_workouts`` / ``*_wellness`` pairs (added 2026-05-11, T7) split
    those totals by bucket so the UI can distinguish workouts from wellness
    rows on Garmin syncs. Strava is workouts-only so its ``*_wellness``
    fields are always 0.
    """

    user_id: int
    source: str
    status: str
    id: int | None = None
    started_at: str = ""
    finished_at: str | None = None
    error_class: str | None = None
    error_message: str | None = None
    rows_fetched: int = 0
    rows_normalized: int = 0
    workouts_fetched: int = 0
    wellness_fetched: int = 0
    workouts_normalized: int = 0
    wellness_normalized: int = 0
    notes: dict[str, Any] = field(default_factory=dict)


@dataclass
class FitnessRawRow:
    """One raw payload row (Strava or Garmin)."""

    user_id: int
    source: str
    source_id: str
    endpoint: str
    payload_json: str
    payload_sha256: str
    sync_run_id: int | None = None
    id: int | None = None
    fetched_at: str = ""


@dataclass
class FitnessActivity:
    """One discrete activity (run, ride, swim, …). Per
    fitness-schema.md S1, both Strava and Garmin live in this table."""

    user_id: int
    source: str
    source_id: str
    activity_type: str
    source_subtype: str
    start_time: str
    local_date: str
    duration_s: int
    raw_ref_id: int
    moving_time_s: int | None = None
    distance_m: float | None = None
    elevation_gain_m: float | None = None
    avg_hr_bpm: int | None = None
    max_hr_bpm: int | None = None
    avg_pace_s_per_km: float | None = None
    calories_kcal: int | None = None
    perceived_exertion: int | None = None
    extras: dict[str, Any] = field(default_factory=dict)
    id: int | None = None
    normalized_at: str = ""


@dataclass
class ConversationMessage:
    """One turn in a conversation about a journal answer."""

    id: int
    role: str               # "user" | "assistant"
    content: str
    citations: list[dict]   # [{entry_id, entry_date, snippet}]; [] for user turns
    created_at: str


@dataclass
class Conversation:
    """A persisted chat thread, seeded from a Search answer."""

    id: int
    user_id: int
    title: str
    created_at: str
    updated_at: str
    messages: list[ConversationMessage] = field(default_factory=list)
    message_count: int = 0


@dataclass
class FitnessDaily:
    """One daily rollup row (recovery + training-state metrics)."""

    user_id: int
    source: str
    local_date: str
    sleep_score: int | None = None
    sleep_duration_s: int | None = None
    sleep_efficiency_pct: float | None = None
    hrv_overnight_ms: float | None = None
    resting_hr_bpm: int | None = None
    body_battery_high: int | None = None
    body_battery_low: int | None = None
    stress_avg: int | None = None
    training_load_acute: float | None = None
    training_load_chronic: float | None = None
    training_readiness: int | None = None
    extras: dict[str, Any] = field(default_factory=dict)
    raw_ref_ids: list[int] = field(default_factory=list)
    id: int | None = None
    normalized_at: str = ""
