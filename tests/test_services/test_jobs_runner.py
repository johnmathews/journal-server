"""Tests for the JobRunner service."""

from __future__ import annotations

import contextlib
import threading
import time
from typing import TYPE_CHECKING, Any

import pytest

from journal.db.factory import ConnectionFactory
from journal.db.jobs_repository import SQLiteJobRepository
from journal.db.migrations import run_migrations
from journal.models import ExtractionResult
from journal.services.backfill import MoodBackfillResult
from journal.services.jobs import JobRunner
from journal.services.jobs.errors import friendly_error, is_transient

if TYPE_CHECKING:
    from collections.abc import Callable


# --------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------


class FakeEntityExtractionService:
    """Matches the slice of EntityExtractionService the runner touches.

    - `extract_batch(..., on_progress=...)` respects the progress
      callback contract: calls ``(0, total)`` before the loop and
      ``(i, total)`` after each fake entry.
    - `extract_from_entry(entry_id)` returns a single fake
      ExtractionResult (used by the single-entry path).
    - Optional knobs: `raise_in_batch` to simulate a total-batch
      failure, and `hold_event` to prove serialisation by pausing
      mid-run until the test releases it.
    """

    def __init__(
        self,
        *,
        batch_results: list[ExtractionResult] | None = None,
        single_result: ExtractionResult | None = None,
        raise_in_batch: BaseException | None = None,
        raise_in_single: BaseException | None = None,
        hold_event: threading.Event | None = None,
        entered_event: threading.Event | None = None,
        reembed_result: dict[str, Any] | None = None,
        raise_in_reembed: BaseException | None = None,
    ) -> None:
        self._batch_results = batch_results or []
        self._single_result = single_result
        self._raise = raise_in_batch
        self._raise_single = raise_in_single
        self._hold = hold_event
        self._entered = entered_event
        self._reembed_result = reembed_result
        self._raise_reembed = raise_in_reembed
        self.batch_calls: list[dict[str, Any]] = []
        self.single_calls: list[int] = []
        self.reembed_calls: list[dict[str, Any]] = []

    def reembed_entity_for_description(
        self, entity_id: int, *, user_id: int,
    ) -> dict[str, Any]:
        self.reembed_calls.append(
            {"entity_id": entity_id, "user_id": user_id}
        )
        if self._raise_reembed is not None:
            raise self._raise_reembed
        return self._reembed_result or {
            "entity_id": entity_id,
            "embedded": True,
            "dimensions": 3,
        }

    def extract_batch(
        self,
        entry_ids: list[int] | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        stale_only: bool = False,
        *,
        on_progress: Callable[[int, int], None] | None = None,
        user_id: int | None = None,
    ) -> list[ExtractionResult]:
        self.batch_calls.append(
            {
                "entry_ids": entry_ids,
                "start_date": start_date,
                "end_date": end_date,
                "stale_only": stale_only,
            }
        )
        if self._entered is not None:
            self._entered.set()
        if self._hold is not None:
            # Block so the test can prove serialisation.
            self._hold.wait(timeout=5)

        if self._raise is not None:
            raise self._raise

        total = len(self._batch_results)
        if on_progress is not None:
            on_progress(0, total)
        for i, _ in enumerate(self._batch_results, start=1):
            if on_progress is not None:
                on_progress(i, total)
        return list(self._batch_results)

    def extract_from_entry(self, entry_id: int) -> ExtractionResult:
        self.single_calls.append(entry_id)
        if self._raise_single is not None:
            raise self._raise_single
        if self._single_result is None:
            raise AssertionError(
                "single_result not configured on fake"
            )
        return self._single_result


class FakeMoodBackfill:
    """Callable stand-in for `backfill_mood_scores`."""

    def __init__(
        self,
        *,
        result: MoodBackfillResult | None = None,
        raise_exc: BaseException | None = None,
        entries_to_count: int = 0,
    ) -> None:
        self._result = result or MoodBackfillResult()
        self._raise = raise_exc
        self._entries_to_count = entries_to_count
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        *,
        repository: Any,
        mood_scoring: Any,
        mode: str,
        start_date: str | None = None,
        end_date: str | None = None,
        on_progress: Callable[[int, int], None] | None = None,
        user_id: int | None = None,
    ) -> MoodBackfillResult:
        self.calls.append(
            {
                "repository": repository,
                "mood_scoring": mood_scoring,
                "mode": mode,
                "start_date": start_date,
                "end_date": end_date,
            }
        )
        if self._raise is not None:
            raise self._raise
        total = self._entries_to_count
        if on_progress is not None:
            on_progress(0, total)
            for i in range(1, total + 1):
                on_progress(i, total)
        return self._result


# --------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------


def _make_extraction_result(
    entry_id: int,
    *,
    entities_created: int = 0,
    entities_matched: int = 0,
    mentions_created: int = 0,
    relationships_created: int = 0,
    warnings: list[str] | None = None,
) -> ExtractionResult:
    return ExtractionResult(
        entry_id=entry_id,
        extraction_run_id=f"run-{entry_id}",
        entities_created=entities_created,
        entities_matched=entities_matched,
        mentions_created=mentions_created,
        relationships_created=relationships_created,
        warnings=warnings or [],
    )


def _wait_terminal(
    jobs_repo: SQLiteJobRepository, job_id: str, timeout: float = 5.0
) -> None:
    """Busy-wait until the job row is in a terminal state.

    Used by tests that don't want to shut down the executor to
    flush. Polling is fine — the fakes complete in microseconds.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = jobs_repo.get(job_id)
        if row is not None and row.status in ("succeeded", "failed"):
            return
        time.sleep(0.01)
    raise AssertionError(
        f"Job {job_id} did not reach terminal state within {timeout}s"
    )


def _wait_status(
    jobs_repo: SQLiteJobRepository,
    job_id: str,
    status: str,
    timeout: float = 5.0,
) -> None:
    """Busy-wait until the job row reaches ``status`` (e.g. ``running``).

    Event-driven waits are preferred where possible, but job-status
    transitions happen inside the worker and are only observable via
    the jobs table, so a short poll is the right tool here.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = jobs_repo.get(job_id)
        if row is not None and row.status == status:
            return
        time.sleep(0.01)
    row = jobs_repo.get(job_id)
    actual = row.status if row is not None else "<missing>"
    raise AssertionError(
        f"Job {job_id} did not reach status {status!r} within "
        f"{timeout}s (last seen: {actual!r})"
    )


# --------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------


@pytest.fixture
def threadsafe_factory(tmp_path):
    """``ConnectionFactory`` for the JobRunner's worker thread.

    The worker thread opens its own connection lazily via the
    factory, which is how production runs after W4.
    """
    db_path = tmp_path / "jobs-runner.db"
    factory = ConnectionFactory(db_path)
    run_migrations(factory.get())
    return factory


@pytest.fixture
def jobs_repo(threadsafe_factory) -> SQLiteJobRepository:
    return SQLiteJobRepository(threadsafe_factory)


@pytest.fixture
def runner_factory(jobs_repo, threadsafe_factory):
    """Build a JobRunner with swappable fakes.

    Returns a factory so tests that need bespoke fake behaviour
    can construct the runner with their own fakes and still get
    automatic shutdown cleanup.
    """
    created: list[JobRunner] = []

    def _factory(
        *,
        extraction: FakeEntityExtractionService | None = None,
        mood_backfill: FakeMoodBackfill | None = None,
        entity_reembedder: Any = None,
        ingestion: Any = None,
        fetch_strava_callable: Any = None,
        fetch_garmin_callable: Any = None,
        normalize_strava_callable: Any = None,
        normalize_garmin_callable: Any = None,
        backfill_strava_callable: Any = None,
        backfill_garmin_callable: Any = None,
        storyline_generation: Any = None,
        storyline_extension_classifier: Any = None,
        worker_count: int = 1,
    ) -> JobRunner:
        # ``worker_count`` defaults to 1 so the serialisation/timing
        # tests below stay deterministic on Pool A; the production
        # default (config.job_worker_count) is 4. Tests that want to
        # observe parallelism pass ``worker_count=2`` explicitly.
        runner = JobRunner(
            job_repository=jobs_repo,
            entity_extraction_service=(
                extraction or FakeEntityExtractionService()
            ),
            entity_reembedder=entity_reembedder,
            mood_backfill_callable=mood_backfill or FakeMoodBackfill(),
            mood_scoring_service=object(),  # type: ignore[arg-type]
            entry_repository=object(),  # type: ignore[arg-type]
            ingestion_service=ingestion,
            fetch_strava_callable=fetch_strava_callable,
            fetch_garmin_callable=fetch_garmin_callable,
            normalize_strava_callable=normalize_strava_callable,
            normalize_garmin_callable=normalize_garmin_callable,
            backfill_strava_callable=backfill_strava_callable,
            backfill_garmin_callable=backfill_garmin_callable,
            storyline_generation_service=storyline_generation,
            storyline_extension_classifier=storyline_extension_classifier,
            worker_count=worker_count,
        )
        created.append(runner)
        return runner

    yield _factory

    for runner in created:
        with contextlib.suppress(Exception):
            runner.shutdown(wait=True, cancel_futures=False)


# --------------------------------------------------------------------
# Friendly error mapping
# --------------------------------------------------------------------


class TestFriendlyError:
    """Tests for _friendly_error — maps raw exceptions to UI messages."""

    def test_google_503_overloaded(self) -> None:
        exc = Exception(
            "503 UNAVAILABLE. {'error': {'message': 'This model is currently "
            "experiencing high demand.'}}"
        )
        msg = friendly_error(exc)
        assert msg == "OCR service overloaded"

    def test_google_429_rate_limit(self) -> None:
        exc = Exception(
            "429 RESOURCE_EXHAUSTED. {'error': {'message': 'You exceeded your "
            "current quota'}}"
        )
        msg = friendly_error(exc)
        assert msg == "Google API rate limit exceeded"

    def test_google_404_model_not_found(self) -> None:
        exc = Exception(
            "404 NOT_FOUND. {'error': {'message': 'models/gemini-99 "
            "is not found for API version v1beta'}}"
        )
        msg = friendly_error(exc)
        assert "OCR_MODEL" in msg

    def test_unknown_error_passes_through(self) -> None:
        exc = Exception("something completely unexpected")
        assert friendly_error(exc) == "something completely unexpected"


class TestIsTransient:
    """Tests for _is_transient — identifies retryable API errors."""

    def test_google_503(self) -> None:
        exc = Exception("503 UNAVAILABLE. high demand")
        assert is_transient(exc) is True

    def test_google_429(self) -> None:
        exc = Exception("429 RESOURCE_EXHAUSTED. quota exceeded")
        assert is_transient(exc) is True

    def test_not_transient(self) -> None:
        exc = Exception("404 NOT_FOUND. model not found")
        assert is_transient(exc) is False

    def test_unknown_not_transient(self) -> None:
        exc = Exception("something unexpected")
        assert is_transient(exc) is False


