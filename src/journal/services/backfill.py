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

from journal.db.repository import EntryRepository
from journal.services.chunking import chunk_text


@dataclass
class BackfillResult:
    """Outcome of a backfill run."""

    updated: int = 0
    unchanged: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


def backfill_chunk_counts(
    repository: EntryRepository,
    max_tokens: int = 150,
    overlap_tokens: int = 40,
) -> BackfillResult:
    """Recompute and persist `chunk_count` for every entry.

    Uses `final_text` when present, otherwise falls back to `raw_text`.
    Entries with no text at all are skipped. Entries whose stored count
    already matches the recomputed value are left alone.

    Does not touch the vector store — search coverage is unaffected. If
    you need to regenerate embeddings as well, re-ingest the entry.
    """
    result = BackfillResult()
    entries = repository.list_entries(limit=1_000_000)

    for entry in entries:
        text = (entry.final_text or entry.raw_text or "").strip()
        if not text:
            result.skipped += 1
            continue

        try:
            chunks = chunk_text(text, max_tokens, overlap_tokens)
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
