"""Configuration loaded from environment variables."""

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Config:
    # Database
    db_path: Path = field(default_factory=lambda: Path(os.environ.get("DB_PATH", "journal.db")))

    # ChromaDB
    chromadb_host: str = field(default_factory=lambda: os.environ.get("CHROMADB_HOST", "localhost"))
    chromadb_port: int = field(
        default_factory=lambda: int(os.environ.get("CHROMADB_PORT", "8000"))
    )
    chromadb_collection: str = "journal_entries"

    # OCR provider selection: "anthropic" or "gemini"
    ocr_provider: str = field(
        default_factory=lambda: os.environ.get("OCR_PROVIDER", "anthropic")
    )

    # Anthropic
    anthropic_api_key: str = field(
        default_factory=lambda: os.environ.get("ANTHROPIC_API_KEY", "")
    )

    # Google (Gemini)
    google_api_key: str = field(
        default_factory=lambda: os.environ.get("GOOGLE_API_KEY", "")
    )

    # OCR model — interpreted by the selected provider.
    # Defaults depend on provider: claude-opus-4-6 for anthropic, gemini-2.5-pro for gemini.
    # When unset, the factory in ocr.py picks the provider's default.
    ocr_model: str = field(
        default_factory=lambda: os.environ.get("OCR_MODEL", "")
    )
    ocr_max_tokens: int = 4096
    # Optional directory of markdown files loaded once at startup and
    # injected into the OCR system prompt to prime the model with
    # known proper nouns (family, places, topics). When unset the
    # adapter behaves exactly as before. See docs/ocr-context.md.
    ocr_context_dir: Path | None = field(
        default_factory=lambda: (
            Path(p) if (p := os.environ.get("OCR_CONTEXT_DIR")) else None
        )
    )
    # Cache TTL for the OCR system prompt block. "5m" or "1h".
    # 1-hour is cheaper when an ingestion session does more than
    # ~5 OCR calls in an hour.
    ocr_context_cache_ttl: str = field(
        default_factory=lambda: os.environ.get("OCR_CONTEXT_CACHE_TTL", "1h")
    )

    # Image preprocessing — auto-rotate, crop to text area, downscale,
    # contrast enhancement. Applied before OCR to improve accuracy.
    preprocess_images: bool = field(
        default_factory=lambda: os.environ.get(
            "PREPROCESS_IMAGES", "true"
        ).lower() in ("1", "true", "yes", "on")
    )

    # Dual-pass OCR — run both Anthropic and Gemini on each page,
    # reconcile disagreements as uncertain spans ("doubts"). Requires
    # both ANTHROPIC_API_KEY and GOOGLE_API_KEY.
    ocr_dual_pass: bool = field(
        default_factory=lambda: os.environ.get(
            "OCR_DUAL_PASS", "false"
        ).lower() in ("1", "true", "yes", "on")
    )

    # OpenAI (Whisper + Embeddings)
    openai_api_key: str = field(default_factory=lambda: os.environ.get("OPENAI_API_KEY", ""))
    transcription_model: str = field(
        default_factory=lambda: os.environ.get(
            "TRANSCRIPTION_MODEL", "gpt-4o-transcribe"
        )
    )
    # Log-probability threshold for flagging uncertain words during
    # transcription.  Tokens with logprob below this value are marked
    # as uncertain spans.  Only effective with models that support
    # logprobs (gpt-4o-transcribe, gpt-4o-mini-transcribe).
    # -0.5 ≈ 60% confidence.  More negative = fewer flags.
    transcription_confidence_threshold: float = field(
        default_factory=lambda: float(
            os.environ.get("TRANSCRIPTION_CONFIDENCE_THRESHOLD", "-0.5")
        )
    )
    embedding_model: str = "text-embedding-3-large"
    embedding_dimensions: int = 1024

    # Transcript formatting (LLM paragraph insertion)
    transcript_formatting: bool = field(
        default_factory=lambda: os.environ.get(
            "TRANSCRIPT_FORMATTING", "false"
        ).lower()
        in ("1", "true", "yes", "on")
    )
    transcript_formatter_model: str = field(
        default_factory=lambda: os.environ.get(
            "TRANSCRIPT_FORMATTER_MODEL", "claude-haiku-4-5"
        )
    )

    # Date-heading detection — when on, ingestion lifts a leading date
    # in the OCR or voice text into a markdown heading on `final_text`.
    # `raw_text` is never modified. Default-on so new ingests get the
    # benefit without configuration; users can toggle it off via the
    # runtime settings UI without a server restart.
    date_heading_detection: bool = field(
        default_factory=lambda: os.environ.get(
            "DATE_HEADING_DETECTION", "true"
        ).lower()
        in ("1", "true", "yes", "on")
    )
    date_heading_model: str = field(
        default_factory=lambda: os.environ.get(
            "DATE_HEADING_MODEL", "claude-haiku-4-5"
        )
    )

    # Whisper transcription prompt — when on, the OCR context files
    # (people, places, glossary) are stripped of markdown, truncated to
    # ~200 tokens, and passed as the `prompt` parameter to Whisper to
    # bias toward correct spellings of proper nouns. Server restart
    # required after editing the context files (matches OCR behaviour).
    transcription_context_enabled: bool = field(
        default_factory=lambda: os.environ.get(
            "TRANSCRIPTION_CONTEXT_ENABLED", "true"
        ).lower()
        in ("1", "true", "yes", "on")
    )

    # Transcription provider selection: "openai" or "gemini".
    # Validated in __post_init__.
    transcription_provider: str = field(
        default_factory=lambda: os.environ.get("TRANSCRIPTION_PROVIDER", "openai")
    )
    # Wrap the primary transcription provider in a retry/fallback wrapper.
    transcription_fallback_enabled: bool = field(
        default_factory=lambda: os.environ.get(
            "TRANSCRIPTION_FALLBACK_ENABLED", "true"
        ).lower()
        in ("1", "true", "yes", "on")
    )
    # OpenAI model used for the fallback adapter (cheap & robust default).
    transcription_fallback_model: str = field(
        default_factory=lambda: os.environ.get(
            "TRANSCRIPTION_FALLBACK_MODEL", "whisper-1"
        )
    )
    transcription_retry_max_attempts: int = field(
        default_factory=lambda: int(
            os.environ.get("TRANSCRIPTION_RETRY_MAX_ATTEMPTS", "3")
        )
    )
    transcription_retry_base_delay: float = field(
        default_factory=lambda: float(
            os.environ.get("TRANSCRIPTION_RETRY_BASE_DELAY", "1.0")
        )
    )
    transcription_retry_max_delay: float = field(
        default_factory=lambda: float(
            os.environ.get("TRANSCRIPTION_RETRY_MAX_DELAY", "30.0")
        )
    )
    # Shadow provider: empty string disables, otherwise "openai" or "gemini".
    transcription_shadow_provider: str = field(
        default_factory=lambda: os.environ.get("TRANSCRIPTION_SHADOW_PROVIDER", "")
    )
    # Empty string means "use the shadow provider's default model".
    transcription_shadow_model: str = field(
        default_factory=lambda: os.environ.get("TRANSCRIPTION_SHADOW_MODEL", "")
    )

    # Slack (for downloading files from Slack URLs)
    slack_bot_token: str = field(
        default_factory=lambda: os.environ.get("SLACK_BOT_TOKEN", "")
    )

    # Chunking
    chunking_strategy: str = field(
        default_factory=lambda: os.environ.get("CHUNKING_STRATEGY", "semantic")
    )
    chunking_max_tokens: int = field(
        default_factory=lambda: int(os.environ.get("CHUNKING_MAX_TOKENS", "150"))
    )
    chunking_overlap_tokens: int = field(
        default_factory=lambda: int(os.environ.get("CHUNKING_OVERLAP_TOKENS", "40"))
    )
    # SemanticChunker only — min chunk size in tokens. Segments below this
    # are merged with their nearest neighbour.
    chunking_min_tokens: int = field(
        default_factory=lambda: int(os.environ.get("CHUNKING_MIN_TOKENS", "30"))
    )
    # SemanticChunker only — percentile (0-100) at or below which adjacent
    # sentence similarity counts as a chunk boundary. Smaller = more
    # conservative (fewer cuts, larger chunks). Larger = more aggressive.
    chunking_boundary_percentile: int = field(
        default_factory=lambda: int(os.environ.get("CHUNKING_BOUNDARY_PERCENTILE", "25"))
    )
    # SemanticChunker only — percentile below which a cut is considered
    # "decisive" and no tail overlap is carried. Cuts between
    # decisive_percentile and boundary_percentile are "weak" cuts that
    # duplicate the boundary sentence into both adjacent chunks as
    # transition context.
    chunking_decisive_percentile: int = field(
        default_factory=lambda: int(os.environ.get("CHUNKING_DECISIVE_PERCENTILE", "10"))
    )
    # If true, prepend a "Date: YYYY-MM-DD. Weekday." header to each chunk
    # before embedding (but store the un-prefixed chunk as the ChromaDB
    # document). Helps date-sensitive queries retrieve the right entries.
    chunking_embed_metadata_prefix: bool = field(
        default_factory=lambda: os.environ.get(
            "CHUNKING_EMBED_METADATA_PREFIX", "true"
        ).lower() in ("1", "true", "yes", "on")
    )

    # MCP Server
    mcp_host: str = field(default_factory=lambda: os.environ.get("MCP_HOST", "0.0.0.0"))
    mcp_port: int = field(default_factory=lambda: int(os.environ.get("MCP_PORT", "8000")))
    # Hosts permitted by MCP's DNS rebinding protection. When the env var is
    # unset, we default to loopback only — any production deployment must set
    # this explicitly to the externally-reachable host(s). An empty list is
    # impossible with this default, and the rebinding guard is always on.
    mcp_allowed_hosts: list[str] = field(
        default_factory=lambda: [
            h.strip()
            for h in os.environ.get(
                "MCP_ALLOWED_HOSTS", "127.0.0.1,localhost"
            ).split(",")
            if h.strip()
        ]
    )

    # REST API CORS
    api_cors_origins: list[str] = field(
        default_factory=lambda: [
            h.strip()
            for h in os.environ.get("API_CORS_ORIGINS", "").split(",")
            if h.strip()
        ]
    )

    # Mood scoring. On by default — opt out explicitly via
    # `JOURNAL_ENABLE_MOOD_SCORING=false`. When enabled, ingestion
    # calls the MoodScorer provider after chunking/embedding for
    # each entry and writes the results to `mood_scores`. The
    # dimension set is loaded from `mood_dimensions_path` (TOML)
    # at server startup.
    enable_mood_scoring: bool = field(
        default_factory=lambda: os.environ.get(
            "JOURNAL_ENABLE_MOOD_SCORING", "true"
        ).lower() in ("1", "true", "yes", "on")
    )
    mood_scorer_model: str = field(
        default_factory=lambda: os.environ.get(
            "MOOD_SCORER_MODEL", "claude-sonnet-4-5"
        )
    )
    mood_scorer_max_tokens: int = field(
        default_factory=lambda: int(
            os.environ.get("MOOD_SCORER_MAX_TOKENS", "1024")
        )
    )
    mood_dimensions_path: Path = field(
        default_factory=lambda: Path(
            os.environ.get(
                "MOOD_DIMENSIONS_PATH", "config/mood-dimensions.toml"
            )
        )
    )

    # Authentication
    secret_key: str = field(
        default_factory=lambda: os.environ.get("JOURNAL_SECRET_KEY", "")
    )
    registration_enabled: bool = field(
        default_factory=lambda: os.environ.get(
            "REGISTRATION_ENABLED", "false"
        ).lower() in ("1", "true", "yes", "on")
    )
    session_expiry_days: int = field(
        default_factory=lambda: int(os.environ.get("SESSION_EXPIRY_DAYS", "7"))
    )

    # Per-IP rate limiting on the anonymous auth endpoints (login,
    # register, forgot-password, reset-password). On by default — opt
    # out explicitly via `AUTH_RATE_LIMIT_ENABLED=false`. Defaults allow
    # 10 POSTs per 5 minutes per IP per path; see `journal/ratelimit.py`.
    auth_rate_limit_enabled: bool = field(
        default_factory=lambda: os.environ.get(
            "AUTH_RATE_LIMIT_ENABLED", "true"
        ).lower() in ("1", "true", "yes", "on")
    )
    auth_rate_limit_max_requests: int = field(
        default_factory=lambda: int(
            os.environ.get("AUTH_RATE_LIMIT_MAX_REQUESTS", "10")
        )
    )
    auth_rate_limit_window_seconds: int = field(
        default_factory=lambda: int(
            os.environ.get("AUTH_RATE_LIMIT_WINDOW_SECONDS", "300")
        )
    )

    # Email (SMTP)
    smtp_host: str = field(
        default_factory=lambda: os.environ.get("SMTP_HOST", "smtp.gmail.com")
    )
    smtp_port: int = field(
        default_factory=lambda: int(os.environ.get("SMTP_PORT", "465"))
    )
    smtp_username: str = field(
        default_factory=lambda: os.environ.get("SMTP_USERNAME", "")
    )
    smtp_password: str = field(
        default_factory=lambda: os.environ.get("SMTP_PASSWORD", "")
    )
    smtp_from_email: str = field(
        default_factory=lambda: os.environ.get("SMTP_FROM_EMAIL", "")
    )

    # App base URL (for email links)
    app_base_url: str = field(
        default_factory=lambda: os.environ.get("APP_BASE_URL", "http://localhost:5173")
    )

    # Pushover notifications
    pushover_user_key: str = field(
        default_factory=lambda: os.environ.get("PUSHOVER_USER_KEY", "")
    )
    pushover_app_token: str = field(
        default_factory=lambda: os.environ.get("PUSHOVER_APP_API_TOKEN", "")
    )

    # Hybrid search
    # The /api/search endpoint and the journal_search_entries MCP tool
    # combine BM25 (SQLite FTS5) with dense embedding retrieval, fuse
    # candidates with Reciprocal Rank Fusion, then rerank the top
    # `hybrid_fusion_top_m` candidates with the configured reranker.
    # All knobs are tunable via env vars; defaults match published
    # guidance for hybrid retrieval at this corpus scale.
    hybrid_bm25_candidates: int = field(
        default_factory=lambda: int(os.environ.get("HYBRID_BM25_CANDIDATES", "50"))
    )
    hybrid_dense_candidates: int = field(
        default_factory=lambda: int(os.environ.get("HYBRID_DENSE_CANDIDATES", "50"))
    )
    hybrid_fusion_top_m: int = field(
        default_factory=lambda: int(os.environ.get("HYBRID_FUSION_TOP_M", "30"))
    )
    hybrid_rrf_k: int = field(
        default_factory=lambda: int(os.environ.get("HYBRID_RRF_K", "60"))
    )
    # `anthropic` (default) | `none`. The `none` value runs RRF-only —
    # useful for benchmarking the fusion stage in isolation or for
    # cutting per-search latency at a quality cost.
    hybrid_reranker: str = field(
        default_factory=lambda: os.environ.get("HYBRID_RERANKER", "anthropic")
    )
    reranker_model: str = field(
        default_factory=lambda: os.environ.get("RERANKER_MODEL", "claude-haiku-4-5")
    )

    # ── Answer synthesis (POST /api/search/answer) ──────────────────
    # `anthropic` enables grounded answer synthesis over the hybrid
    # search top-N; `none` disables it (the endpoint returns an
    # answered=false "disabled" payload). `answer_context_entries` is
    # how many retrieved entries are fed to the answerer as grounding.
    answer_provider: str = field(
        default_factory=lambda: os.environ.get("ANSWER_PROVIDER", "anthropic")
    )
    answer_model: str = field(
        default_factory=lambda: os.environ.get("ANSWER_MODEL", "claude-sonnet-4-6")
    )
    answer_context_entries: int = field(
        default_factory=lambda: int(os.environ.get("ANSWER_CONTEXT_ENTRIES", "8"))
    )
    # Cheap model that classifies a query as a question (→ auto-answer)
    # vs. a plain keyword search (→ results only), so the expensive
    # answer model only runs for questions.
    answer_classifier_model: str = field(
        default_factory=lambda: os.environ.get(
            "ANSWER_CLASSIFIER_MODEL", "claude-haiku-4-5"
        )
    )

    # Storylines (docs/storylines-plan.md)
    storyline_narrator_model: str = field(
        default_factory=lambda: os.environ.get(
            "STORYLINE_NARRATOR_MODEL", "claude-opus-4-7",
        )
    )
    storyline_narrator_max_tokens: int = field(
        default_factory=lambda: int(
            os.environ.get("STORYLINE_NARRATOR_MAX_TOKENS", "4096")
        )
    )
    storyline_judge_model: str = field(
        default_factory=lambda: os.environ.get(
            "STORYLINE_JUDGE_MODEL", "claude-haiku-4-5",
        )
    )
    storyline_extension_decider_model: str = field(
        default_factory=lambda: os.environ.get(
            "STORYLINE_EXTENSION_DECIDER_MODEL", "claude-haiku-4-5",
        )
    )
    # Extension classifier embedding fallback (W6): when neither an entity
    # overlap nor a surface-form name match fires, compare the entry's
    # embedding to the storyline's summary embedding; a cosine at/above
    # this threshold escalates to the Haiku decider instead of an outright
    # "no". Catches semantically-related entries the extractor missed.
    storyline_extension_relevance_threshold: float = field(
        default_factory=lambda: float(
            os.environ.get("STORYLINE_EXTENSION_RELEVANCE_THRESHOLD", "0.5")
        )
    )
    # Guards against publishing a chapter with too little material to be
    # worth reading — see StorylineEngine.
    storyline_min_publish_entries: int = field(
        default_factory=lambda: int(
            os.environ.get("STORYLINE_MIN_PUBLISH_ENTRIES", "3")
        )
    )

    # Entity extraction
    entity_extraction_model: str = "claude-opus-4-6"
    entity_extraction_max_tokens: int = 4096
    entity_dedup_similarity_threshold: float = field(
        default_factory=lambda: float(
            os.environ.get("ENTITY_DEDUP_SIMILARITY_THRESHOLD", "0.88")
        )
    )
    # Vector pre-filter for the "known_entities" block we send to the
    # extraction LLM (WU4). Per-call: embed the entry, retrieve top-K
    # user entities by cosine similarity, drop anything below
    # ``candidate_threshold``. The LLM only sees entities that clear
    # the floor, so it can't anchor onto a distant candidate.
    entity_llm_candidate_top_k: int = field(
        default_factory=lambda: int(
            os.environ.get("ENTITY_LLM_CANDIDATE_TOP_K", "30")
        )
    )
    entity_llm_candidate_threshold: float = field(
        default_factory=lambda: float(
            os.environ.get("ENTITY_LLM_CANDIDATE_THRESHOLD", "0.4")
        )
    )
    # Guard D in the four-guard hybrid sanity check on LLM-asserted
    # matches: cosine(new mention's embedding, asserted match's
    # stored embedding) must be at least this. Below the floor, we
    # reject the assertion and fall through to the existing
    # stage-a/b/c resolution.
    entity_llm_match_min_cosine: float = field(
        default_factory=lambda: float(
            os.environ.get("ENTITY_LLM_MATCH_MIN_COSINE", "0.3")
        )
    )
    # Name the extractor uses for the journal author — "I went to Blue
    # Bottle" becomes a relationship with this name as the subject.
    journal_author_name: str = field(
        default_factory=lambda: os.environ.get("JOURNAL_AUTHOR_NAME", "John")
    )

    # Entity casing — operator-managed exception list for smart title-case
    # normalization applied at entity-write time. See
    # `services/entity_naming.py` and `docs/entity-tracking.md`.
    entity_casing_exceptions_path: Path = field(
        default_factory=lambda: Path(
            os.environ.get(
                "ENTITY_CASING_EXCEPTIONS_PATH",
                "config/entity-casing-exceptions.toml",
            )
        )
    )

    # ── Fitness pipeline (W3 of fitness-tier-plan.md) ──────────────
    # Strava OAuth — register an app at https://www.strava.com/settings/api
    # then drop the values here. See docs/fitness-tier-plan.md §1 P0.1.
    strava_client_id: str = field(
        default_factory=lambda: os.environ.get("STRAVA_CLIENT_ID", ""),
    )
    strava_client_secret: str = field(
        default_factory=lambda: os.environ.get("STRAVA_CLIENT_SECRET", ""),
    )
    strava_redirect_uri: str = field(
        default_factory=lambda: os.environ.get(
            "STRAVA_REDIRECT_URI", "http://localhost:8400/strava/callback",
        ),
    )
    # Garmin Connect — credentials are per-user from W2 onwards. Users
    # connect via POST /api/fitness/garmin/connect (webapp Settings panel)
    # or via `journal fitness-reauth-garmin --user-id N --username EMAIL`
    # (operator-only fallback). Token blobs are persisted in
    # `fitness_auth_state.extra_state_json["tokens_blob"]` per user. No
    # global GARMIN_USERNAME / GARMIN_PASSWORD env vars; see
    # docs/fitness-multiuser-plan.md §5 W6.
    # How many consecutive transient failures before Pushover fires
    # (per D5 in fitness-integration-plan.md). 3 is a reasonable default
    # for a daily-cadence pipeline — one bad day is noise; three in a row
    # is a real outage worth paging for.
    fitness_transient_failure_threshold: int = field(
        default_factory=lambda: int(
            os.environ.get("FITNESS_TRANSIENT_FAILURE_THRESHOLD", "3"),
        ),
    )
    # Backfill cutoff. ISO date. Defaults to 2026-01-01 — the start of
    # journal data, the only window where correlation is meaningful.
    fitness_backfill_start: str = field(
        default_factory=lambda: os.environ.get(
            "FITNESS_BACKFILL_START", "2026-01-01",
        ),
    )
    # How long an `auth_status='broken'` row must persist before
    # `/api/health` rolls up to `degraded`. 48h is long enough to ride
    # out Garmin's typical SSO-incident weekend break, short enough to
    # surface a real outage.
    fitness_health_broken_degraded_hours: int = field(
        default_factory=lambda: int(
            os.environ.get("FITNESS_HEALTH_BROKEN_DEGRADED_HOURS", "48"),
        ),
    )
    # Daily fitness auto-sync scheduler (services/fitness/scheduler.py).
    # When true (default), a daemon thread enqueues per-user Strava/Garmin
    # syncs once a day at 17:00 server-local time. Set false to disable.
    fitness_sync_enabled: bool = field(
        default_factory=lambda: os.environ.get(
            "FITNESS_SYNC_ENABLED", "true"
        ).lower() in ("1", "true", "yes", "on")
    )

    # Background job runner worker pool (services/jobs/runner.py).
    # Sizes Pool A, which runs everything except storyline jobs in
    # parallel. The storyline pool is always single-worker (ingestion
    # priority + same-storyline race avoidance) and has no knob. Must
    # be >= 1.
    job_worker_count: int = field(
        default_factory=lambda: int(os.environ.get("JOB_WORKER_COUNT", "4"))
    )

    def __post_init__(self) -> None:
        valid_providers = {"openai", "gemini"}
        if self.transcription_provider not in valid_providers:
            raise ValueError(
                "TRANSCRIPTION_PROVIDER must be 'openai' or 'gemini'"
            )
        if (
            self.transcription_shadow_provider
            and self.transcription_shadow_provider not in valid_providers
        ):
            raise ValueError(
                "TRANSCRIPTION_SHADOW_PROVIDER must be 'openai' or 'gemini'"
            )
        if self.transcription_retry_max_attempts < 1:
            raise ValueError(
                "TRANSCRIPTION_RETRY_MAX_ATTEMPTS must be >= 1"
            )
        if self.transcription_retry_base_delay < 0:
            raise ValueError(
                "TRANSCRIPTION_RETRY_BASE_DELAY must be >= 0"
            )
        if self.transcription_retry_max_delay < 0:
            raise ValueError(
                "TRANSCRIPTION_RETRY_MAX_DELAY must be >= 0"
            )
        if self.fitness_transient_failure_threshold < 1:
            raise ValueError(
                "FITNESS_TRANSIENT_FAILURE_THRESHOLD must be >= 1"
            )
        if self.fitness_health_broken_degraded_hours < 1:
            raise ValueError(
                "FITNESS_HEALTH_BROKEN_DEGRADED_HOURS must be >= 1"
            )
        if self.job_worker_count < 1:
            raise ValueError("JOB_WORKER_COUNT must be >= 1")


def load_config() -> Config:
    """Load configuration from environment variables."""
    return Config()
