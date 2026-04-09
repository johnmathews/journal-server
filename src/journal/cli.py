"""CLI interface for the journal analysis tool."""

import argparse
import sys
from datetime import date
from pathlib import Path

from journal.config import load_config
from journal.db.connection import get_connection
from journal.db.migrations import run_migrations
from journal.db.repository import SQLiteEntryRepository
from journal.logging import setup_logging
from journal.providers.embeddings import OpenAIEmbeddingsProvider
from journal.providers.ocr import AnthropicOCRProvider
from journal.providers.transcription import OpenAITranscriptionProvider
from journal.services.ingestion import IngestionService
from journal.services.query import QueryService
from journal.vectorstore.store import ChromaVectorStore


def _build_services(config):
    conn = get_connection(config.db_path)
    run_migrations(conn)
    repo = SQLiteEntryRepository(conn)

    vector_store = ChromaVectorStore(
        host=config.chromadb_host,
        port=config.chromadb_port,
        collection_name=config.chromadb_collection,
    )

    ocr = AnthropicOCRProvider(
        api_key=config.anthropic_api_key,
        model=config.ocr_model,
        max_tokens=config.ocr_max_tokens,
    )
    transcription = OpenAITranscriptionProvider(
        api_key=config.openai_api_key,
        model=config.transcription_model,
    )
    embeddings = OpenAIEmbeddingsProvider(
        api_key=config.openai_api_key,
        model=config.embedding_model,
        dimensions=config.embedding_dimensions,
    )

    ingestion = IngestionService(
        repository=repo,
        vector_store=vector_store,
        ocr_provider=ocr,
        transcription_provider=transcription,
        embeddings_provider=embeddings,
        chunk_max_tokens=config.chunk_max_tokens,
        chunk_overlap_tokens=config.chunk_overlap_tokens,
    )
    query = QueryService(
        repository=repo,
        vector_store=vector_store,
        embeddings_provider=embeddings,
    )

    return ingestion, query


def cmd_ingest(args, config):
    """Ingest a journal entry from an image or audio file."""
    ingestion, _ = _build_services(config)
    file_path = Path(args.file)

    if not file_path.exists():
        print(f"Error: File not found: {file_path}", file=sys.stderr)
        sys.exit(1)

    data = file_path.read_bytes()
    entry_date = args.date or date.today().isoformat()

    # Detect source type from file extension
    ext = file_path.suffix.lower()
    image_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
    audio_exts = {".mp3", ".m4a", ".wav", ".mp4", ".webm"}

    media_types = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".mp3": "audio/mpeg",
        ".m4a": "audio/mp4",
        ".wav": "audio/wav",
        ".mp4": "audio/mp4",
        ".webm": "audio/webm",
    }
    media_type = media_types.get(ext, "application/octet-stream")

    if ext in image_exts:
        entry = ingestion.ingest_image(data, media_type, entry_date)
    elif ext in audio_exts:
        entry = ingestion.ingest_voice(data, media_type, entry_date, args.language)
    else:
        print(f"Error: Unsupported file type: {ext}", file=sys.stderr)
        sys.exit(1)

    print(f"Ingested entry {entry.id} for {entry.entry_date} ({entry.word_count} words)")
    print(f"Preview: {entry.final_text[:200]}...")


def cmd_search(args, config):
    """Search journal entries semantically."""
    _, query = _build_services(config)
    results = query.search_entries(args.query, args.start_date, args.end_date, args.limit)

    if not results:
        print(f"No entries found matching '{args.query}'.")
        return

    for r in results:
        print(f"\n--- {r.entry_date} (relevance: {r.score:.0%}) ---")
        print(r.text[:300])
        if len(r.text) > 300:
            print(f"... ({len(r.text)} chars total)")


def cmd_list(args, config):
    """List journal entries."""
    _, query = _build_services(config)
    entries = query.list_entries(args.start_date, args.end_date, args.limit)

    if not entries:
        print("No entries found.")
        return

    for e in entries:
        preview = e.final_text[:80].replace("\n", " ")
        print(f"{e.entry_date} | {e.source_type} | {e.word_count:>5} words | {preview}...")


