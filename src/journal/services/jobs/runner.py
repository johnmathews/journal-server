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
- TWO `ThreadPoolExecutor` pools that between them run every
  submitted job:

    * **Pool A (ingestion/fast)** — ``max_workers = worker_count``
      (from ``config.job_worker_count``, default 4). Handles
      EVERYTHING except storyline jobs, so independent ingestion /
      mood / fitness / extraction jobs run in parallel.
    * **Pool B (storyline)** — ``max_workers = 1``. Handles ONLY
      ``storyline_generation`` and ``storyline_extension_check``.

  The split buys three things:
    1. Parallel ingestion throughput (Pool A is multi-worker).
    2. Ingestion is never starved by slow storyline work — storyline
       jobs can't occupy a Pool A slot, so there is no head-of-line
       blocking behind a long regeneration.
    3. The same-storyline regeneration race is structurally
       impossible: Pool B has a single worker, so two regenerations
       of the same storyline can never run concurrently — no locking
       required.

  SQLite is safe under this concurrency: since the per-thread
  `ConnectionFactory` migration (2026-05-11, see `db/factory.py` and
  `docs/archive/sqlite-per-thread-connections-plan.md`) every thread
  — each pool worker and each API thread — opens its own connection,
  and `db/connection.py` applies WAL mode + ``busy_timeout=5000`` so
  concurrent writers wait on the file-level writer lock instead of
  erroring. Parallel Pool A workers writing to the jobs table are
  therefore fine.

Worker bodies live in ``services/jobs/workers/<name>.py``. Each one
is a free function ``run_<name>(ctx, job_id, params)`` taking a
``WorkerContext`` so the worker is independently testable without
constructing the full runner. This module is the dispatcher: it
holds the two executors + the in-memory blob queues used by image
and audio ingestion (large bytes don't fit in the jobs.params_json
column), and it exposes the ``submit_*`` API the rest of the system
calls. Each ``submit_*`` routes onto Pool A except
``submit_storyline_generation`` / ``submit_storyline_extension_check``,
which go to the single-worker Pool B.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable

    from journal.db.jobs_repository import SQLiteJobRepository
    from journal.db.repository import EntryRepository
    from journal.models import Job
    from journal.services.backfill import MoodBackfillResult
    from journal.services.entity_extraction import EntityExtractionService
    from journal.services.fitness.backfill import BackfillResult
    from journal.services.fitness.fetch import FitnessSyncResult
    from journal.services.fitness.normalize import NormalizeResult
    from journal.services.ingestion import IngestionService
    from journal.services.mood_scoring import MoodScoringService
    from journal.services.notifications import PushoverNotificationService
    from journal.services.storylines.extension import (
        StorylineExtensionClassifierProtocol,
    )
    from journal.services.storylines.service import (
        StorylineGenerationServiceProtocol,
    )

