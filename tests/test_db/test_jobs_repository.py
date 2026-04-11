"""Tests for SQLiteJobRepository."""

import json

import pytest

from journal.db.jobs_repository import SQLiteJobRepository
from journal.models import Job


@pytest.fixture
def jobs_repo(db_conn):
    return SQLiteJobRepository(db_conn)


class TestCreate:
    def test_create_inserts_queued_row(self, jobs_repo):
        job = jobs_repo.create("entity_extraction", {"entry_ids": [1, 2, 3]})
        assert isinstance(job, Job)
        assert job.id  # non-empty UUID
        assert job.type == "entity_extraction"
        assert job.status == "queued"
        assert job.params == {"entry_ids": [1, 2, 3]}
        assert job.progress_current == 0
        assert job.progress_total == 0
        assert job.result is None
        assert job.error_message is None
        assert job.created_at
        assert job.started_at is None
        assert job.finished_at is None

    def test_create_generates_unique_ids(self, jobs_repo):
        job1 = jobs_repo.create("mood_backfill", {})
        job2 = jobs_repo.create("mood_backfill", {})
        assert job1.id != job2.id


class TestMarkRunning:
    def test_mark_running_transitions_status(self, jobs_repo):
        job = jobs_repo.create("entity_extraction", {})
        jobs_repo.mark_running(job.id)
        updated = jobs_repo.get(job.id)
        assert updated is not None
        assert updated.status == "running"
        assert updated.started_at is not None
        assert updated.finished_at is None


class TestUpdateProgress:
    def test_update_progress_sets_columns(self, jobs_repo):
        job = jobs_repo.create("entity_extraction", {})
        jobs_repo.update_progress(job.id, 3, 10)
        updated = jobs_repo.get(job.id)
        assert updated is not None
        assert updated.progress_current == 3
        assert updated.progress_total == 10

    def test_update_progress_overwrites(self, jobs_repo):
        job = jobs_repo.create("entity_extraction", {})
        jobs_repo.update_progress(job.id, 1, 10)
        jobs_repo.update_progress(job.id, 7, 10)
        updated = jobs_repo.get(job.id)
        assert updated is not None
        assert updated.progress_current == 7
        assert updated.progress_total == 10


class TestMarkSucceeded:
    def test_mark_succeeded_stores_result(self, jobs_repo):
        job = jobs_repo.create("entity_extraction", {})
        jobs_repo.mark_running(job.id)
        result = {"entities_created": 5, "mentions_created": 12}
        jobs_repo.mark_succeeded(job.id, result)
        updated = jobs_repo.get(job.id)
        assert updated is not None
        assert updated.status == "succeeded"
        assert updated.result == result
        assert updated.finished_at is not None
        assert updated.error_message is None


class TestMarkFailed:
    def test_mark_failed_stores_error(self, jobs_repo):
        job = jobs_repo.create("entity_extraction", {})
        jobs_repo.mark_running(job.id)
        jobs_repo.mark_failed(job.id, "something broke")
        updated = jobs_repo.get(job.id)
        assert updated is not None
        assert updated.status == "failed"
        assert updated.error_message == "something broke"
        assert updated.finished_at is not None
        assert updated.result is None


class TestGet:
    def test_get_missing_returns_none(self, jobs_repo):
        assert jobs_repo.get("does-not-exist") is None


class TestReconcileStuckJobs:
    def test_reconcile_updates_non_terminal_rows(self, jobs_repo, db_conn):
        running1 = jobs_repo.create("entity_extraction", {})
        jobs_repo.mark_running(running1.id)
        running2 = jobs_repo.create("mood_backfill", {})
        jobs_repo.mark_running(running2.id)
        queued = jobs_repo.create("entity_extraction", {})
        succeeded = jobs_repo.create("mood_backfill", {})
        jobs_repo.mark_running(succeeded.id)
        jobs_repo.mark_succeeded(succeeded.id, {"scored": 3})

        touched = jobs_repo.reconcile_stuck_jobs()
        assert touched == 3

        for job_id in (running1.id, running2.id, queued.id):
            row = jobs_repo.get(job_id)
            assert row is not None
            assert row.status == "failed"
            assert row.error_message == "server restarted before job completed"
            assert row.finished_at is not None

        untouched = jobs_repo.get(succeeded.id)
        assert untouched is not None
        assert untouched.status == "succeeded"
        assert untouched.error_message is None

    def test_reconcile_no_stuck_jobs_returns_zero(self, jobs_repo):
        assert jobs_repo.reconcile_stuck_jobs() == 0


class TestJsonRoundTrip:
    def test_params_round_trip_with_nested_dict(self, jobs_repo, db_conn):
        nested = {
            "entry_ids": [1, 2, 3],
            "options": {"force": True, "limit": 50, "tags": ["a", "b"]},
            "note": "round-trip",
        }
        job = jobs_repo.create("entity_extraction", nested)
        fetched = jobs_repo.get(job.id)
        assert fetched is not None
        assert fetched.params == nested

        # Also verify the on-disk JSON deserialises to the same structure.
        row = db_conn.execute(
            "SELECT params_json FROM jobs WHERE id = ?", (job.id,)
        ).fetchone()
        assert json.loads(row["params_json"]) == nested

    def test_result_round_trip_with_nested_dict(self, jobs_repo):
        job = jobs_repo.create("mood_backfill", {})
        jobs_repo.mark_running(job.id)
        result = {
            "processed": 7,
            "per_dimension": {"valence": 7, "arousal": 7},
            "warnings": ["one", "two"],
        }
        jobs_repo.mark_succeeded(job.id, result)
        fetched = jobs_repo.get(job.id)
        assert fetched is not None
        assert fetched.result == result
