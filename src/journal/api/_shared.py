"""Shared helpers for the api/ package.

Routing rules (read `docs/code-quality-principles.md` § "Routing rules for
src/journal/api/" before adding new routes):

1. **Default — primary resource.** A route under `/api/<resource>/...` lives
   in `api/<resource>.py`. Cross-resource routes place by URL prefix root
   and call across services as needed.
2. **Override — responsibility (write/job creation).** Routes whose primary
   effect is to create a job or perform a long-running write live in a
   write module, regardless of URL prefix. The override family is split
   across three sibling modules (carved out of `ingestion.py` per the
   ~800-line size rule): `api/ingestion.py` (entry ingest + entity
   extraction + mood backfill), `api/storylines_write.py` (storyline
   create/regenerate/delete/anchors), and `api/fitness_jobs.py` (fitness
   sync + backfill). See each module's docstring for its route list and
   the rationale.

Helpers in this module are free functions — no closure capture. Resource
modules import what they need. Helpers used by a single resource module
should generally live with their consumer; this module is for genuinely
shared serialisers and utilities.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import tiktoken
from PIL import Image

if TYPE_CHECKING:
    from journal.db.pricing import PricingEntry
    from journal.models import Job


def _now_iso() -> str:
    """UTC now as ``YYYY-MM-DDTHH:MM:SSZ`` (auth-state timestamp format)."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

# Cache the encoding at module load — tiktoken.get_encoding is not free
# and the tokens endpoint may be called repeatedly as the user switches
# overlays. cl100k_base matches text-embedding-3-large, which is the
# embedding model the chunker's token counts are computed against.
_TOKEN_ENCODING_NAME = "cl100k_base"
_TOKEN_MODEL_HINT = "text-embedding-3-large"
_token_encoder = tiktoken.get_encoding(_TOKEN_ENCODING_NAME)


def _convert_heic_to_jpeg(data: bytes, quality: int = 92) -> tuple[bytes, str]:
    """Convert HEIC/HEIF image bytes to JPEG. Returns (jpeg_bytes, 'image/jpeg')."""
    import pillow_heif  # noqa: F811 — register HEIF opener with Pillow

    pillow_heif.register_heif_opener()
    img = Image.open(io.BytesIO(data))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=quality)
    return buf.getvalue(), "image/jpeg"


def _runtime_get(services: dict, key: str) -> Any:
    """Read a runtime setting, falling back to the frozen Config."""
    runtime = services.get("runtime_settings")
    if runtime is not None:
        try:
            return runtime.get(key)
        except KeyError:
            pass
    config = services.get("config")
    return getattr(config, key, None) if config else None


def _pricing_to_dict(entry: PricingEntry) -> dict[str, object]:
    """Convert a PricingEntry to a JSON-serializable dict."""
    return {
        "model": entry.model,
        "category": entry.category,
        "input_cost_per_mtok": entry.input_cost_per_mtok,
        "output_cost_per_mtok": entry.output_cost_per_mtok,
        "cost_per_minute": entry.cost_per_minute,
        "last_verified": entry.last_verified,
    }


def _entry_to_dict(
    entry: Any,
    page_count: int = 0,
    uncertain_spans: list[tuple[int, int]] | None = None,
) -> dict[str, Any]:
    """Convert an Entry to a JSON-serializable dict.

    `uncertain_spans` is a list of `(char_start, char_end)` pairs in
    ``entries.raw_text`` coordinates, flagged by the OCR model at
    ingestion time. They power the webapp's Review toggle. Callers
    that don't have the span list (or don't need it — e.g. the list
    endpoint) omit the argument; the serializer then emits an empty
    array so the field is always present in the response shape.

    When ``entry.doubts_verified`` is true, spans are suppressed —
    the user has confirmed all doubts are correct.
    """
    verified = getattr(entry, "doubts_verified", False)
    return {
        "id": entry.id,
        "entry_date": entry.entry_date,
        "source_type": entry.source_type,
        "raw_text": entry.raw_text,
        "final_text": entry.final_text,
        "word_count": entry.word_count,
        "chunk_count": entry.chunk_count,
        "page_count": page_count,
        "language": entry.language,
        "created_at": entry.created_at,
        "updated_at": entry.updated_at,
        "doubts_verified": verified,
        "uncertain_spans": []
        if verified
        else [{"char_start": start, "char_end": end} for start, end in (uncertain_spans or [])],
    }