from journal.services.jobs.notifier import JobNotifier
from journal.services.jobs.run_job import run_job
from journal.services.jobs.save_pipeline import submit_save_entry_pipeline
from journal.services.jobs.validation import (
    ENTITY_EXTRACTION_KEYS,
    ENTITY_REEMBED_KEYS,
    FITNESS_BACKFILL_KEYS,
    FITNESS_SYNC_KEYS,
    INGEST_AUDIO_KEYS,
    INGEST_IMAGES_KEYS,
    MOOD_BACKFILL_KEYS,
    MOOD_BACKFILL_MODES,
    MOOD_SCORE_ENTRY_KEYS,
    REPROCESS_EMBEDDINGS_KEYS,
    STORYLINE_EXTENSION_CHECK_KEYS,
    STORYLINE_GENERATION_KEYS,
    STORYLINE_GENERATION_MODES,
    validate_params,
)
from journal.services.jobs.workers import WorkerContext
from journal.services.jobs.workers.audio_ingestion import run_audio_ingestion
from journal.services.jobs.workers.entity_extraction import run_entity_extraction
from journal.services.jobs.workers.entity_reembed import run_entity_reembed
from journal.services.jobs.workers.fitness_backfill_garmin import (
    run_fitness_backfill_garmin,
)
from journal.services.jobs.workers.fitness_backfill_strava import (
    run_fitness_backfill_strava,
)
from journal.services.jobs.workers.fitness_sync_garmin import (
    run_fitness_sync_garmin,
)
from journal.services.jobs.workers.fitness_sync_strava import (
    run_fitness_sync_strava,
)
from journal.services.jobs.workers.image_ingestion import run_image_ingestion
from journal.services.jobs.workers.mood_backfill import run_mood_backfill
from journal.services.jobs.workers.mood_score_entry import run_mood_score_entry
from journal.services.jobs.workers.reprocess_embeddings import (
    run_reprocess_embeddings,
)
from journal.services.jobs.workers.storyline_extension_check import (
    run_storyline_extension_check,
)
from journal.services.jobs.workers.storyline_generation import (
    run_storyline_generation,
)

log = logging.getLogger(__name__)


class EntityReembedder(Protocol):
    """Reembed an entity given an edited description. The seam JobRunner
    uses for the ``entity_reembed`` worker.

    Production wires this to ``EntityExtractionService.reembed_entity_for_description``
    — the same instance JobRunner already uses for ``extract_from_entry``
    and ``extract_batch``. The Protocol exists so tests can drive
    ``run_entity_reembed`` against a fake without standing up the full
    extraction pipeline. ``extract_from_entry`` and ``extract_batch``
    intentionally stay on the concrete ``EntityExtractionService``
    constructor parameter — they are normal cross-service interactions,
    not reach-ins, and abstracting them would add boilerplate without
    meaningful benefit.
    """

    def reembed_entity_for_description(
        self, entity_id: int, *, user_id: int,
    ) -> dict[str, object]: ...


