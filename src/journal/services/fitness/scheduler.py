"""Daily fitness auto-sync scheduler.

A daemon thread (modeled on :class:`journal.services.health_poll.HealthPoller`)
that wakes once per day at a fixed local hour (default 17:00) and enqueues an
incremental sync for every user with working credentials per source. See
``docs/superpowers/specs/2026-06-14-daily-fitness-auto-sync-design.md``.
"""

from __future__ import annotations

from datetime import datetime, timedelta


def next_fire_after(now: datetime, *, hour: int) -> datetime:
    """Next occurrence of ``hour``:00:00 strictly after ``now``.

    If ``now`` is at or past today's fire time, returns tomorrow's. Naive
    datetimes throughout — the server runs in UTC (Docker), so "local" is
    UTC there; tests pass naive datetimes directly.
    """
    candidate = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate
