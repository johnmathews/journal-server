"""Daily fitness auto-sync scheduler.

A daemon thread (modeled on :class:`journal.services.health_poll.HealthPoller`)
that wakes once per day at a fixed local hour (default 17:00) and enqueues an
incremental sync for every user with working credentials per source. See
``docs/superpowers/specs/2026-06-14-daily-fitness-auto-sync-design.md``.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from journal.db.fitness_repository import FitnessRepository
    from journal.services.jobs import JobRunner

log = logging.getLogger(__name__)

_SOURCES = ("strava", "garmin")
_DEFAULT_HOUR = 17
_POLL_SLICE = 60  # seconds; max latency for stop() to take effect


def next_fire_after(now: datetime, *, hour: int) -> datetime:
    """Next occurrence of ``hour``:00:00 strictly after ``now``.

    If ``now`` is at or past today's fire time, returns tomorrow's. Naive
    datetimes throughout — ``hour`` is interpreted in the container's local
    timezone (``datetime.now()``). The prod ``media`` VM inherits the host
    TZ, which is CEST/UTC+2 as of 2026-06-14, so 17:00 local = 5pm CEST
    (15:00 UTC), NOT 17:00 UTC. Tests pass naive datetimes directly.
    """
    candidate = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


class FitnessSyncScheduler:
    """Daemon thread that enqueues a daily per-user fitness sync.

    Mirrors :class:`HealthPoller`'s lifecycle (``start``/``stop``/
    ``is_running``). Each day at ``hour``:00 local it asks the repo which
    users have active auth per source and submits incremental syncs via
    the JobRunner with ``quiet_success=True``. ``enabled=False`` makes
    ``start()`` a no-op (used by tests and the FITNESS_SYNC_ENABLED gate).

    ``sources`` narrows which providers the daily loop touches — the
    bootstrap passes ``("garmin",)`` when Strava is mothballed
    (``STRAVA_ENABLED=false``, roadmap D8) so the loop never lists or
    submits Strava work on a Strava-less server.
    """

    def __init__(
        self,
        *,
        job_runner: JobRunner,
        fitness_repo: FitnessRepository,
        hour: int = _DEFAULT_HOUR,
        enabled: bool = True,
        sources: tuple[str, ...] = _SOURCES,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._runner = job_runner
        self._repo = fitness_repo
        self._hour = hour
        self._enabled = enabled
        self._sources = sources
        self._clock: Callable[[], datetime] = clock or datetime.now
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def run_daily_sync(self) -> None:
        """Enqueue syncs for all users with active auth, per source.

        One bad submit (e.g. a RuntimeError because Strava isn't wired,
        or a transient JobRunner error) is logged and skipped so the rest
        of the run still happens.
        """
        for source in self._sources:
            submit = (
                self._runner.submit_fitness_sync_strava
                if source == "strava"
                else self._runner.submit_fitness_sync_garmin
            )
            try:
                user_ids = self._repo.list_users_with_active_auth(source=source)
            except Exception:  # noqa: BLE001 — never let one source abort the run
                log.exception("daily fitness sync: failed to list %s users", source)
                continue
            enqueued = 0
            for user_id in user_ids:
                try:
                    submit(user_id=user_id, quiet_success=True)
                    enqueued += 1
                except Exception:  # noqa: BLE001 — skip one user, keep going
                    log.exception(
                        "daily fitness sync: %s submit failed for user %d",
                        source,
                        user_id,
                    )
            log.info("daily fitness sync: %s=%d enqueued", source, enqueued)

    def start(self) -> None:
        """Start the daemon thread (no-op if disabled)."""
        if not self._enabled:
            log.info("Fitness sync scheduler disabled (FITNESS_SYNC_ENABLED=false)")
            return
        self._thread = threading.Thread(
            target=self._run,
            name="fitness-sync-scheduler",
            daemon=True,
        )
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        """Signal stop and join. Idempotent; safe before start()."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def is_running(self) -> bool:
        """True iff the scheduler thread is alive (started, not yet joined)."""
        return self._thread is not None and self._thread.is_alive()

    def _run(self) -> None:
        log.info(
            "Fitness sync scheduler started (fires daily at %02d:00)", self._hour
        )
        next_fire = next_fire_after(self._clock(), hour=self._hour)
        while not self._stop_event.is_set():
            now = self._clock()
            if now >= next_fire:
                try:
                    self.run_daily_sync()
                except Exception:  # noqa: BLE001 — keep the thread alive to next day
                    log.exception("daily fitness sync run failed")
                next_fire = next_fire_after(self._clock(), hour=self._hour)
            # Sleep in <=60s slices so stop() takes effect promptly even
            # though the next fire may be ~24h away. No shutdown log here:
            # stop() runs from an atexit hook (see HealthPoller for the
            # closed-stream rationale).
            remaining = (next_fire - self._clock()).total_seconds()
            self._stop_event.wait(min(_POLL_SLICE, max(0.0, remaining)))
