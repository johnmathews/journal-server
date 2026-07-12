"""CLI helper that wires up the production service stack from a Config.

Used by every command that needs ingestion / query / entity-extraction
services. Lives separately from ``cli/__init__.py`` so the per-command
modules can import it without pulling the whole CLI package.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from journal.db.factory import ConnectionFactory
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
    so the wiring stays in one place. The factory hands out per-thread
    connections; CLI commands are single-threaded so they all see the
    same connection across the call.
    """
    db_factory = ConnectionFactory(config.db_path)
    run_migrations(db_factory.get())
    repo = SQLiteEntryRepository(db_factory)

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

    entity_store = SQLiteEntityStore(db_factory)
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


@dataclass
class StorylineStack:
    """Storyline collaborators the ``bootstrap-storylines`` CLI command
    needs."""

    entry_repository: SQLiteEntryRepository
    storyline_repository: object
    engine: object


def build_storyline_stack(config: Config) -> StorylineStack:
    """Build the storyline engine + collaborators for CLI commands.

    Requires ``ANTHROPIC_API_KEY`` — storylines are an Anthropic-backed
    feature and there is no offline fallback, so we fail fast with an
    actionable error rather than constructing a half-wired stack.

    Only invoked from the ``--execute`` path of ``bootstrap-storylines``;
    the dry-run path never calls this (see ``cmd_bootstrap_storylines``),
    so listing storylines never requires an API key or constructs an LLM
    client.
    """
    if not config.anthropic_api_key:
        raise RuntimeError(
            "Storylines require ANTHROPIC_API_KEY to be set; "
            "cannot run this storyline command without it."
        )
    from journal.db.storyline_repository import SQLiteStorylineRepository
    from journal.providers.storyline_judge import AnthropicStorylineJudge
    from journal.providers.storyline_narrator import AnthropicStorylineNarrator
    from journal.services.storylines.engine import StorylineEngine

    db_factory = ConnectionFactory(config.db_path)
    run_migrations(db_factory.get())
    repo = SQLiteEntryRepository(db_factory)
    entity_store = SQLiteEntityStore(db_factory)
    embeddings = OpenAIEmbeddingsProvider(
        api_key=config.openai_api_key,
        model=config.embedding_model,
        dimensions=config.embedding_dimensions,
    )
    storyline_repository = SQLiteStorylineRepository(db_factory)
    narrator = AnthropicStorylineNarrator(
        api_key=config.anthropic_api_key,
        model=config.storyline_narrator_model,
        max_tokens=config.storyline_narrator_max_tokens,
    )
    judge = AnthropicStorylineJudge(
        api_key=config.anthropic_api_key,
        model=config.storyline_judge_model,
    )
    embedder = lambda text: embeddings.embed_texts([text])[0]  # noqa: E731
    engine = StorylineEngine(
        entity_store=entity_store,
        entry_repository=repo,
        storyline_repository=storyline_repository,
        narrator=narrator,
        judge=judge,
        embedder=embedder,
        min_publish_entries=config.storyline_min_publish_entries,
    )
    return StorylineStack(
        entry_repository=repo,
        storyline_repository=storyline_repository,
        engine=engine,
    )