def _entity_summary(
    entity: Any,
    mention_count: int = 0,
    last_seen: str = "",
    quotes: list[str] | None = None,
) -> dict[str, Any]:
    """Convert an Entity to a JSON-serialisable summary dict."""
    d: dict[str, Any] = {
        "id": entity.id,
        "canonical_name": entity.canonical_name,
        "entity_type": entity.entity_type,
        "aliases": list(entity.aliases),
        "mention_count": mention_count,
        "first_seen": entity.first_seen,
        "last_seen": last_seen,
        "is_quarantined": bool(getattr(entity, "is_quarantined", False)),
        "quarantine_reason": getattr(entity, "quarantine_reason", "") or "",
        "quarantined_at": getattr(entity, "quarantined_at", "") or "",
    }
    if quotes is not None:
        d["quotes"] = quotes
    return d


def _entity_detail(entity: Any) -> dict[str, Any]:
    """Convert an Entity to a full JSON-serialisable dict."""
    return {
        "id": entity.id,
        "canonical_name": entity.canonical_name,
        "entity_type": entity.entity_type,
        "aliases": list(entity.aliases),
        "description": entity.description,
        "first_seen": entity.first_seen,
        "created_at": entity.created_at,
        "updated_at": entity.updated_at,
        "is_quarantined": bool(getattr(entity, "is_quarantined", False)),
        "quarantine_reason": getattr(entity, "quarantine_reason", "") or "",
        "quarantined_at": getattr(entity, "quarantined_at", "") or "",
    }


def _mention_dict(mention: Any, entry_date: str | None = None) -> dict[str, Any]:
    return {
        "id": mention.id,
        "entity_id": mention.entity_id,
        "entry_id": mention.entry_id,
        "entry_date": entry_date,
        "quote": mention.quote,
        "confidence": mention.confidence,
        "extraction_run_id": mention.extraction_run_id,
        "created_at": mention.created_at,
    }


def _relationship_dict(rel: Any) -> dict[str, Any]:
    return {
        "id": rel.id,
        "subject_entity_id": rel.subject_entity_id,
        "predicate": rel.predicate,
        "object_entity_id": rel.object_entity_id,
        "quote": rel.quote,
        "entry_id": rel.entry_id,
        "confidence": rel.confidence,
        "extraction_run_id": rel.extraction_run_id,
        "created_at": rel.created_at,
    }


def _job_to_dict(job: Job) -> dict[str, Any]:
    """Convert a Job dataclass to a JSON-serialisable dict.

    Mirrors the canonical serialised shape the webapp consumes for
    jobs — every field is always present, even when null, so the
    client can rely on a fixed schema.
    """
    return {
        "id": job.id,
        "type": job.type,
        "status": job.status,
        "params": job.params,
        "progress_current": job.progress_current,
        "progress_total": job.progress_total,
        "result": job.result,
        "error_message": job.error_message,
        "status_detail": job.status_detail,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
    }


def _entry_summary(
    entry: Any,
    page_count: int = 0,
    uncertain_span_count: int = 0,
    entity_mention_count: int = 0,
) -> dict[str, Any]:
    """Convert an Entry to a summary dict (no text fields)."""
    return {
        "id": entry.id,
        "entry_date": entry.entry_date,
        "source_type": entry.source_type,
        "word_count": entry.word_count,
        "chunk_count": entry.chunk_count,
        "page_count": page_count,
        "uncertain_span_count": uncertain_span_count,
        "doubts_verified": getattr(entry, "doubts_verified", False),
        "created_at": entry.created_at,
        "language": getattr(entry, "language", "en"),
        "updated_at": getattr(entry, "updated_at", ""),
        "entity_mention_count": entity_mention_count,
    }


def _chunk_match_dict(cm: Any) -> dict[str, Any]:
    return {
        "text": cm.text,
        "score": cm.score,
        "chunk_index": cm.chunk_index,
        "char_start": cm.char_start,
        "char_end": cm.char_end,
    }


def _search_result_dict(result: Any) -> dict[str, Any]:
    return {
        "entry_id": result.entry_id,
        "entry_date": result.entry_date,
        "text": result.text,
        "score": result.score,
        "snippet": result.snippet,
        "matching_chunks": [_chunk_match_dict(c) for c in result.matching_chunks],
    }
