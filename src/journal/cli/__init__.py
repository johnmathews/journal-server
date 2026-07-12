"""CLI interface for the journal analysis tool.

The package's ``main`` is the argparse entry point wired up in
``pyproject.toml`` (``journal = "journal.cli:main"``). Per-command
bodies live in this module unless the command is large or
self-contained; the bulkier ones split out:

- ``journal extract-entities`` / ``backfill-entity-embeddings`` /
  ``repair-entity-names`` → ``cli/entities.py``.
- ``journal backfill-mood`` → ``cli/mood.py``.
- ``journal seed`` reads its sample data from ``cli/_seed_samples.py``.

The shared service-construction helper lives in ``cli/_services.py``
so per-command modules can import it without pulling the whole
package.
"""

import argparse
import sys
from datetime import date
from pathlib import Path

from journal.cli._services import build_services as _build_services
from journal.cli.entities import (
    cmd_backfill_entity_embeddings,
    cmd_extract_entities,
    cmd_renormalise_entity_casing,
    cmd_repair_entity_names,
)
from journal.cli.fitness import (
    cmd_fitness_audit,
    cmd_fitness_backfill,
    cmd_fitness_garmin_import_token,
    cmd_fitness_garmin_mint_token,
    cmd_fitness_reauth_garmin,
    cmd_fitness_reauth_strava,
    cmd_fitness_status,
    cmd_fitness_sync,
)
from journal.cli.mood import cmd_backfill_mood
from journal.config import load_config
from journal.db.factory import ConnectionFactory
from journal.db.migrations import run_migrations
from journal.db.repository import SQLiteEntryRepository
from journal.logging import setup_logging
from journal.providers.embeddings import OpenAIEmbeddingsProvider
from journal.services.backfill import backfill_chunk_counts, rechunk_entries
from journal.services.chunking import build_chunker
from journal.services.chunking_eval import evaluate_chunking
from journal.vectorstore.store import ChromaVectorStore


def cmd_ingest(args, config):
    """Ingest a journal entry from an image or audio file."""
    ingestion, _, _ = _build_services(config)
    file_path = Path(args.file)

    if not file_path.exists():
        print(f"Error: File not found: {file_path}", file=sys.stderr)
        sys.exit(1)

    data = file_path.read_bytes()
    entry_date = args.date or date.today().isoformat()

    # Detect source type from file extension
    ext = file_path.suffix.lower()
    image_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif"}
    audio_exts = {".mp3", ".m4a", ".wav", ".mp4", ".webm"}

    media_types = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".heic": "image/heic",
        ".heif": "image/heif",
        ".mp3": "audio/mpeg",
        ".m4a": "audio/mp4",
        ".wav": "audio/wav",
        ".mp4": "audio/mp4",
        ".webm": "audio/webm",
    }
    media_type = media_types.get(ext, "application/octet-stream")

    if ext in {".heic", ".heif"}:
        from journal.api import _convert_heic_to_jpeg

        data, media_type = _convert_heic_to_jpeg(data)

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
    _, query, _ = _build_services(config)
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
    _, query, _ = _build_services(config)
    entries = query.list_entries(args.start_date, args.end_date, args.limit)

    if not entries:
        print("No entries found.")
        return

    for e in entries:
        preview = e.final_text[:80].replace("\n", " ")
        print(f"{e.entry_date} | {e.source_type} | {e.word_count:>5} words | {preview}...")


def cmd_ingest_multi(args, config):
    """Ingest multiple page images as a single journal entry."""
    ingestion, _, _ = _build_services(config)

    images: list[tuple[bytes, str]] = []
    image_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif"}
    media_types_map = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".heic": "image/heic",
        ".heif": "image/heif",
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
        img_data = file_path.read_bytes()
        if ext in {".heic", ".heif"}:
            from journal.api import _convert_heic_to_jpeg

            img_data, media_type = _convert_heic_to_jpeg(img_data)
        images.append((img_data, media_type))

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
    db_factory = ConnectionFactory(config.db_path)
    run_migrations(db_factory.get())
    repo = SQLiteEntryRepository(db_factory)

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


