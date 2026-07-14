from datetime import datetime
from unittest.mock import MagicMock

from journal.services.fitness.scheduler import (
    FitnessSyncScheduler,
    next_fire_after,
)


def test_next_fire_later_today():
    now = datetime(2026, 6, 14, 9, 0, 0)
    assert next_fire_after(now, hour=17) == datetime(2026, 6, 14, 17, 0, 0)


def test_next_fire_rolls_to_tomorrow_when_past_hour():
    now = datetime(2026, 6, 14, 17, 30, 0)
    assert next_fire_after(now, hour=17) == datetime(2026, 6, 15, 17, 0, 0)


def test_next_fire_exactly_on_the_hour_rolls_forward():
    now = datetime(2026, 6, 14, 17, 0, 0)
    assert next_fire_after(now, hour=17) == datetime(2026, 6, 15, 17, 0, 0)


def test_next_fire_strips_minutes_seconds_micros():
    now = datetime(2026, 6, 14, 8, 45, 13, 500)
    assert next_fire_after(now, hour=17) == datetime(2026, 6, 14, 17, 0, 0)


def _scheduler(strava_users, garmin_users):
    repo = MagicMock()
    repo.list_users_with_active_auth.side_effect = lambda *, source: (
        strava_users if source == "strava" else garmin_users
    )
    runner = MagicMock()
    sched = FitnessSyncScheduler(job_runner=runner, fitness_repo=repo)
    return sched, runner, repo


def test_run_daily_sync_enqueues_per_source():
    sched, runner, _ = _scheduler(strava_users=[1, 2], garmin_users=[1, 3])
    sched.run_daily_sync()
    runner.submit_fitness_sync_strava.assert_any_call(user_id=1, quiet_success=True)
    runner.submit_fitness_sync_strava.assert_any_call(user_id=2, quiet_success=True)
    assert runner.submit_fitness_sync_strava.call_count == 2
    runner.submit_fitness_sync_garmin.assert_any_call(user_id=1, quiet_success=True)
    runner.submit_fitness_sync_garmin.assert_any_call(user_id=3, quiet_success=True)
    assert runner.submit_fitness_sync_garmin.call_count == 2


def test_run_daily_sync_no_users_is_noop():
    sched, runner, _ = _scheduler(strava_users=[], garmin_users=[])
    sched.run_daily_sync()
    runner.submit_fitness_sync_strava.assert_not_called()
    runner.submit_fitness_sync_garmin.assert_not_called()


def test_run_daily_sync_continues_past_submit_error():
    sched, runner, _ = _scheduler(strava_users=[1, 2], garmin_users=[])
    runner.submit_fitness_sync_strava.side_effect = [RuntimeError("not wired"), MagicMock()]
    sched.run_daily_sync()  # must not raise
    assert runner.submit_fitness_sync_strava.call_count == 2


def test_run_daily_sync_continues_past_list_error():
    # If listing strava users raises, garmin must still be processed.
    repo = MagicMock()

    def _list(*, source):
        if source == "strava":
            raise RuntimeError("db blip")
        return [9]

    repo.list_users_with_active_auth.side_effect = _list
    runner = MagicMock()
    sched = FitnessSyncScheduler(job_runner=runner, fitness_repo=repo)
    sched.run_daily_sync()  # must not raise
    runner.submit_fitness_sync_garmin.assert_called_once_with(user_id=9, quiet_success=True)


def test_run_daily_sync_garmin_only_sources_skips_strava():
    """STRAVA_ENABLED=false mothball: bootstrap passes sources=("garmin",),
    and the daily loop must neither list nor submit Strava syncs."""
    repo = MagicMock()
    repo.list_users_with_active_auth.side_effect = lambda *, source: [1, 2]
    runner = MagicMock()
    sched = FitnessSyncScheduler(
        job_runner=runner, fitness_repo=repo, sources=("garmin",),
    )
    sched.run_daily_sync()
    runner.submit_fitness_sync_strava.assert_not_called()
    assert runner.submit_fitness_sync_garmin.call_count == 2
    listed = {c.kwargs["source"] for c in repo.list_users_with_active_auth.call_args_list}
    assert listed == {"garmin"}


def test_scheduler_defaults_to_both_sources():
    """Without an explicit sources override the scheduler still covers both."""
    sched, runner, _ = _scheduler(strava_users=[1], garmin_users=[1])
    sched.run_daily_sync()
    runner.submit_fitness_sync_strava.assert_called_once_with(
        user_id=1, quiet_success=True,
    )
    runner.submit_fitness_sync_garmin.assert_called_once_with(
        user_id=1, quiet_success=True,
    )


def test_start_disabled_is_noop():
    sched = FitnessSyncScheduler(job_runner=MagicMock(), fitness_repo=MagicMock(), enabled=False)
    sched.start()
    assert sched.is_running() is False
    sched.stop()  # must not raise


def test_start_then_stop_joins_thread():
    repo = MagicMock()
    repo.list_users_with_active_auth.return_value = []
    sched = FitnessSyncScheduler(job_runner=MagicMock(), fitness_repo=repo, enabled=True)
    sched.start()
    assert sched.is_running() is True
    sched.stop(timeout=5.0)
    assert sched.is_running() is False
