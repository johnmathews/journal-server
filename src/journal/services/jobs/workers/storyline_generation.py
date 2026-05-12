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
    extension-check hook.
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
        result = ctx.storyline_generation.regenerate(storyline_id)
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
        if result.warnings:
            summary["warnings"] = result.warnings
        ctx.jobs.mark_succeeded(job_id, summary)

        if parent_job_id:
            ctx.notifier.try_pipeline_notification(parent_job_id, user_id)
        else:
            ctx.notifier.notify_success(
                user_id, "storyline_generation", summary,
            )
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
