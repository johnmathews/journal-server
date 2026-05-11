"""Tests for SQLiteJobRepository."""

import json
import sqlite3

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


class TestHasActiveJobsForEntry:
    def test_returns_running_job_for_entry(self, jobs_repo):
        job = jobs_repo.create("entity_extraction", {"entry_id": 42})
        jobs_repo.mark_running(job.id)
        active = jobs_repo.has_active_jobs_for_entry(42)
        assert len(active) == 1
        assert active[0].id == job.id

    def test_returns_queued_job_for_entry(self, jobs_repo):
        job = jobs_repo.create("entity_extraction", {"entry_id": 42})
        active = jobs_repo.has_active_jobs_for_entry(42)
        assert len(active) == 1
        assert active[0].id == job.id

    def test_ignores_succeeded_jobs(self, jobs_repo):
        job = jobs_repo.create("entity_extraction", {"entry_id": 42})
        jobs_repo.mark_running(job.id)
        jobs_repo.mark_succeeded(job.id, {"ok": True})
        assert jobs_repo.has_active_jobs_for_entry(42) == []

    def test_ignores_failed_jobs(self, jobs_repo):
        job = jobs_repo.create("entity_extraction", {"entry_id": 42})
        jobs_repo.mark_running(job.id)
        jobs_repo.mark_failed(job.id, "boom")
        assert jobs_repo.has_active_jobs_for_entry(42) == []

    def test_ignores_jobs_for_other_entries(self, jobs_repo):
        jobs_repo.create("entity_extraction", {"entry_id": 99})
        assert jobs_repo.has_active_jobs_for_entry(42) == []

    def test_returns_empty_when_no_jobs(self, jobs_repo):
        assert jobs_repo.has_active_jobs_for_entry(42) == []


