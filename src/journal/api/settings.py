"""Server settings routes (frozen config, runtime overrides, model pricing).

- ``GET  /api/settings`` — non-secret config snapshot (provider/model selection,
  chunking parameters, hybrid-search knobs, runtime feature flags, pricing).
- ``GET  /api/settings/runtime`` — runtime-editable settings with metadata.
- ``PATCH /api/settings/runtime`` — admin-only update of runtime settings.
- ``GET  /api/settings/pricing`` — current API model pricing entries.
- ``PATCH /api/settings/pricing`` — admin-only pricing update; 207 multi-status
  when some models in the body succeed and others fail.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from starlette.responses import JSONResponse

from journal.api._handler import handler
from journal.api._shared import _pricing_to_dict, _runtime_get
from journal.auth import get_authenticated_user
from journal.db.pricing import get_all_pricing, update_pricing

if TYPE_CHECKING:
    from collections.abc import Callable

    from mcp.server.fastmcp import FastMCP
    from starlette.requests import Request

    from journal.service_registry import ServicesDict

log = logging.getLogger(__name__)


def register_settings_routes(
    mcp: FastMCP,
    services_getter: Callable[[], ServicesDict | None],
) -> None:
    """Register /api/settings, /api/settings/runtime, /api/settings/pricing."""

    @mcp.custom_route("/api/settings", methods=["GET"], name="api_settings")
    @handler(services_getter)
    def get_settings(
        request: Request, services: ServicesDict, body: None
    ) -> JSONResponse:
        """Return current server configuration (non-secret values only).

        Secrets (API keys, bearer tokens, Slack bot token) are redacted.
        """
        config = services.get("config")
        if config is None:
            return JSONResponse({"error": "Config not available"}, status_code=503)
        from journal.providers.ocr import _DEFAULT_MODELS

        ocr_model = config.ocr_model or _DEFAULT_MODELS.get(config.ocr_provider, "")
        db_factory = services.get("db_factory")
        pricing_entries = get_all_pricing(db_factory.get()) if db_factory else []
        return JSONResponse(
            {
                "ocr": {
                    "provider": config.ocr_provider,
                    "model": ocr_model,
                },
                "transcription": {
                    "provider": config.transcription_provider,
                    "model": config.transcription_model,
                    "fallback": {
                        "enabled": config.transcription_fallback_enabled,
                        "model": config.transcription_fallback_model,
                    },
                    "shadow": {
                        "enabled": bool(config.transcription_shadow_provider),
                        "provider": config.transcription_shadow_provider or None,
                        "model": config.transcription_shadow_model or None,
                    },
                    "retry": {
                        "max_attempts": config.transcription_retry_max_attempts,
                        "base_delay_seconds": config.transcription_retry_base_delay,
                        "max_delay_seconds": config.transcription_retry_max_delay,
                    },
                },
                "transcript_formatting": {
                    "model": config.transcript_formatter_model,
                },
                "embedding": {
                    "model": config.embedding_model,
                    "dimensions": config.embedding_dimensions,
                },
                "chunking": {
                    "strategy": config.chunking_strategy,
                    "max_tokens": config.chunking_max_tokens,
                    "min_tokens": config.chunking_min_tokens,
                    "overlap_tokens": config.chunking_overlap_tokens,
                    "boundary_percentile": config.chunking_boundary_percentile,
                    "decisive_percentile": config.chunking_decisive_percentile,
                    "embed_metadata_prefix": config.chunking_embed_metadata_prefix,
                },
                "entity_extraction": {
                    "model": config.entity_extraction_model,
                    "dedup_similarity_threshold": config.entity_dedup_similarity_threshold,
                },
                "search": {
                    "reranker": config.hybrid_reranker,
                    "reranker_model": (
                        config.reranker_model
                        if config.hybrid_reranker != "none"
                        else None
                    ),
                    "bm25_candidates": config.hybrid_bm25_candidates,
                    "dense_candidates": config.hybrid_dense_candidates,
                    "fusion_top_m": config.hybrid_fusion_top_m,
                    "rrf_k": config.hybrid_rrf_k,
                },
                "features": {
                    "mood_scoring": _runtime_get(services, "enable_mood_scoring"),
                    "mood_scorer_model": config.mood_scorer_model,
                    "journal_author_name": config.journal_author_name,
                    # W1 strava-mothball: straight from the frozen config
                    # (not a runtime setting) — the webapp uses it to hide
                    # the Strava UI.
                    "strava_enabled": config.strava_enabled,
                },
                "runtime": (
                    runtime.get_all()
                    if (runtime := services.get("runtime_settings"))
                    else []
                ),
                "pricing": [_pricing_to_dict(e) for e in pricing_entries],
            }
        )

    @mcp.custom_route(
        "/api/settings/runtime", methods=["GET"], name="api_runtime_settings_get",
    )
    @handler(services_getter)
    def get_runtime_settings(
        request: Request, services: ServicesDict, body: None
    ) -> JSONResponse:
        """Return all runtime-editable settings with metadata."""
        runtime = services.get("runtime_settings")
        if runtime is None:
            return JSONResponse({"error": "Runtime settings not available"}, status_code=503)
        return JSONResponse({"settings": runtime.get_all()})

    @mcp.custom_route(
        "/api/settings/runtime", methods=["PATCH"], name="api_runtime_settings_patch",
    )
    @handler(services_getter, parse_json="raw")
    def patch_runtime_settings(
        request: Request, services: ServicesDict, raw: bytes
    ) -> JSONResponse:
        """Update one or more runtime settings. Admin-only."""
        user = get_authenticated_user(request)
        if not user or not user.is_admin:
            return JSONResponse({"error": "Admin access required"}, status_code=403)
        runtime = services.get("runtime_settings")
        if runtime is None:
            return JSONResponse({"error": "Runtime settings not available"}, status_code=503)

        # Parse in-body ("raw" mode): the admin 403 above must keep
        # precedence over body-shape 400s.
        try:
            body = json.loads(raw)
        except ValueError:
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)

        if not isinstance(body, dict):
            return JSONResponse({"error": "Request body must be a JSON object"}, status_code=400)

        errors: list[str] = []
        updated: list[str] = []
        for key, value in body.items():
            try:
                runtime.set(key, value)
                updated.append(key)
            except (KeyError, ValueError) as e:
                errors.append(str(e))

        if errors and not updated:
            return JSONResponse({"error": "; ".join(errors)}, status_code=400)

        log.info("PATCH /api/settings/runtime — updated %s", updated)
        result: dict = {"updated": updated, "settings": runtime.get_all()}
        if errors:
            result["warnings"] = errors
        return JSONResponse(result)

    # ── Pricing configuration ────────────────────────────────────────

    @mcp.custom_route(
        "/api/settings/pricing", methods=["GET"], name="api_pricing_get",
    )
    @handler(services_getter)
    def get_pricing(
        request: Request, services: ServicesDict, body: None
    ) -> JSONResponse:
        """Return all API model pricing entries."""
        db_factory = services.get("db_factory")
        if db_factory is None:
            return JSONResponse({"error": "Database not available"}, status_code=503)
        entries = get_all_pricing(db_factory.get())
        return JSONResponse({"pricing": [_pricing_to_dict(e) for e in entries]})

    @mcp.custom_route(
        "/api/settings/pricing", methods=["PATCH"], name="api_pricing_patch",
    )
    @handler(services_getter, parse_json="raw")
    def patch_pricing(
        request: Request, services: ServicesDict, raw: bytes
    ) -> JSONResponse:
        """Update pricing for one or more models. Admin-only."""
        user = get_authenticated_user(request)
        if not user or not user.is_admin:
            return JSONResponse({"error": "Admin access required"}, status_code=403)
        db_factory = services.get("db_factory")
        if db_factory is None:
            return JSONResponse({"error": "Database not available"}, status_code=503)
        conn = db_factory.get()

        # Parse in-body ("raw" mode): the admin 403 above must keep
        # precedence over body-shape 400s.
        try:
            body = json.loads(raw)
        except ValueError:
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)

        if not isinstance(body, dict):
            return JSONResponse(
                {"error": "Request body must be a JSON object"}, status_code=400,
            )

        updated: list[str] = []
        errors: list[str] = []
        for model_name, model_updates in body.items():
            if not isinstance(model_updates, dict):
                errors.append(f"{model_name}: value must be an object")
                continue
            result = update_pricing(conn, model_name, model_updates)
            if result is None:
                errors.append(f"{model_name}: unknown model or no valid fields")
            else:
                updated.append(model_name)

        status = 200 if not errors else (207 if updated else 400)
        entries = get_all_pricing(conn)
        result_dict: dict[str, object] = {
            "updated": updated,
            "pricing": [_pricing_to_dict(e) for e in entries],
        }
        if errors:
            result_dict["errors"] = errors
        log.info("PATCH /api/settings/pricing — updated %s", updated)
        return JSONResponse(result_dict, status_code=status)
