"""Per-worker free-function bodies for ``JobRunner``.

Each ``run_<name>(ctx, job_id, params)`` is a standalone module that
takes a ``WorkerContext`` (the bag of dependencies the runner used to
hold as instance state) and the job's id/params. ``JobRunner.submit_*``
calls dispatch ``self._executor.submit(run_<name>, ctx, ...)``.

Splitting into free functions makes each worker independently
testable: tests construct a minimal ``WorkerContext`` from fakes and
call the function directly without standing up the executor.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from journal.db.jobs_repository import SQLiteJobRepository
    from journal.db.repository import EntryRepository
    from journal.services.backfill import MoodBackfillResult
    from journal.services.entity_extraction import EntityExtractionService
    from journal.services.fitness.backfill import BackfillResult
    from journal.services.fitness.fetch import FitnessSyncResult
    from journal.services.fitness.normalize import NormalizeResult
    from journal.services.ingestion import IngestionService
    from journal.services.jobs.notifier import JobNotifier
    from journal.services.jobs.runner import EntityReembedder
    from journal.services.mood_scoring import MoodScoringService


@dataclass
class WorkerContext:
    """Bag of dependencies a worker function needs.

    Built once by ``JobRunner.__init__`` and passed to every worker
    submission. Workers that don't need a particular field simply
    don't read it; future workers can opt into existing fields without
    plumbing new constructor parameters through ``JobRunner``.

    Two workers (``image_ingestion``, ``audio_ingestion``) need
    in-memory queues of large binary blobs that don't round-trip
    through SQLite — those live on the runner as ``_pending_images``
    and ``_pending_audio``. The workers reach those via callables
    stored on the context (``pop_pending_images`` / ``pop_pending_audio``)
    so the workers stay free of any direct ``runner._pending_*`` poke.
    """

    jobs: SQLiteJobRepository
    notifier: JobNotifier
    extraction: EntityExtractionService
    reembedder: EntityReembedder
    mood_backfill: Callable[..., MoodBackfillResult]
    mood_scoring: MoodScoringService
    entries: EntryRepository
    ingestion: IngestionService | None
    pop_pending_images: Callable[[str], list[tuple[bytes, str, str]]]
    pop_pending_audio: Callable[[str], list[tuple[bytes, str, str]]]
    queue_post_ingestion_jobs: Callable[
        [str, str, int, int | None], dict[str, str]
    ]
    # Fitness sync seams (W8). All four are optional because
    # fitness sync is opt-in at server boot — when the providers
    # aren't wired the workers raise; the JobRunner gates submission
    # on these being set so an unconfigured server never queues a
    # fitness job in the first place.
    fetch_strava: Callable[..., FitnessSyncResult] | None = None
    fetch_garmin: Callable[..., FitnessSyncResult] | None = None
    normalize_strava: Callable[..., NormalizeResult] | None = None
    normalize_garmin: Callable[..., NormalizeResult] | None = None
    # Fitness backfill seams (W5). Wrap
    # ``services/fitness/backfill.{backfill_strava,backfill_garmin}``
    # with the per-source fetch service + repo + notifier already
    # bound; the worker only supplies ``user_id``, ``start``, ``end``.
    # ``None`` when the source isn't configured on this server (same
    # opt-in gate as the fetch_*/normalize_* callables above).
    backfill_strava: Callable[..., BackfillResult] | None = None
    backfill_garmin: Callable[..., BackfillResult] | None = None
