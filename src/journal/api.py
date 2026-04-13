"""REST API endpoints for the journal webapp."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import tiktoken
from starlette.responses import JSONResponse

from journal.services.liveness import (
    check_api_key,
    check_chromadb,
    check_sqlite,
    overall_status,
)

# Cache the encoding at module load — tiktoken.get_encoding is not free
# and the tokens endpoint may be called repeatedly as the user switches
# overlays. cl100k_base matches text-embedding-3-large, which is the
# embedding model the chunker's token counts are computed against.
_TOKEN_ENCODING_NAME = "cl100k_base"
_TOKEN_MODEL_HINT = "text-embedding-3-large"
_token_encoder = tiktoken.get_encoding(_TOKEN_ENCODING_NAME)

if TYPE_CHECKING:
    from collections.abc import Callable

    from mcp.server.fastmcp import FastMCP
    from starlette.requests import Request

    from journal.db.jobs_repository import SQLiteJobRepository
    from journal.entitystore.store import EntityStore
    from journal.models import Job
    from journal.services.ingestion import IngestionService
    from journal.services.jobs import JobRunner
    from journal.services.query import QueryService

log = logging.getLogger(__name__)


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
    """
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
        "uncertain_spans": [
            {"char_start": start, "char_end": end}
            for start, end in (uncertain_spans or [])
        ],
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
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
    }


