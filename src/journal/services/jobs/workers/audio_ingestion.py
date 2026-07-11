"""Worker body: ingest one or more audio recordings into a single entry."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from journal.services.jobs.errors import friendly_error, is_transient
from journal.services.jobs.retry import run_with_retry

if TYPE_CHECKING:
    from journal.services.jobs.workers import WorkerContext

log = logging.getLogger(__name__)


def run_audio_ingestion(
    ctx: WorkerContext, job_id: str, params: dict[str, Any],
) -> None:
    """Execute one audio-ingestion job from start to terminal state.

    Transient API errors (429 rate limit) trigger automatic retries
    with exponential backoff, mirroring ``run_image_ingestion``.
    """
    job_user_id: int | None = None
    try:
        ctx.jobs.mark_running(job_id)
        recordings_with_names = ctx.pop_pending_audio(job_id)
        if not recordings_with_names:
            ctx.jobs.mark_failed(job_id, "No audio data found for job")
            return
        if ctx.ingestion is None:
            ctx.jobs.mark_failed(job_id, "Ingestion service not available")
            return

        # Strip filenames — IngestionService expects (bytes, media_type)
        recordings: list[tuple[bytes, str]] = [
            (data, media_type)
            for data, media_type, _filename in recordings_with_names
        ]
        entry_date = params["entry_date"]
        job_user_id = params.get("user_id")
        # Resolve the effective user once so the entry and every follow-up
        # (mood, entity extraction → storyline check) share one attribution
        # and the storyline trigger is never silently dropped (W3).
        resolved_user_id = job_user_id or 1
        total = len(recordings)

        def progress_callback(current: int, _total_recs: int) -> None:
            ctx.jobs.update_progress(job_id, current, total)

        def operation():  # noqa: ANN202 — local helper
            assert ctx.ingestion is not None  # noqa: S101 — guarded above
            ctx.jobs.update_progress(job_id, 0, total)
            return ctx.ingestion.ingest_multi_voice(
                recordings, entry_date,
                source_type=params.get("source_type", "voice"),
                skip_mood=True,
                on_progress=progress_callback,
                user_id=resolved_user_id,
            )

        entry = run_with_retry(
            jobs=ctx.jobs,
            notifier=ctx.notifier,
            job_id=job_id,
            job_type="ingest_audio",
            user_id=job_user_id,
            operation=operation,
            log_prefix="Audio ingestion",
        )

        ctx.jobs.update_progress(job_id, total, total)

        # Queue follow-up jobs: mood scoring + entity extraction (the
        # latter fires the storyline check). Attributed to the resolved
        # user so the storyline trigger is scoped, not skipped.
        follow_up_ids = ctx.queue_post_ingestion_jobs(
            job_id, "Audio", entry.id, resolved_user_id,
        )

        result: dict[str, Any] = {
            "entry_id": entry.id,
            "entry_date": entry.entry_date,
            "source_type": entry.source_type,
            "word_count": entry.word_count,
            "chunk_count": entry.chunk_count,
            "recording_count": total,
            "follow_up_jobs": follow_up_ids,
        }
        ctx.jobs.mark_succeeded(job_id, result)
        if not follow_up_ids:
            # No follow-ups were queued (e.g. executor shutting down) —
            # notify directly so the user learns the entry was created.
            # If follow-ups were queued the combined pipeline notification
            # fires when the last one completes.
            ctx.notifier.notify_success(job_user_id, "ingest_audio", result)
        else:
            # Defensive sweep: on the multi-worker Pool A, a follow-up
            # child can reach a terminal state BEFORE this parent marked
            # itself succeeded — that child's try_pipeline_notification
            # then saw the parent still running and returned early. Now
            # that the parent row is succeeded, re-check so the
            # consolidated push still fires. try_acquire_notification_lock
            # dedupes against the last child's own call, so this is a
            # no-op when a child already fired.
            ctx.notifier.try_pipeline_notification(job_id, job_user_id)
    except Exception as exc:  # noqa: BLE001 — terminal-state guard
        log.exception("Audio ingestion job %s failed", job_id)
        # Clean up any remaining audio data
        ctx.pop_pending_audio(job_id)
        try:
            friendly = friendly_error(exc)
            if is_transient(exc):
                friendly += " — please try again later"
            ctx.jobs.mark_failed(job_id, friendly)
            ctx.notifier.notify_failed(
                job_user_id, "ingest_audio", friendly, exc,
            )
        except Exception:  # noqa: BLE001 — last-resort bookkeeping
            log.exception("Failed to record failure for job %s", job_id)