class JobRunner:
    """Run background batch jobs across two thread pools.

    * **Pool A (ingestion/fast)** — ``max_workers = worker_count``
      (``config.job_worker_count``, default 4). Runs everything
      except storyline jobs, so independent jobs execute in parallel.
    * **Pool B (storyline)** — ``max_workers = 1``. Runs only
      ``storyline_generation`` and ``storyline_extension_check``.

    Keeping storyline work on its own single-worker pool means (a)
    ingestion throughput is parallel, (b) storyline jobs can never
    occupy an ingestion slot (no head-of-line blocking), and (c) two
    regenerations of the same storyline can never run at once, so the
    same-storyline race is impossible without any locking.

    NOTE: SQLite access is per-thread. Every pool worker and each API
    thread get their own connection from the process-wide
    `ConnectionFactory` (`db/factory.py`); the historical
    shared-connection hazard (one `check_same_thread=False`
    connection written by multiple threads) was retired on
    2026-05-11 — see
    `docs/archive/sqlite-per-thread-connections-plan.md`. With WAL +
    ``busy_timeout=5000`` (`db/connection.py`), parallel Pool A
    writers wait on the file-level writer lock rather than erroring,
    so multi-worker concurrency is safe at the database layer.
    """

    def __init__(
        self,
        *,
        job_repository: SQLiteJobRepository,
        entity_extraction_service: EntityExtractionService,
        entity_reembedder: EntityReembedder | None = None,
        mood_backfill_callable: Callable[..., MoodBackfillResult],
        mood_scoring_service: MoodScoringService,
        entry_repository: EntryRepository,
        ingestion_service: IngestionService | None = None,
        notification_service: PushoverNotificationService | None = None,
        fetch_strava_callable: Callable[..., FitnessSyncResult] | None = None,
        fetch_garmin_callable: Callable[..., FitnessSyncResult] | None = None,
        normalize_strava_callable: Callable[..., NormalizeResult] | None = None,
        normalize_garmin_callable: Callable[..., NormalizeResult] | None = None,
        backfill_strava_callable: Callable[..., BackfillResult] | None = None,
        backfill_garmin_callable: Callable[..., BackfillResult] | None = None,
        storyline_generation_service: (
            StorylineGenerationServiceProtocol | None
        ) = None,
        storyline_extension_classifier: (
            StorylineExtensionClassifierProtocol | None
        ) = None,
        worker_count: int = 4,
    ) -> None:
        self._jobs = job_repository
        # Default the reembedder to the extraction service: it implements
        # the EntityReembedder Protocol via reembed_entity_for_description.
        # Tests pass a fake to drive run_entity_reembed in isolation.
        reembedder: EntityReembedder = (
            entity_reembedder
            if entity_reembedder is not None
            else entity_extraction_service
        )
        # Pool A: ingestion/fast jobs, parallel across ``worker_count``
        # workers. Pool B: storyline jobs, single-worker so ingestion is
        # never blocked and same-storyline regenerations can't race.
        self._executor = ThreadPoolExecutor(
            max_workers=worker_count, thread_name_prefix="journal-jobs"
        )
        self._storyline_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="journal-storyline"
        )
        # Temporary storage for image data keyed by job_id. The images
        # are large binary blobs that cannot be serialised into the
        # jobs table params_json column. They are popped from the dict
        # when the worker starts so memory is released promptly. Keys are
        # unique per job id, so the concurrent Pool A workers never
        # touch the same key — dict get/set on distinct keys is
        # GIL-atomic, so no lock is needed.
        self._pending_images: dict[str, list[tuple[bytes, str, str]]] = {}
        # Same pattern for audio recordings: (data, media_type, filename).
        self._pending_audio: dict[str, list[tuple[bytes, str, str]]] = {}

        self._notifier = JobNotifier(
            jobs=job_repository,
            notifications=notification_service,
        )
        # Single context every worker submission shares. Workers that
        # don't need every field simply don't read it.
        self._ctx = WorkerContext(
            jobs=job_repository,
            notifier=self._notifier,
            extraction=entity_extraction_service,
            reembedder=reembedder,
            mood_backfill=mood_backfill_callable,
            mood_scoring=mood_scoring_service,
            entries=entry_repository,
            ingestion=ingestion_service,
            pop_pending_images=lambda jid: self._pending_images.pop(jid, []),
            pop_pending_audio=lambda jid: self._pending_audio.pop(jid, []),
            queue_post_ingestion_jobs=self._queue_post_ingestion_jobs,
            queue_storyline_extension_check=(
                self._maybe_queue_storyline_extension_check
            ),
            fetch_strava=fetch_strava_callable,
            fetch_garmin=fetch_garmin_callable,
            normalize_strava=normalize_strava_callable,
            normalize_garmin=normalize_garmin_callable,
            backfill_strava=backfill_strava_callable,
            backfill_garmin=backfill_garmin_callable,
            storyline_generation=storyline_generation_service,
            storyline_extension_classifier=storyline_extension_classifier,
        )

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
        validate_params(
            run_params, ENTITY_EXTRACTION_KEYS, job_type="entity_extraction"
        )
        job = self._jobs.create("entity_extraction", run_params, user_id=user_id)
        self._executor.submit(run_job, run_entity_extraction, self._ctx, job.id, run_params)
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
        validate_params(
            run_params, MOOD_BACKFILL_KEYS, job_type="mood_backfill"
        )
        if "mode" not in run_params:
            raise ValueError(
                "Param 'mode' is required for mood_backfill"
            )
        if run_params["mode"] not in MOOD_BACKFILL_MODES:
            raise ValueError(
                f"Param 'mode' must be one of "
                f"{sorted(MOOD_BACKFILL_MODES)}, got {run_params['mode']!r}"
            )
        job = self._jobs.create("mood_backfill", run_params, user_id=user_id)
        self._executor.submit(run_job, run_mood_backfill, self._ctx, job.id, run_params)
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
        validate_params(params, INGEST_IMAGES_KEYS, job_type="ingest_images")
        job = self._jobs.create(
            "ingest_images", {**params, "page_count": len(images)},
            user_id=user_id,
        )
        self._pending_images[job.id] = images
        self._executor.submit(run_job, run_image_ingestion, self._ctx, job.id, params)
        return job

    def submit_mood_score_entry(
        self, entry_id: int, *, user_id: int | None = None,
        parent_job_id: str | None = None,
    ) -> Job:
        """Queue a mood-scoring job for a single entry.

        Lighter than a full backfill — scores one entry and returns.
        Used by the text/file ingest endpoints to defer mood scoring
        to a background thread.
        """
        params: dict[str, Any] = {"entry_id": entry_id}
        if user_id is not None:
            params["user_id"] = user_id
        if parent_job_id is not None:
            params["parent_job_id"] = parent_job_id
        validate_params(params, MOOD_SCORE_ENTRY_KEYS, job_type="mood_score_entry")
        job = self._jobs.create("mood_score_entry", params, user_id=user_id)
        self._executor.submit(run_job, run_mood_score_entry, self._ctx, job.id, params)
        return job

    def submit_reprocess_embeddings(
        self, entry_id: int, *, user_id: int | None = None,
        parent_job_id: str | None = None,
    ) -> Job:
        """Queue a re-embedding job for an entry after text is saved.

        Re-chunks the entry's text, calls the embedding provider, and
        updates the vector store. This is the slow part of a text save
        that was previously done synchronously in the PATCH handler.

        When ``parent_job_id`` is set, the job participates in a
        consolidated pipeline notification (see
        ``submit_save_entry_pipeline``) and skips its own per-job
        Pushover.
        """
        params: dict[str, Any] = {"entry_id": entry_id}
        if user_id is not None:
            params["user_id"] = user_id
        if parent_job_id is not None:
            params["parent_job_id"] = parent_job_id
        validate_params(params, REPROCESS_EMBEDDINGS_KEYS, job_type="reprocess_embeddings")
        job = self._jobs.create("reprocess_embeddings", params, user_id=user_id)
        self._executor.submit(run_job, run_reprocess_embeddings, self._ctx, job.id, params)
        return job

    def submit_save_entry_pipeline(
        self,
        *,
        entry_id: int,
        user_id: int | None = None,
        enable_mood_scoring: bool = True,
    ) -> tuple[Job, dict[str, str]]:
        """Queue the three background jobs that run after an entry edit
        and orchestrate ONE consolidated Pushover for them.

        See ``services/jobs/save_pipeline.py`` for the full design;
        this method is a thin delegating shim so api/ callers keep
        the existing ``job_runner.submit_save_entry_pipeline(...)``
        call site.
        """
        return submit_save_entry_pipeline(
            jobs=self._jobs,
            executor=self._executor,
            ctx=self._ctx,
            notifier=self._notifier,
            entry_id=entry_id,
            user_id=user_id,
            enable_mood_scoring=enable_mood_scoring,
        )

    def submit_storyline_generation(
        self,
        storyline_id: int,
        *,
        user_id: int | None = None,
        parent_job_id: str | None = None,
        chapter_id: int | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        mode: str | None = None,
        resegment: bool = False,
        override_locked: bool = False,
        auto_split: bool = False,
    ) -> Job:
        """Queue a storyline regeneration job.

        Refuses to queue if the StorylineGenerationService isn't
        wired on this server (opt-in at boot, like fitness sync).
        ``user_id`` routes notifications; ``parent_job_id`` opts into
        the consolidated pipeline-notification pattern used by the
        extension-check hook.

        ``start_date`` and ``end_date`` (ISO YYYY-MM-DD) override the
        storyline row's stored date window for this run only. ``mode``
        is ``"replace"`` (default) or ``"append"``; append requires
        ``start_date`` to be on or after the storyline's last
        generation date.

        ``chapter_id`` scopes the job to a single chapter: the worker
        calls ``regenerate_chapter`` (replace-only) and the chapter's
        own window is authoritative, so ``start_date``/``end_date`` are
        ignored for chapter-scoped runs.

        ``resegment`` (storyline-level only) re-carves the storyline
        into titled, word-sized chapters via ``resegment_storyline``
        instead of refreshing the open chapter. ``override_locked`` is
        only meaningful with ``resegment=True``; it lets the re-carve
        cross hand-painted (boundary-locked) chapter boundaries. Both
        default False, preserving the legacy refresh behavior.
        ``resegment`` is incompatible with ``chapter_id`` (a
        chapter-scoped run can't re-segment) and with ``mode="append"``.

        ``auto_split`` (storyline-level default path only) is the
        ingest-time auto-split flag (W5): when True the worker forwards it
        into ``regenerate`` so an over-budget open chapter is
        automatically re-segmented after the refresh. The ingest
        extension-check hook sets this; manual refreshes leave it False so
        re-segmentation stays opt-in. It is ignored when combined with
        ``chapter_id`` or ``resegment`` (those paths don't read it).
        """
        if self._ctx.storyline_generation is None:
            raise RuntimeError(
                "StorylineGenerationService not configured; "
                "cannot queue storyline_generation job."
            )
        if mode is not None and mode not in STORYLINE_GENERATION_MODES:
            raise ValueError(
                f"Invalid mode {mode!r}; expected one of "
                f"{sorted(STORYLINE_GENERATION_MODES)}"
            )
        if resegment and chapter_id is not None:
            raise ValueError(
                "resegment is incompatible with chapter_id: a "
                "chapter-scoped run cannot re-segment the storyline."
            )
        if resegment and mode == "append":
            raise ValueError(
                "resegment is incompatible with mode='append': "
                "re-segmentation always rebuilds the chapter set."
            )
        params: dict[str, Any] = {"storyline_id": storyline_id}
        if user_id is not None:
            params["user_id"] = user_id
        if parent_job_id is not None:
            params["parent_job_id"] = parent_job_id
        if chapter_id is not None:
            params["chapter_id"] = chapter_id
        if start_date is not None:
            params["start_date"] = start_date
        if end_date is not None:
            params["end_date"] = end_date
        if mode is not None:
            params["mode"] = mode
        if resegment:
            params["resegment"] = True
        if override_locked:
            params["override_locked"] = True
        if auto_split:
            params["auto_split"] = True
        validate_params(
            params, STORYLINE_GENERATION_KEYS, job_type="storyline_generation",
        )
        job = self._jobs.create(
            "storyline_generation", params, user_id=user_id,
        )
        # Pool B (single-worker): storyline jobs never contend for a
        # Pool A ingestion slot, and same-storyline runs can't race.
        self._storyline_executor.submit(
            run_job, run_storyline_generation, self._ctx, job.id, params,
        )
        return job

    def submit_storyline_extension_check(
        self,
        entry_id: int,
        *,
        user_id: int,
        parent_job_id: str | None = None,
    ) -> Job:
        """Queue an extension-check job for one entry.

        Refuses to queue if the StorylineExtensionClassifier isn't
        wired on this server. The worker iterates the user's active
        storylines and queues a regeneration job for each ``yes``
        classification via ``submit_storyline_generation``.
        """
        if self._ctx.storyline_extension_classifier is None:
            raise RuntimeError(
                "StorylineExtensionClassifier not configured; "
                "cannot queue storyline_extension_check job."
            )
        params: dict[str, Any] = {
            "entry_id": entry_id,
            "user_id": user_id,
        }
        if parent_job_id is not None:
            params["parent_job_id"] = parent_job_id
        validate_params(
            params, STORYLINE_EXTENSION_CHECK_KEYS,
            job_type="storyline_extension_check",
        )
        job = self._jobs.create(
            "storyline_extension_check", params, user_id=user_id,
        )
        # The worker calls back into submit_storyline_generation to
        # queue regenerations. We pass the bound method explicitly so
        # the worker is free of any runner-side reach-in. Runs on Pool B
        # (single-worker) alongside storyline_generation.
        self._storyline_executor.submit(
            run_job,
            run_storyline_extension_check,
            self._ctx, job.id, params,
            self.submit_storyline_generation,
        )
        return job

    def _maybe_queue_storyline_extension_check(
        self, entry_id: int, user_id: int | None,
    ) -> None:
        """Best-effort storyline extension check for a freshly-extracted
        entry, called by the entity-extraction worker.

        Safe to call unconditionally: no-ops when storylines aren't wired
        on this server (opt-in feature), and logs — rather than silently
        drops — when the entry has no known user, since the classifier
        scopes to a user's active storylines. Queue failures are logged
        and swallowed so they never fail the parent extraction job.
        """
        if self._ctx.storyline_extension_classifier is None:
            return
        if user_id is None:
            log.warning(
                "Not queuing storyline extension check for entry %d: "
                "no user_id on the extraction job.",
                entry_id,
            )
            return
        try:
            self.submit_storyline_extension_check(entry_id, user_id=user_id)
        except Exception:  # noqa: BLE001 — never fail the parent job
            log.warning(
                "Failed to queue storyline extension check for entry %d",
                entry_id, exc_info=True,
            )

    def submit_entity_reembed(
        self, entity_id: int, *, user_id: int,
    ) -> Job:
        """Queue a job that recomputes an entity's stored embedding from
        its current canonical name + description.

        Triggered when the user edits an entity's description so that
        future entity-recognition uses the refreshed text.
        """
        params: dict[str, Any] = {
            "entity_id": entity_id,
            "user_id": user_id,
        }
        validate_params(params, ENTITY_REEMBED_KEYS, job_type="entity_reembed")
        job = self._jobs.create("entity_reembed", params, user_id=user_id)
        self._executor.submit(run_job, run_entity_reembed, self._ctx, job.id, params)
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
        validate_params(params, INGEST_AUDIO_KEYS, job_type="ingest_audio")
        job = self._jobs.create(
            "ingest_audio", {**params, "recording_count": len(recordings)},
            user_id=user_id,
        )
        self._pending_audio[job.id] = recordings
        self._executor.submit(run_job, run_audio_ingestion, self._ctx, job.id, params)
        return job

    def submit_fitness_sync_strava(
        self, *, user_id: int, quiet_success: bool = False,
    ) -> Job:
        """Queue a Strava fitness sync (fetch + normalize end-to-end).

        Raises ``RuntimeError`` if the runner was constructed without a
        Strava fetch + normalize pair — the worker has nothing to call
        in that case, so we fail at submit time rather than queueing a
        row that's guaranteed to fail.

        ``quiet_success`` (set by the daily scheduler) makes the worker
        suppress the success notification when the run fetched no new rows.
        """
        if self._ctx.fetch_strava is None or self._ctx.normalize_strava is None:
            raise RuntimeError(
                "Strava fitness sync is not configured on this server "
                "(no fetch_strava_callable / normalize_strava_callable "
                "passed to JobRunner)",
            )
        params: dict[str, Any] = {"user_id": user_id}
        if quiet_success:
            params["quiet_success"] = True
        validate_params(params, FITNESS_SYNC_KEYS, job_type="fitness_sync_strava")
        job = self._jobs.create("fitness_sync_strava", params, user_id=user_id)
        self._executor.submit(run_job, run_fitness_sync_strava, self._ctx, job.id, params)
        return job

    def submit_fitness_sync_garmin(
        self, *, user_id: int, quiet_success: bool = False,
    ) -> Job:
        """Queue a Garmin fitness sync (fetch + normalize end-to-end).

        Same configuration gate as ``submit_fitness_sync_strava``.

        ``quiet_success`` (set by the daily scheduler) makes the worker
        suppress the success notification when the run fetched no new rows.
        """
        if self._ctx.fetch_garmin is None or self._ctx.normalize_garmin is None:
            raise RuntimeError(
                "Garmin fitness sync is not configured on this server "
                "(no fetch_garmin_callable / normalize_garmin_callable "
                "passed to JobRunner)",
            )
        params: dict[str, Any] = {"user_id": user_id}
        if quiet_success:
            params["quiet_success"] = True
        validate_params(params, FITNESS_SYNC_KEYS, job_type="fitness_sync_garmin")
        job = self._jobs.create("fitness_sync_garmin", params, user_id=user_id)
        self._executor.submit(run_job, run_fitness_sync_garmin, self._ctx, job.id, params)
        return job

    def submit_fitness_backfill_strava(
        self, *, user_id: int, start: str, end: str | None = None,
    ) -> Job:
        """Queue a Strava historical backfill job (W5).

        The submit-time idempotency policy (one fetch job per
        ``(user_id, source)`` across both sync and backfill worker
        classes) is enforced at the *endpoint / MCP-tool* layer via
        :meth:`SQLiteJobRepository.find_active_fitness_fetch_job`. The
        runner itself stays the dumb dispatcher — its only contract is
        "create + submit," matching ``submit_fitness_sync_*``.
        """
        if self._ctx.backfill_strava is None:
            raise RuntimeError(
                "Strava fitness backfill is not configured on this server "
                "(no backfill_strava_callable passed to JobRunner)",
            )
        params: dict[str, Any] = {"user_id": user_id, "start": start}
        if end is not None:
            params["end"] = end
        validate_params(
            params, FITNESS_BACKFILL_KEYS, job_type="fitness_backfill_strava",
        )
        job = self._jobs.create(
            "fitness_backfill_strava", params, user_id=user_id,
        )
        self._executor.submit(
            run_job, run_fitness_backfill_strava, self._ctx, job.id, params,
        )
        return job

    def submit_fitness_backfill_garmin(
        self, *, user_id: int, start: str, end: str | None = None,
    ) -> Job:
        """Queue a Garmin historical backfill job (W5).

        Same dispatcher posture as :meth:`submit_fitness_backfill_strava`.
        """
        if self._ctx.backfill_garmin is None:
            raise RuntimeError(
                "Garmin fitness backfill is not configured on this server "
                "(no backfill_garmin_callable passed to JobRunner)",
            )
        params: dict[str, Any] = {"user_id": user_id, "start": start}
        if end is not None:
            params["end"] = end
        validate_params(
            params, FITNESS_BACKFILL_KEYS, job_type="fitness_backfill_garmin",
        )
        job = self._jobs.create(
            "fitness_backfill_garmin", params, user_id=user_id,
        )
        self._executor.submit(
            run_job, run_fitness_backfill_garmin, self._ctx, job.id, params,
        )
        return job

    def shutdown(self, wait: bool = False, *, cancel_futures: bool = True) -> None:
        """Stop both executors (Pool A + Pool B).

        Call once at server shutdown. Running tasks are allowed to
        finish their current iteration; with the default
        ``cancel_futures=True``, queued-but-not-started tasks are
        cancelled — their job rows stay ``queued`` and are reconciled
        as stuck on next boot. Tests that submit work and then want to
        assert on its outcome must pass ``cancel_futures=False`` so
        shutdown *drains* the queue instead of racing it: with the
        default, a submit immediately followed by ``shutdown(wait=True)``
        can cancel the future before the worker dequeues it on a loaded
        machine (observed as a CI-only flake, 2026-06-10).
        After `shutdown`, further `submit_*` calls raise
        `RuntimeError` from whichever executor they target (storyline
        submits from Pool B, everything else from Pool A).
        """
        self._executor.shutdown(wait=wait, cancel_futures=cancel_futures)
        self._storyline_executor.shutdown(
            wait=wait, cancel_futures=cancel_futures,
        )

    @property
    def mood_scoring(self) -> MoodScoringService | None:
        """Read-only accessor for the mood-scoring service workers see.

        Lives on ``self._ctx`` post-item-2 (the worker-extraction
        refactor). Exposed so callers — primarily
        ``services/reload.py`` — read the live handle instead of
        reaching into ``self._ctx.mood_scoring``. May return
        ``None`` if mood scoring was disabled at runtime via
        ``replace_mood_scoring(None)``.
        """
        return self._ctx.mood_scoring

    def replace_mood_scoring(
        self, scoring: MoodScoringService | None,
    ) -> None:
        """Atomically swap the mood-scoring service every worker sees.

        ``services/reload.py`` calls this when the mood-dimension
        TOML changes — workers picking up the next job read the
        fresh service from the WorkerContext, while any worker
        currently mid-call keeps its already-resolved reference.

        Pass ``None`` to disable mood scoring at runtime; workers
        for mood-related job types should not be submitted while
        disabled (the api/ submission paths gate on the
        ``enable_mood_scoring`` runtime flag), so ``None`` is safe
        for the disabled case even though the WorkerContext field
        type doesn't advertise it.

        Note: this writes through ``self._ctx.mood_scoring``, the
        live handle workers actually consume. Earlier reload code
        wrote ``self._mood_scoring`` directly, which silently
        became a phantom attribute after item 2 moved the field
        onto the WorkerContext — fixed by this method.
        """
        self._ctx.mood_scoring = scoring  # type: ignore[assignment]

    def _queue_post_ingestion_jobs(
        self,
        parent_job_id: str,
        kind: str,
        entry_id: int,
        user_id: int | None,
    ) -> dict[str, str]:
        """Queue mood scoring + entity extraction after ingestion.

        Returns a mapping of follow-up label → job ID for each
        successfully queued job (e.g. ``{"mood_scoring": "abc-123",
        "entity_extraction": "def-456"}``).

        Lives on ``JobRunner`` (rather than as a free function) so it
        has access to the runner's submit_* methods, which do the
        param-validation + executor.submit dance the workers' follow-
        up flow needs.
        """
        follow_up_jobs: list[
            tuple[str, str, Callable[[], Job]]
        ] = [
            ("mood scoring", "mood_scoring",
             lambda: self.submit_mood_score_entry(
                 entry_id, user_id=user_id, parent_job_id=parent_job_id,
             )),
            ("entity extraction", "entity_extraction",
             lambda: self.submit_entity_extraction(
                 {"entry_id": entry_id, "parent_job_id": parent_job_id},
                 user_id=user_id,
             )),
        ]
        # NB: the storyline extension check is intentionally NOT queued
        # here. It is fired by the entity-extraction worker once mentions
        # are committed (see _maybe_queue_storyline_extension_check), so
        # the classifier's entity-overlap signal is reliable. Queuing it
        # here — concurrently with entity extraction on a separate pool —
        # was the root cause of ingests updating zero storylines.

        follow_up_ids: dict[str, str] = {}
        for label, key, submit in follow_up_jobs:
            try:
                fj = submit()
                follow_up_ids[key] = fj.id
                log.info(
                    "%s ingestion job %s — queued %s %s for entry %d",
                    kind, parent_job_id, label, fj.id, entry_id,
                )
            except Exception:  # noqa: BLE001
                log.warning(
                    "%s ingestion job %s — failed to queue %s",
                    kind, parent_job_id, label,
                    exc_info=True,
                )
        return follow_up_ids
