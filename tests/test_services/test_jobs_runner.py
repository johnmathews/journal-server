"""Tests for the JobRunner service."""

from __future__ import annotations

import contextlib
import threading
import time
from typing import TYPE_CHECKING, Any

import pytest

from journal.db.connection import get_connection
from journal.db.jobs_repository import SQLiteJobRepository
from journal.db.migrations import run_migrations
from journal.models import ExtractionResult
from journal.services.backfill import MoodBackfillResult
from journal.services.jobs import JobRunner, _friendly_error, _is_transient

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
        hold_event: threading.Event | None = None,
        entered_event: threading.Event | None = None,
    ) -> None:
        self._batch_results = batch_results or []
        self._single_result = single_result
        self._raise = raise_in_batch
        self._hold = hold_event
        self._entered = entered_event
        self.batch_calls: list[dict[str, Any]] = []
        self.single_calls: list[int] = []

    def extract_batch(
        self,
        entry_ids: list[int] | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        stale_only: bool = False,
        *,
        on_progress: Callable[[int, int], None] | None = None,
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


# --------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------


@pytest.fixture
def threadsafe_conn(tmp_path):
    """Shared SQLite connection opened for cross-thread use.

    The JobRunner's worker thread must be able to write to the
    same connection the test thread created it with — the
    production setup relies on `check_same_thread=False` plus the
    single-worker executor for safety. Tests must mirror that.
    """
    db_path = tmp_path / "jobs-runner.db"
    conn = get_connection(db_path, check_same_thread=False)
    run_migrations(conn)
    yield conn
    conn.close()


@pytest.fixture
def jobs_repo(threadsafe_conn) -> SQLiteJobRepository:
    return SQLiteJobRepository(threadsafe_conn)


@pytest.fixture
def runner_factory(jobs_repo, threadsafe_conn):
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
    ) -> JobRunner:
        runner = JobRunner(
            job_repository=jobs_repo,
            entity_extraction_service=(
                extraction or FakeEntityExtractionService()
            ),
            mood_backfill_callable=mood_backfill or FakeMoodBackfill(),
            mood_scoring_service=object(),  # type: ignore[arg-type]
            entry_repository=object(),  # type: ignore[arg-type]
        )
        created.append(runner)
        return runner

    yield _factory

    for runner in created:
        with contextlib.suppress(Exception):
            runner.shutdown(wait=True)


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
        msg = _friendly_error(exc)
        assert msg == "OCR service overloaded"

    def test_google_429_rate_limit(self) -> None:
        exc = Exception(
            "429 RESOURCE_EXHAUSTED. {'error': {'message': 'You exceeded your "
            "current quota'}}"
        )
        msg = _friendly_error(exc)
        assert msg == "Google API rate limit exceeded"

    def test_google_404_model_not_found(self) -> None:
        exc = Exception(
            "404 NOT_FOUND. {'error': {'message': 'models/gemini-99 "
            "is not found for API version v1beta'}}"
        )
        msg = _friendly_error(exc)
        assert "OCR_MODEL" in msg

    def test_unknown_error_passes_through(self) -> None:
        exc = Exception("something completely unexpected")
        assert _friendly_error(exc) == "something completely unexpected"