# Happy path — entity extraction
# --------------------------------------------------------------------


class TestEntityExtractionHappyPath:
    def test_batch_job_runs_to_success(self, runner_factory, jobs_repo):
        results = [
            _make_extraction_result(
                1,
                entities_created=2,
                entities_matched=1,
                mentions_created=3,
                relationships_created=1,
                warnings=["w1"],
            ),
            _make_extraction_result(
                2,
                entities_created=0,
                entities_matched=4,
                mentions_created=4,
                relationships_created=2,
                warnings=["w2", "w3"],
            ),
        ]
        extraction = FakeEntityExtractionService(batch_results=results)
        runner = runner_factory(extraction=extraction)

        job = runner.submit_entity_extraction(
            {"start_date": "2026-01-01", "end_date": "2026-02-01"}
        )
        assert job.status == "queued"

        runner.shutdown(wait=True, cancel_futures=False)

        final = jobs_repo.get(job.id)
        assert final is not None
        assert final.status == "succeeded"
        assert final.started_at is not None
        assert final.finished_at is not None
        assert final.progress_current == 2
        assert final.progress_total == 2
        assert final.result == {
            "entries_processed": 2,
            "entities_created": 2,
            "entities_matched": 5,
            "entities_deleted": 0,
            "mentions_created": 7,
            "relationships_created": 3,
            "warnings": ["w1", "w2", "w3"],
        }

        assert extraction.batch_calls == [
            {
                "entry_ids": None,
                "start_date": "2026-01-01",
                "end_date": "2026-02-01",
                "stale_only": False,
            }
        ]

    def test_single_entry_path_uses_extract_from_entry(
        self, runner_factory, jobs_repo
    ):
        single = _make_extraction_result(
            42, entities_created=1, mentions_created=1
        )
        extraction = FakeEntityExtractionService(single_result=single)
        runner = runner_factory(extraction=extraction)

        job = runner.submit_entity_extraction({"entry_id": 42})
        runner.shutdown(wait=True, cancel_futures=False)

        final = jobs_repo.get(job.id)
        assert final is not None
        assert final.status == "succeeded"
        assert final.progress_current == 1
        assert final.progress_total == 1
        assert final.result is not None
        assert final.result["entries_processed"] == 1
        assert final.result["entities_created"] == 1
        assert extraction.single_calls == [42]
        assert extraction.batch_calls == []


# --------------------------------------------------------------------
# Happy path — mood backfill
# --------------------------------------------------------------------


class TestMoodBackfillHappyPath:
    def test_backfill_job_runs_to_success(self, runner_factory, jobs_repo):
        backfill_result = MoodBackfillResult(
            scored=5, skipped=2, errors=["boom on 3"]
        )
        mood_backfill = FakeMoodBackfill(
            result=backfill_result, entries_to_count=7
        )
        runner = runner_factory(mood_backfill=mood_backfill)

        job = runner.submit_mood_backfill(
            {"mode": "stale-only", "start_date": "2026-01-01"}
        )
        runner.shutdown(wait=True, cancel_futures=False)

        final = jobs_repo.get(job.id)
        assert final is not None
        assert final.status == "succeeded"
        assert final.result == {
            "scored": 5,
            "skipped": 2,
            "errors": ["boom on 3"],
        }
        assert final.progress_current == 7
        assert final.progress_total == 7

        assert len(mood_backfill.calls) == 1
        call = mood_backfill.calls[0]
        assert call["mode"] == "stale-only"
        assert call["start_date"] == "2026-01-01"
        assert call["end_date"] is None


# --------------------------------------------------------------------
# Error path
# --------------------------------------------------------------------


class TestErrorHandling:
    def test_batch_exception_marks_failed(self, runner_factory, jobs_repo):
        extraction = FakeEntityExtractionService(
            raise_in_batch=RuntimeError("boom")
        )
        runner = runner_factory(extraction=extraction)

        job = runner.submit_entity_extraction({"stale_only": True})
        _wait_terminal(jobs_repo, job.id)

        final = jobs_repo.get(job.id)
        assert final is not None
        assert final.status == "failed"
        assert final.error_message is not None
        assert "boom" in final.error_message
        assert final.finished_at is not None

    def test_runner_recovers_after_failing_job(
        self, runner_factory, jobs_repo
    ):
        extraction = FakeEntityExtractionService(
            raise_in_batch=RuntimeError("boom")
        )
        runner = runner_factory(extraction=extraction)

        failing = runner.submit_entity_extraction({"stale_only": True})
        _wait_terminal(jobs_repo, failing.id)

        # Swap in a happy fake and submit a second job — it must
        # still run. This guards against the executor being wedged
        # by an earlier exception. The swap happens on the
        # WorkerContext the runner builds in __init__; ``_ctx`` is
        # the only seam tests have for mid-flight dependency swaps
        # without rebuilding the whole runner.
        good = FakeEntityExtractionService(
            batch_results=[_make_extraction_result(1)]
        )
        runner._ctx.extraction = good  # noqa: SLF001 — test mid-flight swap

        second = runner.submit_entity_extraction({})
        runner.shutdown(wait=True, cancel_futures=False)

        final_first = jobs_repo.get(failing.id)
        final_second = jobs_repo.get(second.id)
        assert final_first is not None and final_first.status == "failed"
        assert final_second is not None
        assert final_second.status == "succeeded"


# --------------------------------------------------------------------
# Token usage capture (W2)
# --------------------------------------------------------------------


class _RecordingExtraction(FakeEntityExtractionService):
    """Fake that records LLM token usage inside ``extract_batch``.

    Proves the runner's usage-collection shim (services/jobs/run_job.py)
    flushes contextvar-scoped tokens onto the job row. When
    ``raise_after_record`` is set, it records THEN raises — so the row
    still carries usage even though the job fails.
    """

    def __init__(
        self,
        *,
        model: str = "test-model",
        input_tokens: int = 100,
        output_tokens: int = 40,
        raise_after_record: BaseException | None = None,
    ) -> None:
        super().__init__(batch_results=[])
        self._usage_model = model
        self._in = input_tokens
        self._out = output_tokens
        self._raise_after_record = raise_after_record

    def extract_batch(self, *args: Any, **kwargs: Any):  # type: ignore[override]
        from journal.services import usage

        usage.record(self._usage_model, self._in, self._out)
        if self._raise_after_record is not None:
            raise self._raise_after_record
        return super().extract_batch(*args, **kwargs)


class TestTokenUsageCapture:
    def test_succeeding_worker_records_usage(self, runner_factory, jobs_repo):
        extraction = _RecordingExtraction(input_tokens=1200, output_tokens=340)
        runner = runner_factory(extraction=extraction)

        job = runner.submit_entity_extraction({"stale_only": True})
        runner.shutdown(wait=True, cancel_futures=False)

        final = jobs_repo.get(job.id)
        assert final is not None
        assert final.status == "succeeded"
        assert final.input_tokens == 1200
        assert final.output_tokens == 340
        assert final.cost_usd is None

    def test_failing_worker_still_records_usage(self, runner_factory, jobs_repo):
        extraction = _RecordingExtraction(
            input_tokens=50,
            output_tokens=10,
            raise_after_record=RuntimeError("boom"),
        )
        runner = runner_factory(extraction=extraction)

        job = runner.submit_entity_extraction({"stale_only": True})
        _wait_terminal(jobs_repo, job.id)

        final = jobs_repo.get(job.id)
        assert final is not None
        assert final.status == "failed"
        assert final.error_message is not None and "boom" in final.error_message
        # Usage recorded even though the job failed.
        assert final.input_tokens == 50
        assert final.output_tokens == 10

    def test_worker_with_no_llm_calls_leaves_usage_null(
        self, runner_factory, jobs_repo
    ):
        # The default fake makes no usage.record() call → columns stay NULL.
        extraction = FakeEntityExtractionService(
            batch_results=[_make_extraction_result(1)]
        )
        runner = runner_factory(extraction=extraction)

        job = runner.submit_entity_extraction({})
        runner.shutdown(wait=True, cancel_futures=False)

        final = jobs_repo.get(job.id)
        assert final is not None
        assert final.status == "succeeded"
        assert final.input_tokens is None
        assert final.output_tokens is None


# --------------------------------------------------------------------
# Param validation
# --------------------------------------------------------------------


class TestEntityExtractionParamValidation:
    def test_unknown_key_raises_and_creates_no_row(
        self, runner_factory, jobs_repo
    ):
        runner = runner_factory()
        with pytest.raises(ValueError, match="Unknown params"):
            runner.submit_entity_extraction({"unknown_key": 1})

        # Query the runner's own connection — db_conn points at a
        # different SQLite file and would pass trivially.
        count = jobs_repo.connection.execute(
            "SELECT COUNT(*) AS c FROM jobs"
        ).fetchone()["c"]
        assert count == 0

    def test_wrong_type_raises(self, runner_factory, jobs_repo):
        runner = runner_factory()
        with pytest.raises(ValueError, match="stale_only"):
            runner.submit_entity_extraction({"stale_only": "yes"})

        count = jobs_repo.connection.execute(
            "SELECT COUNT(*) AS c FROM jobs"
        ).fetchone()["c"]
        assert count == 0

    def test_entry_id_must_be_int_not_bool(self, runner_factory):
        runner = runner_factory()
        with pytest.raises(ValueError, match="entry_id"):
            runner.submit_entity_extraction({"entry_id": True})


class TestMoodBackfillParamValidation:
    def test_invalid_mode_raises(self, runner_factory, jobs_repo):
        runner = runner_factory()
        with pytest.raises(ValueError, match="mode"):
            runner.submit_mood_backfill({"mode": "turbo"})

        count = jobs_repo.connection.execute(
            "SELECT COUNT(*) AS c FROM jobs"
        ).fetchone()["c"]
        assert count == 0

    def test_missing_mode_raises(self, runner_factory, jobs_repo):
        runner = runner_factory()
        with pytest.raises(ValueError, match="mode"):
            runner.submit_mood_backfill({})

        count = jobs_repo.connection.execute(
            "SELECT COUNT(*) AS c FROM jobs"
        ).fetchone()["c"]
        assert count == 0

    def test_unknown_key_raises(self, runner_factory):
        runner = runner_factory()
        with pytest.raises(ValueError, match="Unknown params"):
            runner.submit_mood_backfill(
                {"mode": "stale-only", "rogue": 1}
            )


# --------------------------------------------------------------------
# Serialisation
# --------------------------------------------------------------------


