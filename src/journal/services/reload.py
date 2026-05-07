"""Operator-triggered reload helpers for file-backed config.

Three resources are read from disk at startup and otherwise never
re-read: the OCR context glossary, the transcription context (same
files, different formatter), and the mood-dimension TOML. Each helper
here rebuilds one of those resources and asks the running services to
swap their references via the public ``replace_*`` methods on
``IngestionService`` and ``JobRunner`` — earlier versions of this
module wrote ``services["ingestion"]._ocr = new`` directly, but those
private-state writes are exactly the kind of cross-component reach-in
the rest of the codebase has been moving away from. After item 2's
worker-extraction the direct write became a real bug for mood
scoring (the live handle moved to ``runner._ctx.mood_scoring``),
which prompted the named-method surface.

Concurrency:
    Python attribute writes inside ``replace_*`` are atomic. An
    in-flight request that already resolved e.g. ``self._ocr``
    keeps its reference and finishes against it; the next request
    resolves the attribute and gets the new one. No locks, no
    special teardown — the old provider is garbage-collected once
    no in-flight code holds a reference.

Each helper takes the live `services` dict (shaped like the one built
in `journal.mcp_server._init_services`) and the current `Config`, and
returns a small JSON-friendly summary that operators can use to verify
the reload landed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from journal.providers.ocr import build_ocr_provider, load_context_files
from journal.providers.transcription import (
    _describe_stack,
    build_transcription_provider,
)

if TYPE_CHECKING:
    from journal.config import Config


def _now_iso() -> str:
    """UTC timestamp in ISO-8601, second precision, with explicit Z."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _context_stats(config: Config) -> tuple[int, int]:
    """Return ``(file_count, total_chars)`` for the OCR context dir.

    Computed independently of the OCR provider type so the same shape
    works for Anthropic, Gemini, or DualPass providers.
    """
    text = load_context_files(config.ocr_context_dir)
    if not config.ocr_context_dir or not config.ocr_context_dir.exists():
        return (0, len(text))
    files = sorted(config.ocr_context_dir.glob("*.md"))
    return (len(files), len(text))


def reload_ocr_provider(services: dict, config: Config) -> dict[str, Any]:
    """Rebuild the OCR provider and swap it into the ingestion service."""
    new_ocr = build_ocr_provider(config)
    services["ingestion"].replace_ocr(new_ocr)

    file_count, char_count = _context_stats(config)
    return {
        "reloaded": "ocr-context",
        "provider": config.ocr_provider,
        "model": config.ocr_model or "default",
        "dual_pass": config.ocr_dual_pass,
        "context_files": file_count,
        "context_chars": char_count,
        "reloaded_at": _now_iso(),
    }


def reload_transcription_provider(services: dict, config: Config) -> dict[str, Any]:
    """Rebuild the transcription provider stack and swap it in.

    The transcription stack may be wrapped (Retrying / Shadow); the
    summary's ``stack`` field is the human-readable stack description
    used in startup logs.
    """
    new_transcription = build_transcription_provider(config)
    services["ingestion"].replace_transcription(new_transcription)

    file_count, char_count = _context_stats(config)
    return {
        "reloaded": "transcription-context",
        "stack": _describe_stack(new_transcription),
        "context_files": file_count,
        "context_chars": char_count,
        "reloaded_at": _now_iso(),
    }


def reload_mood_dimensions(services: dict, config: Config) -> dict[str, Any]:
    """Reload the mood-dimensions TOML and rebuild the scoring service.

    Both ``IngestionService`` and ``JobRunner`` are pointed at the
    *same* fresh ``MoodScoringService`` instance via their public
    ``replace_mood_scoring`` methods — keeping them in sync is
    load-bearing because both services score entries against the
    same dimension set.

    Raises ``RuntimeError`` if mood scoring is disabled in the current
    config; the caller (an admin endpoint) translates that to a 4xx so
    the operator can correct the deployment instead of silently no-op'ing.
    """
    if not config.enable_mood_scoring:
        raise RuntimeError(
            "Cannot reload mood dimensions: mood scoring is disabled "
            "(set JOURNAL_ENABLE_MOOD_SCORING=true to enable)."
        )

    # Imports are local so that the module loads in deployments where
    # mood scoring is disabled and these dependencies aren't needed at
    # import time.
    from journal.providers.mood_scorer import AnthropicMoodScorer
    from journal.services.mood_dimensions import (
        load_mood_dimensions,
        load_mood_meta,
    )
    from journal.services.mood_scoring import MoodScoringService

    dims = load_mood_dimensions(config.mood_dimensions_path)
    meta = load_mood_meta(config.mood_dimensions_path)
    scorer = AnthropicMoodScorer(
        api_key=config.anthropic_api_key,
        model=config.mood_scorer_model,
        max_tokens=config.mood_scorer_max_tokens,
    )
    # `repository` owns the SQLite connection and isn't config-driven —
    # reuse it. Prefer the existing scoring service's repo (for symmetry
    # with the original construction); fall back to the ingestion
    # service's repo when mood scoring was never instantiated (e.g. the
    # operator just enabled it via runtime settings and is now reloading
    # the dimension file).
    ingestion = services["ingestion"]
    existing = ingestion.mood_scoring
    repository = (
        existing._repo  # noqa: SLF001 — MoodScoringService doesn't expose a public accessor yet
        if existing is not None
        else ingestion.repository
    )

    new_service = MoodScoringService(
        scorer=scorer,
        repository=repository,
        dimensions=dims,
    )
    ingestion.replace_mood_scoring(new_service)
    services["job_runner"].replace_mood_scoring(new_service)
    services["mood_dimensions"] = dims
    services["mood_dimensions_meta"] = meta

    return {
        "reloaded": "mood-dimensions",
        "dimension_count": len(dims),
        "dimensions": [d.name for d in dims],
        "version": meta.version,
        "reloaded_at": _now_iso(),
    }


def reload_entity_casing_exceptions(
    services: dict, config: Config
) -> dict[str, Any]:
    """Reload the entity-casing exceptions TOML and rebind it on the store.

    The exceptions dict held by ``SQLiteEntityStore`` is replaced via
    ``set_casing_exceptions`` — a single atomic attribute write. Pre-reload
    callers that already entered ``smart_title_case`` finish with the dict
    they captured; subsequent ``create_entity`` calls see the new table.

    Also stashes the parsed dict under ``services["entity_casing_exceptions"]``
    so the admin tab / future read endpoints can introspect it without
    re-parsing the TOML.
    """
    from journal.services.entity_naming import load_entity_casing_exceptions

    exceptions = load_entity_casing_exceptions(config.entity_casing_exceptions_path)
    store = services["entity_store"]
    store.set_casing_exceptions(exceptions)
    services["entity_casing_exceptions"] = exceptions
    return {
        "reloaded": "entity-casing",
        "exception_count": len(exceptions),
        "path": str(config.entity_casing_exceptions_path),
        "reloaded_at": _now_iso(),
    }
