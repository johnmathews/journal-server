"""Fitness backfill — historical pagination through Strava + Garmin in 30-day windows.

Per W13 of ``docs/fitness-tier-plan.md``. The backfill is the *first
live exercise* of the fitness pipeline against real credentials: it
walks from a configurable ``start`` date (default ``2026-01-01``) up
to ``end`` (default today) one window at a time, asking the existing
W6 fetch service to handle each window, then triggering the W7
normalize service to project freshly persisted raw rows into the
normalized layer.

Reusing :class:`StravaFetchService` / :class:`GarminFetchService` per
window means we get the W6 state machine for free — the single-run
guard, auth-broken classification, transient-failure recording, and
``fitness_sync_runs`` audit trail are all unchanged. Backfill is just
"call ``run_sync`` once per window with explicit ``since`` / ``until``
overrides," plus a thin orchestration layer:

1. **Resume predicate (per source).** Before generating windows, ask
   the repository for ``MAX(local_date)`` over normalized rows for
   the source. The predicate is per-source — a single global
   watermark would silently skip Garmin days if Strava had progressed
   further. For Garmin, the resume point is the *minimum* of the
   activities and daily watermarks so neither stream falls behind.
2. **Single-run guard = fail loud.** If a window's ``run_sync``
   short-circuits with ``status="running"`` (a routine sync is
   already in flight), the orchestrator raises
   :class:`BackfillBlocked` so the operator gets a clear failure
   rather than silent serialisation. Backfill is operator-driven; a
   surprise wait queue is the wrong default.
3. **Transient streak abort.** Each window that returns
   ``transient_failure`` increments a streak counter; ``success``
   resets it. On ``streak == transient_streak_limit`` (default 3 —
   matches W6's ``fitness_transient_failure_threshold``), backfill
   bails out with ``final_status="aborted_transient"`` so the
   operator can investigate before the streak cliffs into bigger
   problems. No retry within a window: stays consistent with the
   fetch service's "one window, one attempt" posture.
4. **Auth-broken short-circuit.** The first window that returns
   ``auth_broken`` ends the backfill with
   ``final_status="aborted_auth"``. The fetch service has already
   recorded the run, transitioned auth state, and (possibly) fired
   the once-per-transition Pushover. The operator runs
   ``journal fitness-reauth-{strava,garmin}`` and re-invokes
   backfill; the resume predicate picks up where the broken run
   stopped writing.
5. **Incremental normalize.** After each successful fetch window,
   call :func:`normalize_strava` / :func:`normalize_garmin` so the
   resume watermark on the next run reflects the rows just
   persisted. Normalize is idempotent (``INSERT OR REPLACE``), so
   re-running mid-window after a crash never produces duplicates.

CLI-driven: invoked from ``cmd_fitness_backfill`` in
:mod:`journal.cli.fitness`. Like ``fitness-sync`` (W11), backfill
runs synchronously on the calling thread and does not construct a
``JobRunner`` — same rationale (CLI lifetimes are short; the
``ThreadPoolExecutor`` shutdown hazard documented in W8/W11 is
sidestepped entirely).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Literal

from journal.services.fitness.errors import FitnessError
from journal.services.fitness.normalize import (
    normalize_garmin,
    normalize_strava,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from journal.db.fitness_repository import FitnessRepository
    from journal.services.fitness.fetch import (
        GarminFetchService,
        StravaFetchService,
    )
    from journal.services.fitness.normalize import NormalizeDriftNotifier


log = logging.getLogger(__name__)


DEFAULT_START_DATE = "2026-01-01"
DEFAULT_WINDOW_DAYS = 30
DEFAULT_TRANSIENT_STREAK_LIMIT = 3


BackfillStatus = Literal[
    "complete",
    "aborted_auth",
    "aborted_transient",
    "no_windows",
]


class BackfillBlocked(FitnessError):  # noqa: N818  named per W13 plan; matches W6's FitnessNormalizeDrift convention
    """Raised when a routine sync is in flight and backfill cannot proceed.

    The W6 single-run guard returns ``status="running"`` rather than
    raising; backfill upgrades that to a hard failure so the operator
    sees a clear "another sync is in flight" message instead of
    silently waiting or producing partial results.
    """


@dataclass(frozen=True)
class BackfillResult:
    """Outcome of one ``backfill_*`` invocation.

    ``windows_attempted`` counts every window the loop entered;
    ``windows_succeeded`` is the subset that returned ``success``
    from the fetch service. Transient and auth-broken windows are
    counted as attempts, not successes. ``rows_fetched`` /
    ``rows_normalized`` aggregate across the successful windows
    only — partial state from a run aborted mid-stream is reflected
    here exactly the way it landed in the DB.
    """

    source: Literal["strava", "garmin"]
    final_status: BackfillStatus
    windows_attempted: int
    windows_succeeded: int
    rows_fetched: int
    rows_normalized: int
    aborted_reason: str | None = None


def backfill_strava(
    *,
    user_id: int,
    repo: FitnessRepository,
    fetch_service: StravaFetchService,
    notifier: NormalizeDriftNotifier | None = None,
    start: str = DEFAULT_START_DATE,
    end: str | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
    transient_streak_limit: int = DEFAULT_TRANSIENT_STREAK_LIMIT,
    clock: Callable[[], datetime] | None = None,
) -> BackfillResult:
    """Backfill Strava activities from ``start`` to ``end`` in 30-day windows.

    Resume predicate is ``MAX(local_date) FROM fitness_activities WHERE
    user_id=:uid AND source='strava'``. If that watermark is later
    than the supplied ``start``, the effective start is shifted to
    one day after the watermark.
    """
    end_date = _resolve_end(end, clock)
    resume = repo.max_normalized_local_date(
        source="strava", user_id=user_id, kind="activities",
    )
    effective_start = _effective_start(start, resume)
    if effective_start > end_date:
        return BackfillResult(
            source="strava", final_status="no_windows",
            windows_attempted=0, windows_succeeded=0,
            rows_fetched=0, rows_normalized=0,
        )

    windows = list(_generate_windows(effective_start, end_date, window_days))
    return _run_windows(
        source="strava",
        windows=windows,
        user_id=user_id,
        repo=repo,
        run_sync=fetch_service.run_sync,
        normalize=lambda: normalize_strava(
            repo, user_id=user_id, notifier=notifier,
        ),
        transient_streak_limit=transient_streak_limit,
    )


def backfill_garmin(
    *,
    user_id: int,
    repo: FitnessRepository,
    fetch_service: GarminFetchService,
    notifier: NormalizeDriftNotifier | None = None,
    start: str = DEFAULT_START_DATE,
    end: str | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
    transient_streak_limit: int = DEFAULT_TRANSIENT_STREAK_LIMIT,
    clock: Callable[[], datetime] | None = None,
) -> BackfillResult:
    """Backfill Garmin activities + daily wellness from ``start`` to ``end``.

    Garmin contributes two parallel streams (per-activity rows in
    ``fitness_activities`` and per-day rollups in ``fitness_daily``).
    The resume predicate is the *minimum* of their watermarks so a
    streak of activity-less days doesn't make the daily stream skip
    days where activities lag, or vice versa.
    """
    end_date = _resolve_end(end, clock)
    activities_max = repo.max_normalized_local_date(
        source="garmin", user_id=user_id, kind="activities",
    )
    daily_max = repo.max_normalized_local_date(
        source="garmin", user_id=user_id, kind="daily",
    )
    resume = _min_watermark(activities_max, daily_max)
    effective_start = _effective_start(start, resume)
    if effective_start > end_date:
        return BackfillResult(
            source="garmin", final_status="no_windows",
            windows_attempted=0, windows_succeeded=0,
            rows_fetched=0, rows_normalized=0,
        )

    windows = list(_generate_windows(effective_start, end_date, window_days))
    return _run_windows(
        source="garmin",
        windows=windows,
        user_id=user_id,
        repo=repo,
        run_sync=fetch_service.run_sync,
        normalize=lambda: normalize_garmin(
            repo, user_id=user_id, notifier=notifier,
        ),
        transient_streak_limit=transient_streak_limit,
    )


# ── Internals ───────────────────────────────────────────────────────


def _run_windows(
    *,
    source: Literal["strava", "garmin"],
    windows: list[tuple[datetime, datetime]],
    user_id: int,
    repo: FitnessRepository,  # noqa: ARG001 — kept for symmetry / future use
    run_sync: Callable[..., object],
    normalize: Callable[[], object],
    transient_streak_limit: int,
) -> BackfillResult:
    """Drive one window-loop. Shared by Strava + Garmin to avoid drift."""
    if not windows:
        return BackfillResult(
            source=source, final_status="no_windows",
            windows_attempted=0, windows_succeeded=0,
            rows_fetched=0, rows_normalized=0,
        )

    streak = 0
    rows_fetched = 0
    rows_normalized = 0
    succeeded = 0
    attempted = 0

    for window_start, window_end in windows:
        attempted += 1
        result = run_sync(user_id=user_id, since=window_start, until=window_end)
        status = result.status

        if status == "running":
            raise BackfillBlocked(
                f"{source} routine sync in flight (run_id={result.run_id}); "
                "backfill aborted. Wait for the in-flight run to finish, then re-run.",
            )

        if status == "auth_broken":
            log.warning(
                "Backfill %s: window %s..%s returned auth_broken (run_id=%d) — aborting",
                source, window_start.date(), window_end.date(), result.run_id,
            )
            return BackfillResult(
                source=source, final_status="aborted_auth",
                windows_attempted=attempted, windows_succeeded=succeeded,
                rows_fetched=rows_fetched, rows_normalized=rows_normalized,
                aborted_reason=(
                    f"auth_broken on window {window_start.date()}..{window_end.date()} "
                    f"(run_id={result.run_id}); run "
                    f"`journal fitness-reauth-{source}` and retry"
                ),
            )

        if status == "transient_failure":
            streak += 1
            log.warning(
                "Backfill %s: window %s..%s transient_failure "
                "(run_id=%d, streak=%d/%d)",
                source, window_start.date(), window_end.date(),
                result.run_id, streak, transient_streak_limit,
            )
            if streak >= transient_streak_limit:
                return BackfillResult(
                    source=source, final_status="aborted_transient",
                    windows_attempted=attempted, windows_succeeded=succeeded,
                    rows_fetched=rows_fetched, rows_normalized=rows_normalized,
                    aborted_reason=(
                        f"{streak} consecutive transient failures "
                        f"(limit={transient_streak_limit}); resume by re-running"
                    ),
                )
            continue

        # success
        streak = 0
        rows_fetched += result.rows_fetched
        succeeded += 1
        norm = normalize()
        rows_normalized += norm.rows_normalized

    return BackfillResult(
        source=source, final_status="complete",
        windows_attempted=attempted, windows_succeeded=succeeded,
        rows_fetched=rows_fetched, rows_normalized=rows_normalized,
    )


def _resolve_end(
    end: str | None, clock: Callable[[], datetime] | None,
) -> date:
    if end is not None:
        return _parse_iso_date(end)
    now_fn = clock or (lambda: datetime.now(UTC))
    return now_fn().astimezone(UTC).date()


def _effective_start(start: str, resume: str | None) -> date:
    """Pick the larger of ``start`` and ``resume + 1 day``.

    ``resume`` is the largest ``local_date`` already normalized — re-fetching
    that day is harmless (raw is INSERT OR IGNORE on payload sha; normalized
    is upsert) but wasteful, so we step past it.
    """
    base = _parse_iso_date(start)
    if resume is None:
        return base
    candidate = _parse_iso_date(resume) + timedelta(days=1)
    return max(base, candidate)


def _min_watermark(a: str | None, b: str | None) -> str | None:
    """Return the earlier of two ``YYYY-MM-DD`` strings, ignoring None."""
    if a is None:
        return b
    if b is None:
        return a
    return a if a <= b else b


def _generate_windows(
    start: date, end: date, window_days: int,
) -> Iterable[tuple[datetime, datetime]]:
    """Yield ``[since, until]`` UTC datetimes covering ``[start, end]``.

    Each window is at most ``window_days`` long. The final window's
    ``until`` is clamped to ``end`` (end-of-day UTC) so the loop never
    overshoots. ``since`` is start-of-day UTC.
    """
    if window_days <= 0:
        raise ValueError(f"window_days must be positive, got {window_days}")
    cur = start
    while cur <= end:
        window_end = min(cur + timedelta(days=window_days - 1), end)
        yield (
            datetime.combine(cur, datetime.min.time(), tzinfo=UTC),
            datetime.combine(
                window_end,
                datetime.min.time().replace(hour=23, minute=59, second=59),
                tzinfo=UTC,
            ),
        )
        cur = window_end + timedelta(days=1)


def _parse_iso_date(value: str) -> date:
    """Parse a ``YYYY-MM-DD`` string into a ``date``."""
    return date.fromisoformat(value)