class TestSerialisation:
    def test_jobs_run_one_at_a_time(self, runner_factory, jobs_repo):
        """Prove the executor is single-worker.

        Submit two jobs where the first one blocks inside the fake.
        While the first is held, the second must still be 'queued'
        — NOT running. Then release the first, wait for both to
        finish, and assert both succeeded.
        """
        hold_first = threading.Event()
        first_entered = threading.Event()
        first_fake = FakeEntityExtractionService(
            batch_results=[_make_extraction_result(1)],
            hold_event=hold_first,
            entered_event=first_entered,
        )
        # The runner only holds one extraction service. For this
        # test we reuse the same fake for both jobs but only the
        # first one has a hold_event set; once it's released the
        # second job runs through the same code and the second
        # submission simply re-enters extract_batch with no hold
        # configured.
        #
        # To keep the two calls distinguishable, track calls via
        # batch_calls which is shared across both invocations.
        runner = runner_factory(extraction=first_fake, worker_count=1)

        job1 = runner.submit_entity_extraction({"stale_only": False})
        # Wait until the first job's worker has actually started.
        assert first_entered.wait(timeout=5)

        # First job is now inside extract_batch, blocked on
        # hold_first. Submit the second — it must be queued.
        job2 = runner.submit_entity_extraction({"stale_only": True})

        row2_before = jobs_repo.get(job2.id)
        assert row2_before is not None
        assert row2_before.status == "queued"

        row1_mid = jobs_repo.get(job1.id)
        assert row1_mid is not None
        assert row1_mid.status == "running"

        # Clear the hold so both jobs can complete. Wait for both
        # to reach a terminal state explicitly — we can't use
        # `shutdown(wait=True)` here because the executor is
        # created with `cancel_futures=True` on shutdown, which
        # would cancel job2 before it got a chance to run.
        hold_first.set()

        _wait_terminal(jobs_repo, job1.id)
        _wait_terminal(jobs_repo, job2.id)

        final1 = jobs_repo.get(job1.id)
        final2 = jobs_repo.get(job2.id)
        assert final1 is not None and final1.status == "succeeded"
        assert final2 is not None and final2.status == "succeeded"
        assert len(first_fake.batch_calls) == 2


# --------------------------------------------------------------------
# Two-pool worker model (Pool A parallel ingestion + single-worker
# Pool B for storyline jobs)
# --------------------------------------------------------------------


class _FakeStorylineResult:
    """Minimal stand-in for a StorylineGenerationResult — the fields
    run_storyline_generation reads to build its summary."""

    def __init__(self) -> None:
        self.entry_count = 0
        self.entity_mention_count = 0
        self.fts_fallback_count = 0
        self.narrative_citation_count = 0
        self.curation_citation_count = 0
        self.narrative_model = "fake-narrator"
        self.curation_model = "fake-curator"
        self.chapter_count = 0
        self.warnings: list[str] = []


class FakeStorylineGenerationService:
    """Blocks inside ``regenerate`` so tests can prove Pool B is
    single-worker and independent of Pool A.

    ``hold_event`` (if set) pauses each ``regenerate`` call until the
    test releases it; ``entered_event`` fires when a call begins.
    """

    def __init__(
        self,
        *,
        hold_event: threading.Event | None = None,
        entered_event: threading.Event | None = None,
    ) -> None:
        self._hold = hold_event
        self._entered = entered_event
        self._lock = threading.Lock()
        self.calls: list[int] = []

    def regenerate(self, storyline_id: int, **_kwargs: Any) -> _FakeStorylineResult:
        with self._lock:
            self.calls.append(storyline_id)
        if self._entered is not None:
            self._entered.set()
        if self._hold is not None:
            self._hold.wait(timeout=10)
        return _FakeStorylineResult()


class TestTwoPoolWorkerModel:
    """Pool A runs ingestion/fast jobs in parallel (``worker_count``);
    Pool B runs storyline jobs on a single worker so ingestion is never
    blocked and same-storyline regenerations can't race."""

    def test_parallel_ingestion_two_jobs_run_concurrently(
        self, runner_factory, jobs_repo,
    ) -> None:
        """With ``worker_count=2``, two blocked entity-extraction jobs
        BOTH reach ``running`` at the same time (they'd serialise on a
        single worker)."""
        hold = threading.Event()
        fake = FakeEntityExtractionService(batch_results=[], hold_event=hold)
        runner = runner_factory(extraction=fake, worker_count=2)

        j1 = runner.submit_entity_extraction({"stale_only": False})
        j2 = runner.submit_entity_extraction({"stale_only": True})

        # Both must be running simultaneously — impossible on one worker.
        _wait_status(jobs_repo, j1.id, "running")
        _wait_status(jobs_repo, j2.id, "running")

        hold.set()
        _wait_terminal(jobs_repo, j1.id)
        _wait_terminal(jobs_repo, j2.id)

        f1 = jobs_repo.get(j1.id)
        f2 = jobs_repo.get(j2.id)
        assert f1 is not None and f1.status == "succeeded"
        assert f2 is not None and f2.status == "succeeded"

    def test_ingestion_not_blocked_by_held_storyline_job(
        self, runner_factory, jobs_repo,
    ) -> None:
        """A storyline job held on Pool B must not stall Pool A: an
        entity-extraction job submitted afterwards still reaches
        ``running`` while the storyline job is held."""
        story_hold = threading.Event()
        story_entered = threading.Event()
        storyline = FakeStorylineGenerationService(
            hold_event=story_hold, entered_event=story_entered,
        )
        # Non-blocking extraction so the ingestion job runs straight
        # through once it gets a Pool A worker.
        extraction = FakeEntityExtractionService(batch_results=[])
        runner = runner_factory(
            extraction=extraction,
            storyline_generation=storyline,
            worker_count=1,
        )

        story_job = runner.submit_storyline_generation(1, user_id=1)
        # Wait until the storyline worker is actually inside regenerate,
        # occupying Pool B's only slot.
        assert story_entered.wait(timeout=5)

        entity_job = runner.submit_entity_extraction({"stale_only": True})
        # Pool A is a separate executor, so this must complete even
        # though Pool B is fully occupied by the held storyline job.
        _wait_terminal(jobs_repo, entity_job.id)

        entity_final = jobs_repo.get(entity_job.id)
        assert entity_final is not None
        assert entity_final.status == "succeeded"

        # Storyline job is still held (not terminal) at this point.
        story_row = jobs_repo.get(story_job.id)
        assert story_row is not None
        assert story_row.status == "running"

        story_hold.set()
        _wait_terminal(jobs_repo, story_job.id)

    def test_same_storyline_jobs_serialise_on_pool_b(
        self, runner_factory, jobs_repo,
    ) -> None:
        """Two storyline_generation jobs for the SAME storyline run one
        at a time on Pool B: while the first is held, the second stays
        ``queued`` — the structural guard against the same-storyline
        regeneration race, with no locking."""
        hold = threading.Event()
        entered = threading.Event()
        storyline = FakeStorylineGenerationService(
            hold_event=hold, entered_event=entered,
        )
        runner = runner_factory(
            storyline_generation=storyline, worker_count=4,
        )

        job1 = runner.submit_storyline_generation(1, user_id=1)
        assert entered.wait(timeout=5)

        job2 = runner.submit_storyline_generation(1, user_id=1)

        # First occupies the single Pool B worker; second must wait even
        # though Pool A has 4 free workers.
        row1 = jobs_repo.get(job1.id)
        row2 = jobs_repo.get(job2.id)
        assert row1 is not None and row1.status == "running"
        assert row2 is not None and row2.status == "queued"

        hold.set()
        _wait_terminal(jobs_repo, job1.id)
        _wait_terminal(jobs_repo, job2.id)

        f1 = jobs_repo.get(job1.id)
        f2 = jobs_repo.get(job2.id)
        assert f1 is not None and f1.status == "succeeded"
        assert f2 is not None and f2.status == "succeeded"
        assert storyline.calls == [1, 1]

    def test_submit_storyline_after_shutdown_raises(
        self, runner_factory,
    ) -> None:
        """Shutdown drains BOTH pools: post-shutdown storyline submits
        raise (exercises Pool B's executor shutdown, not just Pool A)."""
        storyline = FakeStorylineGenerationService()
        runner = runner_factory(storyline_generation=storyline)
        runner.shutdown(wait=True, cancel_futures=False)
        with pytest.raises(RuntimeError):
            runner.submit_storyline_generation(1, user_id=1)


# --------------------------------------------------------------------
# Shutdown
# --------------------------------------------------------------------


class FakeIngestionService:
    """Matches the slice of IngestionService the runner touches.

    Returns a fake Entry with the given id. Optionally calls the
    on_progress callback for multi-page ingestion.
    """

    def __init__(self, *, entry_id: int = 1) -> None:
        from journal.models import Entry

        self._entry = Entry(
            id=entry_id,
            entry_date="2026-04-13",
            source_type="photo",
            raw_text="fake text",
            final_text="fake text",
            word_count=2,
            chunk_count=1,
        )
        self.ingest_image_calls: list[tuple[bytes, str, str]] = []
        self.multi_page_calls: list[int] = []
        self.multi_voice_calls: list[int] = []
        self.reprocess_calls: list[int] = []

    def ingest_image(
        self, image_data: bytes, media_type: str, date: str,
        *, skip_mood: bool = False, user_id: int = 1,
    ) -> Any:
        self.ingest_image_calls.append((image_data, media_type, date))
        return self._entry

    def ingest_multi_page_entry(
        self,
        images: list[tuple[bytes, str]],
        date: str,
        *,
        skip_mood: bool = False,
        on_progress: Callable[[int, int], None] | None = None,
        user_id: int = 1,
    ) -> Any:
        self.multi_page_calls.append(len(images))
        for i in range(len(images)):
            if on_progress is not None:
                on_progress(i + 1, len(images))
        return self._entry

    def ingest_multi_voice(
        self,
        recordings: list[tuple[bytes, str]],
        date: str,
        language: str = "en",
        *,
        source_type: str = "voice",
        skip_mood: bool = False,
        on_progress: Callable[[int, int], None] | None = None,
        user_id: int = 1,
    ) -> Any:
        self.multi_voice_calls.append(len(recordings))
        for i in range(len(recordings)):
            if on_progress is not None:
                on_progress(i + 1, len(recordings))
        return self._entry

    def reprocess_embeddings(self, entry_id: int) -> int:
        self.reprocess_calls.append(entry_id)
        return 5  # fake chunk count


# --------------------------------------------------------------------
# Happy path — image ingestion
# --------------------------------------------------------------------