def _list_all_storylines(storyline_repository, user_id: int):
    """Page through every storyline for ``user_id``, any status.

    ``list_storylines`` is limit/offset paginated; this walks every page
    so the CLI never silently truncates a user with more than one page
    of storylines.
    """
    storylines = []
    limit = 100
    offset = 0
    while True:
        page = storyline_repository.list_storylines(
            user_id, status=None, limit=limit, offset=offset,
        )
        storylines.extend(page)
        if len(page) < limit:
            return storylines
        offset += limit


def _storylines_to_process(storyline_repository, args):
    """Resolve the storylines this invocation targets: one (via
    ``--storyline-id``, scoped to ``--user-id``) or all of the user's."""
    if args.storyline_id is not None:
        storyline = storyline_repository.get_storyline(
            args.storyline_id, user_id=args.user_id,
        )
        return [storyline] if storyline is not None else []
    return _list_all_storylines(storyline_repository, args.user_id)


def cmd_bootstrap_storylines(args, config):
    """Partition each of the user's storylines into judge-drawn chapters.

    Replaces the round-1 ``backfill-storyline-chapters`` (deterministic
    time-bucketed re-sectioning) and ``recheck-storylines`` (extension
    catch-up) commands, which don't exist under the judge/narrator
    engine — ``StorylineEngine.bootstrap`` now does a full-history
    partition in one call, replacing whatever chapters already exist.

    Dry-run by default: lists each storyline with its current chapter
    and entry counts and "would bootstrap". The dry-run path only opens
    the storyline repository — no engine, judge, or narrator is
    constructed and no LLM call is made. Pass ``--execute`` to actually
    run the (LLM-costed: one judge partition call + one narrator call
    per resulting chapter) bootstrap. ``--mark-read`` seeds resulting
    published chapters as already-read — for the one-time migration
    sweep bootstrapping storylines that predate this engine.
    """
    dry_run = not args.execute

    if dry_run:
        from journal.db.factory import ConnectionFactory
        from journal.db.migrations import run_migrations
        from journal.db.storyline_repository import SQLiteStorylineRepository

        db_factory = ConnectionFactory(config.db_path)
        run_migrations(db_factory.get())
        storyline_repository = SQLiteStorylineRepository(db_factory)
        storylines = _storylines_to_process(storyline_repository, args)

        print("Storyline bootstrap (DRY RUN — no changes made)")
        print(f"Candidates: {len(storylines)}\n")
        for s in storylines:
            chapter_count = len(storyline_repository.list_chapters(s.id))
            entry_count = len(storyline_repository.assigned_entry_ids(s.id))
            print(
                f"  [{s.id}] {s.name}: {chapter_count} chapter(s), "
                f"{entry_count} entries — would bootstrap"
            )
        if storylines:
            print(
                "\nRe-run with --execute to apply (one judge call + one "
                "narrator call per resulting chapter, per storyline)."
            )
        return

    from journal.cli._services import build_storyline_stack

    stack = build_storyline_stack(config)
    storylines = _storylines_to_process(stack.storyline_repository, args)

    print("Storyline bootstrap (EXECUTED)")
    print(f"Candidates: {len(storylines)}\n")
    for s in storylines:
        result = stack.engine.bootstrap(s.id, mark_read=args.mark_read)
        print(f"  [{s.id}] {s.name}: {result.chapter_count} chapter(s)")
        for w in result.warnings:
            print(f"      warning: {w}")


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

    db_factory = ConnectionFactory(config.db_path)
    run_migrations(db_factory.get())
    repo = SQLiteEntryRepository(db_factory)

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
    ingestion, _, _ = _build_services(config)
    repo = ingestion.repository

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
    db_factory = ConnectionFactory(config.db_path)
    run_migrations(db_factory.get())
    repo = SQLiteEntryRepository(db_factory)

    from journal.cli._seed_samples import SEED_SAMPLES
    samples = SEED_SAMPLES

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
        # Add a page record for photo entries
        if sample["source_type"] == "photo":
            repo.add_entry_page(entry.id, 1, sample["text"])
        # Compute and store chunks (with offsets) so the UI shows the
        # real value and the overlay works even though we don't
        # generate embeddings during seeding.
        chunks = seed_chunker.chunk(sample["text"])
        repo.replace_chunks(entry.id, chunks)
        repo.update_chunk_count(entry.id, len(chunks))
        created += 1
        src = sample["source_type"]
        print(
            f"  Created entry {entry.id}: {sample['date']} "
            f"({src}, {word_count} words, {len(chunks)} chunks)"
        )

    print(f"\nSeeded {created} entries.")
    print("No embeddings generated (re-ingest entries if you want semantic search).")