def cmd_ingest_multi(args, config):
    """Ingest multiple page images as a single journal entry."""
    ingestion, _ = _build_services(config)

    images: list[tuple[bytes, str]] = []
    image_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
    media_types_map = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }

    for file_str in args.files:
        file_path = Path(file_str)
        if not file_path.exists():
            print(f"Error: File not found: {file_path}", file=sys.stderr)
            sys.exit(1)
        ext = file_path.suffix.lower()
        if ext not in image_exts:
            print(f"Error: Unsupported image type: {ext}", file=sys.stderr)
            sys.exit(1)
        media_type = media_types_map.get(ext, "application/octet-stream")
        images.append((file_path.read_bytes(), media_type))

    entry_date = args.date or date.today().isoformat()
    entry = ingestion.ingest_multi_page_entry(images, entry_date)

    print(f"Ingested multi-page entry {entry.id} for {entry.entry_date}")
    print(f"  Pages: {len(images)}, Words: {entry.word_count}, Chunks: {entry.chunk_count}")
    print(f"  Preview: {entry.final_text[:200]}...")


def cmd_backfill_chunks(args, config):
    """Backfill chunk_count for existing entries from ChromaDB."""
    conn = get_connection(config.db_path)
    run_migrations(conn)
    repo = SQLiteEntryRepository(conn)

    vector_store = ChromaVectorStore(
        host=config.chromadb_host,
        port=config.chromadb_port,
        collection_name=config.chromadb_collection,
    )

    entries = repo.list_entries(limit=10000)
    updated = 0
    for entry in entries:
        if entry.chunk_count == 0:
            # Query ChromaDB for chunks belonging to this entry
            results = vector_store._collection.get(
                where={"entry_id": entry.id},
            )
            count = len(results["ids"]) if results["ids"] else 0
            if count > 0:
                repo.update_chunk_count(entry.id, count)
                updated += 1
                print(f"  Entry {entry.id} ({entry.entry_date}): {count} chunks")

    print(f"\nBackfilled {updated} entries.")


def cmd_stats(args, config):
    """Show journal statistics."""
    _, query = _build_services(config)
    stats = query.get_statistics(args.start_date, args.end_date)

    print("Journal Statistics")
    print(f"  Total entries:          {stats.total_entries}")
    start = stats.date_range_start or "N/A"
    end = stats.date_range_end or "N/A"
    print(f"  Date range:             {start} to {end}")
    print(f"  Total words:            {stats.total_words:,}")
    print(f"  Avg words per entry:    {stats.avg_words_per_entry:.0f}")
    print(f"  Entries per month:      {stats.entries_per_month:.1f}")


def main():
    parser = argparse.ArgumentParser(
        prog="journal",
        description="Journal Analysis Tool — ingest and query personal journal entries",
    )
    parser.add_argument("--log-level", default="INFO", help="Log level (default: INFO)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # ingest
    p_ingest = subparsers.add_parser("ingest", help="Ingest a journal entry from image or audio")
    p_ingest.add_argument("file", help="Path to image or audio file")
    p_ingest.add_argument("--date", help="Entry date (ISO 8601, default: today)")
    p_ingest.add_argument("--language", default="en", help="Language for voice transcription")

    # ingest-multi
    p_ingest_multi = subparsers.add_parser(
        "ingest-multi", help="Ingest multiple pages as one entry"
    )
    p_ingest_multi.add_argument("files", nargs="+", help="Paths to image files (in page order)")
    p_ingest_multi.add_argument("--date", help="Entry date (ISO 8601, default: today)")

    # search
    p_search = subparsers.add_parser("search", help="Search entries semantically")
    p_search.add_argument("query", help="Natural language search query")
    p_search.add_argument("--start-date", help="Filter from date")
    p_search.add_argument("--end-date", help="Filter until date")
    p_search.add_argument("--limit", type=int, default=10, help="Max results")

    # list
    p_list = subparsers.add_parser("list", help="List entries")
    p_list.add_argument("--start-date", help="Filter from date")
    p_list.add_argument("--end-date", help="Filter until date")
    p_list.add_argument("--limit", type=int, default=20, help="Max results")

    # stats
    p_stats = subparsers.add_parser("stats", help="Show statistics")
    p_stats.add_argument("--start-date", help="Filter from date")
    p_stats.add_argument("--end-date", help="Filter until date")

    # backfill-chunks
    subparsers.add_parser("backfill-chunks", help="Backfill chunk_count from ChromaDB")

    args = parser.parse_args()
    setup_logging(args.log_level)
    config = load_config()

    commands = {
        "ingest": cmd_ingest,
        "ingest-multi": cmd_ingest_multi,
        "search": cmd_search,
        "list": cmd_list,
        "stats": cmd_stats,
        "backfill-chunks": cmd_backfill_chunks,
    }
    commands[args.command](args, config)