class TestImageIngestionProgress:
    """Regression tests for progress_total == page count (no off-by-one)."""

    def test_single_image_progress_total_equals_page_count(
        self, runner_factory, jobs_repo
    ):
        ingestion = FakeIngestionService()
        runner = runner_factory(ingestion=ingestion)

        images = [(b"img1", "image/jpeg", "page1.jpg")]
        job = runner.submit_image_ingestion(images, "2026-04-13")
        runner.shutdown(wait=True, cancel_futures=False)

        final = jobs_repo.get(job.id)
        assert final is not None
        assert final.status == "succeeded"
        assert final.progress_total == 1  # 1 page, NOT 2
        assert final.progress_current == 1

    def test_multi_image_progress_total_equals_page_count(
        self, runner_factory, jobs_repo
    ):
        ingestion = FakeIngestionService()
        runner = runner_factory(ingestion=ingestion)

        images = [
            (b"img1", "image/jpeg", "page1.jpg"),
            (b"img2", "image/jpeg", "page2.jpg"),
            (b"img3", "image/jpeg", "page3.jpg"),
        ]
        job = runner.submit_image_ingestion(images, "2026-04-13")
        runner.shutdown(wait=True, cancel_futures=False)

        final = jobs_repo.get(job.id)
        assert final is not None
        assert final.status == "succeeded"
        assert final.progress_total == 3  # 3 pages, NOT 4
        assert final.progress_current == 3
        assert final.result["entry_id"] == 1
        assert final.result["page_count"] == 3
        assert final.result["word_count"] == 2
        assert final.result["chunk_count"] == 1
        assert final.result["entry_date"] == "2026-04-13"
        assert final.result["source_type"] == "photo"
        assert "follow_up_jobs" in final.result

    def test_progress_current_never_exceeds_total(
        self, runner_factory, jobs_repo
    ):
        """Verify every progress update has current <= total."""
        ingestion = FakeIngestionService()
        runner = runner_factory(ingestion=ingestion)

        # Patch update_progress to record all calls
        updates: list[tuple[str, int, int]] = []
        original_update = jobs_repo.update_progress

        def tracking_update(job_id: str, current: int, total: int) -> None:
            updates.append((job_id, current, total))
            original_update(job_id, current, total)

        jobs_repo.update_progress = tracking_update  # type: ignore[method-assign]

        images = [
            (b"img1", "image/jpeg", "page1.jpg"),
            (b"img2", "image/jpeg", "page2.jpg"),
        ]
        job = runner.submit_image_ingestion(images, "2026-04-13")
        runner.shutdown(wait=True, cancel_futures=False)

        # Every update should have current <= total
        for _jid, current, total in updates:
            assert current <= total, (
                f"progress_current ({current}) exceeded "
                f"progress_total ({total})"
            )

        # Final state should be 2/2
        final = jobs_repo.get(job.id)
        assert final is not None
        assert final.progress_current == 2
        assert final.progress_total == 2


# --------------------------------------------------------------------
# Single-entry contract: image worker creates exactly one entry (Task 7)
# --------------------------------------------------------------------


class TestImageIngestionSingleEntryContract:
    """The image worker always produces exactly one entry — single image
    goes through ``ingest_image``, multi-image through
    ``ingest_multi_page_entry``. Follow-up keys are unsuffixed; result
    has no ``entry_ids`` fan-out key. Regression for Task-6/7 collapse."""

    def _make_runner(
        self,
        jobs_repo: SQLiteJobRepository,
        threadsafe_factory: ConnectionFactory,
        *,
        ocr_text: str,
    ) -> tuple[JobRunner, Any, FakeNotificationService]:
        """JobRunner wired to a real IngestionService with a mocked OCR
        provider returning ``ocr_text``."""
        from unittest.mock import MagicMock

        from journal.db.repository import SQLiteEntryRepository
        from journal.providers.ocr import OCRResult
        from journal.services.chunking import FixedTokenChunker
        from journal.services.ingestion import IngestionService
        from journal.vectorstore.store import InMemoryVectorStore

        repo = SQLiteEntryRepository(threadsafe_factory)

        ocr = MagicMock()
        ocr.extract.return_value = OCRResult(
            text=ocr_text,
            uncertain_spans=[],
        )
        embeddings = MagicMock()
        embeddings.embed_texts.return_value = [[0.1, 0.2, 0.3]]

        ingestion = IngestionService(
            repository=repo,
            vector_store=InMemoryVectorStore(),
            ocr_provider=ocr,
            transcription_provider=MagicMock(),
            embeddings_provider=embeddings,
            chunker=FixedTokenChunker(max_tokens=150, overlap_tokens=40),
            preprocess_images=False,
        )
        notif = FakeNotificationService()
        runner = JobRunner(
            job_repository=jobs_repo,
            entity_extraction_service=FakeEntityExtractionService(
                single_result=_make_extraction_result(
                    1, entities_created=2, mentions_created=4,
                ),
            ),
            mood_backfill_callable=FakeMoodBackfill(),
            mood_scoring_service=FakeMoodScoringService(scores=7),
            entry_repository=FakeEntryRepository(),
            ingestion_service=ingestion,
            notification_service=notif,  # type: ignore[arg-type]
        )
        return runner, repo, notif

    def test_single_image_creates_one_entry(
        self, jobs_repo, threadsafe_factory
    ) -> None:
        """A single image produces exactly one entry and no ``entry_ids``
        key in the result (regression: no fan-out)."""
        runner, repo, _ = self._make_runner(
            jobs_repo, threadsafe_factory,
            ocr_text="3 June 2026\n\nSome journal entry body.",
        )

        images = [(b"page data", "image/jpeg", "page1.jpg")]
        parent = runner.submit_image_ingestion(images, "2026-06-03", user_id=1)
        _wait_terminal(jobs_repo, parent.id)
        runner.shutdown(wait=True, cancel_futures=False)

        final = jobs_repo.get(parent.id)
        assert final is not None
        assert final.status == "succeeded"

        # Exactly one entry persisted.
        entries = repo.list_entries(limit=10)
        assert len(entries) == 1

        # No fan-out key in result.
        assert "entry_ids" not in final.result

        # Follow-up keys are unsuffixed.
        follow_ups = final.result["follow_up_jobs"]
        assert "mood_scoring" in follow_ups
        assert "entity_extraction" in follow_ups

    def test_single_image_with_trailing_neighbor_creates_one_entry(
        self, jobs_repo, threadsafe_factory
    ) -> None:
        """Regression: OCR returns body + ENTRY_ENDS marker + a neighbour
        entry. ``ingest_image`` must still produce exactly ONE entry;
        the worker must not fan-out."""
        from journal.providers.ocr import ENTRY_ENDS

        neighbor_text = "Neighbor entry text"
        ocr_text = (
            "3 June 2026\n\nMain entry body."
            f"\n{ENTRY_ENDS}\n"
            f"2025-01-02\n{neighbor_text}"
        )
        runner, repo, _ = self._make_runner(
            jobs_repo, threadsafe_factory, ocr_text=ocr_text,
        )

        images = [(b"page with trailing neighbour", "image/jpeg", "page1.jpg")]
        parent = runner.submit_image_ingestion(images, "2026-06-03", user_id=1)
        _wait_terminal(jobs_repo, parent.id)
        runner.shutdown(wait=True, cancel_futures=False)

        final = jobs_repo.get(parent.id)
        assert final is not None
        assert final.status == "succeeded"

        # Exactly one entry persisted.
        entries = repo.list_entries(limit=10)
        assert len(entries) == 1

        # No multi-entry fan-out key.
        assert "entry_ids" not in final.result

        # Follow-up keys are unsuffixed.
        follow_ups = final.result["follow_up_jobs"]
        assert "mood_scoring" in follow_ups
        assert "entity_extraction" in follow_ups

        # Trimming path: content_end_char must be set (ENTRY_ENDS was present).
        entry = repo.get_entry(entries[0].id)
        assert entry is not None
        assert entry.content_end_char is not None

        # final_text (the in-bounds/reading slice) must NOT contain the
        # neighbour text — it was trimmed at the ENTRY_ENDS boundary.
        assert neighbor_text not in entry.final_text

        # raw_text is verbatim OCR output so the neighbour IS present.
        assert neighbor_text in entry.raw_text

    def test_single_image_queues_exactly_two_follow_ups(
        self, jobs_repo, threadsafe_factory
    ) -> None:
        """Worker queues exactly one mood-scoring + one entity-extraction
        follow-up (two total) for the single created entry."""
        runner, repo, _ = self._make_runner(
            jobs_repo, threadsafe_factory,
            ocr_text="3 June 2026\n\nEntry body.",
        )

        images = [(b"page data", "image/jpeg", "page1.jpg")]
        parent = runner.submit_image_ingestion(images, "2026-06-03", user_id=1)
        _wait_terminal(jobs_repo, parent.id)
        runner.shutdown(wait=True, cancel_futures=False)

        mood_jobs, _ = jobs_repo.list_jobs(job_type="mood_score_entry")
        entity_jobs, _ = jobs_repo.list_jobs(job_type="entity_extraction")
        assert len(mood_jobs) == 1
        assert len(entity_jobs) == 1

        entry = repo.list_entries(limit=10)[0]
        assert mood_jobs[0].params["entry_id"] == entry.id
        assert entity_jobs[0].params["entry_id"] == entry.id

    def test_single_image_sends_one_combined_notification(
        self, jobs_repo, threadsafe_factory
    ) -> None:
        """The pipeline notification fires exactly once with ``ingest_images``
        job type and no ``entry_ids`` key."""
        runner, _, notif = self._make_runner(
            jobs_repo, threadsafe_factory,
            ocr_text="3 June 2026\n\nEntry body.",
        )

        images = [(b"page data", "image/jpeg", "page1.jpg")]
        parent = runner.submit_image_ingestion(images, "2026-06-03", user_id=1)
        _wait_terminal(jobs_repo, parent.id)
        final = jobs_repo.get(parent.id)
        assert final is not None
        for fj_id in (final.result or {}).get("follow_up_jobs", {}).values():
            _wait_terminal(jobs_repo, fj_id)
        runner.shutdown(wait=True, cancel_futures=False)

        assert len(notif.success_calls) == 1
        _, job_type, result = notif.success_calls[0]
        assert job_type == "ingest_images"
        assert "entry_ids" not in result


# --------------------------------------------------------------------
# Audio ingestion
# --------------------------------------------------------------------