def cmd_migrate_chromadb(args, config):
    """Add user_id to all ChromaDB vectors for multi-tenant migration."""
    from journal.db.chromadb_migration import backfill_user_id

    updated = backfill_user_id(
        host=config.chromadb_host,
        port=config.chromadb_port,
        collection_name=config.chromadb_collection,
        admin_user_id=1,
    )
    print(f"Updated {updated} ChromaDB documents with user_id=1")


def cmd_stats(args, config):
    """Show journal statistics."""
    _, query, _ = _build_services(config)
    stats = query.get_statistics(args.start_date, args.end_date)

    print("Journal Statistics")
    print(f"  Total entries:          {stats.total_entries}")
    start = stats.date_range_start or "N/A"
    end = stats.date_range_end or "N/A"
    print(f"  Date range:             {start} to {end}")
    print(f"  Total words:            {stats.total_words:,}")
    print(f"  Avg words per entry:    {stats.avg_words_per_entry:.0f}")
    print(f"  Entries per month:      {stats.entries_per_month:.1f}")


def cmd_health(args, config):
    """Print the same payload served by the `/health` HTTP endpoint.

    Builds the services locally, runs the ingestion stats query
    and all liveness checks, and emits the result as pretty JSON
    (default) or a compact single-line JSON blob (`--compact`).

    Exit code is 0 when the overall status is `ok` or `degraded`,
    non-zero when it is `error`. Docker / cron consumers can pipe
    the output to `jq` or `grep` without caring about the format.
    """
    import json
    from dataclasses import asdict
    from datetime import UTC, datetime

    from journal.services.liveness import (
        check_api_key,
        check_chromadb,
        check_sqlite,
        overall_status,
    )

    db_factory = ConnectionFactory(config.db_path)
    conn = db_factory.get()
    run_migrations(conn)
    repo = SQLiteEntryRepository(db_factory)
    vector_store = ChromaVectorStore(
        host=config.chromadb_host,
        port=config.chromadb_port,
        collection_name=config.chromadb_collection,
    )

    ingestion = repo.get_ingestion_stats(now=datetime.now(UTC))
    checks = [
        check_sqlite(conn),
        check_chromadb(vector_store),
        check_api_key("anthropic", config.anthropic_api_key),
        check_api_key("openai", config.openai_api_key),
    ]
    status = overall_status(checks)

    payload = {
        "status": status,
        "checks": [asdict(c) for c in checks],
        "ingestion": asdict(ingestion),
        # The CLI builds its services fresh, so there are no query
        # stats to show — the in-process stats collector is only
        # populated by the long-running server. Surface an explicit
        # zero rather than pretending.
        "queries": {
            "total_queries": 0,
            "uptime_seconds": 0.0,
            "started_at": None,
            "by_type": {},
        },
    }

    if args.compact:
        print(json.dumps(payload, separators=(",", ":")))
    else:
        print(json.dumps(payload, indent=2, sort_keys=True))

    if status == "error":
        sys.exit(2)


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

    # health
    p_health = subparsers.add_parser(
        "health",
        help="Print the operational health payload (same shape as GET /health)",
    )
    p_health.add_argument(
        "--compact",
        action="store_true",
        help="Emit compact single-line JSON instead of the default indented form",
    )

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

    # backfill-mood
    p_backfill_mood = subparsers.add_parser(
        "backfill-mood",
        help=(
            "Score journal entries against the configured mood "
            "dimensions (sparse by default — only entries missing "
            "a current dimension unless --force)"
        ),
    )
    p_backfill_mood.add_argument(
        "--force",
        action="store_true",
        help=(
            "Rescore every entry in the window, not just those "
            "missing a current dimension"
        ),
    )
    p_backfill_mood.add_argument(
        "--prune-retired",
        action="store_true",
        help=(
            "Delete mood_scores rows whose dimension is not in "
            "the current config. Off by default; historical "
            "scores are preserved unless you pass this flag."
        ),
    )
    p_backfill_mood.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Count what would be scored/pruned without making "
            "any network or DB writes"
        ),
    )
    p_backfill_mood.add_argument(
        "--start-date",
        help="Filter entries from this date (inclusive, ISO 8601)",
    )
    p_backfill_mood.add_argument(
        "--end-date",
        help="Filter entries until this date (inclusive, ISO 8601)",
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

    # migrate-chromadb
    subparsers.add_parser(
        "migrate-chromadb",
        help="Add user_id metadata to all ChromaDB vectors (multi-tenant migration)",
    )

    # extract-entities
    p_extract = subparsers.add_parser(
        "extract-entities",
        help="Run the entity extraction batch job over one or more entries",
    )
    p_extract.add_argument("--entry-id", type=int, help="Extract a single entry by id")
    p_extract.add_argument("--start-date", help="Filter entries from this date (ISO 8601)")
    p_extract.add_argument("--end-date", help="Filter entries until this date (ISO 8601)")
    p_extract.add_argument(
        "--stale-only",
        action="store_true",
        help="Only process entries flagged as stale",
    )

    # backfill-entity-embeddings
    p_reembed = subparsers.add_parser(
        "backfill-entity-embeddings",
        help=(
            "Re-embed every entity that has a non-empty description so "
            "the stored embedding reflects the current text. Used after "
            "deploying the description-driven recognition feature."
        ),
    )
    p_reembed.add_argument(
        "--user-id",
        type=int,
        help="Restrict the backfill to one user (default: all users)",
    )
    p_reembed.add_argument(
        "--dry-run",
        action="store_true",
        help="Count candidates without calling the embeddings API",
    )

    # repair-entity-names
    p_repair = subparsers.add_parser(
        "repair-entity-names",
        help=(
            "Find entities whose canonical_name was clipped by the LLM "
            "(e.g. 'Nautilin' instead of 'Nautiline'). Dry-run by default; "
            "pass --apply to update rows."
        ),
    )
    p_repair.add_argument(
        "--apply",
        action="store_true",
        help="Apply proposed repairs (default is dry-run)",
    )

    # renormalise-entity-casing
    p_renorm = subparsers.add_parser(
        "renormalise-entity-casing",
        help=(
            "Re-apply smart_title_case + the exceptions TOML to every "
            "existing entity's canonical_name. Dry-run by default; pass "
            "--apply to update rows."
        ),
    )
    p_renorm.add_argument(
        "--apply",
        action="store_true",
        help="Apply proposed renames (default is dry-run)",
    )

    # fitness-reauth-strava
    p_fit_strava = subparsers.add_parser(
        "fitness-reauth-strava",
        help=(
            "Run the Strava OAuth flow and persist tokens. Prints the "
            "authorize URL, blocks on a one-shot HTTP listener, exchanges "
            "the received code, upserts fitness_auth_state with "
            "auth_status='ok'."
        ),
    )
    p_fit_strava.add_argument(
        "--user-id",
        type=int,
        required=True,
        help="Owner of the auth row (required — no default).",
    )
    p_fit_strava.add_argument(
        "--code",
        default=None,
        help=(
            "Skip the OAuth listener and exchange this authorization code "
            "directly. Use when the listener cannot bind (e.g. running "
            "inside the long-running server container, or behind a NAT "
            "without port-forwarding). Obtain the code by visiting the "
            "Strava authorize URL in a browser and copying the `code` "
            "query param from the redirect URL."
        ),
    )

    # fitness-reauth-garmin
    p_fit_garmin = subparsers.add_parser(
        "fitness-reauth-garmin",
        help=(
            "Log into Garmin Connect (with optional MFA) and persist the "
            "token blob. --username is required; password is read from "
            "stdin via getpass (never from env vars). Operator-only "
            "fallback for the per-user webapp connect flow."
        ),
    )
    p_fit_garmin.add_argument(
        "--user-id",
        type=int,
        required=True,
        help="Owner of the auth row (required — no default).",
    )
    p_fit_garmin.add_argument(
        "--username",
        required=True,
        help=(
            "Garmin Connect login email (required — no env-var fallback)."
        ),
    )

    # fitness-garmin-mint-token
    p_fit_mint = subparsers.add_parser(
        "fitness-garmin-mint-token",
        help=(
            "Log into Garmin and print a portable token envelope to stdout "
            "(no DB writes). Run this on a laptop / unflagged network when "
            "Garmin's Cloudflare bot defenses are blocking the server's IP, "
            "then feed the envelope to fitness-garmin-import-token."
        ),
    )
    p_fit_mint.add_argument(
        "--username",
        required=True,
        help="Garmin Connect login email (required — no env-var fallback).",
    )
    p_fit_mint.add_argument(
        "--output",
        default="-",
        help="Where to write the JSON envelope ('-' for stdout, the default).",
    )

    # fitness-garmin-import-token
    p_fit_import = subparsers.add_parser(
        "fitness-garmin-import-token",
        help=(
            "Read a token envelope from fitness-garmin-mint-token and persist "
            "it into fitness_auth_state (auth_status='ok'). No network login "
            "— run this on the server."
        ),
    )
    p_fit_import.add_argument(
        "--user-id",
        type=int,
        required=True,
        help="Owner of the auth row (required — no default).",
    )
    p_fit_import.add_argument(
        "--input",
        default="-",
        help="JSON envelope source ('-' for stdin, the default, or a path).",
    )

    # fitness-sync
    p_fit_sync = subparsers.add_parser(
        "fitness-sync",
        help=(
            "Run a fitness sync inline (fetch + normalize) for the "
            "requested source(s). Mirrors the long-running server's "
            "scheduled sync but synchronous from the CLI."
        ),
    )
    p_fit_sync.add_argument(
        "--source",
        choices=("strava", "garmin", "both"),
        default="both",
        help="Which source to sync (default: both)",
    )
    p_fit_sync.add_argument(
        "--since",
        help=(
            "Earliest local_date to fetch (ISO 8601). Defaults to the "
            "fetch service's window logic."
        ),
    )
    p_fit_sync.add_argument(
        "--user-id",
        type=int,
        required=True,
        help="Owner of the sync (required — no default).",
    )

    # fitness-backfill
    p_fit_backfill = subparsers.add_parser(
        "fitness-backfill",
        help=(
            "Walk historical Strava/Garmin data in 30-day windows from "
            "--start (default 2026-01-01) to --end (default today). "
            "Resume predicate is per-source MAX(local_date) so re-running "
            "after a crash picks up where it left off."
        ),
    )
    p_fit_backfill.add_argument(
        "--source",
        choices=("strava", "garmin", "both"),
        default="both",
        help="Which source to backfill (default: both)",
    )
    p_fit_backfill.add_argument(
        "--start",
        default="2026-01-01",
        help="Earliest local_date to fetch (ISO 8601, default: 2026-01-01)",
    )
    p_fit_backfill.add_argument(
        "--end",
        help="Latest local_date to fetch (ISO 8601, default: today UTC)",
    )
    p_fit_backfill.add_argument(
        "--user-id",
        type=int,
        required=True,
        help="Owner of the backfill (required — no default).",
    )

    # fitness-status
    p_fit_status = subparsers.add_parser(
        "fitness-status",
        help=(
            "Print per-source auth + last-runs snapshot. Same shape as "
            "GET /api/fitness/sync/status."
        ),
    )
    p_fit_status.add_argument(
        "--user-id",
        type=int,
        required=True,
        help="User to query (required — no default).",
    )

    # fitness-audit
    subparsers.add_parser(
        "fitness-audit",
        help=(
            "Audit per-user data isolation across every fitness table. "
            "Reports row counts and any rows with NULL or orphan user_id. "
            "Exits 1 on violations."
        ),
    )

    # bootstrap-storylines
    p_bootstrap = subparsers.add_parser(
        "bootstrap-storylines",
        help=(
            "Partition a user's storylines into judge-drawn chapters via "
            "StorylineEngine.bootstrap. Replaces the old "
            "backfill-storyline-chapters / recheck-storylines commands. "
            "Dry-run by default."
        ),
    )
    p_bootstrap.add_argument(
        "--user-id",
        type=int,
        required=True,
        help="Owner whose storylines are bootstrapped (required — no default).",
    )
    p_bootstrap.add_argument(
        "--storyline-id",
        type=int,
        default=None,
        help="Restrict to a single storyline (default: all for the user).",
    )
    p_bootstrap.add_argument(
        "--mark-read",
        action="store_true",
        help=(
            "Seed resulting published chapters as already-read. For the "
            "one-time migration sweep — leave off for routine re-bootstraps."
        ),
    )
    p_bootstrap.add_argument(
        "--execute",
        action="store_true",
        help=(
            "Actually bootstrap (LLM-costed: one judge partition call + "
            "one narrator call per resulting chapter, per storyline). "
            "Without this flag the command only reports (dry-run)."
        ),
    )

    args = parser.parse_args()
    setup_logging(args.log_level)
    config = load_config()

    commands = {
        "ingest": cmd_ingest,
        "ingest-multi": cmd_ingest_multi,
        "search": cmd_search,
        "list": cmd_list,
        "stats": cmd_stats,
        "health": cmd_health,
        "backfill-chunks": cmd_backfill_chunks,
        "backfill-mood": cmd_backfill_mood,
        "rechunk": cmd_rechunk,
        "eval-chunking": cmd_eval_chunking,
        "seed": cmd_seed,
        "extract-entities": cmd_extract_entities,
        "backfill-entity-embeddings": cmd_backfill_entity_embeddings,
        "repair-entity-names": cmd_repair_entity_names,
        "renormalise-entity-casing": cmd_renormalise_entity_casing,
        "migrate-chromadb": cmd_migrate_chromadb,
        "fitness-reauth-strava": cmd_fitness_reauth_strava,
        "fitness-reauth-garmin": cmd_fitness_reauth_garmin,
        "fitness-garmin-mint-token": cmd_fitness_garmin_mint_token,
        "fitness-garmin-import-token": cmd_fitness_garmin_import_token,
        "fitness-sync": cmd_fitness_sync,
        "fitness-backfill": cmd_fitness_backfill,
        "fitness-status": cmd_fitness_status,
        "fitness-audit": cmd_fitness_audit,
        "bootstrap-storylines": cmd_bootstrap_storylines,
    }
    commands[args.command](args, config)