class TestIsTransient:
    """Tests for _is_transient — identifies retryable API errors."""

    def test_google_503(self) -> None:
        exc = Exception("503 UNAVAILABLE. high demand")
        assert _is_transient(exc) is True

    def test_google_429(self) -> None:
        exc = Exception("429 RESOURCE_EXHAUSTED. quota exceeded")
        assert _is_transient(exc) is True

    def test_not_transient(self) -> None:
        exc = Exception("404 NOT_FOUND. model not found")
        assert _is_transient(exc) is False

    def test_unknown_not_transient(self) -> None:
        exc = Exception("something unexpected")
        assert _is_transient(exc) is False


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

        runner.shutdown(wait=True)

        final = jobs_repo.get(job.id)
        assert final is not None
        assert final.status == "succeeded"
        assert final.started_at is not None
        assert final.finished_at is not None
        assert final.progress_current == 2
        assert final.progress_total == 2
        assert final.result == {
            "processed": 2,
            "entities_created": 2,
            "entities_matched": 5,
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
        runner.shutdown(wait=True)

        final = jobs_repo.get(job.id)
        assert final is not None
        assert final.status == "succeeded"
        assert final.progress_current == 1
        assert final.progress_total == 1
        assert final.result is not None
        assert final.result["processed"] == 1
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
        runner.shutdown(wait=True)

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
        # by an earlier exception.
        good = FakeEntityExtractionService(
            batch_results=[_make_extraction_result(1)]
        )
        # Replace the extraction service on the runner. This is a
        # little grubby but keeps the test focused on lifecycle.
        runner._extraction = good  # type: ignore[attr-defined]

        second = runner.submit_entity_extraction({})
        runner.shutdown(wait=True)

        final_first = jobs_repo.get(failing.id)
        final_second = jobs_repo.get(second.id)
        assert final_first is not None and final_first.status == "failed"
        assert final_second is not None
        assert final_second.status == "succeeded"


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
        count = jobs_repo._conn.execute(
            "SELECT COUNT(*) AS c FROM jobs"
        ).fetchone()["c"]
        assert count == 0

    def test_wrong_type_raises(self, runner_factory, jobs_repo):
        runner = runner_factory()
        with pytest.raises(ValueError, match="stale_only"):
            runner.submit_entity_extraction({"stale_only": "yes"})

        count = jobs_repo._conn.execute(
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

        count = jobs_repo._conn.execute(
            "SELECT COUNT(*) AS c FROM jobs"
        ).fetchone()["c"]
        assert count == 0

    def test_missing_mode_raises(self, runner_factory, jobs_repo):
        runner = runner_factory()
        with pytest.raises(ValueError, match="mode"):
            runner.submit_mood_backfill({})

        count = jobs_repo._conn.execute(
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
        runner = runner_factory(extraction=first_fake)

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
        )
        self.ingest_image_calls: list[tuple[bytes, str, str]] = []
        self.multi_page_calls: list[int] = []
        self.multi_voice_calls: list[int] = []
        self.reprocess_calls: list[int] = []

    def ingest_image(
        self, image_data: bytes, media_type: str, date: str
    ) -> Any:
        self.ingest_image_calls.append((image_data, media_type, date))
        return self._entry

    def ingest_multi_page_entry(
        self,
        images: list[tuple[bytes, str]],
        date: str,
        *,
        on_progress: Callable[[int, int], None] | None = None,
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
        on_progress: Callable[[int, int], None] | None = None,
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
        runner = runner_factory()
        runner._ingestion = ingestion  # type: ignore[attr-defined]

        images = [(b"img1", "image/jpeg", "page1.jpg")]
        job = runner.submit_image_ingestion(images, "2026-04-13")
        runner.shutdown(wait=True)

        final = jobs_repo.get(job.id)
        assert final is not None
        assert final.status == "succeeded"
        assert final.progress_total == 1  # 1 page, NOT 2
        assert final.progress_current == 1

    def test_multi_image_progress_total_equals_page_count(
        self, runner_factory, jobs_repo
    ):
        ingestion = FakeIngestionService()
        runner = runner_factory()
        runner._ingestion = ingestion  # type: ignore[attr-defined]

        images = [
            (b"img1", "image/jpeg", "page1.jpg"),
            (b"img2", "image/jpeg", "page2.jpg"),
            (b"img3", "image/jpeg", "page3.jpg"),
        ]
        job = runner.submit_image_ingestion(images, "2026-04-13")
        runner.shutdown(wait=True)

        final = jobs_repo.get(job.id)
        assert final is not None
        assert final.status == "succeeded"
        assert final.progress_total == 3  # 3 pages, NOT 4
        assert final.progress_current == 3
        assert final.result == {"entry_id": 1}

    def test_progress_current_never_exceeds_total(
        self, runner_factory, jobs_repo
    ):
        """Verify every progress update has current <= total."""
        ingestion = FakeIngestionService()
        runner = runner_factory()
        runner._ingestion = ingestion  # type: ignore[attr-defined]

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
        runner.shutdown(wait=True)

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
# Audio ingestion
# --------------------------------------------------------------------


class TestAudioIngestion:
    """Tests for submit_audio_ingestion and _run_audio_ingestion."""

    def test_single_recording_succeeds(self, runner_factory, jobs_repo):
        ingestion = FakeIngestionService()
        runner = runner_factory()
        runner._ingestion = ingestion

        recordings = [(b"audio1", "audio/webm", "rec1.webm")]
        job = runner.submit_audio_ingestion(recordings, "2026-04-14")
        runner.shutdown(wait=True)

        final = jobs_repo.get(job.id)
        assert final is not None
        assert final.status == "succeeded"
        assert final.result == {"entry_id": 1}

    def test_multiple_recordings_succeeds(self, runner_factory, jobs_repo):
        ingestion = FakeIngestionService()
        runner = runner_factory()
        runner._ingestion = ingestion

        recordings = [
            (b"audio1", "audio/webm", "rec1.webm"),
            (b"audio2", "audio/webm", "rec2.webm"),
        ]
        job = runner.submit_audio_ingestion(recordings, "2026-04-14")
        runner.shutdown(wait=True)

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
        runner = runner_factory()
        runner._ingestion = ingestion

        recordings = [(b"audio1", "audio/webm", "rec.webm")]
        job = runner.submit_audio_ingestion(recordings, "2026-04-14")
        assert job.type == "ingest_audio"
        runner.shutdown(wait=True)

    def test_recording_count_in_params(self, runner_factory, jobs_repo):
        ingestion = FakeIngestionService()
        runner = runner_factory()
        runner._ingestion = ingestion

        recordings = [
            (b"a1", "audio/webm", "r1.webm"),
            (b"a2", "audio/mp3", "r2.mp3"),
        ]
        job = runner.submit_audio_ingestion(recordings, "2026-04-14")
        assert job.params["recording_count"] == 2
        runner.shutdown(wait=True)

    def test_no_ingestion_service_fails(self, runner_factory, jobs_repo):
        runner = runner_factory()
        runner._ingestion = None

        recordings = [(b"audio1", "audio/webm", "rec.webm")]
        job = runner.submit_audio_ingestion(recordings, "2026-04-14")
        runner.shutdown(wait=True)

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
        runner = runner_factory()
        runner._ingestion = ingestion  # type: ignore[attr-defined]

        job = runner.submit_reprocess_embeddings(42)
        runner.shutdown(wait=True)

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
        runner = runner_factory()
        # runner._ingestion is None by default (not set on fixture)

        job = runner.submit_reprocess_embeddings(1)
        runner.shutdown(wait=True)

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
        runner.shutdown(wait=True)
        with pytest.raises(RuntimeError):
            runner.submit_entity_extraction({})