class TestAudioIngestion:
    """Tests for submit_audio_ingestion and _run_audio_ingestion."""

    def test_single_recording_succeeds(self, runner_factory, jobs_repo):
        ingestion = FakeIngestionService()
        runner = runner_factory(ingestion=ingestion)

        recordings = [(b"audio1", "audio/webm", "rec1.webm")]
        job = runner.submit_audio_ingestion(recordings, "2026-04-14")
        runner.shutdown(wait=True, cancel_futures=False)

        final = jobs_repo.get(job.id)
        assert final is not None
        assert final.status == "succeeded"
        assert final.result["entry_id"] == 1
        assert final.result["recording_count"] == 1
        assert final.result["word_count"] == 2
        assert final.result["chunk_count"] == 1
        assert final.result["entry_date"] == "2026-04-13"
        assert final.result["source_type"] == "photo"
        assert "follow_up_jobs" in final.result

    def test_multiple_recordings_succeeds(self, runner_factory, jobs_repo):
        ingestion = FakeIngestionService()
        runner = runner_factory(ingestion=ingestion)

        recordings = [
            (b"audio1", "audio/webm", "rec1.webm"),
            (b"audio2", "audio/webm", "rec2.webm"),
        ]
        job = runner.submit_audio_ingestion(recordings, "2026-04-14")
        runner.shutdown(wait=True, cancel_futures=False)

        final = jobs_repo.get(job.id)
        assert final is not None
        assert final.status == "succeeded"
        assert final.progress_total == 2
        assert final.progress_current == 2

    def test_empty_recordings_raises(self, runner_factory):
        runner = runner_factory()
        with pytest.raises(ValueError, match="At least one"):
            runner.submit_audio_ingestion([], "2026-04-14")
        runner.shutdown()

    def test_job_type_is_ingest_audio(self, runner_factory, jobs_repo):
        ingestion = FakeIngestionService()
        runner = runner_factory(ingestion=ingestion)

        recordings = [(b"audio1", "audio/webm", "rec.webm")]
        job = runner.submit_audio_ingestion(recordings, "2026-04-14")
        assert job.type == "ingest_audio"
        runner.shutdown(wait=True, cancel_futures=False)

    def test_recording_count_in_params(self, runner_factory, jobs_repo):
        ingestion = FakeIngestionService()
        runner = runner_factory(ingestion=ingestion)

        recordings = [
            (b"a1", "audio/webm", "r1.webm"),
            (b"a2", "audio/mp3", "r2.mp3"),
        ]
        job = runner.submit_audio_ingestion(recordings, "2026-04-14")
        assert job.params["recording_count"] == 2
        runner.shutdown(wait=True, cancel_futures=False)

    def test_no_ingestion_service_fails(self, runner_factory, jobs_repo):
        # No ingestion= kwarg → JobRunner gets ingestion_service=None.
        runner = runner_factory()

        recordings = [(b"audio1", "audio/webm", "rec.webm")]
        job = runner.submit_audio_ingestion(recordings, "2026-04-14")
        runner.shutdown(wait=True, cancel_futures=False)

        final = jobs_repo.get(job.id)
        assert final is not None
        assert final.status == "failed"
        assert "not available" in final.error_message


# --------------------------------------------------------------------
# Happy path — reprocess embeddings
# --------------------------------------------------------------------


class TestReprocessEmbeddings:
    def test_reprocess_job_runs_to_success(self, runner_factory, jobs_repo):
        ingestion = FakeIngestionService()
        runner = runner_factory(ingestion=ingestion)

        job = runner.submit_reprocess_embeddings(42)
        runner.shutdown(wait=True, cancel_futures=False)

        final = jobs_repo.get(job.id)
        assert final is not None
        assert final.status == "succeeded"
        assert final.progress_current == 1
        assert final.progress_total == 1
        assert final.result == {"entry_id": 42, "chunk_count": 5}
        assert ingestion.reprocess_calls == [42]

    def test_reprocess_without_ingestion_service_fails(
        self, runner_factory, jobs_repo
    ):
        # No ingestion= kwarg → JobRunner gets ingestion_service=None.
        runner = runner_factory()

        job = runner.submit_reprocess_embeddings(1)
        runner.shutdown(wait=True, cancel_futures=False)

        final = jobs_repo.get(job.id)
        assert final is not None
        assert final.status == "failed"
        assert "not available" in (final.error_message or "")


# --------------------------------------------------------------------
# Shutdown
# --------------------------------------------------------------------


class TestShutdown:
    def test_submit_after_shutdown_raises(self, runner_factory):
        runner = runner_factory()
        runner.shutdown(wait=True, cancel_futures=False)
        with pytest.raises(RuntimeError):
            runner.submit_entity_extraction({})


# --------------------------------------------------------------------
# Fakes for pipeline notification tests
# --------------------------------------------------------------------


class FakeNotificationService:
    """Captures notification calls for assertion."""

    def __init__(self) -> None:
        self.success_calls: list[tuple[int, str, dict]] = []
        self.failure_calls: list[tuple[int, str, str]] = []
        self.pipeline_failure_calls: list[tuple[int, str, str]] = []

    def notify_job_success(
        self, user_id: int, job_type: str, result: dict[str, Any],
    ) -> None:
        self.success_calls.append((user_id, job_type, result))

    def notify_job_failed(
        self, user_id: int, job_type: str, error_message: str,
        exc: Exception | None = None,
    ) -> None:
        self.failure_calls.append((user_id, job_type, error_message))

    def notify_admin_job_failed(self, *args: Any, **kwargs: Any) -> None:
        pass

    def notify_job_retrying(self, *args: Any, **kwargs: Any) -> None:
        pass

    def notify_pipeline_failed(
        self, user_id: int, parent_job_type: str, body: str,
    ) -> None:
        self.pipeline_failure_calls.append((user_id, parent_job_type, body))


class FakeMoodScoringService:
    """Returns a fixed score count, or raises if configured to fail."""

    def __init__(
        self, scores: int = 7, *, raise_exc: BaseException | None = None,
    ) -> None:
        self._scores = scores
        self._raise = raise_exc

    def score_entry(self, entry_id: int, text: str) -> int:
        if self._raise is not None:
            raise self._raise
        return self._scores


class FakeEntryRepository:
    """Returns a canned Entry from get_entry."""

    def __init__(self) -> None:
        from journal.models import Entry
        self._entry = Entry(
            id=1,
            entry_date="2026-04-25",
            source_type="voice",
            raw_text="hello world",
            final_text="hello world",
            word_count=2,
            chunk_count=1,
        )

    def get_entry(self, entry_id: int) -> Any:
        return self._entry


# --------------------------------------------------------------------
# Pipeline notification tests
# --------------------------------------------------------------------


