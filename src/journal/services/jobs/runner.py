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
  submitted job. One worker gives us two things:
    1. Predictable LLM rate usage (no contention for tokens).
    2. Simpler failure reasoning: jobs cannot be racing each other.

  SQLite safety is *not* part of that list any more: since the
  per-thread `ConnectionFactory` migration (2026-05-11, see
  `db/factory.py` and
  `docs/archive/sqlite-per-thread-connections-plan.md`), each
  thread — worker or API — opens its own connection, so the old
  shared-connection commit race is structurally impossible.

IMPORTANT: if `max_workers` is ever bumped above 1, the two
rationales above are what you are giving up — LLM rate usage
becomes contended and job-vs-job interactions need real analysis.
SQLite is no longer the blocker (WAL + per-thread connections +
the file-level writer lock handle concurrent writers), but the
LLM-rate and reasoning constraints still make 1 the right number.

Worker bodies live in ``services/jobs/workers/<name>.py``. Each one
is a free function ``run_<name>(ctx, job_id, params)`` taking a
``WorkerContext`` so the worker is independently testable without
constructing the full runner. This module is the dispatcher: it
holds the executor + the in-memory blob queues used by image and
audio ingestion (large bytes don't fit in the jobs.params_json
column), and it exposes the ``submit_*`` API the rest of the system
calls.
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
    """Run background batch jobs serialised on a single worker.

    Uses a single-worker `ThreadPoolExecutor` so jobs are
    serialised — this keeps LLM rate-limiting simple and reasoning
    about job-vs-job interactions tractable.

    NOTE: SQLite access is per-thread. The worker thread and each
    API thread get their own connection from the process-wide
    `ConnectionFactory` (`db/factory.py`); the historical
    shared-connection hazard (one `check_same_thread=False`
    connection written by multiple threads) was retired on
    2026-05-11 — see
    `docs/archive/sqlite-per-thread-connections-plan.md`.

    IMPORTANT: `max_workers=1` is a deliberate choice for the two
    reasons above (predictable LLM rate usage, tractable job-vs-job
    reasoning), not an SQLite constraint. Bumping it requires
    re-thinking LLM rate limits and inter-job interactions, not
    the database layer.
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
        self._executor.submit(run_entity_extraction, self._ctx, job.id, run_params)
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
        self._executor.submit(run_mood_backfill, self._ctx, job.id, run_params)
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
        self._executor.submit(run_image_ingestion, self._ctx, job.id, params)
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
        self._executor.submit(run_mood_score_entry, self._ctx, job.id, params)
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
        self._executor.submit(run_reprocess_embeddings, self._ctx, job.id, params)
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
        start_date: str | None = None,
        end_date: str | None = None,
        mode: str | None = None,
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
        params: dict[str, Any] = {"storyline_id": storyline_id}
        if user_id is not None:
            params["user_id"] = user_id
        if parent_job_id is not None:
            params["parent_job_id"] = parent_job_id
        if start_date is not None:
            params["start_date"] = start_date
        if end_date is not None:
            params["end_date"] = end_date
        if mode is not None:
            params["mode"] = mode
        validate_params(
            params, STORYLINE_GENERATION_KEYS, job_type="storyline_generation",
        )
        job = self._jobs.create(
            "storyline_generation", params, user_id=user_id,
        )
        self._executor.submit(
            run_storyline_generation, self._ctx, job.id, params,
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
        # the worker is free of any runner-side reach-in.
        self._executor.submit(
            run_storyline_extension_check,
            self._ctx, job.id, params,
            self.submit_storyline_generation,
        )
        return job

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
        self._executor.submit(run_entity_reembed, self._ctx, job.id, params)
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
        self._executor.submit(run_audio_ingestion, self._ctx, job.id, params)
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
        self._executor.submit(run_fitness_sync_strava, self._ctx, job.id, params)
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
        self._executor.submit(run_fitness_sync_garmin, self._ctx, job.id, params)
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
            run_fitness_backfill_strava, self._ctx, job.id, params,
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
            run_fitness_backfill_garmin, self._ctx, job.id, params,
        )
        return job

    def shutdown(self, wait: bool = False, *, cancel_futures: bool = True) -> None:
        """Stop the executor.

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
        `RuntimeError` from the underlying executor.
        """
        self._executor.shutdown(wait=wait, cancel_futures=cancel_futures)

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
        # Storylines is opt-in at boot — only queue an extension check
        # when both the classifier service is wired and a user_id is
        # known (the classifier loops the user's active storylines).
        if (
            self._ctx.storyline_extension_classifier is not None
            and user_id is not None
        ):
            follow_up_jobs.append(
                ("storyline extension check", "storyline_extension_check",
                 lambda: self.submit_storyline_extension_check(
                     entry_id,
                     user_id=user_id,
                     parent_job_id=parent_job_id,
                 )),
            )

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
