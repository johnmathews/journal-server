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
from concurrent.futures import ThreadPoolExecutor
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

log = logging.getLogger(__name__)


# --------------------------------------------------------------------
# Param validation
# --------------------------------------------------------------------

_ENTITY_EXTRACTION_KEYS: dict[str, type | tuple[type, ...]] = {
    "entry_id": int,
    "start_date": str,
    "end_date": str,
    "stale_only": bool,
}

_MOOD_BACKFILL_KEYS: dict[str, type | tuple[type, ...]] = {
    "mode": str,
    "start_date": str,
    "end_date": str,
}

_MOOD_BACKFILL_MODES = frozenset({"stale-only", "force"})

_INGEST_IMAGES_KEYS: dict[str, type | tuple[type, ...]] = {
    "entry_date": str,
}

_MOOD_SCORE_ENTRY_KEYS: dict[str, type | tuple[type, ...]] = {
    "entry_id": int,
}

_REPROCESS_EMBEDDINGS_KEYS: dict[str, type | tuple[type, ...]] = {
    "entry_id": int,
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
    ) -> None:
        self._jobs = job_repository
        self._extraction = entity_extraction_service
        self._mood_backfill = mood_backfill_callable
        self._mood_scoring = mood_scoring_service
        self._entries = entry_repository
        self._ingestion = ingestion_service
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="journal-jobs"
        )
        # Temporary storage for image data keyed by job_id. The images
        # are large binary blobs that cannot be serialised into the
        # jobs table params_json column. They are popped from the dict
        # when the worker starts so memory is released promptly.
        self._pending_images: dict[str, list[tuple[bytes, str, str]]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def submit_entity_extraction(self, params: dict[str, Any]) -> Job:
        """Queue an entity-extraction batch job.

        Accepts any subset of `entry_id`, `start_date`, `end_date`,
        `stale_only`. Unknown keys or wrong types raise
        `ValueError` before a row is inserted.
        """
        _validate_params(
            params, _ENTITY_EXTRACTION_KEYS, job_type="entity_extraction"
        )
        job = self._jobs.create("entity_extraction", params)
        self._executor.submit(self._run_entity_extraction, job.id, params)
        return job

    def submit_mood_backfill(self, params: dict[str, Any]) -> Job:
        """Queue a mood-backfill batch job.

        Requires `mode` to be present and set to either
        `"stale-only"` or `"force"`. Accepts optional `start_date`
        and `end_date`. Unknown keys or wrong types raise
        `ValueError` before a row is inserted.
        """
        _validate_params(
            params, _MOOD_BACKFILL_KEYS, job_type="mood_backfill"
        )
        if "mode" not in params:
            raise ValueError(
                "Param 'mode' is required for mood_backfill"
            )
        if params["mode"] not in _MOOD_BACKFILL_MODES:
            raise ValueError(
                f"Param 'mode' must be one of "
                f"{sorted(_MOOD_BACKFILL_MODES)}, got {params['mode']!r}"
            )
        job = self._jobs.create("mood_backfill", params)
        self._executor.submit(self._run_mood_backfill, job.id, params)
        return job

    def submit_image_ingestion(
        self,
        images: list[tuple[bytes, str, str]],  # (data, media_type, filename)
        entry_date: str,
    ) -> Job:
        """Queue an image-ingestion job.

        Images are held in memory until the worker starts. The params
        stored in the jobs table contain only the entry_date and page
        count (image bytes are too large for the JSON column).
        """
        if not images:
            raise ValueError("At least one image is required")
        params = {"entry_date": entry_date}
        _validate_params(params, _INGEST_IMAGES_KEYS, job_type="ingest_images")
        job = self._jobs.create("ingest_images", {**params, "page_count": len(images)})
        self._pending_images[job.id] = images
        self._executor.submit(self._run_image_ingestion, job.id, params)
        return job

    def submit_mood_score_entry(self, entry_id: int) -> Job:
        """Queue a mood-scoring job for a single entry.

        Lighter than a full backfill — scores one entry and returns.
        Used by the text/file ingest endpoints to defer mood scoring
        to a background thread.
        """
        params = {"entry_id": entry_id}
        _validate_params(params, _MOOD_SCORE_ENTRY_KEYS, job_type="mood_score_entry")
        job = self._jobs.create("mood_score_entry", params)
        self._executor.submit(self._run_mood_score_entry, job.id, params)
        return job

    def submit_reprocess_embeddings(self, entry_id: int) -> Job:
        """Queue a re-embedding job for an entry after text is saved.

        Re-chunks the entry's text, calls the embedding provider, and
        updates the vector store. This is the slow part of a text save
        that was previously done synchronously in the PATCH handler.
        """
        params = {"entry_id": entry_id}
        _validate_params(params, _REPROCESS_EMBEDDINGS_KEYS, job_type="reprocess_embeddings")
        job = self._jobs.create("reprocess_embeddings", params)
        self._executor.submit(self._run_reprocess_embeddings, job.id, params)
        return job

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
                )

            summary: dict[str, Any] = {
                "processed": len(results),
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
        except Exception as exc:  # noqa: BLE001 — terminal-state guard
            log.exception(
                "Entity extraction job %s failed", job_id
            )
            try:
                self._jobs.mark_failed(job_id, str(exc))
            except Exception:  # noqa: BLE001 — last-resort bookkeeping
                log.exception(
                    "Failed to record failure for job %s", job_id
                )

    def _run_image_ingestion(
        self, job_id: str, params: dict[str, Any]
    ) -> None:
        """Execute one image-ingestion job from start to terminal state."""
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
            total = len(images)

            def progress_callback(current: int, total_pages: int) -> None:
                self._jobs.update_progress(job_id, current, total)

            if len(images) == 1:
                self._jobs.update_progress(job_id, 0, total)
                entry = self._ingestion.ingest_image(images[0][0], images[0][1], entry_date)
                self._jobs.update_progress(job_id, 1, total)
            else:
                self._jobs.update_progress(job_id, 0, total)
                entry = self._ingestion.ingest_multi_page_entry(
                    images, entry_date, on_progress=progress_callback,
                )

            self._jobs.update_progress(job_id, total, total)
            self._jobs.mark_succeeded(job_id, {"entry_id": entry.id})

            # Queue entity extraction as a follow-up job. Mood scoring
            # already happens inline inside ingest_image / ingest_multi_page_entry.
            try:
                ej = self.submit_entity_extraction({"entry_id": entry.id})
                log.info(
                    "Image ingestion job %s — queued entity extraction %s for entry %d",
                    job_id, ej.id, entry.id,
                )
            except Exception:  # noqa: BLE001 — best-effort enrichment
                log.warning(
                    "Image ingestion job %s — failed to queue entity extraction",
                    job_id,
                    exc_info=True,
                )
        except Exception as exc:  # noqa: BLE001 — terminal-state guard
            log.exception("Image ingestion job %s failed", job_id)
            # Clean up any remaining image data
            self._pending_images.pop(job_id, None)
            try:
                self._jobs.mark_failed(job_id, str(exc))
            except Exception:  # noqa: BLE001 — last-resort bookkeeping
                log.exception("Failed to record failure for job %s", job_id)

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
            self._jobs.mark_succeeded(job_id, {"entry_id": entry_id, "scores_written": count})
        except Exception as exc:  # noqa: BLE001 — terminal-state guard
            log.exception("Mood score entry job %s failed", job_id)
            try:
                self._jobs.mark_failed(job_id, str(exc))
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
            self._jobs.mark_succeeded(
                job_id, {"entry_id": entry_id, "chunk_count": chunk_count}
            )
        except Exception as exc:  # noqa: BLE001 — terminal-state guard
            log.exception("Reprocess embeddings job %s failed", job_id)
            try:
                self._jobs.mark_failed(job_id, str(exc))
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
            )

            summary: dict[str, Any] = {
                "scored": backfill_result.scored,
                "skipped": backfill_result.skipped,
                "errors": list(backfill_result.errors),
            }
            self._jobs.mark_succeeded(job_id, summary)
        except Exception as exc:  # noqa: BLE001 — terminal-state guard
            log.exception("Mood backfill job %s failed", job_id)
            try:
                self._jobs.mark_failed(job_id, str(exc))
            except Exception:  # noqa: BLE001 — last-resort bookkeeping
                log.exception(
                    "Failed to record failure for job %s", job_id
                )