class TestPipelineNotification:
    """Ingestion pipelines (audio/image) send ONE combined Pushover
    notification after all follow-up jobs complete, not one per job."""

    def _make_pipeline_runner(
        self,
        jobs_repo: SQLiteJobRepository,
    ) -> tuple[JobRunner, FakeNotificationService]:
        notif = FakeNotificationService()
        extraction = FakeEntityExtractionService(
            single_result=_make_extraction_result(
                1, entities_created=8, mentions_created=18,
            ),
        )
        runner = JobRunner(
            job_repository=jobs_repo,
            entity_extraction_service=extraction,
            mood_backfill_callable=FakeMoodBackfill(),
            mood_scoring_service=FakeMoodScoringService(scores=7),
            entry_repository=FakeEntryRepository(),
            ingestion_service=FakeIngestionService(),
            notification_service=notif,  # type: ignore[arg-type]
        )
        return runner, notif

    def _wait_pipeline(
        self,
        jobs_repo: SQLiteJobRepository,
        parent_id: str,
        timeout: float = 10.0,
    ) -> None:
        """Wait for parent + all follow-up jobs to reach terminal state."""
        _wait_terminal(jobs_repo, parent_id, timeout)
        parent = jobs_repo.get(parent_id)
        assert parent is not None
        for fj_id in (parent.result or {}).get("follow_up_jobs", {}).values():
            _wait_terminal(jobs_repo, fj_id, timeout)

    def test_audio_pipeline_sends_one_notification(
        self, jobs_repo: SQLiteJobRepository,
    ) -> None:
        runner, notif = self._make_pipeline_runner(jobs_repo)
        recordings = [(b"audio1", "audio/webm", "rec.webm")]
        job = runner.submit_audio_ingestion(
            recordings, "2026-04-25", user_id=1,
        )
        self._wait_pipeline(jobs_repo, job.id)
        runner.shutdown(wait=True, cancel_futures=False)

        # Exactly one success notification for the whole pipeline
        assert len(notif.success_calls) == 1
        user_id, job_type, result = notif.success_calls[0]
        assert job_type == "ingest_audio"

        # Combined result includes follow-up results
        assert "mood_scoring_result" in result
        assert result["mood_scoring_result"]["scores_written"] == 7
        assert "entity_extraction_result" in result
        assert result["entity_extraction_result"]["entities_created"] == 8
        assert result["entity_extraction_result"]["mentions_created"] == 18

        # Parent entry info still present
        assert result["entry_id"] == 1

    def test_pipeline_notifies_when_children_finish_before_parent(
        self, jobs_repo: SQLiteJobRepository,
    ) -> None:
        """Two-pool regression: with parallel Pool A workers a follow-up
        child can reach a terminal state BEFORE the parent ingestion job
        marks itself succeeded. Each such child sees ``parent.status !=
        'succeeded'`` and returns early, so the parent must run a
        defensive pipeline sweep after marking itself succeeded — else
        the consolidated push is lost entirely.

        We force the exact interleaving deterministically: wrap
        ``queue_post_ingestion_jobs`` so it blocks until every queued
        child has finished before returning to the parent worker (which
        then marks itself succeeded). This is precisely what parallel
        Pool A workers can produce; under the old single-worker executor
        it was impossible (children couldn't start until the parent
        freed the worker).
        """
        runner, notif = self._make_pipeline_runner(jobs_repo)

        real_queue = runner._queue_post_ingestion_jobs  # noqa: SLF001

        def queue_and_drain(
            parent_job_id: str, kind: str, entry_id: int,
            user_id: int | None,
        ) -> dict[str, str]:
            ids = real_queue(parent_job_id, kind, entry_id, user_id)
            # Block the parent worker until all children are terminal.
            for fj_id in ids.values():
                _wait_terminal(jobs_repo, fj_id)
            return ids

        # Override the seam the worker calls (ctx captured the bound
        # method at construction).
        runner._ctx.queue_post_ingestion_jobs = queue_and_drain  # noqa: SLF001

        recordings = [(b"audio1", "audio/webm", "rec.webm")]
        parent = runner.submit_audio_ingestion(
            recordings, "2026-04-25", user_id=1,
        )
        _wait_terminal(jobs_repo, parent.id)
        runner.shutdown(wait=True, cancel_futures=False)

        # Exactly one consolidated success push, despite the children
        # finishing before the parent was marked succeeded.
        assert len(notif.success_calls) == 1
        _, job_type, _ = notif.success_calls[0]
        assert job_type == "ingest_audio"

    def test_image_pipeline_sends_one_notification(
        self, jobs_repo: SQLiteJobRepository,
    ) -> None:
        runner, notif = self._make_pipeline_runner(jobs_repo)
        images = [(b"img1", "image/jpeg", "page1.jpg")]
        job = runner.submit_image_ingestion(images, "2026-04-25", user_id=1)
        self._wait_pipeline(jobs_repo, job.id)
        runner.shutdown(wait=True, cancel_futures=False)

        assert len(notif.success_calls) == 1
        user_id, job_type, result = notif.success_calls[0]
        assert job_type == "ingest_images"
        assert "mood_scoring_result" in result
        assert "entity_extraction_result" in result

    def test_standalone_entity_extraction_still_notifies(
        self, jobs_repo: SQLiteJobRepository,
    ) -> None:
        """Manually triggered batch jobs (no parent_job_id) notify individually."""
        runner, notif = self._make_pipeline_runner(jobs_repo)
        runner.submit_entity_extraction({"entry_id": 1}, user_id=1)
        runner.shutdown(wait=True, cancel_futures=False)

        assert len(notif.success_calls) == 1
        _, job_type, _ = notif.success_calls[0]
        assert job_type == "entity_extraction"

    def test_standalone_mood_score_still_notifies(
        self, jobs_repo: SQLiteJobRepository,
    ) -> None:
        """Manually triggered mood scoring (no parent_job_id) notifies individually."""
        runner, notif = self._make_pipeline_runner(jobs_repo)
        runner.submit_mood_score_entry(1, user_id=1)
        runner.shutdown(wait=True, cancel_futures=False)

        assert len(notif.success_calls) == 1
        _, job_type, _ = notif.success_calls[0]
        assert job_type == "mood_score_entry"

    def test_parent_job_id_stored_in_follow_up_params(
        self, jobs_repo: SQLiteJobRepository,
    ) -> None:
        """Follow-up jobs created by ingestion carry parent_job_id in params."""
        runner, _ = self._make_pipeline_runner(jobs_repo)
        recordings = [(b"audio1", "audio/webm", "rec.webm")]
        parent = runner.submit_audio_ingestion(
            recordings, "2026-04-25", user_id=1,
        )
        self._wait_pipeline(jobs_repo, parent.id)
        runner.shutdown(wait=True, cancel_futures=False)

        parent_final = jobs_repo.get(parent.id)
        assert parent_final is not None
        follow_ups = parent_final.result["follow_up_jobs"]

        for _key, fj_id in follow_ups.items():
            fj = jobs_repo.get(fj_id)
            assert fj is not None
            assert fj.params["parent_job_id"] == parent.id

    def test_partial_failure_still_sends_combined_notification(
        self, jobs_repo: SQLiteJobRepository,
    ) -> None:
        """When mood scoring fails but entity extraction succeeds, the user
        still gets a combined notification about what worked."""
        notif = FakeNotificationService()
        extraction = FakeEntityExtractionService(
            single_result=_make_extraction_result(
                1, entities_created=5, mentions_created=12,
            ),
        )
        runner = JobRunner(
            job_repository=jobs_repo,
            entity_extraction_service=extraction,
            mood_backfill_callable=FakeMoodBackfill(),
            mood_scoring_service=FakeMoodScoringService(
                raise_exc=RuntimeError("LLM overloaded"),
            ),
            entry_repository=FakeEntryRepository(),
            ingestion_service=FakeIngestionService(),
            notification_service=notif,  # type: ignore[arg-type]
        )

        recordings = [(b"audio1", "audio/webm", "rec.webm")]
        parent = runner.submit_audio_ingestion(
            recordings, "2026-04-25", user_id=1,
        )
        self._wait_pipeline(jobs_repo, parent.id)
        runner.shutdown(wait=True, cancel_futures=False)

        # 1 failure notification for mood scoring
        assert len(notif.failure_calls) == 1
        _, fail_type, _ = notif.failure_calls[0]
        assert fail_type == "mood_score_entry"

        # 1 combined success notification (entity extraction results only)
        assert len(notif.success_calls) == 1
        _, job_type, result = notif.success_calls[0]
        assert job_type == "ingest_audio"
        assert result["entry_id"] == 1
        assert "entity_extraction_result" in result
        assert result["entity_extraction_result"]["entities_created"] == 5
        # Mood scoring failed, so its results should NOT be in the combined
        assert "mood_scoring_result" not in result

    def test_entity_fails_mood_succeeds_sends_combined(
        self, jobs_repo: SQLiteJobRepository,
    ) -> None:
        """When entity extraction fails but mood scoring succeeds, the user
        still gets a combined notification with mood results."""
        notif = FakeNotificationService()
        extraction = FakeEntityExtractionService(
            raise_in_single=RuntimeError("extraction error"),
        )
        runner = JobRunner(
            job_repository=jobs_repo,
            entity_extraction_service=extraction,
            mood_backfill_callable=FakeMoodBackfill(),
            mood_scoring_service=FakeMoodScoringService(scores=7),
            entry_repository=FakeEntryRepository(),
            ingestion_service=FakeIngestionService(),
            notification_service=notif,  # type: ignore[arg-type]
        )

        images = [(b"img1", "image/jpeg", "page1.jpg")]
        parent = runner.submit_image_ingestion(images, "2026-04-25", user_id=1)
        self._wait_pipeline(jobs_repo, parent.id)
        runner.shutdown(wait=True, cancel_futures=False)

        # 1 failure notification for entity extraction
        assert len(notif.failure_calls) == 1
        _, fail_type, _ = notif.failure_calls[0]
        assert fail_type == "entity_extraction"

        # 1 combined success notification (mood results only)
        assert len(notif.success_calls) == 1
        _, job_type, result = notif.success_calls[0]
        assert job_type == "ingest_images"
        assert result["entry_id"] == 1
        assert "mood_scoring_result" in result
        assert result["mood_scoring_result"]["scores_written"] == 7
        assert "entity_extraction_result" not in result

    def test_both_followups_fail_no_misleading_message(
        self, jobs_repo: SQLiteJobRepository,
    ) -> None:
        """When both follow-ups fail, the combined notification must NOT
        say 'All processing complete'."""
        notif = FakeNotificationService()
        extraction = FakeEntityExtractionService(
            raise_in_single=RuntimeError("extraction error"),
        )
        runner = JobRunner(
            job_repository=jobs_repo,
            entity_extraction_service=extraction,
            mood_backfill_callable=FakeMoodBackfill(),
            mood_scoring_service=FakeMoodScoringService(
                raise_exc=RuntimeError("mood error"),
            ),
            entry_repository=FakeEntryRepository(),
            ingestion_service=FakeIngestionService(),
            notification_service=notif,  # type: ignore[arg-type]
        )

        recordings = [(b"audio1", "audio/webm", "rec.webm")]
        parent = runner.submit_audio_ingestion(
            recordings, "2026-04-25", user_id=1,
        )
        self._wait_pipeline(jobs_repo, parent.id)
        runner.shutdown(wait=True, cancel_futures=False)

        # 2 failure notifications (one per follow-up)
        assert len(notif.failure_calls) == 2
        fail_types = {call[1] for call in notif.failure_calls}
        assert fail_types == {"mood_score_entry", "entity_extraction"}

        # 1 combined notification — entry created, but no follow-up results
        assert len(notif.success_calls) == 1
        _, job_type, result = notif.success_calls[0]
        assert job_type == "ingest_audio"
        assert result["entry_id"] == 1
        assert "mood_scoring_result" not in result
        assert "entity_extraction_result" not in result

    def test_both_followups_fail_message_content(
        self, jobs_repo: SQLiteJobRepository,
    ) -> None:
        """build_success_message must NOT say 'All processing complete'
        when follow-ups were queued but both failed."""
        from journal.services.notifications import build_success_message

        # Simulate the combined result when both follow-ups failed:
        # follow_up_jobs is non-empty but no *_result keys are present.
        combined = {
            "entry_id": 42,
            "follow_up_jobs": {
                "mood_scoring": "abc",
                "entity_extraction": "def",
            },
        }
        msg = build_success_message("ingest_audio", combined)
        assert "Entry 42" in msg
        assert "all processing complete" not in msg.lower()


# --------------------------------------------------------------------
# Save-entry pipeline (PATCH /entries/{id}) — consolidated notifications
# --------------------------------------------------------------------