class TestFindActiveFitnessFetchJob:
    """W5 — the spanning idempotency helper finds any queued/running
    sync OR backfill job for ``(user_id, source)``.
    """

    def test_returns_none_when_nothing_in_flight(self, jobs_repo):
        assert (
            jobs_repo.find_active_fitness_fetch_job(user_id=1, source="strava")
            is None
        )

    def test_finds_queued_sync_job(self, jobs_repo):
        sync = jobs_repo.create(
            "fitness_sync_strava", {"user_id": 1}, user_id=1,
        )
        found = jobs_repo.find_active_fitness_fetch_job(
            user_id=1, source="strava",
        )
        assert found is not None
        assert found.id == sync.id

    def test_finds_running_backfill_job(self, jobs_repo):
        bf = jobs_repo.create(
            "fitness_backfill_strava",
            {"user_id": 1, "start": "2026-01-01"},
            user_id=1,
        )
        jobs_repo.mark_running(bf.id)
        found = jobs_repo.find_active_fitness_fetch_job(
            user_id=1, source="strava",
        )
        assert found is not None
        assert found.id == bf.id
        assert found.status == "running"

    def test_sync_blocks_backfill_submit_check(self, jobs_repo):
        """A queued sync should appear when a backfill caller checks —
        the dedup spans both worker classes."""
        sync = jobs_repo.create(
            "fitness_sync_garmin", {"user_id": 1}, user_id=1,
        )
        jobs_repo.mark_running(sync.id)
        # Caller is about to submit a backfill — uses the same helper.
        found = jobs_repo.find_active_fitness_fetch_job(
            user_id=1, source="garmin",
        )
        assert found is not None
        assert found.id == sync.id
        assert found.type == "fitness_sync_garmin"

    def test_backfill_blocks_sync_submit_check(self, jobs_repo):
        bf = jobs_repo.create(
            "fitness_backfill_garmin",
            {"user_id": 1, "start": "2026-01-01"},
            user_id=1,
        )
        found = jobs_repo.find_active_fitness_fetch_job(
            user_id=1, source="garmin",
        )
        assert found is not None
        assert found.id == bf.id
        assert found.type == "fitness_backfill_garmin"

    def test_terminal_jobs_do_not_block(self, jobs_repo):
        sync = jobs_repo.create(
            "fitness_sync_strava", {"user_id": 1}, user_id=1,
        )
        jobs_repo.mark_succeeded(sync.id, {"ok": True})
        bf = jobs_repo.create(
            "fitness_backfill_strava",
            {"user_id": 1, "start": "2026-01-01"},
            user_id=1,
        )
        jobs_repo.mark_failed(bf.id, "boom")
        assert (
            jobs_repo.find_active_fitness_fetch_job(user_id=1, source="strava")
            is None
        )

    def test_scoped_per_user(self, jobs_repo, db_conn):
        # User 2's running sync must not block user 1. Seed user 2 first
        # to satisfy the jobs.user_id FK against users(id).
        db_conn.execute(
            "INSERT OR IGNORE INTO users (id, email, password_hash, "
            "display_name, email_verified, is_admin) "
            "VALUES (2, 'u2@example.com', 'x', 'u2', 1, 0)",
        )
        db_conn.commit()
        sync_u2 = jobs_repo.create(
            "fitness_sync_strava", {"user_id": 2}, user_id=2,
        )
        jobs_repo.mark_running(sync_u2.id)
        assert (
            jobs_repo.find_active_fitness_fetch_job(user_id=1, source="strava")
            is None
        )
        found = jobs_repo.find_active_fitness_fetch_job(
            user_id=2, source="strava",
        )
        assert found is not None
        assert found.id == sync_u2.id

    def test_scoped_per_source(self, jobs_repo):
        # A Garmin sync must not block a Strava submit check.
        jobs_repo.create(
            "fitness_sync_garmin", {"user_id": 1}, user_id=1,
        )
        assert (
            jobs_repo.find_active_fitness_fetch_job(user_id=1, source="strava")
            is None
        )

    def test_returns_oldest_when_multiple_in_flight(self, jobs_repo):
        """If two rows exist (race condition outcome), the first-enqueued
        wins per the W5 policy — ``ORDER BY created_at ASC``."""
        import time
        first = jobs_repo.create(
            "fitness_sync_strava", {"user_id": 1}, user_id=1,
        )
        # Ensure created_at moves forward (resolution may be coarse).
        time.sleep(0.01)
        jobs_repo.create(
            "fitness_backfill_strava",
            {"user_id": 1, "start": "2026-01-01"},
            user_id=1,
        )
        found = jobs_repo.find_active_fitness_fetch_job(
            user_id=1, source="strava",
        )
        assert found is not None
        assert found.id == first.id


