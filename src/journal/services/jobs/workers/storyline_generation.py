"""Worker body: regenerate one storyline's curation + narrative panels."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from journal.services.jobs.errors import friendly_error

if TYPE_CHECKING:
    from journal.services.jobs.workers import WorkerContext

log = logging.getLogger(__name__)


def run_storyline_generation(
    ctx: WorkerContext, job_id: str, params: dict[str, Any],
) -> None:
    """Regenerate a single storyline's panels.

    `params` carries `storyline_id` (required) and optionally
    `user_id` for notification routing + `parent_job_id` for the
    pipeline-notification consolidation pattern used by the
    extension-check hook. Optional `start_date`/`end_date` (ISO
    YYYY-MM-DD) override the storyline row's date window for this
    run; `mode` is ``"replace"`` (default) or ``"append"``. Date
    strings are parsed at the service boundary, not here.

    When `chapter_id` is present the worker regenerates that specific
    chapter (its own date window is authoritative, so only `mode` is
    forwarded); otherwise, at the storyline level, a truthy `resegment`
    re-carves the storyline into titled word-sized chapters via
    `resegment_storyline(..., override_locked=...)`, while the default
    refreshes the open chapter via the back-compat
    `regenerate(storyline_id)` entry point. On that default path a truthy
    `auto_split` is forwarded as `regenerate(..., auto_split=True)` so an
    over-budget open chapter is re-segmented automatically (ingest path
    only); it is ignored on the chapter and resegment branches.
    """
    user_id = params.get("user_id")
    parent_job_id = params.get("parent_job_id")
    try:
        ctx.jobs.mark_running(job_id)
        ctx.jobs.update_progress(job_id, 0, 1)

        if ctx.storyline_generation is None:
            error_msg = (
                "StorylineGenerationService not configured on this server; "
                "cannot regenerate storyline."
            )
            ctx.jobs.mark_failed(job_id, error_msg)
            if ctx.notifier.get_notify_strategy(parent_job_id) != "compressed_all":
                ctx.notifier.notify_failed(
                    user_id, "storyline_generation", error_msg,
                )
            if parent_job_id:
                ctx.notifier.try_pipeline_notification(parent_job_id, user_id)
            return

        storyline_id = params["storyline_id"]
        chapter_id = params.get("chapter_id")
        if chapter_id is not None:
            # Chapter-scoped regeneration: the chapter's own window is
            # authoritative, so only ``mode`` is forwarded.
            chapter_kwargs: dict[str, Any] = {}
            if "mode" in params:
                chapter_kwargs["mode"] = params["mode"]
            result = ctx.storyline_generation.regenerate_chapter(
                int(chapter_id), **chapter_kwargs,
            )
        elif params.get("resegment"):
            # Storyline-level re-segmentation: re-carve the whole
            # storyline into titled word-sized chapters. ``start_date``/
            # ``end_date``/``mode`` are not meaningful here (the service
            # derives boundaries itself), so only ``override_locked`` is
            # forwarded.
            result = ctx.storyline_generation.resegment_storyline(
                int(storyline_id),
                override_locked=bool(params.get("override_locked")),
            )
        else:
            regenerate_kwargs: dict[str, Any] = {}
            if "start_date" in params:
                regenerate_kwargs["start_date"] = params["start_date"]
            if "end_date" in params:
                regenerate_kwargs["end_date"] = params["end_date"]
            if "mode" in params:
                regenerate_kwargs["mode"] = params["mode"]
            if params.get("auto_split"):
                regenerate_kwargs["auto_split"] = True
            result = ctx.storyline_generation.regenerate(
                int(storyline_id), **regenerate_kwargs,
            )
        ctx.jobs.update_progress(job_id, 1, 1)

        summary: dict[str, Any] = {
            "storyline_id": storyline_id,
            "entry_count": result.entry_count,
            "entity_mention_count": result.entity_mention_count,
            "fts_fallback_count": result.fts_fallback_count,
            "narrative_citation_count": result.narrative_citation_count,
            "curation_citation_count": result.curation_citation_count,
            "narrative_model": result.narrative_model,
            "curation_model": result.curation_model,
        }
        if getattr(result, "chapter_count", 0):
            summary["chapter_count"] = result.chapter_count
        if result.warnings:
            summary["warnings"] = result.warnings
        ctx.jobs.mark_succeeded(job_id, summary)

        if parent_job_id:
            ctx.notifier.try_pipeline_notification(parent_job_id, user_id)
        # No success notification by default — storyline_generation
        # fires on every entry that matches an active storyline's
        # anchors and would be noisy. Failures still notify. Mirrors
        # the pattern in run_storyline_extension_check.
    except Exception as exc:  # noqa: BLE001 — terminal-state guard
        log.exception("Storyline generation job %s failed", job_id)
        try:
            friendly = friendly_error(exc)
            ctx.jobs.mark_failed(job_id, friendly)
            if ctx.notifier.get_notify_strategy(parent_job_id) != "compressed_all":
                ctx.notifier.notify_failed(
                    user_id, "storyline_generation", friendly, exc,
                )
            if parent_job_id:
                ctx.notifier.try_pipeline_notification(parent_job_id, user_id)
        except Exception:  # noqa: BLE001 — last-resort bookkeeping
            log.exception("Failed to record failure for job %s", job_id)