class TestSaveEntryPipeline:
    """Edits to existing entries fan out into 3 background jobs
    (reprocess_embeddings, entity_extraction, mood_score_entry).

    The pipeline must emit exactly ONE Pushover notification covering
    all three — success summary on the happy path, consolidated
    failure summary if any child fails. Per-child failure pushovers
    are explicitly suppressed (this is the `compressed_all` strategy).
    """

    def _make_runner(
        self,
        jobs_repo: SQLiteJobRepository,
        *,
        extraction_raises: BaseException | None = None,
        mood_raises: BaseException | None = None,
        ingestion_raises: BaseException | None = None,
        ingestion_service: Any = None,
    ) -> tuple[JobRunner, FakeNotificationService]:
        notif = FakeNotificationService()
        if extraction_raises is not None:
            extraction = FakeEntityExtractionService(
                raise_in_single=extraction_raises,
            )
        else:
            extraction = FakeEntityExtractionService(
                single_result=_make_extraction_result(
                    1, entities_created=2, mentions_created=5,
                ),
            )
        if ingestion_service is None:
            ingestion_service = FakeIngestionService()
            if ingestion_raises is not None:
                # Replace reprocess_embeddings to raise. This monkey-patch
                # is fine for a fake — we don't want to add a knob to the
                # real fake just for one test path.
                def boom(_entry_id: int) -> int:
                    raise ingestion_raises
                ingestion_service.reprocess_embeddings = boom  # type: ignore[method-assign]
        runner = JobRunner(
            job_repository=jobs_repo,
            entity_extraction_service=extraction,
            mood_backfill_callable=FakeMoodBackfill(),
            mood_scoring_service=FakeMoodScoringService(
                scores=3, raise_exc=mood_raises,
            ),
            entry_repository=FakeEntryRepository(),
            ingestion_service=ingestion_service,
            notification_service=notif,  # type: ignore[arg-type]
        )
        return runner, notif

    def _wait_pipeline(
        self,
        jobs_repo: SQLiteJobRepository,
        parent_id: str,
        timeout: float = 10.0,
    ) -> None:
        _wait_terminal(jobs_repo, parent_id, timeout)
        parent = jobs_repo.get(parent_id)
        assert parent is not None
        for fj_id in (parent.result or {}).get("follow_up_jobs", {}).values():
            _wait_terminal(jobs_repo, fj_id, timeout)

    def test_happy_path_sends_one_success_notification(
        self, jobs_repo: SQLiteJobRepository,
    ) -> None:
        runner, notif = self._make_runner(jobs_repo)
        parent, _children = runner.submit_save_entry_pipeline(
            entry_id=1, user_id=1,
        )
        self._wait_pipeline(jobs_repo, parent.id)
        runner.shutdown(wait=True, cancel_futures=False)

        # Exactly one push, success-flavored, save_entry_pipeline type
        assert len(notif.failure_calls) == 0
        assert len(notif.pipeline_failure_calls) == 0
        assert len(notif.success_calls) == 1
        user_id, job_type, result = notif.success_calls[0]
        assert user_id == 1
        assert job_type == "save_entry_pipeline"
        # Combined result includes per-child results
        assert result["entry_id"] == 1
        assert result["reprocess_embeddings_result"]["chunk_count"] == 5
        assert result["entity_extraction_result"]["entities_created"] == 2
        assert result["entity_extraction_result"]["mentions_created"] == 5
        assert result["mood_scoring_result"]["scores_written"] == 3

    def test_partial_failure_consolidates_into_one_failure_push(
        self, jobs_repo: SQLiteJobRepository,
    ) -> None:
        runner, notif = self._make_runner(
            jobs_repo, mood_raises=RuntimeError("LLM overloaded"),
        )
        parent, _children = runner.submit_save_entry_pipeline(
            entry_id=1, user_id=1,
        )
        self._wait_pipeline(jobs_repo, parent.id)
        runner.shutdown(wait=True, cancel_futures=False)

        # No standalone success and no per-child failure pushes
        assert len(notif.success_calls) == 0
        assert len(notif.failure_calls) == 0
        # One consolidated pipeline-failure push
        assert len(notif.pipeline_failure_calls) == 1
        user_id, parent_type, body = notif.pipeline_failure_calls[0]
        assert user_id == 1
        assert parent_type == "save_entry_pipeline"
        # Body lists what worked and what didn't
        assert "Entry 1" in body
        assert "Reprocessed" in body or "chunks" in body  # reprocess succeeded
        assert "entities" in body.lower()  # entity succeeded
        assert "mood" in body.lower()  # mood failed
        assert "LLM overloaded" in body

    def test_total_failure_consolidates_into_one_failure_push(
        self, jobs_repo: SQLiteJobRepository,
    ) -> None:
        runner, notif = self._make_runner(
            jobs_repo,
            extraction_raises=RuntimeError("extraction broke"),
            mood_raises=RuntimeError("mood broke"),
            ingestion_raises=RuntimeError("reprocess broke"),
        )
        parent, _children = runner.submit_save_entry_pipeline(
            entry_id=1, user_id=1,
        )
        self._wait_pipeline(jobs_repo, parent.id)
        runner.shutdown(wait=True, cancel_futures=False)

        assert len(notif.success_calls) == 0
        assert len(notif.failure_calls) == 0
        assert len(notif.pipeline_failure_calls) == 1
        _user_id, _job_type, body = notif.pipeline_failure_calls[0]
        # All three failure messages should appear
        assert "extraction broke" in body
        assert "mood broke" in body
        assert "reprocess broke" in body

    def test_no_mood_when_disabled(
        self, jobs_repo: SQLiteJobRepository,
    ) -> None:
        runner, notif = self._make_runner(jobs_repo)
        parent, children = runner.submit_save_entry_pipeline(
            entry_id=1, user_id=1, enable_mood_scoring=False,
        )
        self._wait_pipeline(jobs_repo, parent.id)
        runner.shutdown(wait=True, cancel_futures=False)

        # Only 2 children
        assert "mood_scoring" not in children
        assert "reprocess_embeddings" in children
        assert "entity_extraction" in children

        assert len(notif.success_calls) == 1
        _, job_type, result = notif.success_calls[0]
        assert job_type == "save_entry_pipeline"
        # Mood result not in combined
        assert "mood_scoring_result" not in result

    def test_children_carry_parent_job_id(
        self, jobs_repo: SQLiteJobRepository,
    ) -> None:
        runner, _notif = self._make_runner(jobs_repo)
        parent, children = runner.submit_save_entry_pipeline(
            entry_id=1, user_id=1,
        )
        self._wait_pipeline(jobs_repo, parent.id)
        runner.shutdown(wait=True, cancel_futures=False)

        for _key, child_id in children.items():
            child = jobs_repo.get(child_id)
            assert child is not None
            assert child.params.get("parent_job_id") == parent.id

    def test_parent_carries_strategy_in_params_and_map_in_result(
        self, jobs_repo: SQLiteJobRepository,
    ) -> None:
        """The synthetic parent stores ``notify_strategy`` in
        ``params`` (fixed at creation) and the ``follow_up_jobs`` map
        in ``result`` (populated by the single mark_succeeded call).
        Storing the strategy in params is what makes it visible to
        children's strategy checks before the mark_succeeded UPDATE
        lands — without a second early UPDATE that would contend with
        worker writes on the shared SQLite connection."""
        runner, _notif = self._make_runner(jobs_repo)
        parent, children = runner.submit_save_entry_pipeline(
            entry_id=42, user_id=1,
        )
        # We have to wait for the pipeline before asserting on the
        # parent's result, since mark_succeeded happens after children
        # are queued.
        self._wait_pipeline(jobs_repo, parent.id)

        parent_row = jobs_repo.get(parent.id)
        assert parent_row is not None
        assert parent_row.type == "save_entry_pipeline"
        # Strategy in params (creation-time, race-free)
        assert parent_row.params["notify_strategy"] == "compressed_all"
        assert parent_row.params["entry_id"] == 42
        # follow_up_jobs in result (populated by mark_succeeded)
        assert parent_row.status == "succeeded"
        assert parent_row.result is not None
        assert set(parent_row.result["follow_up_jobs"]) == set(children)

        runner.shutdown(wait=True, cancel_futures=False)

    def test_existing_new_entry_pipeline_unaffected(
        self, jobs_repo: SQLiteJobRepository,
    ) -> None:
        """Sanity check: an audio ingestion still uses the unchanged
        new-entry behavior — partial failure produces 1 immediate
        per-child failure push + 1 success summary."""
        notif = FakeNotificationService()
        extraction = FakeEntityExtractionService(
            single_result=_make_extraction_result(
                1, entities_created=8, mentions_created=18,
            ),
        )
        runner = JobRunner(
            job_repository=jobs_repo,
            entity_extraction_service=extraction,
            mood_backfill_callable=FakeMoodBackfill(),
            mood_scoring_service=FakeMoodScoringService(
                raise_exc=RuntimeError("LLM down"),
            ),
            entry_repository=FakeEntryRepository(),
            ingestion_service=FakeIngestionService(),
            notification_service=notif,  # type: ignore[arg-type]
        )
        recordings = [(b"audio1", "audio/webm", "rec.webm")]
        parent = runner.submit_audio_ingestion(
            recordings, "2026-04-25", user_id=1,
        )
        # Wait for parent + follow-ups
        _wait_terminal(jobs_repo, parent.id)
        parent_row = jobs_repo.get(parent.id)
        assert parent_row is not None
        for fj_id in (parent_row.result or {}).get("follow_up_jobs", {}).values():
            _wait_terminal(jobs_repo, fj_id)
        runner.shutdown(wait=True, cancel_futures=False)

        # OLD behavior preserved: per-child failure + success summary
        assert len(notif.failure_calls) == 1
        assert notif.failure_calls[0][1] == "mood_score_entry"
        assert len(notif.success_calls) == 1
        assert notif.success_calls[0][1] == "ingest_audio"
        # No pipeline-failure consolidation for new-entry flow
        assert len(notif.pipeline_failure_calls) == 0


# --------------------------------------------------------------------
# Entity reembed (description-driven embedding refresh)
# --------------------------------------------------------------------


class TestEntityReembed:
    """WU2: ``submit_entity_reembed`` queues a job that refreshes an
    entity's stored embedding by calling
    ``EntityExtractionService.reembed_entity_for_description``.
    """

    def test_submit_creates_queued_job(
        self, runner_factory, jobs_repo
    ) -> None:
        extraction = FakeEntityExtractionService(
            reembed_result={
                "entity_id": 7, "embedded": True, "dimensions": 1536,
            },
        )
        runner = runner_factory(extraction=extraction)
        job = runner.submit_entity_reembed(7, user_id=1)
        assert job.type == "entity_reembed"
        assert job.params == {"entity_id": 7, "user_id": 1}

    def test_run_calls_reembed_and_marks_succeeded(
        self, runner_factory, jobs_repo
    ) -> None:
        extraction = FakeEntityExtractionService(
            reembed_result={
                "entity_id": 7, "embedded": True, "dimensions": 1536,
            },
        )
        runner = runner_factory(extraction=extraction)
        job = runner.submit_entity_reembed(7, user_id=1)
        _wait_terminal(jobs_repo, job.id)
        runner.shutdown(wait=True, cancel_futures=False)

        final = jobs_repo.get(job.id)
        assert final is not None
        assert final.status == "succeeded"
        assert final.progress_current == 1
        assert final.progress_total == 1
        assert final.result == {
            "entity_id": 7, "embedded": True, "dimensions": 1536,
        }
        assert extraction.reembed_calls == [{"entity_id": 7, "user_id": 1}]

    def test_run_marks_failed_when_service_raises(
        self, runner_factory, jobs_repo
    ) -> None:
        extraction = FakeEntityExtractionService(
            raise_in_reembed=ValueError("Entity 99 not found for user 1"),
        )
        runner = runner_factory(extraction=extraction)
        job = runner.submit_entity_reembed(99, user_id=1)
        _wait_terminal(jobs_repo, job.id)
        runner.shutdown(wait=True, cancel_futures=False)

        final = jobs_repo.get(job.id)
        assert final is not None
        assert final.status == "failed"
        assert "not found" in (final.error_message or "")

    def test_submit_rejects_unexpected_param(self) -> None:
        # The public submit signature only accepts entity_id + user_id;
        # there is no path for stray params, so we exercise the
        # validation directly via the underlying allowed-keys map.
        from journal.services.jobs.validation import ENTITY_REEMBED_KEYS, validate_params
        with pytest.raises(ValueError, match="Unknown params"):
            validate_params(
                {"entity_id": 1, "user_id": 1, "extra": "bad"},
                ENTITY_REEMBED_KEYS,
                job_type="entity_reembed",
            )

    def test_uses_injected_entity_reembedder_when_provided(
        self, runner_factory, jobs_repo,
    ) -> None:
        """Unit 3 seam: when JobRunner is constructed with a separate
        ``entity_reembedder``, ``_run_entity_reembed`` calls into that
        instance — *not* the ``EntityExtractionService`` it also holds.
        """

        class FakeReembedder:
            def __init__(self) -> None:
                self.calls: list[dict[str, Any]] = []

            def reembed_entity_for_description(
                self, entity_id: int, *, user_id: int,
            ) -> dict[str, Any]:
                self.calls.append({"entity_id": entity_id, "user_id": user_id})
                return {"entity_id": entity_id, "embedded": True, "via": "injected"}

        extraction = FakeEntityExtractionService()
        reembedder = FakeReembedder()
        runner = runner_factory(extraction=extraction, entity_reembedder=reembedder)
        job = runner.submit_entity_reembed(11, user_id=1)
        _wait_terminal(jobs_repo, job.id)
        runner.shutdown(wait=True, cancel_futures=False)

        final = jobs_repo.get(job.id)
        assert final is not None
        assert final.status == "succeeded"
        assert final.result == {
            "entity_id": 11, "embedded": True, "via": "injected",
        }
        # The injected reembedder was used; the extraction service's
        # reembed path was *not* touched.
        assert reembedder.calls == [{"entity_id": 11, "user_id": 1}]
        assert extraction.reembed_calls == []


