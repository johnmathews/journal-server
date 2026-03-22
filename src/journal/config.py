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

    # Anthropic (OCR)
    anthropic_api_key: str = field(
        default_factory=lambda: os.environ.get("ANTHROPIC_API_KEY", "")
    )
    ocr_model: str = "claude-opus-4-6"
    ocr_max_tokens: int = 4096

    # OpenAI (Whisper + Embeddings)
    openai_api_key: str = field(default_factory=lambda: os.environ.get("OPENAI_API_KEY", ""))
    transcription_model: str = "gpt-4o-transcribe"
    embedding_model: str = "text-embedding-3-large"
    embedding_dimensions: int = 1024

    # Chunking
    chunk_max_tokens: int = 500
    chunk_overlap_tokens: int = 100

    # MCP Server
    mcp_host: str = field(default_factory=lambda: os.environ.get("MCP_HOST", "0.0.0.0"))
    mcp_port: int = field(default_factory=lambda: int(os.environ.get("MCP_PORT", "8000")))


def load_config() -> Config:
    """Load configuration from environment variables."""
    return Config()
