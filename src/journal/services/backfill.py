"""Backfill utilities — recompute per-entry metadata that may be missing.

The `backfill_chunk_counts` function exists because entries created before
migration 0002 (which added the `chunk_count` column) were backfilled to 0,
and because the `seed` CLI command historically inserted entries without
invoking the chunker. Running this backfill re-runs the chunker over every
entry's `final_text || raw_text` and updates the stored column. It does
**not** generate embeddings — it only syncs the stored chunk count to what
the chunker would produce today.
"""

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from journal.db.repository import EntryRepository
from journal.services.chunking import ChunkingStrategy

if TYPE_CHECKING:
    from journal.services.ingestion import IngestionService
    from journal.services.mood_scoring import MoodScoringService

log = logging.getLogger(__name__)


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
    """Recompute and persist `chunk_count` AND `entry_chunks` rows.

    Uses `final_text` when present, otherwise falls back to `raw_text`.
    Entries with no text at all are skipped.

    For every entry the chunker is re-run and the resulting `ChunkSpan`
    list is written to the `entry_chunks` table via `replace_chunks` —
    this populates chunks-with-offsets for entries that were ingested
    before migration 0003 (when chunks only lived in ChromaDB). The
    stored `chunk_count` column is also refreshed.

    "Unchanged" here means both the stored count matches the recomputed
    count AND the same number of chunk rows already exist in
    `entry_chunks`; otherwise the row set is rewritten so offsets stay
    in sync with the chunker's current output.

    Does not touch the vector store — search coverage is unaffected. If
    you need to regenerate embeddings as well, use `rechunk_entries` or
    re-ingest the entry.
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

        existing_rows = len(repository.get_chunks(entry.id))
        if new_count == entry.chunk_count and existing_rows == new_count:
            result.unchanged += 1
            continue

        repository.replace_chunks(entry.id, chunks)
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


@dataclass
class MoodBackfillResult:
    """Outcome of `backfill_mood_scores`."""

    scored: int = 0          # entries successfully re-scored
    skipped: int = 0         # entries skipped (no text, already current, etc.)
    pruned: int = 0          # mood_scores rows deleted when --prune-retired
    errors: list[str] = field(default_factory=list)
    # Set True when dry-run flag is on; counters then reflect what
    # WOULD happen, no scoring calls or writes actually executed.
    dry_run: bool = False


def backfill_mood_scores(
    *,
    repository: EntryRepository,
    mood_scoring: "MoodScoringService",
    mode: str = "stale-only",
    start_date: str | None = None,
    end_date: str | None = None,
    prune_retired: bool = False,
    dry_run: bool = False,
) -> MoodBackfillResult:
    """Backfill mood scores against the currently-loaded dimensions.

    Modes:

    - **`stale-only`** (default): score entries that are missing at
      least one of the current dimensions. Idempotent and cheap —
      repeatedly running this walks toward completeness without
      re-scoring already-complete entries.
    - **`force`**: rescore every entry in the selected date range,
      regardless of what's already stored. Used when you edit a
      dimension's `notes`/labels and want deterministic
      reinterpretation.

    `prune_retired`, when set, deletes `mood_scores` rows whose
    `dimension` is not in the currently loaded tuple. Preserved
    across runs by default so historical data survives config
    edits; users opt into deletion explicitly.

    `dry_run=True` makes the function count what it would do but
    not call the scorer or write to the database. Returns a
    `MoodBackfillResult` with `dry_run=True` set so the CLI can
    distinguish "would do" from "did".

    Returns a `MoodBackfillResult`. Per-entry errors are captured
    and do not abort the batch — the backfill should make
    progress even if one entry happens to trigger an LLM failure.
    """
    if mode not in ("stale-only", "force"):
        raise ValueError(
            f"Unsupported mode {mode!r}; must be 'stale-only' or 'force'"
        )

    result = MoodBackfillResult(dry_run=dry_run)
    dim_names = [d.name for d in mood_scoring.dimensions]

    if not dim_names:
        log.warning(
            "No mood dimensions loaded; nothing to backfill."
        )
        return result

    # Prune first so `--prune-retired` with `--dry-run` still
    # reports what WOULD be removed, but does not actually delete.
    if prune_retired:
        if dry_run:
            # Dry-run count: every row whose dimension is NOT in
            # the current set. Using a lightweight count query
            # rather than the real prune keeps the dry-run path
            # side-effect-free.
            placeholders = ",".join("?" for _ in dim_names)
            row = repository._conn.execute(  # type: ignore[attr-defined]
                f"SELECT COUNT(*) AS cnt FROM mood_scores "
                f"WHERE dimension NOT IN ({placeholders})",
                tuple(dim_names),
            ).fetchone()
            result.pruned = int(row["cnt"])
        else:
            result.pruned = repository.prune_retired_mood_scores(
                dim_names
            )

    # Pick the target entry set.
    if mode == "stale-only":
        entry_ids = repository.get_entries_missing_mood_scores(dim_names)
    else:
        # Force mode: every entry (optionally date-windowed).
        entries = repository.list_entries(
            start_date=start_date,
            end_date=end_date,
            limit=1_000_000,
        )
        entry_ids = [e.id for e in entries]

    # Apply date window to stale-only mode too — cheaper as a
    # post-filter than a second SQL query.
    if mode == "stale-only" and (start_date or end_date):
        filtered: list[int] = []
        for entry_id in entry_ids:
            entry = repository.get_entry(entry_id)
            if entry is None:
                continue
            if start_date and entry.entry_date < start_date:
                continue
            if end_date and entry.entry_date > end_date:
                continue
            filtered.append(entry_id)
        entry_ids = filtered

    for entry_id in entry_ids:
        entry = repository.get_entry(entry_id)
        if entry is None:
            result.skipped += 1
            continue
        text = (entry.final_text or entry.raw_text or "").strip()
        if not text:
            result.skipped += 1
            continue

        if dry_run:
            result.scored += 1
            continue

        try:
            n = mood_scoring.score_entry(entry.id, text)
        except Exception as exc:  # noqa: BLE001 — batch resilience
            result.errors.append(f"entry {entry.id}: {exc}")
            continue

        if n > 0:
            result.scored += 1
        else:
            result.skipped += 1

    return result