def _entry_summary(
    entry: Any, page_count: int = 0, uncertain_span_count: int = 0,
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
        "created_at": entry.created_at,
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


def register_api_routes(
    mcp: FastMCP,
    services_getter: Callable[[], dict | None],
) -> None:
    """Register REST API routes on the MCP server.

    Args:
        mcp: The FastMCP instance.
        services_getter: A callable that returns the services dict
            (with 'query' and 'ingestion' keys).
    """

    @mcp.custom_route("/api/entries", methods=["GET"], name="api_list_entries")
    async def list_entries(request: Request) -> JSONResponse:
        """List journal entries with pagination and optional date filtering."""
        services = services_getter()
        if services is None:
            log.error("GET /api/entries — services not initialized")
            return JSONResponse(
                {"error": "Server not initialized"}, status_code=503
            )

        query_svc: QueryService = services["query"]

        # Parse query params
        start_date = request.query_params.get("start_date")
        end_date = request.query_params.get("end_date")
        try:
            limit = min(int(request.query_params.get("limit", "20")), 100)
        except ValueError:
            limit = 20
        try:
            offset = max(int(request.query_params.get("offset", "0")), 0)
        except ValueError:
            offset = 0

        entries = query_svc.list_entries(start_date, end_date, limit, offset)
        total = query_svc._repo.count_entries(start_date, end_date)

        items = []
        for entry in entries:
            page_count = query_svc._repo.get_page_count(entry.id)
            span_count = query_svc._repo.get_uncertain_span_count(entry.id)
            items.append(_entry_summary(entry, page_count, span_count))

        log.info("GET /api/entries — returned %d/%d entries (offset=%d)", len(items), total, offset)
        return JSONResponse({
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
        })

    @mcp.custom_route(
        "/api/entries/{entry_id:int}",
        methods=["GET", "PATCH", "DELETE"],
        name="api_entry_detail",
    )
    async def entry_detail(request: Request) -> JSONResponse:
        """Get, update, or delete a single journal entry."""
        services = services_getter()
        if services is None:
            return JSONResponse(
                {"error": "Server not initialized"}, status_code=503
            )

        entry_id = int(request.path_params["entry_id"])

        if request.method == "GET":
            return await _get_entry(services, entry_id)
        elif request.method == "PATCH":
            return await _patch_entry(request, services, entry_id)
        elif request.method == "DELETE":
            return await _delete_entry(services, entry_id)
        else:
            return JSONResponse(
                {"error": "Method not allowed"}, status_code=405
            )

    async def _get_entry(services: dict, entry_id: int) -> JSONResponse:
        query_svc: QueryService = services["query"]
        entry = query_svc._repo.get_entry(entry_id)
        if entry is None:
            log.warning("GET /api/entries/%d — not found", entry_id)
            return JSONResponse(
                {"error": f"Entry {entry_id} not found"}, status_code=404
            )
        page_count = query_svc._repo.get_page_count(entry_id)
        uncertain_spans = query_svc._repo.get_uncertain_spans(entry_id)
        log.info("GET /api/entries/%d — %s, %d words", entry_id, entry.entry_date, entry.word_count)
        return JSONResponse(_entry_to_dict(entry, page_count, uncertain_spans))

    async def _patch_entry(
        request: Request, services: dict, entry_id: int
    ) -> JSONResponse:
        query_svc: QueryService = services["query"]
        ingestion_svc: IngestionService = services["ingestion"]

        # Verify entry exists
        entry = query_svc._repo.get_entry(entry_id)
        if entry is None:
            return JSONResponse(
                {"error": f"Entry {entry_id} not found"}, status_code=404
            )

        # Parse request body
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return JSONResponse(
                {"error": "Invalid JSON body"}, status_code=400
            )

        final_text = body.get("final_text")
        new_date = body.get("entry_date")

        if final_text is None and new_date is None:
            return JSONResponse(
                {"error": "At least one of 'final_text' or 'entry_date' is required"},
                status_code=400,
            )

        updated = entry

        # Update date if provided
        if new_date is not None:
            if not isinstance(new_date, str) or not new_date.strip():
                return JSONResponse(
                    {"error": "'entry_date' must be a non-empty string"},
                    status_code=400,
                )
            try:
                import datetime as dt
                dt.date.fromisoformat(new_date)
            except ValueError:
                return JSONResponse(
                    {"error": "'entry_date' must be a valid ISO 8601 date (YYYY-MM-DD)"},
                    status_code=400,
                )
            updated = query_svc._repo.update_entry_date(entry_id, new_date)

        # Update text if provided
        entity_extraction_job_id: str | None = None
        reprocess_job_id: str | None = None
        mood_job_id: str | None = None
        if final_text is not None:
            if not isinstance(final_text, str):
                return JSONResponse(
                    {"error": "'final_text' must be a string"},
                    status_code=400,
                )
            if not final_text.strip():
                return JSONResponse(
                    {"error": "'final_text' must not be empty"},
                    status_code=400,
                )
            try:
                updated = ingestion_svc.save_final_text(entry_id, final_text)
            except ValueError as e:
                log.warning("PATCH /api/entries/%d — error: %s", entry_id, e)
                return JSONResponse({"error": str(e)}, status_code=400)

            # Queue background jobs: re-embedding (slow), entity
            # re-extraction, and mood re-scoring. All are best-effort
            # — don't fail the save if the job queue is unavailable.
            job_runner: JobRunner | None = services.get("job_runner")
            if job_runner is not None:
                try:
                    job = job_runner.submit_reprocess_embeddings(entry_id)
                    reprocess_job_id = job.id
                    log.info(
                        "PATCH /api/entries/%d — queued reprocess-embeddings job %s",
                        entry_id,
                        job.id,
                    )
                except Exception:
                    log.warning(
                        "PATCH /api/entries/%d — failed to queue reprocess-embeddings",
                        entry_id,
                        exc_info=True,
                    )

                try:
                    job = job_runner.submit_entity_extraction(
                        {"entry_id": entry_id}
                    )
                    entity_extraction_job_id = job.id
                    log.info(
                        "PATCH /api/entries/%d — queued entity re-extraction job %s",
                        entry_id,
                        job.id,
                    )
                except Exception:
                    log.warning(
                        "PATCH /api/entries/%d — failed to queue entity re-extraction",
                        entry_id,
                        exc_info=True,
                    )

                config = services.get("config")
                if config and config.enable_mood_scoring:
                    try:
                        mood_job = job_runner.submit_mood_score_entry(entry_id)
                        mood_job_id = mood_job.id
                        log.info(
                            "PATCH /api/entries/%d — queued mood re-scoring job %s",
                            entry_id,
                            mood_job.id,
                        )
                    except Exception:
                        log.warning(
                            "PATCH /api/entries/%d — failed to queue mood re-scoring",
                            entry_id,
                            exc_info=True,
                        )

        page_count = query_svc._repo.get_page_count(entry_id)
        uncertain_spans = query_svc._repo.get_uncertain_spans(entry_id)
        log.info("PATCH /api/entries/%d — updated", entry_id)
        resp = _entry_to_dict(updated, page_count, uncertain_spans)
        if entity_extraction_job_id is not None:
            resp["entity_extraction_job_id"] = entity_extraction_job_id
        if reprocess_job_id is not None:
            resp["reprocess_job_id"] = reprocess_job_id
        if mood_job_id is not None:
            resp["mood_job_id"] = mood_job_id
        return JSONResponse(resp)

    async def _delete_entry(services: dict, entry_id: int) -> JSONResponse:
        ingestion_svc: IngestionService = services["ingestion"]
        deleted = ingestion_svc.delete_entry(entry_id)
        if not deleted:
            log.warning("DELETE /api/entries/%d — not found", entry_id)
            return JSONResponse(
                {"error": f"Entry {entry_id} not found"}, status_code=404
            )
        log.info("DELETE /api/entries/%d — deleted", entry_id)
        return JSONResponse({"deleted": True, "id": entry_id})

    @mcp.custom_route(
        "/api/entries/{entry_id:int}/chunks",
        methods=["GET"],
        name="api_entry_chunks",
    )
    async def entry_chunks(request: Request) -> JSONResponse:
        """Return the persisted chunks for an entry, with source offsets.

        Used by the webapp overlay to draw chunk boundaries on top of
        the entry text. The 404 `chunks_not_backfilled` response is
        distinguished from `entry_not_found` so the webapp can surface
        a clear message telling the user to re-ingest or run backfill.
        """
        services = services_getter()
        if services is None:
            return JSONResponse(
                {"error": "Server not initialized"}, status_code=503
            )

        query_svc: QueryService = services["query"]
        entry_id = int(request.path_params["entry_id"])

        entry = query_svc._repo.get_entry(entry_id)
        if entry is None:
            log.warning("GET /api/entries/%d/chunks — entry not found", entry_id)
            return JSONResponse(
                {
                    "error": "entry_not_found",
                    "message": f"Entry {entry_id} not found",
                },
                status_code=404,
            )

        chunks = query_svc._repo.get_chunks(entry_id)
        if not chunks:
            log.info(
                "GET /api/entries/%d/chunks — no chunks persisted (pre-backfill entry)",
                entry_id,
            )
            return JSONResponse(
                {
                    "error": "chunks_not_backfilled",
                    "message": (
                        "This entry was ingested before chunk persistence was "
                        "available. Re-ingest the entry or run the backfill "
                        "service to populate chunks."
                    ),
                },
                status_code=404,
            )

        payload = {
            "entry_id": entry_id,
            "chunks": [
                {
                    "index": i,
                    "text": c.text,
                    "char_start": c.char_start,
                    "char_end": c.char_end,
                    "token_count": c.token_count,
                }
                for i, c in enumerate(chunks)
            ],
        }
        log.info("GET /api/entries/%d/chunks — %d chunks", entry_id, len(chunks))
        return JSONResponse(payload)

    @mcp.custom_route(
        "/api/entries/{entry_id:int}/tokens",
        methods=["GET"],
        name="api_entry_tokens",
    )
    async def entry_tokens(request: Request) -> JSONResponse:
        """Tokenise an entry's text on demand using tiktoken `cl100k_base`.

        Returns per-token `{index, token_id, text, char_start, char_end}`
        where the character offsets are positions in `final_text` (or
        `raw_text` as fallback). Valid UTF-8 text round-trips through
        tiktoken exactly, so the offsets slice the original text without
        any loss. Computed per request — the call is cheap (< 10 ms for
        journal-scale text) and avoids any cache invalidation logic
        when `final_text` is edited.
        """
        services = services_getter()
        if services is None:
            return JSONResponse(
                {"error": "Server not initialized"}, status_code=503
            )

        query_svc: QueryService = services["query"]
        entry_id = int(request.path_params["entry_id"])

        entry = query_svc._repo.get_entry(entry_id)
        if entry is None:
            log.warning("GET /api/entries/%d/tokens — entry not found", entry_id)
            return JSONResponse(
                {
                    "error": "entry_not_found",
                    "message": f"Entry {entry_id} not found",
                },
                status_code=404,
            )

        text = entry.final_text or entry.raw_text or ""
        token_ids = _token_encoder.encode(text)
        # `decode_with_offsets` returns (decoded_str, offsets) where each
        # offset is the character index in the decoded string where the
        # corresponding token begins. For valid UTF-8 input the decoded
        # string equals the input, so these offsets are positions in the
        # original text the webapp will render.
        decoded, starts = _token_encoder.decode_with_offsets(token_ids)
        tokens: list[dict[str, Any]] = []
        for i, (tid, start) in enumerate(zip(token_ids, starts, strict=True)):
            end = starts[i + 1] if i + 1 < len(starts) else len(decoded)
            tokens.append(
                {
                    "index": i,
                    "token_id": int(tid),
                    "text": decoded[start:end],
                    "char_start": int(start),
                    "char_end": int(end),
                }
            )

        log.info(
            "GET /api/entries/%d/tokens — %d tokens", entry_id, len(tokens)
        )
        return JSONResponse(
            {
                "entry_id": entry_id,
                "encoding": _TOKEN_ENCODING_NAME,
                "model_hint": _TOKEN_MODEL_HINT,
                "token_count": len(tokens),
                "tokens": tokens,
            }
        )

    @mcp.custom_route("/health", methods=["GET"], name="api_health")
    async def get_health(request: Request) -> JSONResponse:
        """Operational health endpoint. Bypasses bearer auth.

        Returns three blocks:

        - `ingestion`: corpus stats (total/last-7d/last-30d counts,
          by source type, avg words/chunks, last ingestion timestamp,
          per-table row counts).
        - `queries`: per-query-type counts and p50/p95/p99 latency,
          plus server uptime and start timestamp.
        - `status`: per-component check list (sqlite, chromadb,
          anthropic, openai) plus the worst-of rollup.

        Most-frequent search terms are deliberately NOT exposed —
        they'd leak what the user was curious about and `/health`
        is unauthenticated on loopback-only deployments. The query
        stats block carries counts-by-type only.
        """
        services = services_getter()
        if services is None:
            return JSONResponse(
                {
                    "status": "error",
                    "message": "Server not initialized",
                },
                status_code=503,
            )

        query_svc: QueryService = services["query"]
        config = services.get("config")
        stats_collector = services.get("stats")

        # Ingestion stats block — pure SQL aggregation.
        try:
            ingestion = query_svc._repo.get_ingestion_stats(
                now=datetime.now(UTC)
            )
            ingestion_dict: dict[str, Any] = asdict(ingestion)
        except sqlite3.Error as e:
            log.warning("GET /health — ingestion stats failed: %s", e)
            ingestion_dict = {"error": str(e)}

        # Query stats block — from the in-memory collector.
        if stats_collector is not None:
            snap = stats_collector.snapshot()
            queries_dict: dict[str, Any] = {
                "total_queries": snap.total_queries,
                "uptime_seconds": snap.uptime_seconds,
                "started_at": snap.started_at,
                "by_type": {
                    name: {
                        "count": ts.count,
                        "latency": asdict(ts.latency),
                    }
                    for name, ts in snap.by_type.items()
                },
            }
        else:
            queries_dict = {
                "total_queries": 0,
                "uptime_seconds": 0.0,
                "started_at": None,
                "by_type": {},
            }

        # Liveness checks. Each is independent and returns a
        # ComponentCheck so the overall rollup can be computed at the end.
        checks = [
            check_sqlite(query_svc._repo._conn),
            check_chromadb(query_svc._vector_store),
        ]
        if config is not None:
            checks.append(
                check_api_key("anthropic", config.anthropic_api_key)
            )
            checks.append(
                check_api_key("openai", config.openai_api_key)
            )

        status = overall_status(checks)
        log.info(
            "GET /health — status=%s total_queries=%d total_entries=%s",
            status,
            queries_dict.get("total_queries", 0),
            ingestion_dict.get("total_entries", "n/a"),
        )
        return JSONResponse(
            {
                "status": status,
                "checks": [asdict(c) for c in checks],
                "ingestion": ingestion_dict,
                "queries": queries_dict,
            }
        )

    @mcp.custom_route("/api/stats", methods=["GET"], name="api_stats")
    async def get_stats(request: Request) -> JSONResponse:
        """Get journal statistics."""
        services = services_getter()
        if services is None:
            return JSONResponse(
                {"error": "Server not initialized"}, status_code=503
            )

        query_svc: QueryService = services["query"]

        start_date = request.query_params.get("start_date")
        end_date = request.query_params.get("end_date")

        stats = query_svc.get_statistics(start_date, end_date)
        log.info("GET /api/stats — %d entries, %d words", stats.total_entries, stats.total_words)
        return JSONResponse(asdict(stats))

    @mcp.custom_route(
        "/api/dashboard/writing-stats",
        methods=["GET"],
        name="api_dashboard_writing_stats",
    )
    async def dashboard_writing_stats(request: Request) -> JSONResponse:
        """Aggregate writing frequency + word count per time bucket.

        Query params:

        - `from` — ISO-8601 start date (optional)
        - `to` — ISO-8601 end date (optional)
        - `bin` — `week` (default), `month`, `quarter`, or `year`

        Returns a `{from, to, bin, bins: [...]}` envelope where
        each `bin` entry carries `bin_start`, `entry_count`, and
        `total_words`. Empty buckets are NOT emitted — a month with
        zero entries will simply not appear in the series, and the
        frontend is expected to fill gaps client-side if it wants
        a dense line chart.
        """
        services = services_getter()
        if services is None:
            return JSONResponse(
                {"error": "Server not initialized"}, status_code=503
            )

        query_svc: QueryService = services["query"]

        bin_param = request.query_params.get("bin", "week")
        start_date = request.query_params.get("from")
        end_date = request.query_params.get("to")

        try:
            bins = query_svc._repo.get_writing_frequency(
                start_date=start_date,
                end_date=end_date,
                granularity=bin_param,
            )
        except ValueError as e:
            log.info(
                "GET /api/dashboard/writing-stats — invalid bin %r: %s",
                bin_param,
                e,
            )
            return JSONResponse(
                {
                    "error": "invalid_bin",
                    "message": str(e),
                },
                status_code=400,
            )

        log.info(
            "GET /api/dashboard/writing-stats — bin=%s from=%s to=%s "
            "returned %d non-empty buckets",
            bin_param,
            start_date,
            end_date,
            len(bins),
        )
        return JSONResponse(
            {
                "from": start_date,
                "to": end_date,
                "bin": bin_param,
                "bins": [asdict(b) for b in bins],
            }
        )

    @mcp.custom_route(
        "/api/dashboard/mood-dimensions",
        methods=["GET"],
        name="api_dashboard_mood_dimensions",
    )
    async def dashboard_mood_dimensions(
        request: Request,
    ) -> JSONResponse:
        """Return the currently-loaded mood dimensions.

        Serves as the source of truth for the webapp's mood chart:
        the dimension definitions live in a server-side TOML file,
        and the frontend fetches them via this endpoint on page
        load so adding/removing a facet in the file (plus a
        server restart) flows through to the UI without a webapp
        rebuild.

        When mood scoring is disabled (`JOURNAL_ENABLE_MOOD_SCORING=false`)
        or no dimensions are loaded, returns an empty list with
        200 — callers should treat that as "no mood data to
        display" rather than an error.
        """
        services = services_getter()
        if services is None:
            return JSONResponse(
                {"error": "Server not initialized"}, status_code=503
            )

        dimensions = services.get("mood_dimensions") or ()
        payload = [
            {
                "name": d.name,
                "positive_pole": d.positive_pole,
                "negative_pole": d.negative_pole,
                "scale_type": d.scale_type,
                "score_min": d.score_min,
                "score_max": d.score_max,
                "notes": d.notes,
            }
            for d in dimensions
        ]
        log.info(
            "GET /api/dashboard/mood-dimensions — %d dimensions",
            len(payload),
        )
        return JSONResponse({"dimensions": payload})

    @mcp.custom_route(
        "/api/dashboard/mood-trends",
        methods=["GET"],
        name="api_dashboard_mood_trends",
    )
    async def dashboard_mood_trends(request: Request) -> JSONResponse:
        """Aggregate mood scores per bucket, grouped by dimension.

        Query params:

        - `from` — ISO-8601 start date (optional)
        - `to` — ISO-8601 end date (optional)
        - `bin` — `week` (default), `month`, `quarter`, `year`
          (matches the writing-stats endpoint)
        - `dimension` — optional filter. When present, only the
          named dimension is returned. When absent, all
          dimensions are returned.

        Returns a `{from, to, bin, bins}` envelope. Each `bin`
        entry is `{period, dimension, avg_score, entry_count}`.
        Empty buckets are omitted — a bucket with zero scored
        entries does not appear in the series.
        """
        services = services_getter()
        if services is None:
            return JSONResponse(
                {"error": "Server not initialized"}, status_code=503
            )

        query_svc: QueryService = services["query"]

        bin_param = request.query_params.get("bin", "week")
        start_date = request.query_params.get("from")
        end_date = request.query_params.get("to")
        dimension_filter = request.query_params.get("dimension")

        try:
            trends = query_svc.get_mood_trends(
                start_date=start_date,
                end_date=end_date,
                granularity=bin_param,
            )
        except ValueError as e:
            log.info(
                "GET /api/dashboard/mood-trends — invalid bin %r: %s",
                bin_param,
                e,
            )
            return JSONResponse(
                {
                    "error": "invalid_bin",
                    "message": str(e),
                },
                status_code=400,
            )

        if dimension_filter:
            trends = [t for t in trends if t.dimension == dimension_filter]

        log.info(
            "GET /api/dashboard/mood-trends — bin=%s from=%s to=%s "
            "dim=%s returned %d buckets",
            bin_param,
            start_date,
            end_date,
            dimension_filter,
            len(trends),
        )
        return JSONResponse(
            {
                "from": start_date,
                "to": end_date,
                "bin": bin_param,
                "bins": [asdict(t) for t in trends],
            }
        )

    @mcp.custom_route("/api/search", methods=["GET"], name="api_search")
    async def search(request: Request) -> JSONResponse:
        """Full-text search across journal entries.

        Two modes, both bearer-authenticated via the app-wide auth
        middleware:

        - `semantic` (default): vector similarity over persisted chunk
          embeddings. Each result's `matching_chunks` list carries
          `char_start`/`char_end`/`chunk_index` so the client can
          render in-place highlights.
        - `keyword`: SQLite FTS5 over `final_text`. Each result has a
          `snippet` string with `\\x02`/`\\x03` control chars wrapping
          matched terms; `matching_chunks` is empty.
        """
        services = services_getter()
        if services is None:
            return JSONResponse(
                {"error": "Server not initialized"}, status_code=503
            )

        query_svc: QueryService = services["query"]

        q = (request.query_params.get("q") or "").strip()
        if not q:
            return JSONResponse(
                {
                    "error": "missing_query",
                    "message": "'q' query parameter is required",
                },
                status_code=400,
            )

        mode = request.query_params.get("mode", "semantic")
        if mode not in ("semantic", "keyword"):
            return JSONResponse(
                {
                    "error": "invalid_mode",
                    "message": "'mode' must be 'semantic' or 'keyword'",
                },
                status_code=400,
            )

        start_date = request.query_params.get("start_date")
        end_date = request.query_params.get("end_date")

        try:
            limit = min(max(int(request.query_params.get("limit", "10")), 1), 50)
        except ValueError:
            limit = 10
        try:
            offset = max(int(request.query_params.get("offset", "0")), 0)
        except ValueError:
            offset = 0

        try:
            if mode == "semantic":
                results = query_svc.search_entries(
                    query=q,
                    start_date=start_date,
                    end_date=end_date,
                    limit=limit,
                    offset=offset,
                )
            else:
                results = query_svc.keyword_search(
                    query=q,
                    start_date=start_date,
                    end_date=end_date,
                    limit=limit,
                    offset=offset,
                )
        except sqlite3.OperationalError as e:
            # FTS5 raises this on malformed queries (unterminated
            # quotes, bare operators like `AND`, etc.). Surface as a
            # 400 rather than a 500 so clients can tell the user.
            log.info(
                "GET /api/search — invalid FTS5 query %r: %s", q, e
            )
            return JSONResponse(
                {
                    "error": "invalid_query",
                    "message": f"Query could not be parsed: {e}",
                },
                status_code=400,
            )

        log.info(
            "GET /api/search — mode=%s q=%r returned %d results",
            mode,
            q,
            len(results),
        )
        return JSONResponse(
            {
                "query": q,
                "mode": mode,
                "limit": limit,
                "offset": offset,
                "items": [_search_result_dict(r) for r in results],
            }
        )

    # -----------------------------------------------------------------
    # Ingestion routes (entry creation)
    # -----------------------------------------------------------------

    def _require_services() -> dict | None:
        svcs = services_getter()
        return svcs

    @mcp.custom_route(
        "/api/entries/ingest/text",
        methods=["POST"],
        name="api_ingest_text",
    )
    async def ingest_text(request: Request) -> JSONResponse:
        """Create a journal entry from plain text (no OCR).

        Request body (JSON):
            text (str, required): The entry content.
            entry_date (str, optional): ISO-8601 date, defaults to today.
            source_type (str, optional): defaults to "manual".

        Returns 201 with the created entry and an optional mood_job_id.
        """
        services = _require_services()
        if services is None:
            return JSONResponse(
                {"error": "Server not initialized"}, status_code=503
            )

        ingestion_svc: IngestionService = services["ingestion"]

        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return JSONResponse(
                {"error": "Invalid JSON body"}, status_code=400
            )

        if not isinstance(body, dict):
            return JSONResponse(
                {"error": "Request body must be a JSON object"},
                status_code=400,
            )

        text = body.get("text")
        if text is None or not isinstance(text, str):
            return JSONResponse(
                {"error": "'text' is required and must be a string"},
                status_code=400,
            )
        if not text.strip():
            return JSONResponse(
                {"error": "'text' must not be empty"},
                status_code=400,
            )

        entry_date = body.get("entry_date") or datetime.now(UTC).strftime("%Y-%m-%d")
        source_type = body.get("source_type", "manual")

        try:
            entry = ingestion_svc.ingest_text(
                text, entry_date, source_type, skip_mood=True,
            )
        except ValueError as e:
            log.warning("POST /api/entries/ingest/text — %s", e)
            return JSONResponse({"error": str(e)}, status_code=400)

        # Fire async post-ingestion jobs
        mood_job_id = None
        entity_extraction_job_id = None
        config = services.get("config")
        job_runner: JobRunner = services["job_runner"]
        if config and config.enable_mood_scoring:
            try:
                mood_job = job_runner.submit_mood_score_entry(entry.id)
                mood_job_id = mood_job.id
            except Exception:
                log.warning(
                    "POST /api/entries/ingest/text — failed to queue mood scoring",
                    exc_info=True,
                )
        try:
            ej = job_runner.submit_entity_extraction({"entry_id": entry.id})
            entity_extraction_job_id = ej.id
        except Exception:
            log.warning(
                "POST /api/entries/ingest/text — failed to queue entity extraction",
                exc_info=True,
            )

        page_count = ingestion_svc._repo.get_page_count(entry.id)
        log.info(
            "POST /api/entries/ingest/text — created entry %d (%d words)",
            entry.id, entry.word_count,
        )
        return JSONResponse(
            {
                "entry": _entry_to_dict(entry, page_count),
                "mood_job_id": mood_job_id,
                "entity_extraction_job_id": entity_extraction_job_id,
            },
            status_code=201,
        )

    @mcp.custom_route(
        "/api/entries/ingest/file",
        methods=["POST"],
        name="api_ingest_file",
    )
    async def ingest_file(request: Request) -> JSONResponse:
        """Create a journal entry from an uploaded .md or .txt file.

        Expects multipart/form-data with:
            file: a single .md or .txt file
            entry_date (optional): ISO-8601 date, defaults to today.

        Returns 201 with the created entry and an optional mood_job_id.
        """
        from journal.api_utils import parse_multipart_request

        services = _require_services()
        if services is None:
            return JSONResponse(
                {"error": "Server not initialized"}, status_code=503
            )

        ingestion_svc: IngestionService = services["ingestion"]

        try:
            fields, files = await parse_multipart_request(request)
        except Exception as e:
            log.warning("POST /api/entries/ingest/file — parse error: %s", e)
            return JSONResponse(
                {"error": f"Failed to parse multipart request: {e}"},
                status_code=400,
            )

        file_list = files.get("file", [])
        if len(file_list) != 1:
            return JSONResponse(
                {"error": "Exactly one file is required in the 'file' field"},
                status_code=400,
            )

        uploaded = file_list[0]
        filename_lower = uploaded.filename.lower()
        if not (filename_lower.endswith(".md") or filename_lower.endswith(".txt")):
            return JSONResponse(
                {"error": "File must be .md or .txt"},
                status_code=400,
            )

        try:
            content = uploaded.data.decode("utf-8")
        except UnicodeDecodeError:
            return JSONResponse(
                {"error": "File must be valid UTF-8 text"},
                status_code=400,
            )

        if not content.strip():
            return JSONResponse(
                {"error": "File is empty"},
                status_code=400,
            )

        entry_date = fields.get("entry_date") or datetime.now(UTC).strftime("%Y-%m-%d")

        try:
            entry = ingestion_svc.ingest_text(
                content, entry_date, "import", skip_mood=True,
            )
        except ValueError as e:
            log.warning("POST /api/entries/ingest/file — %s", e)
            return JSONResponse({"error": str(e)}, status_code=400)

        # Store source file metadata for dedup
        file_hash = hashlib.sha256(uploaded.data).hexdigest()
        ingestion_svc._store_source_file(
            entry.id, f"upload:{uploaded.filename}", uploaded.content_type, file_hash,
        )

        # Fire async post-ingestion jobs
        mood_job_id = None
        entity_extraction_job_id = None
        config = services.get("config")
        job_runner: JobRunner = services["job_runner"]
        if config and config.enable_mood_scoring:
            try:
                mood_job = job_runner.submit_mood_score_entry(entry.id)
                mood_job_id = mood_job.id
            except Exception:
                log.warning(
                    "POST /api/entries/ingest/file — failed to queue mood scoring",
                    exc_info=True,
                )
        try:
            ej = job_runner.submit_entity_extraction({"entry_id": entry.id})
            entity_extraction_job_id = ej.id
        except Exception:
            log.warning(
                "POST /api/entries/ingest/file — failed to queue entity extraction",
                exc_info=True,
            )

        page_count = ingestion_svc._repo.get_page_count(entry.id)
        log.info(
            "POST /api/entries/ingest/file — created entry %d from %s (%d words)",
            entry.id, uploaded.filename, entry.word_count,
        )
        return JSONResponse(
            {
                "entry": _entry_to_dict(entry, page_count),
                "mood_job_id": mood_job_id,
                "entity_extraction_job_id": entity_extraction_job_id,
            },
            status_code=201,
        )

    @mcp.custom_route(
        "/api/entries/ingest/images",
        methods=["POST"],
        name="api_ingest_images",
    )
    async def ingest_images(request: Request) -> JSONResponse:
        """Upload one or more journal page images for OCR ingestion.

        Expects multipart/form-data with:
            images: one or more image files (jpeg, png, gif, webp)
            entry_date (optional): ISO-8601 date, defaults to today.

        Returns 202 with a job_id for async processing.
        """
        from journal.api_utils import parse_multipart_request

        services = _require_services()
        if services is None:
            return JSONResponse(
                {"error": "Server not initialized"}, status_code=503
            )

        try:
            fields, files = await parse_multipart_request(request)
        except Exception as e:
            log.warning("POST /api/entries/ingest/images — parse error: %s", e)
            return JSONResponse(
                {"error": f"Failed to parse multipart request: {e}"},
                status_code=400,
            )

        image_list = files.get("images", [])
        if not image_list:
            return JSONResponse(
                {"error": "At least one image is required in the 'images' field"},
                status_code=400,
            )

        allowed_types = {"image/jpeg", "image/png", "image/gif", "image/webp"}
        max_file_size = 10 * 1024 * 1024  # 10 MB per file
        max_total_size = 50 * 1024 * 1024  # 50 MB total

        total_size = 0
        images: list[tuple[bytes, str, str]] = []
        for uploaded in image_list:
            if uploaded.content_type not in allowed_types:
                return JSONResponse(
                    {
                        "error": f"File '{uploaded.filename}' has unsupported type "
                        f"'{uploaded.content_type}'. Allowed: JPEG, PNG, GIF, WebP."
                    },
                    status_code=400,
                )
            if len(uploaded.data) > max_file_size:
                return JSONResponse(
                    {
                        "error": f"File '{uploaded.filename}' exceeds 10 MB limit"
                    },
                    status_code=400,
                )
            total_size += len(uploaded.data)
            if total_size > max_total_size:
                return JSONResponse(
                    {"error": "Total upload size exceeds 50 MB limit"},
                    status_code=413,
                )
            images.append((uploaded.data, uploaded.content_type, uploaded.filename))

        entry_date = fields.get("entry_date") or datetime.now(UTC).strftime("%Y-%m-%d")

        job_runner: JobRunner = services["job_runner"]
        try:
            job = job_runner.submit_image_ingestion(images, entry_date)
        except ValueError as e:
            log.warning("POST /api/entries/ingest/images — %s", e)
            return JSONResponse({"error": str(e)}, status_code=400)

        log.info(
            "POST /api/entries/ingest/images — queued job %s (%d images)",
            job.id, len(images),
        )
        return JSONResponse(
            {"job_id": job.id, "status": job.status},
            status_code=202,
        )

    # -----------------------------------------------------------------
    # Entity routes
    # -----------------------------------------------------------------

    @mcp.custom_route(
        "/api/entities/extract",
        methods=["POST"],
        name="api_entities_extract",
    )
    async def extract_entities(request: Request) -> JSONResponse:
        """Submit an entity-extraction batch job.

        Request body matches the legacy synchronous shape —
        ``{entry_id?, start_date?, end_date?, stale_only?}`` — so
        existing clients continue to compile. Validation (unknown
        keys, bad types) happens in the JobRunner and bubbles up as
        a 400. The single-entry ``entry_id`` path also goes through
        the jobs table now; there is no synchronous path.

        Returns 202 with ``{"job_id", "status"}``. Clients should
        poll ``GET /api/jobs/{job_id}`` to observe progress and
        result.
        """
        services = _require_services()
        if services is None:
            return JSONResponse(
                {"error": "Server not initialized"}, status_code=503
            )
        job_runner: JobRunner = services["job_runner"]

        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            body = {}
        if not isinstance(body, dict):
            return JSONResponse(
                {"error": "Request body must be a JSON object"},
                status_code=400,
            )

        try:
            job = job_runner.submit_entity_extraction(body)
        except ValueError as e:
            log.warning("POST /api/entities/extract — %s", e)
            return JSONResponse({"error": str(e)}, status_code=400)

        log.info(
            "POST /api/entities/extract — queued job %s", job.id
        )
        return JSONResponse(
            {"job_id": job.id, "status": job.status},
            status_code=202,
        )

    @mcp.custom_route(
        "/api/mood/backfill",
        methods=["POST"],
        name="api_mood_backfill",
    )
    async def mood_backfill(request: Request) -> JSONResponse:
        """Submit a mood-score backfill batch job.

        Request body: ``{mode, start_date?, end_date?}`` where
        ``mode`` is ``"stale-only"`` or ``"force"``. Unknown keys
        or bad types return 400. Returns 202 with
        ``{"job_id", "status"}``.
        """
        services = _require_services()
        if services is None:
            return JSONResponse(
                {"error": "Server not initialized"}, status_code=503
            )
        job_runner: JobRunner = services["job_runner"]

        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            body = {}
        if not isinstance(body, dict):
            return JSONResponse(
                {"error": "Request body must be a JSON object"},
                status_code=400,
            )

        try:
            job = job_runner.submit_mood_backfill(body)
        except ValueError as e:
            log.warning("POST /api/mood/backfill — %s", e)
            return JSONResponse({"error": str(e)}, status_code=400)

        log.info("POST /api/mood/backfill — queued job %s", job.id)
        return JSONResponse(
            {"job_id": job.id, "status": job.status},
            status_code=202,
        )

    @mcp.custom_route(
        "/api/jobs",
        methods=["GET"],
        name="api_list_jobs",
    )
    async def list_jobs(request: Request) -> JSONResponse:
        """List jobs with optional filters, ordered newest first."""
        services = _require_services()
        if services is None:
            return JSONResponse(
                {"error": "Server not initialized"}, status_code=503
            )
        job_repository: SQLiteJobRepository = services["job_repository"]

        status = request.query_params.get("status")
        job_type = request.query_params.get("type")
        try:
            limit = int(request.query_params.get("limit", "50"))
            offset = int(request.query_params.get("offset", "0"))
        except ValueError:
            return JSONResponse(
                {"error": "limit and offset must be integers"},
                status_code=400,
            )

        jobs, total = job_repository.list_jobs(
            status=status, job_type=job_type, limit=limit, offset=offset,
        )
        log.info(
            "GET /api/jobs — %d jobs (total %d, offset %d)",
            len(jobs), total, offset,
        )
        return JSONResponse({
            "items": [_job_to_dict(j) for j in jobs],
            "total": total,
            "limit": limit,
            "offset": offset,
        })

    @mcp.custom_route(
        "/api/jobs/{job_id:str}",
        methods=["GET"],
        name="api_job_detail",
    )
    async def job_detail(request: Request) -> JSONResponse:
        """Return the current state of a batch job by id.

        404 if the job id is unknown. Otherwise returns the full
        serialised job dict (``_job_to_dict`` shape).
        """
        services = _require_services()
        if services is None:
            return JSONResponse(
                {"error": "Server not initialized"}, status_code=503
            )
        job_repository: SQLiteJobRepository = services["job_repository"]
        job_id = str(request.path_params["job_id"])
        job = job_repository.get(job_id)
        if job is None:
            log.info("GET /api/jobs/%s — not found", job_id)
            return JSONResponse(
                {"error": "Job not found"}, status_code=404
            )
        log.info(
            "GET /api/jobs/%s — status=%s progress=%d/%d",
            job_id, job.status, job.progress_current, job.progress_total,
        )
        return JSONResponse(_job_to_dict(job))

    @mcp.custom_route(
        "/api/entities", methods=["GET"], name="api_list_entities"
    )
    async def list_entities_route(request: Request) -> JSONResponse:
        services = _require_services()
        if services is None:
            return JSONResponse(
                {"error": "Server not initialized"}, status_code=503
            )
        entity_store: EntityStore = services["entity_store"]

        entity_type = request.query_params.get("type")
        search = request.query_params.get("search")
        try:
            limit = min(int(request.query_params.get("limit", "50")), 200)
        except ValueError:
            limit = 50
        try:
            offset = max(int(request.query_params.get("offset", "0")), 0)
        except ValueError:
            offset = 0

        rows = entity_store.list_entities_with_mention_counts(
            entity_type=entity_type, limit=limit, offset=offset
        )
        if search:
            needle = search.strip().lower()
            rows = [
                (e, c, ls)
                for e, c, ls in rows
                if needle in e.canonical_name.lower()
                or any(needle in a.lower() for a in e.aliases)
            ]
        total = entity_store.count_entities(entity_type=entity_type)
        items = [_entity_summary(e, c, ls) for e, c, ls in rows]
        log.info(
            "GET /api/entities — returned %d/%d entities", len(items), total
        )
        return JSONResponse(
            {
                "items": items,
                "total": total,
                "limit": limit,
                "offset": offset,
            }
        )

    @mcp.custom_route(
        "/api/entities/{entity_id:int}",
        methods=["GET"],
        name="api_entity_detail",
    )
    async def entity_detail(request: Request) -> JSONResponse:
        services = _require_services()
        if services is None:
            return JSONResponse(
                {"error": "Server not initialized"}, status_code=503
            )
        entity_store: EntityStore = services["entity_store"]
        entity_id = int(request.path_params["entity_id"])

        entity = entity_store.get_entity(entity_id)
        if entity is None:
            log.warning("GET /api/entities/%d — not found", entity_id)
            return JSONResponse(
                {"error": f"Entity {entity_id} not found"}, status_code=404
            )
        log.info("GET /api/entities/%d — %s", entity_id, entity.canonical_name)
        return JSONResponse(_entity_detail(entity))

    @mcp.custom_route(
        "/api/entities/{entity_id:int}/mentions",
        methods=["GET"],
        name="api_entity_mentions",
    )
    async def entity_mentions(request: Request) -> JSONResponse:
        services = _require_services()
        if services is None:
            return JSONResponse(
                {"error": "Server not initialized"}, status_code=503
            )
        entity_store: EntityStore = services["entity_store"]
        query_svc: QueryService = services["query"]
        entity_id = int(request.path_params["entity_id"])

        entity = entity_store.get_entity(entity_id)
        if entity is None:
            return JSONResponse(
                {"error": f"Entity {entity_id} not found"}, status_code=404
            )

        try:
            limit = min(int(request.query_params.get("limit", "50")), 200)
        except ValueError:
            limit = 50
        try:
            offset = max(int(request.query_params.get("offset", "0")), 0)
        except ValueError:
            offset = 0

        mentions = entity_store.get_mentions_for_entity(
            entity_id, limit=limit, offset=offset
        )
        mention_payload: list[dict[str, Any]] = []
        for m in mentions:
            entry = query_svc._repo.get_entry(m.entry_id)
            entry_date = entry.entry_date if entry else None
            mention_payload.append(_mention_dict(m, entry_date))
        log.info(
            "GET /api/entities/%d/mentions — %d mentions",
            entity_id, len(mention_payload),
        )
        return JSONResponse(
            {
                "entity_id": entity_id,
                "mentions": mention_payload,
                "total": len(mention_payload),
            }
        )

    @mcp.custom_route(
        "/api/entities/{entity_id:int}/relationships",
        methods=["GET"],
        name="api_entity_relationships",
    )
    async def entity_relationships(request: Request) -> JSONResponse:
        services = _require_services()
        if services is None:
            return JSONResponse(
                {"error": "Server not initialized"}, status_code=503
            )
        entity_store: EntityStore = services["entity_store"]
        entity_id = int(request.path_params["entity_id"])

        entity = entity_store.get_entity(entity_id)
        if entity is None:
            return JSONResponse(
                {"error": f"Entity {entity_id} not found"}, status_code=404
            )

        outgoing, incoming = entity_store.get_relationships_for_entity(
            entity_id
        )
        log.info(
            "GET /api/entities/%d/relationships — %d out, %d in",
            entity_id, len(outgoing), len(incoming),
        )
        return JSONResponse(
            {
                "entity_id": entity_id,
                "outgoing": [_relationship_dict(r) for r in outgoing],
                "incoming": [_relationship_dict(r) for r in incoming],
            }
        )

    # ---- entity management: update / delete / merge ----------------------

    @mcp.custom_route(
        "/api/entities/{entity_id:int}",
        methods=["PATCH"],
        name="api_update_entity",
    )
    async def update_entity(request: Request) -> JSONResponse:
        services = _require_services()
        if services is None:
            return JSONResponse(
                {"error": "Server not initialized"}, status_code=503
            )
        entity_store: EntityStore = services["entity_store"]
        entity_id = int(request.path_params["entity_id"])

        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                {"error": "Invalid JSON body"}, status_code=400
            )

        canonical_name = body.get("canonical_name")
        entity_type = body.get("entity_type")
        description = body.get("description")

        if canonical_name is not None and (
            not isinstance(canonical_name, str) or not canonical_name.strip()
        ):
            return JSONResponse(
                {"error": "'canonical_name' must be a non-empty string"},
                status_code=400,
            )
        valid_types = {
            "person", "place", "activity", "organization", "topic", "other"
        }
        if entity_type is not None and entity_type not in valid_types:
            return JSONResponse(
                {"error": f"'entity_type' must be one of {sorted(valid_types)}"},
                status_code=400,
            )

        try:
            updated = entity_store.update_entity(
                entity_id,
                canonical_name=canonical_name,
                entity_type=entity_type,
                description=description,
            )
        except ValueError:
            return JSONResponse(
                {"error": f"Entity {entity_id} not found"}, status_code=404
            )

        log.info("PATCH /api/entities/%d — updated", entity_id)
        return JSONResponse(_entity_detail(updated))

    @mcp.custom_route(
        "/api/entities/{entity_id:int}",
        methods=["DELETE"],
        name="api_delete_entity",
    )
    async def delete_entity_route(request: Request) -> JSONResponse:
        services = _require_services()
        if services is None:
            return JSONResponse(
                {"error": "Server not initialized"}, status_code=503
            )
        entity_store: EntityStore = services["entity_store"]
        entity_id = int(request.path_params["entity_id"])

        try:
            entity_store.delete_entity(entity_id)
        except ValueError:
            return JSONResponse(
                {"error": f"Entity {entity_id} not found"}, status_code=404
            )

        log.info("DELETE /api/entities/%d — deleted", entity_id)
        return JSONResponse({"deleted": True, "id": entity_id})

    @mcp.custom_route(
        "/api/entities/merge",
        methods=["POST"],
        name="api_merge_entities",
    )
    async def merge_entities_route(request: Request) -> JSONResponse:
        services = _require_services()
        if services is None:
            return JSONResponse(
                {"error": "Server not initialized"}, status_code=503
            )
        entity_store: EntityStore = services["entity_store"]

        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                {"error": "Invalid JSON body"}, status_code=400
            )

        survivor_id = body.get("survivor_id")
        absorbed_ids = body.get("absorbed_ids")

        if not isinstance(survivor_id, int):
            return JSONResponse(
                {"error": "'survivor_id' must be an integer"}, status_code=400
            )
        if (
            not isinstance(absorbed_ids, list)
            or not absorbed_ids
            or not all(isinstance(i, int) for i in absorbed_ids)
        ):
            return JSONResponse(
                {"error": "'absorbed_ids' must be a non-empty list of integers"},
                status_code=400,
            )

        try:
            result = entity_store.merge_entities(survivor_id, absorbed_ids)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

        survivor = entity_store.get_entity(survivor_id)
        log.info(
            "POST /api/entities/merge — merged %s into %d",
            absorbed_ids, survivor_id,
        )
        return JSONResponse({
            "survivor": _entity_detail(survivor) if survivor else None,
            "absorbed_ids": result.absorbed_ids,
            "mentions_reassigned": result.mentions_reassigned,
            "relationships_reassigned": result.relationships_reassigned,
            "aliases_added": result.aliases_added,
        })

    # ---- merge candidates -----------------------------------------------

    @mcp.custom_route(
        "/api/entities/merge-candidates",
        methods=["GET"],
        name="api_merge_candidates",
    )
    async def list_merge_candidates_route(
        request: Request,
    ) -> JSONResponse:
        services = _require_services()
        if services is None:
            return JSONResponse(
                {"error": "Server not initialized"}, status_code=503
            )
        entity_store: EntityStore = services["entity_store"]

        status = request.query_params.get("status", "pending")
        try:
            limit = min(int(request.query_params.get("limit", "50")), 200)
        except ValueError:
            limit = 50

        candidates = entity_store.list_merge_candidates(
            status=status, limit=limit
        )
        items = [
            {
                "id": c.id,
                "entity_a": _entity_summary(c.entity_a),
                "entity_b": _entity_summary(c.entity_b),
                "similarity": c.similarity,
                "status": c.status,
                "extraction_run_id": c.extraction_run_id,
                "created_at": c.created_at,
            }
            for c in candidates
        ]
        log.info(
            "GET /api/entities/merge-candidates — %d candidates", len(items)
        )
        return JSONResponse({"items": items, "total": len(items)})

    @mcp.custom_route(
        "/api/entities/merge-candidates/{candidate_id:int}",
        methods=["PATCH"],
        name="api_resolve_merge_candidate",
    )
    async def resolve_merge_candidate_route(
        request: Request,
    ) -> JSONResponse:
        services = _require_services()
        if services is None:
            return JSONResponse(
                {"error": "Server not initialized"}, status_code=503
            )
        entity_store: EntityStore = services["entity_store"]
        candidate_id = int(request.path_params["candidate_id"])

        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                {"error": "Invalid JSON body"}, status_code=400
            )

        status = body.get("status")
        if status not in ("accepted", "dismissed"):
            return JSONResponse(
                {"error": "'status' must be 'accepted' or 'dismissed'"},
                status_code=400,
            )

        try:
            entity_store.resolve_merge_candidate(candidate_id, status)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

        log.info(
            "PATCH /api/entities/merge-candidates/%d — %s",
            candidate_id, status,
        )
        return JSONResponse({"id": candidate_id, "status": status})

    @mcp.custom_route(
        "/api/entities/{entity_id:int}/merge-history",
        methods=["GET"],
        name="api_entity_merge_history",
    )
    async def entity_merge_history(request: Request) -> JSONResponse:
        services = _require_services()
        if services is None:
            return JSONResponse(
                {"error": "Server not initialized"}, status_code=503
            )
        entity_store: EntityStore = services["entity_store"]
        entity_id = int(request.path_params["entity_id"])

        entity = entity_store.get_entity(entity_id)
        if entity is None:
            return JSONResponse(
                {"error": f"Entity {entity_id} not found"}, status_code=404
            )

        history = entity_store.get_merge_history(entity_id)
        log.info(
            "GET /api/entities/%d/merge-history — %d entries",
            entity_id, len(history),
        )
        return JSONResponse(
            {"entity_id": entity_id, "history": history}
        )

    # ---- per-entry entity lookups ---------------------------------------

    @mcp.custom_route(
        "/api/entries/{entry_id:int}/entities",
        methods=["GET"],
        name="api_entry_entities",
    )
    async def entry_entities(request: Request) -> JSONResponse:
        services = _require_services()
        if services is None:
            return JSONResponse(
                {"error": "Server not initialized"}, status_code=503
            )
        entity_store: EntityStore = services["entity_store"]
        query_svc: QueryService = services["query"]
        entry_id = int(request.path_params["entry_id"])

        entry = query_svc._repo.get_entry(entry_id)
        if entry is None:
            return JSONResponse(
                {"error": f"Entry {entry_id} not found"}, status_code=404
            )

        entities = entity_store.get_entities_for_entry(entry_id)
        mentions = entity_store.get_mentions_for_entry(entry_id)
        mentions_by_entity: dict[int, int] = {}
        quotes_by_entity: dict[int, list[str]] = {}
        for m in mentions:
            mentions_by_entity[m.entity_id] = (
                mentions_by_entity.get(m.entity_id, 0) + 1
            )
            quotes_by_entity.setdefault(m.entity_id, [])
            if m.quote not in quotes_by_entity[m.entity_id]:
                quotes_by_entity[m.entity_id].append(m.quote)
        items = [
            _entity_summary(
                e,
                mentions_by_entity.get(e.id, 0),
                quotes=quotes_by_entity.get(e.id, []),
            )
            for e in entities
        ]
        log.info(
            "GET /api/entries/%d/entities — %d entities", entry_id, len(items)
        )
        return JSONResponse(
            {"entry_id": entry_id, "items": items, "total": len(items)}
        )