class TestFitnessSync:
    """W8 — ``submit_fitness_sync_strava`` / ``submit_fitness_sync_garmin``
    queue jobs that run fetch + normalize end-to-end.
    """

    def test_submit_strava_creates_queued_job(
        self, runner_factory, jobs_repo,
    ) -> None:
        from journal.services.fitness.fetch import FitnessSyncResult
        from journal.services.fitness.normalize import NormalizeResult

        fetch_calls: list[dict[str, Any]] = []
        norm_calls: list[dict[str, Any]] = []

        def fake_fetch(**kwargs: Any) -> FitnessSyncResult:
            fetch_calls.append(kwargs)
            return FitnessSyncResult(
                status="success", run_id=1, rows_fetched=1, rows_normalized=0,
            )

        def fake_norm(**kwargs: Any) -> NormalizeResult:
            norm_calls.append(kwargs)
            return NormalizeResult(
                source="strava", rows_normalized=1, drift_count=0,
            )

        runner = runner_factory(
            fetch_strava_callable=fake_fetch,
            normalize_strava_callable=fake_norm,
        )
        job = runner.submit_fitness_sync_strava(user_id=1)
        assert job.type == "fitness_sync_strava"
        assert job.params == {"user_id": 1}
        _wait_terminal(jobs_repo, job.id)
        runner.shutdown(wait=True, cancel_futures=False)

        final = jobs_repo.get(job.id)
        assert final is not None
        assert final.status == "succeeded"
        assert final.result["fetch"]["status"] == "success"
        assert final.result["normalize"]["source"] == "strava"
        assert fetch_calls == [{"user_id": 1}]
        assert norm_calls == [{"user_id": 1, "sync_run_id": 1}]

    def test_submit_garmin_creates_queued_job(
        self, runner_factory, jobs_repo,
    ) -> None:
        from journal.services.fitness.fetch import FitnessSyncResult
        from journal.services.fitness.normalize import NormalizeResult

        def fake_fetch(**_kw: Any) -> FitnessSyncResult:
            return FitnessSyncResult(
                status="success", run_id=2, rows_fetched=6, rows_normalized=0,
            )

        def fake_norm(**_kw: Any) -> NormalizeResult:
            return NormalizeResult(
                source="garmin", rows_normalized=4, drift_count=0,
            )

        runner = runner_factory(
            fetch_garmin_callable=fake_fetch,
            normalize_garmin_callable=fake_norm,
        )
        job = runner.submit_fitness_sync_garmin(user_id=1)
        assert job.type == "fitness_sync_garmin"
        _wait_terminal(jobs_repo, job.id)
        runner.shutdown(wait=True, cancel_futures=False)

        final = jobs_repo.get(job.id)
        assert final is not None
        assert final.status == "succeeded"
        assert final.result["normalize"]["rows_normalized"] == 4

    def test_submit_strava_raises_when_not_configured(
        self, runner_factory,
    ) -> None:
        runner = runner_factory()  # no fitness callables
        with pytest.raises(RuntimeError, match="not configured"):
            runner.submit_fitness_sync_strava(user_id=1)

    def test_submit_garmin_raises_when_not_configured(
        self, runner_factory,
    ) -> None:
        runner = runner_factory()
        with pytest.raises(RuntimeError, match="not configured"):
            runner.submit_fitness_sync_garmin(user_id=1)

    def test_submit_validates_user_id_type(self, runner_factory) -> None:
        from journal.services.fitness.fetch import FitnessSyncResult
        from journal.services.fitness.normalize import NormalizeResult

        runner = runner_factory(
            fetch_strava_callable=lambda **_kw: FitnessSyncResult(
                "success", 1, 0, 0,
            ),
            normalize_strava_callable=lambda **_kw: NormalizeResult(
                "strava", 0, 0,
            ),
        )
        with pytest.raises(ValueError, match="user_id"):
            runner.submit_fitness_sync_strava(user_id="42")  # type: ignore[arg-type]

    def test_submit_strava_passes_quiet_success(
        self, runner_factory, jobs_repo,
    ) -> None:
        from journal.services.fitness.fetch import FitnessSyncResult
        from journal.services.fitness.normalize import NormalizeResult

        def fake_fetch(**_kw: Any) -> FitnessSyncResult:
            return FitnessSyncResult(
                status="success", run_id=1, rows_fetched=0, rows_normalized=0,
            )

        def fake_norm(**_kw: Any) -> NormalizeResult:
            return NormalizeResult(source="strava", rows_normalized=0, drift_count=0)

        runner = runner_factory(
            fetch_strava_callable=fake_fetch,
            normalize_strava_callable=fake_norm,
        )
        job = runner.submit_fitness_sync_strava(user_id=1, quiet_success=True)
        assert job.params == {"user_id": 1, "quiet_success": True}
        runner.shutdown(wait=True, cancel_futures=False)

    def test_submit_strava_quiet_success_defaults_false(
        self, runner_factory, jobs_repo,
    ) -> None:
        from journal.services.fitness.fetch import FitnessSyncResult
        from journal.services.fitness.normalize import NormalizeResult

        def fake_fetch(**_kw: Any) -> FitnessSyncResult:
            return FitnessSyncResult(
                status="success", run_id=1, rows_fetched=0, rows_normalized=0,
            )

        def fake_norm(**_kw: Any) -> NormalizeResult:
            return NormalizeResult(source="strava", rows_normalized=0, drift_count=0)

        runner = runner_factory(
            fetch_strava_callable=fake_fetch,
            normalize_strava_callable=fake_norm,
        )
        job = runner.submit_fitness_sync_strava(user_id=1)
        assert job.params == {"user_id": 1}
        runner.shutdown(wait=True, cancel_futures=False)

    def test_submit_garmin_passes_quiet_success(
        self, runner_factory, jobs_repo,
    ) -> None:
        from journal.services.fitness.fetch import FitnessSyncResult
        from journal.services.fitness.normalize import NormalizeResult

        def fake_fetch(**_kw: Any) -> FitnessSyncResult:
            return FitnessSyncResult(
                status="success", run_id=2, rows_fetched=0, rows_normalized=0,
            )

        def fake_norm(**_kw: Any) -> NormalizeResult:
            return NormalizeResult(source="garmin", rows_normalized=0, drift_count=0)

        runner = runner_factory(
            fetch_garmin_callable=fake_fetch,
            normalize_garmin_callable=fake_norm,
        )
        job = runner.submit_fitness_sync_garmin(user_id=1, quiet_success=True)
        assert job.params == {"user_id": 1, "quiet_success": True}
        runner.shutdown(wait=True, cancel_futures=False)


class TestFitnessBackfill:
    """W5 — ``submit_fitness_backfill_strava`` / ``submit_fitness_backfill_garmin``
    queue jobs that run the existing CLI backfill orchestrator inside
    the JobRunner.
    """

    def test_submit_strava_backfill_creates_queued_job(
        self, runner_factory, jobs_repo,
    ) -> None:
        from journal.services.fitness.backfill import BackfillResult

        calls: list[dict[str, Any]] = []

        def fake_backfill(**kwargs: Any) -> BackfillResult:
            calls.append(kwargs)
            return BackfillResult(
                source="strava", final_status="complete",
                windows_attempted=1, windows_succeeded=1,
                rows_fetched=2, rows_normalized=2,
            )

        runner = runner_factory(backfill_strava_callable=fake_backfill)
        job = runner.submit_fitness_backfill_strava(
            user_id=1, start="2026-01-01", end="2026-02-01",
        )
        assert job.type == "fitness_backfill_strava"
        assert job.params == {
            "user_id": 1, "start": "2026-01-01", "end": "2026-02-01",
        }
        _wait_terminal(jobs_repo, job.id)
        runner.shutdown(wait=True, cancel_futures=False)

        final = jobs_repo.get(job.id)
        assert final is not None
        assert final.status == "succeeded"
        assert final.result["final_status"] == "complete"
        assert calls == [
            {"user_id": 1, "start": "2026-01-01", "end": "2026-02-01"},
        ]

    def test_submit_garmin_backfill_creates_queued_job(
        self, runner_factory, jobs_repo,
    ) -> None:
        from journal.services.fitness.backfill import BackfillResult

        def fake_backfill(**_kw: Any) -> BackfillResult:
            return BackfillResult(
                source="garmin", final_status="complete",
                windows_attempted=1, windows_succeeded=1,
                rows_fetched=3, rows_normalized=3,
            )

        runner = runner_factory(backfill_garmin_callable=fake_backfill)
        job = runner.submit_fitness_backfill_garmin(
            user_id=1, start="2026-01-01",
        )
        # Optional end omitted — params should not carry the key.
        assert job.type == "fitness_backfill_garmin"
        assert job.params == {"user_id": 1, "start": "2026-01-01"}
        _wait_terminal(jobs_repo, job.id)
        runner.shutdown(wait=True, cancel_futures=False)
        final = jobs_repo.get(job.id)
        assert final is not None
        assert final.status == "succeeded"

    def test_submit_strava_backfill_raises_when_not_configured(
        self, runner_factory,
    ) -> None:
        runner = runner_factory()  # no backfill callables
        with pytest.raises(RuntimeError, match="not configured"):
            runner.submit_fitness_backfill_strava(
                user_id=1, start="2026-01-01",
            )

    def test_submit_garmin_backfill_raises_when_not_configured(
        self, runner_factory,
    ) -> None:
        runner = runner_factory()
        with pytest.raises(RuntimeError, match="not configured"):
            runner.submit_fitness_backfill_garmin(
                user_id=1, start="2026-01-01",
            )

    def test_submit_backfill_validates_param_types(
        self, runner_factory,
    ) -> None:
        from journal.services.fitness.backfill import BackfillResult

        runner = runner_factory(
            backfill_strava_callable=lambda **_kw: BackfillResult(
                source="strava", final_status="complete",
                windows_attempted=0, windows_succeeded=0,
                rows_fetched=0, rows_normalized=0,
            ),
        )
        with pytest.raises(ValueError, match="user_id"):
            runner.submit_fitness_backfill_strava(
                user_id="42", start="2026-01-01",  # type: ignore[arg-type]
            )
        with pytest.raises(ValueError, match="start"):
            runner.submit_fitness_backfill_strava(
                user_id=1, start=123,  # type: ignore[arg-type]
            )
