"""Worker body: run the StorylineEngine for one storyline."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from journal.services.jobs.errors import friendly_error

if TYPE_CHECKING:
    from journal.services.jobs.workers import WorkerContext
    from journal.services.storylines.engine import UpdateResult

log = logging.getLogger(__name__)


def run_storyline_update(
    ctx: WorkerContext, job_id: str, params: dict[str, Any],
) -> None:
    """Run the StorylineEngine for one storyline.

    ``params`` carries `storyline_id` (required) and `user_id` for
    notification routing + `parent_job_id` for the pipeline-
    notification consolidation pattern used by the extension-check
    hook. At most one of `bootstrap` / `refresh_only` / `unpublish`
    may be truthy (enforced by `JobRunner.submit_storyline_update`
    before the job is queued):

    - `bootstrap`: partition the storyline's full history into
      chapters via `engine.bootstrap`, replacing any existing ones.
    - `refresh_only`: re-narrate the draft from its existing
      membership via `engine.refresh_draft`, without consulting the
      judge or changing membership.
    - `unpublish`: fold the newest published chapter back into the
      draft (`storyline_repository.unpublish_newest`) *then*
      re-narrate it via `engine.refresh_draft` — the fold must land
      before the re-narration reads the draft's membership.
    - none of the above: the default steady-state `engine.update`
      call — continue-or-break judged against whatever is new.

    A publish (`result.published` set) fires exactly one Pushover
    notification via `ctx.notifier.notify_chapter_published`, best-
    effort — a notification failure never fails the job. Failures
    from the engine itself mark the job failed with a friendly error
    message; nothing here suppresses that path.
    """
    user_id = params.get("user_id")
    parent_job_id = params.get("parent_job_id")
    storyline_id = int(params["storyline_id"])
    try:
        ctx.jobs.mark_running(job_id)
        ctx.jobs.update_progress(job_id, 0, 1)

        if ctx.storyline_engine is None:
            error_msg = (
                "StorylineEngine not configured on this server; "
                "cannot update storyline."
            )
            ctx.jobs.mark_failed(job_id, error_msg)
            if ctx.notifier.get_notify_strategy(parent_job_id) != "compressed_all":
                ctx.notifier.notify_failed(
                    user_id, "storyline_update", error_msg,
                )
            if parent_job_id:
                ctx.notifier.try_pipeline_notification(parent_job_id, user_id)
            return

        engine = ctx.storyline_engine
        result: UpdateResult
        if params.get("bootstrap"):
            result = engine.bootstrap(storyline_id)
        elif params.get("refresh_only"):
            result = engine.refresh_draft(storyline_id)
        elif params.get("unpublish"):
            if ctx.storyline_repository is None:
                raise RuntimeError(
                    "Storyline repository not configured; cannot unpublish."
                )
            # Fold the newest published chapter back into the draft
            # BEFORE re-narrating: refresh_draft reads the draft's
            # current membership, so the fold must land first.
            ctx.storyline_repository.unpublish_newest(storyline_id)
            result = engine.refresh_draft(storyline_id)
        else:
            result = engine.update(storyline_id)
        ctx.jobs.update_progress(job_id, 1, 1)

        summary: dict[str, Any] = {"storyline_id": result.storyline_id}
        if result.new_entry_count:
            summary["new_entry_count"] = result.new_entry_count
        if result.draft_entry_count:
            summary["draft_entry_count"] = result.draft_entry_count
        if result.published is not None:
            summary["published_chapter_id"] = result.published.chapter_id
            summary["published_title"] = result.published.title
        if result.addenda_chapter_ids:
            summary["addenda_chapter_ids"] = result.addenda_chapter_ids
        if result.chapter_count:
            summary["chapter_count"] = result.chapter_count
        if result.reasoning:
            summary["reasoning"] = result.reasoning
        if result.warnings:
            summary["warnings"] = result.warnings
        ctx.jobs.mark_succeeded(job_id, summary)

        if result.published is not None and user_id is not None:
            try:
                storyline_name = ""
                if ctx.storyline_repository is not None:
                    storyline = ctx.storyline_repository.get_storyline(storyline_id)
                    if storyline is not None:
                        storyline_name = storyline.name
                ctx.notifier.notify_chapter_published(
                    user_id, storyline_name, result.published.title,
                )
            except Exception:  # noqa: BLE001 — notification is best-effort
                log.exception("Publish notification failed (job %s)", job_id)

        if parent_job_id:
            ctx.notifier.try_pipeline_notification(parent_job_id, user_id)
        # No success notification by default for the plain update path —
        # it fires on every entry that matches an active storyline's
        # anchors and would be noisy. The publish notification above is
        # the user-facing success signal; failures still notify.
    except Exception as exc:  # noqa: BLE001 — terminal-state guard
        log.exception("Storyline update job %s failed", job_id)
        try:
            friendly = friendly_error(exc)
            ctx.jobs.mark_failed(job_id, friendly)
            if ctx.notifier.get_notify_strategy(parent_job_id) != "compressed_all":
                ctx.notifier.notify_failed(
                    user_id, "storyline_update", friendly, exc,
                )
            if parent_job_id:
                ctx.notifier.try_pipeline_notification(parent_job_id, user_id)
        except Exception:  # noqa: BLE001 — last-resort bookkeeping
            log.exception("Failed to record failure for job %s", job_id)
