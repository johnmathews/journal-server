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
from journal.services.backfill import backfill_chunk_counts, rechunk_entries
from journal.services.chunking import build_chunker
from journal.services.chunking_eval import evaluate_chunking
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

    chunker = build_chunker(config, embeddings)

    ingestion = IngestionService(
        repository=repo,
        vector_store=vector_store,
        ocr_provider=ocr,
        transcription_provider=transcription,
        embeddings_provider=embeddings,
        chunker=chunker,
        embed_metadata_prefix=config.chunking_embed_metadata_prefix,
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
    """Re-run the chunker over every entry and update its stored chunk_count.

    Fixes entries whose `chunk_count` is stale (e.g. seeded entries, or
    entries created before migration 0002 added the column). Does not touch
    the vector store — embeddings are not regenerated.
    """
    conn = get_connection(config.db_path)
    run_migrations(conn)
    repo = SQLiteEntryRepository(conn)

    # Backfill doesn't need embeddings, so pass None — SemanticChunker
    # would require one but we're intentionally using the configured
    # chunker (which, if semantic, would need embeddings; see WU-D for
    # the rechunk command that does full re-embedding).
    chunker = build_chunker(config, embeddings=None)
    result = backfill_chunk_counts(repo, chunker=chunker)

    print(f"Updated:   {result.updated}")
    print(f"Unchanged: {result.unchanged}")
    print(f"Skipped:   {result.skipped} (no text)")
    if result.errors:
        print(f"\nErrors ({len(result.errors)}):")
        for err in result.errors:
            print(f"  {err}")


def cmd_eval_chunking(args, config):
    """Measure chunking quality on the currently-stored corpus.

    Computes three numbers:
    - cohesion: sentences within a chunk are similar (higher = better)
    - separation: adjacent chunks within an entry are distinct (higher = better)
    - ratio: cohesion / (1 - separation), a single number to optimise

    No ground truth required. Re-run after `journal rechunk` to compare
    chunking configurations — higher ratio means better chunks.
    """
    import json

    conn = get_connection(config.db_path)
    run_migrations(conn)
    repo = SQLiteEntryRepository(conn)

    vector_store = ChromaVectorStore(
        host=config.chromadb_host,
        port=config.chromadb_port,
        collection_name=config.chromadb_collection,
    )
    embeddings = OpenAIEmbeddingsProvider(
        api_key=config.openai_api_key,
        model=config.embedding_model,
        dimensions=config.embedding_dimensions,
    )

    result = evaluate_chunking(repo, vector_store, embeddings)

    if args.json:
        print(json.dumps(result.as_dict(), indent=2))
        return

    print("Chunking quality (higher = better):")
    print(f"  Cohesion:   {result.cohesion:.3f}  (intra-chunk sentence similarity)")
    print(f"  Separation: {result.separation:.3f}  (inter-chunk distinctness)")
    print(f"  Ratio:      {result.ratio:.3f}  (cohesion / (1 - separation))")
    print()
    print(f"  {result.n_chunks_evaluated} chunks evaluated")
    print(f"  {result.n_entries_evaluated} entries evaluated")
    print(f"  {result.n_pairs_evaluated} adjacent chunk pairs evaluated")


def cmd_rechunk(args, config):
    """Re-run the FULL chunking + embedding pipeline over every entry.

    Unlike `backfill-chunks`, which only recomputes the `chunk_count`
    column, this command deletes each entry's existing vectors from
    ChromaDB and regenerates them using the currently-configured
    strategy. Use this when you've changed `CHUNKING_STRATEGY` or any
    semantic chunker parameter and want the stored chunks to match.

    With `--dry-run`, reports what would change without writing to
    ChromaDB or SQLite and without calling the embeddings API.
    """
    ingestion, _ = _build_services(config)
    repo = ingestion._repo  # type: ignore[attr-defined]

    result = rechunk_entries(ingestion, repo, dry_run=args.dry_run)

    prefix = "[dry-run] " if args.dry_run else ""
    print(f"{prefix}Updated:          {result.updated}")
    print(f"{prefix}Skipped:          {result.skipped} (no text)")
    print(f"{prefix}Old total chunks: {result.old_total_chunks}")
    print(f"{prefix}New total chunks: {result.new_total_chunks}")
    if result.errors:
        print(f"\nErrors ({len(result.errors)}):")
        for err in result.errors:
            print(f"  {err}")


def cmd_seed(args, config):
    """Seed the database with sample journal entries for development."""
    conn = get_connection(config.db_path)
    run_migrations(conn)
    repo = SQLiteEntryRepository(conn)

    samples = [
        {
            "date": "2026-03-15",
            "source_type": "ocr",
            "text": (
                "Woke up early today and went for a long walk through the park. "
                "The cherry blossoms are starting to bloom and the air smelled "
                "incredible. Met Atlas at the coffee shop afterwards — we talked "
                "about his new project and the upcoming trip to Vienna. Feeling "
                "optimistic about the week ahead. Need to remember to call the "
                "dentist and finish the report for work."
            ),
        },
        {
            "date": "2026-03-16",
            "source_type": "ocr",
            "text": (
                "Rainy day. Spent most of it inside reading and working on the "
                "journal analysis tool. Made good progress on the chunking "
                "algorithm — it now handles edge cases with very short paragraphs "
                "much better. Had a video call with Sarah about the conference "
                "next month. She suggested we submit a talk proposal together."
            ),
        },
        {
            "date": "2026-03-18",
            "source_type": "voice",
            "text": (
                "Quick voice note before bed. Today was intense at work — three "
                "back-to-back meetings and a production incident that took most "
                "of the afternoon to resolve. The root cause was a misconfigured "
                "timeout on the database connection pool. Lesson learned: always "
                "check the connection pool settings when deploying to a new "
                "environment. On the bright side, dinner with Emma was lovely."
            ),
        },
        {
            "date": "2026-03-20",
            "source_type": "ocr",
            "text": (
                "Took the train to Amsterdam for the day. Visited the "
                "Rijksmuseum — the Vermeer room was as stunning as ever. Had "
                "stroopwafels from that stand near Centraal Station. Walking "
                "along the canals in the late afternoon light is one of my "
                "favourite things. Bumped into Marcus at the station on the way "
                "back — small world. He's doing well, just started a new role "
                "at a fintech startup."
            ),
        },
        {
            "date": "2026-03-22",
            "source_type": "ocr",
            "text": (
                "Saturday morning journaling. This week flew by. Highlights: "
                "the Amsterdam trip, solving that production bug, and the long "
                "walk on Monday. I want to be more intentional about exercise "
                "next week — aim for at least 3 runs. Also need to start "
                "planning the Vienna trip properly. Atlas sent me a list of "
                "restaurants to try. Feeling grateful for good friends and "
                "interesting work."
            ),
        },
    ]

    # Seeding does not have access to an embeddings provider (no API keys
    # required to seed), so we force a FixedTokenChunker regardless of the
    # configured strategy. Good enough for populating chunk_count on dev data.
    from journal.services.chunking import FixedTokenChunker
    seed_chunker = FixedTokenChunker(
        max_tokens=config.chunking_max_tokens,
        overlap_tokens=config.chunking_overlap_tokens,
    )

    count = int(args.count) if hasattr(args, "count") and args.count else len(samples)
    created = 0
    for sample in samples[:count]:
        word_count = len(sample["text"].split())
        entry = repo.create_entry(
            sample["date"], sample["source_type"], sample["text"], word_count,
        )
        # Add a page record for OCR entries
        if sample["source_type"] == "ocr":
            repo.add_entry_page(entry.id, 1, sample["text"])
        # Compute and store chunk_count so the UI shows the real value even
        # though we don't generate embeddings during seeding.
        chunks = seed_chunker.chunk(sample["text"])
        repo.update_chunk_count(entry.id, len(chunks))
        created += 1
        src = sample["source_type"]
        print(
            f"  Created entry {entry.id}: {sample['date']} "
            f"({src}, {word_count} words, {len(chunks)} chunks)"
        )

    print(f"\nSeeded {created} entries.")
    print("No embeddings generated (re-ingest entries if you want semantic search).")


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
    subparsers.add_parser(
        "backfill-chunks",
        help="Re-run the chunker and update stored chunk_count (no re-embedding)",
    )

    # rechunk
    p_rechunk = subparsers.add_parser(
        "rechunk",
        help="Re-chunk and re-embed every entry using the current strategy",
    )
    p_rechunk.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing to ChromaDB or SQLite",
    )

    # eval-chunking
    p_eval = subparsers.add_parser(
        "eval-chunking",
        help="Measure chunking quality (cohesion / separation / ratio)",
    )
    p_eval.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output",
    )

    # seed
    p_seed = subparsers.add_parser(
        "seed", help="Seed database with sample entries (no API keys needed)",
    )
    p_seed.add_argument("--count", type=int, help="Number of sample entries (default: all 5)")

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
        "rechunk": cmd_rechunk,
        "eval-chunking": cmd_eval_chunking,
        "seed": cmd_seed,
    }
    commands[args.command](args, config)
