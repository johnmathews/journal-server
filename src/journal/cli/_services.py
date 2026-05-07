"""CLI helper that wires up the production service stack from a Config.

Used by every command that needs ingestion / query / entity-extraction
services. Lives separately from ``cli/__init__.py`` so the per-command
modules can import it without pulling the whole CLI package.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from journal.db.connection import get_connection
from journal.db.migrations import run_migrations
from journal.db.repository import SQLiteEntryRepository
from journal.entitystore.store import SQLiteEntityStore
from journal.providers.embeddings import OpenAIEmbeddingsProvider
from journal.providers.extraction import AnthropicExtractionProvider
from journal.providers.ocr import build_ocr_provider
from journal.providers.transcription import build_transcription_provider
from journal.services.chunking import build_chunker
from journal.services.entity_extraction import EntityExtractionService
from journal.services.ingestion import IngestionService
from journal.services.query import QueryService
from journal.vectorstore.store import ChromaVectorStore

if TYPE_CHECKING:
    from journal.config import Config


def build_services(
    config: Config,
) -> tuple[IngestionService, QueryService, EntityExtractionService]:
    """Build the standard CLI service stack against the configured backends.

    Returns ``(ingestion, query, entity_extraction)``. Every CLI
    command that needs to talk to the storage / LLM stack calls this
    so the wiring stays in one place. The single SQLite connection
    feeding both ``SQLiteEntryRepository`` and ``SQLiteEntityStore``
    is opened with the default ``check_same_thread=True``: CLI
    commands are single-threaded.
    """
    conn = get_connection(config.db_path)
    run_migrations(conn)
    repo = SQLiteEntryRepository(conn)

    vector_store = ChromaVectorStore(
        host=config.chromadb_host,
        port=config.chromadb_port,
        collection_name=config.chromadb_collection,
    )

    ocr = build_ocr_provider(config)
    transcription = build_transcription_provider(config)
    embeddings = OpenAIEmbeddingsProvider(
        api_key=config.openai_api_key,
        model=config.embedding_model,
        dimensions=config.embedding_dimensions,
    )

    chunker = build_chunker(config, embeddings)

    ingestion = IngestionService(
        repository=repo,
        vector_store=vector_store,
        ocr_provider=ocr,
        transcription_provider=transcription,
        embeddings_provider=embeddings,
        chunker=chunker,
        embed_metadata_prefix=config.chunking_embed_metadata_prefix,
        preprocess_images=config.preprocess_images,
    )
    query = QueryService(
        repository=repo,
        vector_store=vector_store,
        embeddings_provider=embeddings,
    )

    entity_store = SQLiteEntityStore(conn)
    extraction_provider = AnthropicExtractionProvider(
        api_key=config.anthropic_api_key,
        model=config.entity_extraction_model,
        max_tokens=config.entity_extraction_max_tokens,
    )
    entity_extraction = EntityExtractionService(
        repository=repo,
        entity_store=entity_store,
        extraction_provider=extraction_provider,
        embeddings_provider=embeddings,
        author_name=config.journal_author_name,
        dedup_similarity_threshold=config.entity_dedup_similarity_threshold,
        llm_candidate_top_k=config.entity_llm_candidate_top_k,
        llm_candidate_threshold=config.entity_llm_candidate_threshold,
        llm_match_min_cosine=config.entity_llm_match_min_cosine,
    )

    return ingestion, query, entity_extraction