class TestSharedConnectionCommitRace:
    """Regression tests for the shared-Connection commit race.

    Prod incident on 2026-05-11: ``mark_running`` raised
    ``sqlite3.OperationalError: cannot commit - no transaction is active``
    because another writer on the same shared ``sqlite3.Connection``
    (the entries repo / a sibling worker / the API thread) committed
    the connection's implicit transaction between this repo's
    ``UPDATE`` and ``commit()``. The Python ``sqlite3`` driver tracks
    that state on the connection object, not per-thread — so per-repo
    locks don't cut it.

    The proper fix is per-thread connections (see
    ``docs/sqlite-per-thread-connections-plan.md``). Until that lands,
    the ``mark_*`` methods tolerate the no-op commit: if another writer
    already committed our pending ``UPDATE`` as part of their
    transaction, the row is still persisted and the workflow continues.
    """

    @staticmethod
    def _racy_repo(db_conn, sql_substring):
        """Build a ``SQLiteJobRepository`` whose connection simulates
        the prod race:

        1. When ``execute()`` is called with SQL containing
           *sql_substring*, the underlying ``UPDATE`` runs against the
           real connection AND a sneak ``commit()`` fires immediately
           after — simulating another writer ending the connection's
           transaction (which also persists our pending UPDATE).
        2. The next ``commit()`` call against the proxy raises the
           exact ``OperationalError`` observed in prod:
           ``cannot commit - no transaction is active``.

        ``sqlite3.Connection`` is a C type whose methods aren't
        monkey-patchable, so we wrap it in a proxy. We must raise the
        error explicitly because Python's local sqlite3 (any version we
        run on today) silently no-ops a double-commit — the exact prod
        OperationalError is only triggered by a specific multi-thread
        interleaving on a shared Connection that's hard to coerce
        deterministically. The post-fix behaviour we care about is the
        same in both cases: tolerate the error and confirm the UPDATE
        was persisted.
        """

        class _RacyConn:
            def __init__(self, real):
                self._real = real
                self._poisoned = False
                self._trigger = sql_substring

            def execute(self, sql, *args, **kwargs):
                cursor = self._real.execute(sql, *args, **kwargs)
                if not self._poisoned and self._trigger in sql:
                    # Persist the pending write as if another thread's
                    # commit() captured it, then arm the failure for
                    # the caller's own commit().
                    self._real.commit()
                    self._poisoned = True
                return cursor

            def commit(self):
                if self._poisoned:
                    self._poisoned = False
                    raise sqlite3.OperationalError(
                        "cannot commit - no transaction is active",
                    )
                return self._real.commit()

            def __getattr__(self, name):
                return getattr(self._real, name)

        return SQLiteJobRepository(_RacyConn(db_conn))

    def test_mark_running_survives_concurrent_commit(self, jobs_repo, db_conn):
        job = jobs_repo.create("entity_extraction", {})
        racy = self._racy_repo(db_conn, "UPDATE jobs SET status = 'running'")
        racy.mark_running(job.id)
        updated = jobs_repo.get(job.id)
        assert updated is not None
        assert updated.status == "running"
        assert updated.started_at is not None

    def test_mark_succeeded_survives_concurrent_commit(self, jobs_repo, db_conn):
        job = jobs_repo.create("entity_extraction", {})
        jobs_repo.mark_running(job.id)
        racy = self._racy_repo(db_conn, "UPDATE jobs SET status = 'succeeded'")
        racy.mark_succeeded(job.id, {"scores_written": 7})
        updated = jobs_repo.get(job.id)
        assert updated is not None
        assert updated.status == "succeeded"
        assert updated.result == {"scores_written": 7}
        assert updated.finished_at is not None

    def test_mark_failed_survives_concurrent_commit(self, jobs_repo, db_conn):
        job = jobs_repo.create("entity_extraction", {})
        jobs_repo.mark_running(job.id)
        racy = self._racy_repo(db_conn, "UPDATE jobs SET status = 'failed'")
        racy.mark_failed(job.id, "boom")
        updated = jobs_repo.get(job.id)
        assert updated is not None
        assert updated.status == "failed"
        assert updated.error_message == "boom"
        assert updated.finished_at is not None

    def test_update_progress_survives_concurrent_commit(
        self, jobs_repo, db_conn,
    ):
        job = jobs_repo.create("entity_extraction", {})
        racy = self._racy_repo(db_conn, "UPDATE jobs SET progress_current")
        racy.update_progress(job.id, 4, 10)
        updated = jobs_repo.get(job.id)
        assert updated is not None
        assert updated.progress_current == 4
        assert updated.progress_total == 10

    def test_other_operational_errors_still_propagate(self, jobs_repo, db_conn):
        """The workaround must be narrow: only ``no transaction is
        active`` is tolerated. Any other ``OperationalError`` (e.g.
        ``database is locked``, ``no such table``) must still surface
        so genuine bugs aren't silently swallowed."""

        class _OtherErrorConn:
            def __init__(self, real):
                self._real = real
                self._poisoned = False

            def execute(self, sql, *args, **kwargs):
                cursor = self._real.execute(sql, *args, **kwargs)
                if "UPDATE jobs SET status = 'running'" in sql:
                    self._poisoned = True
                return cursor

            def commit(self):
                if self._poisoned:
                    self._poisoned = False
                    raise sqlite3.OperationalError("database is locked")
                return self._real.commit()

            def __getattr__(self, name):
                return getattr(self._real, name)

        job = jobs_repo.create("entity_extraction", {})
        racy = SQLiteJobRepository(_OtherErrorConn(db_conn))
        with pytest.raises(
            sqlite3.OperationalError, match="database is locked",
        ):
            racy.mark_running(job.id)
