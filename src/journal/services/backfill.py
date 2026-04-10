"""Backfill utilities — recompute per-entry metadata that may be missing.

The `backfill_chunk_counts` function exists because entries created before
migration 0002 (which added the `chunk_count` column) were backfilled to 0,
and because the `seed` CLI command historically inserted entries without
invoking the chunker. Running this backfill re-runs the chunker over every
entry's `final_text || raw_text` and updates the stored column. It does
**not** generate embeddings — it only syncs the stored chunk count to what
the chunker would produce today.
"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from journal.db.repository import EntryRepository
from journal.services.chunking import ChunkingStrategy

if TYPE_CHECKING:
    from journal.services.ingestion import IngestionService


@dataclass
class BackfillResult:
    """Outcome of a backfill run."""

    updated: int = 0
    unchanged: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class RechunkResult:
    """Outcome of a rechunk run — re-runs chunking AND re-embeds every entry."""

    updated: int = 0
    skipped: int = 0
    old_total_chunks: int = 0
    new_total_chunks: int = 0
    errors: list[str] = field(default_factory=list)


def backfill_chunk_counts(
    repository: EntryRepository,
    chunker: ChunkingStrategy,
) -> BackfillResult:
    """Recompute and persist `chunk_count` for every entry.

    Uses `final_text` when present, otherwise falls back to `raw_text`.
    Entries with no text at all are skipped. Entries whose stored count
    already matches the recomputed value are left alone.

    Does not touch the vector store — search coverage is unaffected. If
    you need to regenerate embeddings as well, use `rechunk_entries`
    (added in WU-D) or re-ingest the entry.
    """
    result = BackfillResult()
    entries = repository.list_entries(limit=1_000_000)

    for entry in entries:
        text = (entry.final_text or entry.raw_text or "").strip()
        if not text:
            result.skipped += 1
            continue

        try:
            chunks = chunker.chunk(text)
            new_count = len(chunks)
        except Exception as exc:  # noqa: BLE001 — surface any chunker failure
            result.errors.append(f"entry {entry.id}: {exc}")
            continue

        if new_count == entry.chunk_count:
            result.unchanged += 1
            continue

        repository.update_chunk_count(entry.id, new_count)
        result.updated += 1

    return result


def rechunk_entries(
    ingestion_service: "IngestionService",
    repository: EntryRepository,
    *,
    dry_run: bool = False,
) -> RechunkResult:
    """Re-chunk and re-embed every entry using the currently-configured strategy.

    Unlike `backfill_chunk_counts` (which only updates the `chunk_count`
    column), this function:
    - Deletes the entry's existing vectors from ChromaDB
    - Runs the full chunk → embed → store pipeline (via
      `IngestionService.rechunk_entry`)
    - Updates the stored `chunk_count`

    Use this after changing `chunking_strategy` or any semantic-chunker
    parameter — the old stored chunks would otherwise reflect the old
    strategy.

    When `dry_run=True`, reports what *would* change without touching
    the vector store or SQLite. Embeddings are not computed in dry-run
    mode either, so it's free to run.

    Per-entry errors are captured in `RechunkResult.errors` and do not
    abort the batch.
    """
    result = RechunkResult()
    entries = repository.list_entries(limit=1_000_000)

    for entry in entries:
        text = (entry.final_text or entry.raw_text or "").strip()
        if not text:
            result.skipped += 1
            continue

        try:
            result.old_total_chunks += entry.chunk_count
            new_count = ingestion_service.rechunk_entry(entry.id, dry_run=dry_run)
            result.new_total_chunks += new_count
            result.updated += 1
        except Exception as exc:  # noqa: BLE001 — surface any pipeline failure
            result.errors.append(f"entry {entry.id}: {exc}")
            continue

    return result
