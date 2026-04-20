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
    transcription_model: str = "gpt-4o-transcribe"
    embedding_model: str = "text-embedding-3-large"
    embedding_dimensions: int = 1024

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

    # REST API / MCP bearer token. Every request to /api/* and /mcp must
    # send `Authorization: Bearer <token>` matching this value. None means
    # no token is configured, which is fail-closed: the server refuses to
    # start in `mcp_server.main()`. Generate a token with:
    #     python -c "import secrets; print(secrets.token_urlsafe(32))"
    api_bearer_token: str | None = field(
        default_factory=lambda: os.environ.get("JOURNAL_API_TOKEN") or None
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

    # Entity extraction
    entity_extraction_model: str = "claude-opus-4-6"
    entity_extraction_max_tokens: int = 4096
    entity_dedup_similarity_threshold: float = field(
        default_factory=lambda: float(
            os.environ.get("ENTITY_DEDUP_SIMILARITY_THRESHOLD", "0.88")
        )
    )
    # Name the extractor uses for the journal author — "I went to Blue
    # Bottle" becomes a relationship with this name as the subject.
    journal_author_name: str = field(
        default_factory=lambda: os.environ.get("JOURNAL_AUTHOR_NAME", "John")
    )


def load_config() -> Config:
    """Load configuration from environment variables."""
    return Config()
