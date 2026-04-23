"""Async batch job runner.

The `JobRunner` service is the glue between "please run this batch"
requests arriving from the API/MCP layer and the long-running
entity-extraction / mood-backfill workloads living in the rest of
the services package.

It owns:

- Param-shape validation (reject unknown keys up front rather than
  letting the worker discover them mid-flight).
- Lifecycle bookkeeping against the `jobs` table: queued -> running
  -> (succeeded | failed), with per-step progress updates driven by
  the `on_progress` callback contract added in Work Unit 1.
- A single-worker `ThreadPoolExecutor` that serialises every
  submitted job. One worker gives us three things at once:
    1. Predictable LLM rate usage (no contention for tokens).
    2. A clean story for the shared SQLite connection opened with
       `check_same_thread=False` — only one thread writes at a time,
       so WAL + NORMAL synchronous stays safe.
    3. Simpler failure reasoning: jobs cannot be racing each other.

IMPORTANT: bumping `max_workers` above 1 is a serious change. The
SQLite connection opened by the server for jobs use is shared
across threads under the explicit assumption that this executor
serialises access to it (see `db/connection.py` docstring). If that
invariant is relaxed, writes from multiple worker threads on a
single connection with WAL + NORMAL synchronous is NOT safe and
the schema access pattern must be redesigned first.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from journal.db.jobs_repository import SQLiteJobRepository
    from journal.db.repository import EntryRepository
    from journal.models import Job
    from journal.services.backfill import MoodBackfillResult
    from journal.services.entity_extraction import EntityExtractionService
    from journal.services.ingestion import IngestionService
    from journal.services.mood_scoring import MoodScoringService
    from journal.services.notifications import PushoverNotificationService

log = logging.getLogger(__name__)


def _friendly_error(exc: Exception) -> str:
    """Map known external-service exceptions to user-friendly messages.

    The raw exception is already logged by the caller; this produces a
    short message suitable for display in the webapp UI.
    """
    msg = str(exc)
    # Google Gemini API errors
    if "503" in msg and ("UNAVAILABLE" in msg or "high demand" in msg):
        return "OCR service overloaded"
    if "429" in msg and "RESOURCE_EXHAUSTED" in msg:
        return "Google API rate limit exceeded"
    if "404" in msg and "is not found for API version" in msg:
        return (
            "The configured OCR model was not found. "
            "Check the OCR_MODEL setting."
        )
    # OpenAI API errors
    if "openai" in msg.lower() and ("rate_limit" in msg.lower() or "429" in msg):
        return "OpenAI rate limit exceeded"
    # Anthropic API errors
    if "overloaded" in msg.lower() and ("anthropic" in msg.lower() or "529" in msg):
        return "Anthropic API overloaded"
    # Fall through — return the raw message for unexpected errors
    return msg


def _is_transient(exc: Exception) -> bool:
    """Return True if the exception looks like a temporary API issue worth retrying."""
    msg = str(exc)
    if "503" in msg and ("UNAVAILABLE" in msg or "high demand" in msg):
        return True
    if "429" in msg and ("RESOURCE_EXHAUSTED" in msg or "rate_limit" in msg.lower()):
        return True
    return "overloaded" in msg.lower() and ("529" in msg or "anthropic" in msg.lower())


# Retry schedule: 3 min, 6 min, 12 min (exponential backoff)
_RETRY_DELAYS_SECONDS = [180, 360, 720, 1440, 2880]


# --------------------------------------------------------------------
# Param validation
# --------------------------------------------------------------------

_ENTITY_EXTRACTION_KEYS: dict[str, type | tuple[type, ...]] = {
    "entry_id": int,
    "start_date": str,
    "end_date": str,
    "stale_only": bool,
    "user_id": int,
}

_MOOD_BACKFILL_KEYS: dict[str, type | tuple[type, ...]] = {
    "mode": str,
    "start_date": str,
    "end_date": str,
    "user_id": int,
}

_MOOD_BACKFILL_MODES = frozenset({"stale-only", "force"})

_INGEST_IMAGES_KEYS: dict[str, type | tuple[type, ...]] = {
    "entry_date": str,
    "user_id": int,
}

_MOOD_SCORE_ENTRY_KEYS: dict[str, type | tuple[type, ...]] = {
    "entry_id": int,
    "user_id": int,
}

_REPROCESS_EMBEDDINGS_KEYS: dict[str, type | tuple[type, ...]] = {
    "entry_id": int,
    "user_id": int,
}

_INGEST_AUDIO_KEYS: dict[str, type | tuple[type, ...]] = {
    "entry_date": str,
    "source_type": str,
    "user_id": int,
}


def _validate_params(
    params: dict[str, Any],
    allowed: dict[str, type | tuple[type, ...]],
    *,
    job_type: str,
) -> None:
    """Reject params with unknown keys or wrong value types.

    Booleans are a subclass of int in Python, so `stale_only=True`
    would incorrectly satisfy `int` typing. We handle that by
    checking bool BEFORE the generic isinstance when int is allowed
    but bool is not, and vice versa.
    """
    unknown = set(params) - set(allowed)
    if unknown:
        raise ValueError(
            f"Unknown params for {job_type}: {sorted(unknown)}"
        )
    for key, value in params.items():
        expected = allowed[key]
        # Python quirk: bool is a subclass of int. Disallow the
        # cross-type acceptance that isinstance would otherwise
        # silently allow.
        if expected is int and isinstance(value, bool):
            raise ValueError(
                f"Param {key!r} for {job_type} must be int, "
                f"got bool ({value!r})"
            )
        if expected is bool and not isinstance(value, bool):
            raise ValueError(
                f"Param {key!r} for {job_type} must be bool, "
                f"got {type(value).__name__} ({value!r})"
            )
        if not isinstance(value, expected):  # type: ignore[arg-type]
            raise ValueError(
                f"Param {key!r} for {job_type} must be "
                f"{expected}, got {type(value).__name__} ({value!r})"
            )


# --------------------------------------------------------------------
# JobRunner
# --------------------------------------------------------------------


class JobRunner:
    """Run background batch jobs serialised on a single worker.

    Uses a single-worker `ThreadPoolExecutor` so jobs are serialised
    — this keeps LLM rate-limiting simple and guarantees the shared
    SQLite connection (opened with `check_same_thread=False`) only
    receives one writer at a time.

    IMPORTANT: if `max_workers` is ever bumped above 1, the SQLite
    threading assumption (see `db/connection.py` docstring) must be
    re-examined. Writes from multiple threads on a single
    connection with WAL + NORMAL synchronous is NOT safe.
    """

    def __init__(
        self,
        *,
        job_repository: SQLiteJobRepository,
        entity_extraction_service: EntityExtractionService,
        mood_backfill_callable: Callable[..., MoodBackfillResult],
        mood_scoring_service: MoodScoringService,
        entry_repository: EntryRepository,
        ingestion_service: IngestionService | None = None,
        notification_service: PushoverNotificationService | None = None,
    ) -> None:
        self._jobs = job_repository
        self._extraction = entity_extraction_service
        self._mood_backfill = mood_backfill_callable
        self._mood_scoring = mood_scoring_service
        self._entries = entry_repository
        self._ingestion = ingestion_service
        self._notifications = notification_service
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="journal-jobs"
        )
        # Temporary storage for image data keyed by job_id. The images
        # are large binary blobs that cannot be serialised into the
        # jobs table params_json column. They are popped from the dict
        # when the worker starts so memory is released promptly.
        self._pending_images: dict[str, list[tuple[bytes, str, str]]] = {}
        # Same pattern for audio recordings: (data, media_type, filename).
        self._pending_audio: dict[str, list[tuple[bytes, str, str]]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def submit_entity_extraction(
        self, params: dict[str, Any], *, user_id: int | None = None,
    ) -> Job:
        """Queue an entity-extraction batch job.

        Accepts any subset of `entry_id`, `start_date`, `end_date`,
        `stale_only`. Unknown keys or wrong types raise
        `ValueError` before a row is inserted.
        """
        # Inject user_id into params so the extraction worker can scope
        # batch queries and entity creation to the correct user.
        run_params = {**params}
        if user_id is not None:
            run_params["user_id"] = user_id
        _validate_params(
            run_params, _ENTITY_EXTRACTION_KEYS, job_type="entity_extraction"
        )
        job = self._jobs.create("entity_extraction", run_params, user_id=user_id)
        self._executor.submit(self._run_entity_extraction, job.id, run_params)
        return job

    def submit_mood_backfill(
        self, params: dict[str, Any], *, user_id: int | None = None,
    ) -> Job:
        """Queue a mood-backfill batch job.

        Requires `mode` to be present and set to either
        `"stale-only"` or `"force"`. Accepts optional `start_date`
        and `end_date`. Unknown keys or wrong types raise
        `ValueError` before a row is inserted.
        """
        run_params = {**params}
        if user_id is not None:
            run_params["user_id"] = user_id
        _validate_params(
            run_params, _MOOD_BACKFILL_KEYS, job_type="mood_backfill"
        )
        if "mode" not in run_params:
            raise ValueError(
                "Param 'mode' is required for mood_backfill"
            )
        if run_params["mode"] not in _MOOD_BACKFILL_MODES:
            raise ValueError(
                f"Param 'mode' must be one of "
                f"{sorted(_MOOD_BACKFILL_MODES)}, got {run_params['mode']!r}"
            )
        job = self._jobs.create("mood_backfill", run_params, user_id=user_id)
        self._executor.submit(self._run_mood_backfill, job.id, run_params)
        return job

    def submit_image_ingestion(
        self,
        images: list[tuple[bytes, str, str]],  # (data, media_type, filename)
        entry_date: str,
        *,
        user_id: int | None = None,
    ) -> Job:
        """Queue an image-ingestion job.

        Images are held in memory until the worker starts. The params
        stored in the jobs table contain only the entry_date and page
        count (image bytes are too large for the JSON column).
        """
        if not images:
            raise ValueError("At least one image is required")
        params: dict[str, Any] = {"entry_date": entry_date}
        if user_id is not None:
            params["user_id"] = user_id
        _validate_params(params, _INGEST_IMAGES_KEYS, job_type="ingest_images")
        job = self._jobs.create(
            "ingest_images", {**params, "page_count": len(images)}, user_id=user_id,
        )
        self._pending_images[job.id] = images
        self._executor.submit(self._run_image_ingestion, job.id, params)
        return job

    def submit_mood_score_entry(
        self, entry_id: int, *, user_id: int | None = None,
    ) -> Job:
        """Queue a mood-scoring job for a single entry.

        Lighter than a full backfill — scores one entry and returns.
        Used by the text/file ingest endpoints to defer mood scoring
        to a background thread.
        """
        params: dict[str, Any] = {"entry_id": entry_id}
        if user_id is not None:
            params["user_id"] = user_id
        _validate_params(params, _MOOD_SCORE_ENTRY_KEYS, job_type="mood_score_entry")
        job = self._jobs.create("mood_score_entry", params, user_id=user_id)
        self._executor.submit(self._run_mood_score_entry, job.id, params)
        return job

    def submit_reprocess_embeddings(
        self, entry_id: int, *, user_id: int | None = None,
    ) -> Job:
        """Queue a re-embedding job for an entry after text is saved.

        Re-chunks the entry's text, calls the embedding provider, and
        updates the vector store. This is the slow part of a text save
        that was previously done synchronously in the PATCH handler.
        """
        params: dict[str, Any] = {"entry_id": entry_id}
        if user_id is not None:
            params["user_id"] = user_id
        _validate_params(params, _REPROCESS_EMBEDDINGS_KEYS, job_type="reprocess_embeddings")
        job = self._jobs.create("reprocess_embeddings", params, user_id=user_id)
        self._executor.submit(self._run_reprocess_embeddings, job.id, params)
        return job

    def submit_audio_ingestion(
        self,
        recordings: list[tuple[bytes, str, str]],  # (data, media_type, filename)
        entry_date: str,
        *,
        source_type: str = "voice",
        user_id: int | None = None,
    ) -> Job:
        """Queue an audio-ingestion job.

        Audio recordings are held in memory until the worker starts.
        The params stored in the jobs table contain only the entry_date,
        recording count, and source_type (audio bytes are too large for
        the JSON column).
        """
        if not recordings:
            raise ValueError("At least one audio recording is required")
        params: dict[str, Any] = {"entry_date": entry_date, "source_type": source_type}
        if user_id is not None:
            params["user_id"] = user_id
        _validate_params(params, _INGEST_AUDIO_KEYS, job_type="ingest_audio")
        job = self._jobs.create(
            "ingest_audio", {**params, "recording_count": len(recordings)},
            user_id=user_id,
        )
        self._pending_audio[job.id] = recordings
        self._executor.submit(self._run_audio_ingestion, job.id, params)
        return job

    # ------------------------------------------------------------------
    # Notification helpers
    # ------------------------------------------------------------------

    def _notify_success(
        self, user_id: int | None, job_type: str, result: dict,
    ) -> None:
        if self._notifications is not None and user_id is not None:
            try:
                self._notifications.notify_job_success(user_id, job_type, result)
            except Exception:  # noqa: BLE001
                log.warning("Notification send failed (success)", exc_info=True)

    def _notify_failed(
        self, user_id: int | None, job_type: str, error_msg: str,
        exc: Exception | None = None,
    ) -> None:
        if self._notifications is not None and user_id is not None:
            try:
                self._notifications.notify_job_failed(
                    user_id, job_type, error_msg, exc,
                )
                self._notifications.notify_admin_job_failed(
                    user_id, job_type, error_msg, exc,
                )
            except Exception:  # noqa: BLE001
                log.warning("Notification send failed (failure)", exc_info=True)

    def _notify_retrying(
        self, user_id: int | None, job_type: str, attempt: int,
        delay: int, error_msg: str, exc: Exception | None = None,
    ) -> None:
        if self._notifications is not None and user_id is not None:
            try:
                self._notifications.notify_job_retrying(
                    user_id, job_type, attempt, delay, error_msg, exc,
                )
            except Exception:  # noqa: BLE001
                log.warning("Notification send failed (retry)", exc_info=True)

    def shutdown(self, wait: bool = False) -> None:
        """Stop the executor, cancelling queued-but-not-started tasks.

        Call once at server shutdown. Running tasks are allowed to
        finish their current iteration; queued tasks are cancelled.
        After `shutdown`, further `submit_*` calls raise
        `RuntimeError` from the underlying executor.
        """
        self._executor.shutdown(wait=wait, cancel_futures=True)

    # ------------------------------------------------------------------
    # Worker bodies
    # ------------------------------------------------------------------

    def _run_entity_extraction(
        self, job_id: str, params: dict[str, Any]
    ) -> None:
        """Execute one entity-extraction job from start to terminal state.

        Must never let an exception escape: a "stuck running" row
        is strictly worse than a "failed" row for the UI. Any
        exception raised by the underlying extraction is caught,
        logged, and recorded via `mark_failed`.
        """
        try:
            self._jobs.mark_running(job_id)

            def progress_callback(current: int, total: int) -> None:
                self._jobs.update_progress(job_id, current, total)

            entry_id = params.get("entry_id")
            job_user_id = params.get("user_id")
            if entry_id is not None:
                # Single-entry path: extract_from_entry has no
                # native on_progress hook, so bracket the call with
                # manual (0, 1) and (1, 1) updates.
                progress_callback(0, 1)
                result_obj = self._extraction.extract_from_entry(
                    int(entry_id)
                )
                results = [result_obj]
                progress_callback(1, 1)
            else:
                results = self._extraction.extract_batch(
                    start_date=params.get("start_date"),
                    end_date=params.get("end_date"),
                    stale_only=bool(params.get("stale_only", False)),
                    on_progress=progress_callback,
                    user_id=job_user_id,
                )

            summary: dict[str, Any] = {
                "entries_processed": len(results),
                "entities_created": sum(
                    r.entities_created for r in results
                ),
                "entities_matched": sum(
                    r.entities_matched for r in results
                ),
                "mentions_created": sum(
                    r.mentions_created for r in results
                ),
                "relationships_created": sum(
                    r.relationships_created for r in results
                ),
                "warnings": [w for r in results for w in r.warnings],
            }
            self._jobs.mark_succeeded(job_id, summary)
            self._notify_success(job_user_id, "entity_extraction", summary)
        except Exception as exc:  # noqa: BLE001 — terminal-state guard
            log.exception(
                "Entity extraction job %s failed", job_id
            )
            try:
                friendly = _friendly_error(exc)
                self._jobs.mark_failed(job_id, friendly)
                self._notify_failed(
                    params.get("user_id"), "entity_extraction", friendly, exc,
                )
            except Exception:  # noqa: BLE001 — last-resort bookkeeping
                log.exception(
                    "Failed to record failure for job %s", job_id
                )

    def _run_image_ingestion(
        self, job_id: str, params: dict[str, Any]
    ) -> None:
        """Execute one image-ingestion job from start to terminal state.

        Transient API errors (503 overload, 429 rate limit) trigger
        automatic retries with exponential backoff (3 min, 6 min, 12 min).
        The job stays in ``running`` state during waits and
        ``status_detail`` shows the next retry time so the webapp can
        display it.
        """
        try:
            self._jobs.mark_running(job_id)
            images_with_names = self._pending_images.pop(job_id, [])
            if not images_with_names:
                self._jobs.mark_failed(job_id, "No image data found for job")
                return
            if self._ingestion is None:
                self._jobs.mark_failed(job_id, "Ingestion service not available")
                return

            # Strip filenames — IngestionService expects (bytes, media_type)
            images: list[tuple[bytes, str]] = [
                (data, media_type) for data, media_type, _filename in images_with_names
            ]
            entry_date = params["entry_date"]
            job_user_id: int | None = params.get("user_id")
            total = len(images)

            def progress_callback(current: int, total_pages: int) -> None:
                self._jobs.update_progress(job_id, current, total)

            last_exc: Exception | None = None
            for attempt in range(len(_RETRY_DELAYS_SECONDS) + 1):
                try:
                    if attempt > 0:
                        self._jobs.update_status_detail(job_id, None)
                        log.info(
                            "Image ingestion job %s — retry attempt %d",
                            job_id, attempt,
                        )

                    if len(images) == 1:
                        self._jobs.update_progress(job_id, 0, total)
                        entry = self._ingestion.ingest_image(
                            images[0][0], images[0][1], entry_date,
                            skip_mood=True, user_id=job_user_id or 1,
                        )
                        self._jobs.update_progress(job_id, 1, total)
                    else:
                        self._jobs.update_progress(job_id, 0, total)
                        entry = self._ingestion.ingest_multi_page_entry(
                            images, entry_date, skip_mood=True,
                            on_progress=progress_callback,
                            user_id=job_user_id or 1,
                        )
                    last_exc = None
                    break  # success
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
                    if not _is_transient(exc) or attempt >= len(_RETRY_DELAYS_SECONDS):
                        break  # non-transient or out of retries
                    delay = _RETRY_DELAYS_SECONDS[attempt]
                    delay_minutes = delay // 60
                    retry_at_local = datetime.now().astimezone() + timedelta(seconds=delay)
                    retry_time = retry_at_local.strftime("%H:%M")
                    friendly = _friendly_error(exc)
                    detail = f"{friendly}, retrying in {delay_minutes} minutes at {retry_time}"
                    log.warning(
                        "Image ingestion job %s — transient error, "
                        "retrying in %ds (attempt %d): %s",
                        job_id, delay, attempt + 1, exc,
                    )
                    self._jobs.update_status_detail(job_id, detail)
                    # Notify on first retry only
                    if attempt == 0:
                        self._notify_retrying(
                            job_user_id, "ingest_images", attempt + 1,
                            delay, friendly, exc,
                        )
                    time.sleep(delay)

            if last_exc is not None:
                raise last_exc

            self._jobs.update_progress(job_id, total, total)
            result = {"entry_id": entry.id}
            self._jobs.mark_succeeded(job_id, result)
            self._notify_success(job_user_id, "ingest_images", result)

            # Queue follow-up jobs: mood scoring + entity extraction.
            self._queue_post_ingestion_jobs(
                job_id, "Image", entry.id, job_user_id,
            )
        except Exception as exc:  # noqa: BLE001 — terminal-state guard
            log.exception("Image ingestion job %s failed", job_id)
            # Clean up any remaining image data
            self._pending_images.pop(job_id, None)
            try:
                friendly = _friendly_error(exc)
                if _is_transient(exc):
                    friendly += " — please try again later"
                self._jobs.mark_failed(job_id, friendly)
                self._notify_failed(
                    job_user_id, "ingest_images", friendly, exc,
                )
            except Exception:  # noqa: BLE001 — last-resort bookkeeping
                log.exception("Failed to record failure for job %s", job_id)

    def _queue_post_ingestion_jobs(
        self,
        parent_job_id: str,
        kind: str,
        entry_id: int,
        user_id: int | None,
    ) -> None:
        """Queue mood scoring + entity extraction after ingestion."""
        follow_ups: list[tuple[str, Job]] = []
        for label, submit in [
            ("mood scoring", lambda: self.submit_mood_score_entry(
                entry_id, user_id=user_id,
            )),
            ("entity extraction", lambda: self.submit_entity_extraction(
                {"entry_id": entry_id}, user_id=user_id,
            )),
        ]:
            try:
                fj = submit()
                follow_ups.append((label, fj))
                log.info(
                    "%s ingestion job %s — queued %s %s"
                    " for entry %d",
                    kind, parent_job_id, label, fj.id,
                    entry_id,
                )
            except Exception:  # noqa: BLE001
                log.warning(
                    "%s ingestion job %s — failed to queue %s",
                    kind, parent_job_id, label,
                    exc_info=True,
                )

    def _run_mood_score_entry(
        self, job_id: str, params: dict[str, Any]
    ) -> None:
        """Score a single entry's mood dimensions."""
        try:
            self._jobs.mark_running(job_id)
            self._jobs.update_progress(job_id, 0, 1)

            entry_id = params["entry_id"]
            entry = self._entries.get_entry(entry_id)
            if entry is None:
                self._jobs.mark_failed(job_id, f"Entry {entry_id} not found")
                return

            text = entry.final_text or entry.raw_text
            if not text or not text.strip():
                self._jobs.mark_failed(job_id, f"Entry {entry_id} has no text")
                return

            count = self._mood_scoring.score_entry(entry_id, text)
            self._jobs.update_progress(job_id, 1, 1)
            result = {"entry_id": entry_id, "scores_written": count}
            self._jobs.mark_succeeded(job_id, result)
            self._notify_success(
                params.get("user_id"), "mood_score_entry", result,
            )
        except Exception as exc:  # noqa: BLE001 — terminal-state guard
            log.exception("Mood score entry job %s failed", job_id)
            try:
                friendly = _friendly_error(exc)
                self._jobs.mark_failed(job_id, friendly)
                self._notify_failed(
                    params.get("user_id"), "mood_score_entry", friendly, exc,
                )
            except Exception:  # noqa: BLE001 — last-resort bookkeeping
                log.exception("Failed to record failure for job %s", job_id)

    def _run_reprocess_embeddings(
        self, job_id: str, params: dict[str, Any]
    ) -> None:
        """Re-chunk and re-embed an entry's text in the background."""
        try:
            self._jobs.mark_running(job_id)
            self._jobs.update_progress(job_id, 0, 1)

            entry_id = params["entry_id"]
            if self._ingestion is None:
                self._jobs.mark_failed(job_id, "Ingestion service not available")
                return

            chunk_count = self._ingestion.reprocess_embeddings(entry_id)
            self._jobs.update_progress(job_id, 1, 1)
            result = {"entry_id": entry_id, "chunk_count": chunk_count}
            self._jobs.mark_succeeded(job_id, result)
            self._notify_success(
                params.get("user_id"), "reprocess_embeddings", result,
            )
        except Exception as exc:  # noqa: BLE001 — terminal-state guard
            log.exception("Reprocess embeddings job %s failed", job_id)
            try:
                friendly = _friendly_error(exc)
                self._jobs.mark_failed(job_id, friendly)
                self._notify_failed(
                    params.get("user_id"), "reprocess_embeddings",
                    friendly, exc,
                )
            except Exception:  # noqa: BLE001 — last-resort bookkeeping
                log.exception("Failed to record failure for job %s", job_id)

    def _run_mood_backfill(
        self, job_id: str, params: dict[str, Any]
    ) -> None:
        """Execute one mood-backfill job from start to terminal state.

        Same guarantee as `_run_entity_extraction`: always reaches a
        terminal state, never lets exceptions escape the executor.
        """
        try:
            self._jobs.mark_running(job_id)

            def progress_callback(current: int, total: int) -> None:
                self._jobs.update_progress(job_id, current, total)

            backfill_result = self._mood_backfill(
                repository=self._entries,
                mood_scoring=self._mood_scoring,
                mode=params["mode"],
                start_date=params.get("start_date"),
                end_date=params.get("end_date"),
                on_progress=progress_callback,
                user_id=params.get("user_id"),
            )

            summary: dict[str, Any] = {
                "scored": backfill_result.scored,
                "skipped": backfill_result.skipped,
                "errors": list(backfill_result.errors),
            }
            self._jobs.mark_succeeded(job_id, summary)
            self._notify_success(
                params.get("user_id"), "mood_backfill", summary,
            )
        except Exception as exc:  # noqa: BLE001 — terminal-state guard
            log.exception("Mood backfill job %s failed", job_id)
            try:
                friendly = _friendly_error(exc)
                self._jobs.mark_failed(job_id, friendly)
                self._notify_failed(
                    params.get("user_id"), "mood_backfill", friendly, exc,
                )
            except Exception:  # noqa: BLE001 — last-resort bookkeeping
                log.exception(
                    "Failed to record failure for job %s", job_id
                )

    def _run_audio_ingestion(
        self, job_id: str, params: dict[str, Any]
    ) -> None:
        """Execute one audio-ingestion job from start to terminal state.

        Transient API errors (429 rate limit) trigger automatic retries
        with exponential backoff, mirroring ``_run_image_ingestion``.
        """
        try:
            self._jobs.mark_running(job_id)
            recordings_with_names = self._pending_audio.pop(job_id, [])
            if not recordings_with_names:
                self._jobs.mark_failed(job_id, "No audio data found for job")
                return
            if self._ingestion is None:
                self._jobs.mark_failed(job_id, "Ingestion service not available")
                return

            # Strip filenames — IngestionService expects (bytes, media_type)
            recordings: list[tuple[bytes, str]] = [
                (data, media_type)
                for data, media_type, _filename in recordings_with_names
            ]
            entry_date = params["entry_date"]
            job_user_id: int | None = params.get("user_id")
            total = len(recordings)

            def progress_callback(current: int, total_recs: int) -> None:
                self._jobs.update_progress(job_id, current, total)

            last_exc: Exception | None = None
            for attempt in range(len(_RETRY_DELAYS_SECONDS) + 1):
                try:
                    if attempt > 0:
                        self._jobs.update_status_detail(job_id, None)
                        log.info(
                            "Audio ingestion job %s — retry attempt %d",
                            job_id, attempt,
                        )

                    self._jobs.update_progress(job_id, 0, total)
                    entry = self._ingestion.ingest_multi_voice(
                        recordings, entry_date,
                        source_type=params.get("source_type", "voice"),
                        skip_mood=True,
                        on_progress=progress_callback,
                        user_id=job_user_id or 1,
                    )
                    last_exc = None
                    break  # success
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
                    if not _is_transient(exc) or attempt >= len(_RETRY_DELAYS_SECONDS):
                        break  # non-transient or out of retries
                    delay = _RETRY_DELAYS_SECONDS[attempt]
                    delay_minutes = delay // 60
                    retry_at_local = datetime.now().astimezone() + timedelta(seconds=delay)
                    retry_time = retry_at_local.strftime("%H:%M")
                    friendly = _friendly_error(exc)
                    detail = f"{friendly}, retrying in {delay_minutes} minutes at {retry_time}"
                    log.warning(
                        "Audio ingestion job %s — transient error, "
                        "retrying in %ds (attempt %d): %s",
                        job_id, delay, attempt + 1, exc,
                    )
                    self._jobs.update_status_detail(job_id, detail)
                    # Notify on first retry only
                    if attempt == 0:
                        self._notify_retrying(
                            job_user_id, "ingest_audio", attempt + 1,
                            delay, friendly, exc,
                        )
                    time.sleep(delay)

            if last_exc is not None:
                raise last_exc

            self._jobs.update_progress(job_id, total, total)
            result = {"entry_id": entry.id}
            self._jobs.mark_succeeded(job_id, result)
            self._notify_success(job_user_id, "ingest_audio", result)

            # Queue follow-up jobs: mood scoring + entity extraction.
            self._queue_post_ingestion_jobs(
                job_id, "Audio", entry.id, job_user_id,
            )
        except Exception as exc:  # noqa: BLE001 — terminal-state guard
            log.exception("Audio ingestion job %s failed", job_id)
            # Clean up any remaining audio data
            self._pending_audio.pop(job_id, None)
            try:
                friendly = _friendly_error(exc)
                if _is_transient(exc):
                    friendly += " — please try again later"
                self._jobs.mark_failed(job_id, friendly)
                self._notify_failed(
                    job_user_id, "ingest_audio", friendly, exc,
                )
            except Exception:  # noqa: BLE001 — last-resort bookkeeping
                log.exception("Failed to record failure for job %s", job_id)
