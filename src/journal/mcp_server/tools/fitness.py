"""Fitness MCP tools — read, operate, and correlate.

Master plan D6: every meaningful query and operational lever is also
reachable as an MCP tool. This module is the MCP twin of
``api/fitness.py`` (read routes) and the ``POST /api/fitness/sync/{source}``
companion in ``api/ingestion.py`` (job creation), plus three correlation
queries that are MCP-only because they're cross-table joins the REST
surface doesn't expose today.

All tools return JSON-serialisable dicts/lists — these are
LLM-consumed, and structured payloads are easier to reason about than
formatted text for tabular fitness data.

The correlation queries (Q1/Q2/Q3) are reproduced verbatim from
``docs/fitness-schema.md`` §8. Any change to those queries should be
made there first, and copied here — the schema doc is the source of
truth (the queries were the schema's acceptance test, so changing them
in code means changing the schema's contract).
"""

import logging
from dataclasses import asdict
from typing import Any

from mcp.server.fastmcp import Context

from journal.api.fitness import (
    _activity_to_dict,
    _daily_to_dict,
    _per_source_status,
)
from journal.db.fitness_integrity import check_fitness_integrity
from journal.mcp_server.app import mcp
from journal.mcp_server.tools._ctx import (
    _get_db_conn,
    _get_fitness_repo,
    _get_job_repository,
    _get_job_runner,
    _user_id,
)

log = logging.getLogger(__name__)

_VALID_SOURCES = ("strava", "garmin")

# W1 strava-mothball (roadmap D8, Strava API paywall 2026-06-30): the
# write/trigger tools refuse Strava unless STRAVA_ENABLED is true. Read
# tools — including fitness_sync_status — are flag-independent: the
# status payload keeps both source keys (webapp contract) and historical
# Strava rows stay queryable.
_STRAVA_DISABLED_ERROR = "Strava integration is disabled on this server"


def _strava_enabled(ctx: Context) -> bool:
    """True iff the STRAVA_ENABLED mothball flag is on (fail-closed)."""
    config = ctx.request_context.lifespan_context.get("config")
    return bool(getattr(config, "strava_enabled", False))


# ── Read tools ─────────────────────────────────────────────────────


@mcp.tool()
def fitness_list_activities(
    start: str,
    end: str,
    activity_type: str | None = None,
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """List fitness activities in a date window.

    Returns the same shape as ``GET /api/fitness/activities`` so a
    caller can use either entry point interchangeably.

    Args:
        start: Inclusive start date (ISO 8601, e.g. ``"2026-05-01"``).
        end: Inclusive end date (ISO 8601).
        activity_type: Optional filter — one of ``"run"``, ``"ride"``,
            ``"swim"``, ``"walk"``, ``"hike"``, ``"strength"``,
            ``"other"``. Omit to return all types.

    Returns:
        ``{"items": [...]}`` with one dict per activity. Empty list
        is a valid response — out-of-range dates are not an error.
    """
    log.info(
        "Tool call: fitness_list_activities(start=%s, end=%s, type=%s)",
        start, end, activity_type,
    )
    repo = _get_fitness_repo(ctx)
    user_id = _user_id(ctx)
    activities = repo.list_activities(
        user_id=user_id, start=start, end=end, activity_type=activity_type,
    )
    return {"items": [_activity_to_dict(a) for a in activities]}


@mcp.tool()
def fitness_list_daily(
    start: str,
    end: str,
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """List daily fitness rollup metrics in a date window.

    Returns the same shape as ``GET /api/fitness/daily``. Daily rows
    are recovery + training-state metrics (sleep, HRV, body battery,
    training load / readiness, resting HR, stress).

    Args:
        start: Inclusive start date (ISO 8601).
        end: Inclusive end date (ISO 8601).

    Returns:
        ``{"items": [...]}`` with one dict per day that has data.
    """
    log.info("Tool call: fitness_list_daily(start=%s, end=%s)", start, end)
    repo = _get_fitness_repo(ctx)
    user_id = _user_id(ctx)
    daily = repo.list_daily(user_id=user_id, start=start, end=end)
    return {"items": [_daily_to_dict(d) for d in daily]}


@mcp.tool()
def fitness_sync_status(
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Return the per-source sync status snapshot.

    Mirrors ``GET /api/fitness/sync/status`` exactly: each of
    ``strava`` / ``garmin`` is either ``null`` (no auth state, never
    synced) or a dict with ``auth_status``, ``auth_broken_since``,
    ``last_success_at``, and the last 10 sync runs.
    """
    log.info("Tool call: fitness_sync_status()")
    repo = _get_fitness_repo(ctx)
    user_id = _user_id(ctx)
    return {
        source: _per_source_status(repo, user_id=user_id, source=source)
        for source in _VALID_SOURCES
    }


@mcp.tool()
def fitness_integrity_check(
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Run the soft-pointer integrity check across normalized rows.

    Returns the orphan report — normalized activity/daily rows whose
    ``raw_ref_id`` (or any id in ``raw_ref_ids_json``) does not
    resolve into the matching per-source raw table. Mirrors
    ``GET /api/fitness/integrity``. Empty arrays mean a clean DB.

    Per-user scoped (W4 of the fitness multi-user plan): only orphans
    owned by the calling user are returned.
    """
    log.info("Tool call: fitness_integrity_check()")
    conn = _get_db_conn(ctx)
    user_id = _user_id(ctx)
    report = check_fitness_integrity(conn, user_id=user_id)
    return {
        "activities": [asdict(o) for o in report.activities],
        "daily": [asdict(o) for o in report.daily],
    }


# ── Operational tools ──────────────────────────────────────────────


@mcp.tool()
def fitness_trigger_sync(
    source: str,
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Submit a fitness fetch+normalize job for the given source.

    Mirrors ``POST /api/fitness/sync/{source}`` including the W5
    spanning dedup posture: if **any** in-flight fetch job for this
    user and source (sync **or** backfill) already exists, the
    existing ``job_id`` is returned with ``already_running: true``
    instead of queueing a duplicate. The job runs asynchronously —
    use ``journal_get_job_status`` (jobs tool) to check progress.

    Args:
        source: Either ``"strava"`` or ``"garmin"``.

    Returns:
        ``{"job_id", "status", ...}``. On the deduped path, an
        ``"already_running": true`` field is set. On
        misconfiguration ("source not configured on this server"),
        an ``"error"`` field is returned and ``job_id`` is None.
    """
    log.info("Tool call: fitness_trigger_sync(source=%s)", source)
    if source not in _VALID_SOURCES:
        return {
            "error": f"Unknown fitness source: {source!r}",
            "job_id": None,
        }
    if source == "strava" and not _strava_enabled(ctx):
        return {"error": _STRAVA_DISABLED_ERROR, "job_id": None}
    user_id = _user_id(ctx)
    job_repository = _get_job_repository(ctx)
    # W5: spanning dedup — sync ↔ backfill collisions both return the
    # in-flight job rather than enqueueing a duplicate.
    in_flight = job_repository.find_active_fitness_fetch_job(
        user_id=user_id, source=source,
    )
    if in_flight is not None:
        return {
            "job_id": in_flight.id,
            "status": in_flight.status,
            "already_running": True,
        }

    runner = _get_job_runner(ctx)
    submit = (
        runner.submit_fitness_sync_strava
        if source == "strava"
        else runner.submit_fitness_sync_garmin
    )
    try:
        job = submit(user_id=user_id)
    except RuntimeError as e:
        # Source not wired on this server (no STRAVA_CLIENT_ID etc.).
        return {"error": str(e), "job_id": None}
    return {"job_id": job.id, "status": job.status}


@mcp.tool()
def fitness_trigger_backfill(
    source: str,
    start: str,
    end: str | None = None,
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Submit a historical backfill job (W5).

    Mirrors ``POST /api/fitness/backfill/{source}`` and shares the W5
    spanning idempotency with :func:`fitness_trigger_sync` — only one
    fetch job per ``(user_id, source)`` runs at a time, across both
    sync and backfill worker classes.

    Args:
        source: Either ``"strava"`` or ``"garmin"``.
        start: Inclusive start date (``"YYYY-MM-DD"``).
        end: Inclusive end date (``"YYYY-MM-DD"``). Defaults to today
            (UTC) when omitted.

    Returns:
        ``{"job_id", "status", ...}`` on success;
        ``{"error": "...", "job_id": None}`` if the source isn't
        configured on this server, or validation fails.
    """
    log.info(
        "Tool call: fitness_trigger_backfill(source=%s, start=%s, end=%s)",
        source, start, end,
    )
    if source not in _VALID_SOURCES:
        return {
            "error": f"Unknown fitness source: {source!r}",
            "job_id": None,
        }
    if source == "strava" and not _strava_enabled(ctx):
        return {"error": _STRAVA_DISABLED_ERROR, "job_id": None}
    if not isinstance(start, str) or not start:
        return {
            "error": "'start' is required and must be a YYYY-MM-DD string",
            "job_id": None,
        }
    # Defer richer date-format validation to the runner / orchestrator
    # so the MCP surface stays thin; the orchestrator parses with
    # ``date.fromisoformat`` and surfaces ValueError, which the runner
    # turns into a friendly job-failure message.
    user_id = _user_id(ctx)
    job_repository = _get_job_repository(ctx)
    in_flight = job_repository.find_active_fitness_fetch_job(
        user_id=user_id, source=source,
    )
    if in_flight is not None:
        return {
            "job_id": in_flight.id,
            "status": in_flight.status,
            "already_running": True,
        }

    runner = _get_job_runner(ctx)
    submit = (
        runner.submit_fitness_backfill_strava
        if source == "strava"
        else runner.submit_fitness_backfill_garmin
    )
    try:
        job = submit(user_id=user_id, start=start, end=end)
    except RuntimeError as e:
        return {"error": str(e), "job_id": None}
    except ValueError as e:
        return {"error": str(e), "job_id": None}
    return {"job_id": job.id, "status": job.status}


# ── Correlation queries (master plan §8) ──────────────────────────


def _q1_sleep_mood(
    conn: Any, *, user_id: int, start: str, end: str,
) -> list[dict[str, Any]]:
    """Q1 from fitness-schema.md §8 — verbatim."""
    rows = conn.execute(
        """
        SELECT
            fd.local_date,
            fd.sleep_score,
            fd.sleep_efficiency_pct,
            AVG(CASE WHEN ms.dimension = 'energy_fatigue' THEN ms.score END) AS energy,
            AVG(CASE WHEN ms.dimension = 'joy_sadness'    THEN ms.score END) AS joy
        FROM fitness_daily fd
        LEFT JOIN entries e
            ON e.user_id = fd.user_id AND e.entry_date = fd.local_date
        LEFT JOIN mood_scores ms ON ms.entry_id = e.id
        WHERE fd.user_id = :uid AND fd.local_date BETWEEN :start AND :end
        GROUP BY fd.local_date, fd.sleep_score, fd.sleep_efficiency_pct
        ORDER BY fd.local_date
        """,
        {"uid": user_id, "start": start, "end": end},
    ).fetchall()
    return [
        {
            "local_date": r["local_date"],
            "sleep_score": r["sleep_score"],
            "sleep_efficiency_pct": r["sleep_efficiency_pct"],
            "energy": r["energy"],
            "joy": r["joy"],
        }
        for r in rows
    ]


def _q2_weekly_runs_stress(
    conn: Any, *, user_id: int, start: str, end: str,
) -> list[dict[str, Any]]:
    """Q2 from fitness-schema.md §8 — verbatim."""
    rows = conn.execute(
        """
        WITH weekly_runs AS (
            SELECT
                date(local_date,
                     '-' || ((strftime('%w', local_date) + 6) % 7) || ' days') AS week_start,
                SUM(distance_m) / 1000.0                                       AS distance_km
            FROM fitness_activities
            WHERE user_id = :uid AND activity_type = 'run'
              AND local_date BETWEEN :start AND :end
            GROUP BY week_start
        ),
        weekly_stress AS (
            SELECT
                date(e.entry_date,
                     '-' || ((strftime('%w', e.entry_date) + 6) % 7) || ' days') AS week_start,
                AVG(ms.score)                                                    AS stress_proxy
            FROM entries e
            JOIN mood_scores ms ON ms.entry_id = e.id
            WHERE e.user_id = :uid AND ms.dimension = 'frustration'
              AND e.entry_date BETWEEN :start AND :end
            GROUP BY week_start
        )
        SELECT r.week_start, r.distance_km, s.stress_proxy
        FROM weekly_runs r LEFT JOIN weekly_stress s USING (week_start)
        ORDER BY r.week_start
        """,
        {"uid": user_id, "start": start, "end": end},
    ).fetchall()
    return [
        {
            "week_start": r["week_start"],
            "distance_km": r["distance_km"],
            "stress_proxy": r["stress_proxy"],
        }
        for r in rows
    ]


def _q3_hrv_mood(
    conn: Any, *, user_id: int, start: str, end: str, window: int,
) -> list[dict[str, Any]]:
    """Q3 from fitness-schema.md §8 — verbatim. ``window`` is 7 or 14."""
    rows = conn.execute(
        """
        WITH RECURSIVE date_series(d) AS (
            SELECT :start
            UNION ALL
            SELECT date(d, '+1 day') FROM date_series WHERE d < :end
        ),
        daily_mood AS (
            SELECT
                e.entry_date AS d,
                AVG(CASE WHEN ms.dimension = 'joy_sadness'    THEN ms.score END) AS joy,
                AVG(CASE WHEN ms.dimension = 'energy_fatigue' THEN ms.score END) AS energy
            FROM entries e
            JOIN mood_scores ms ON ms.entry_id = e.id
            WHERE e.user_id = :uid AND e.entry_date BETWEEN :start AND :end
            GROUP BY e.entry_date
        ),
        joined AS (
            SELECT
                ds.d,
                fd.hrv_overnight_ms,
                dm.joy,
                dm.energy
            FROM date_series ds
            LEFT JOIN fitness_daily fd ON fd.user_id = :uid AND fd.local_date = ds.d
            LEFT JOIN daily_mood    dm ON dm.d = ds.d
        )
        SELECT
            d,
            AVG(hrv_overnight_ms) OVER w AS hrv_roll,
            AVG(joy)              OVER w AS joy_roll,
            AVG(energy)           OVER w AS energy_roll
        FROM joined
        WINDOW w AS (
            ORDER BY d
            ROWS BETWEEN (:window - 1) PRECEDING AND CURRENT ROW
        )
        ORDER BY d
        """,
        {"uid": user_id, "start": start, "end": end, "window": window},
    ).fetchall()
    return [
        {
            "d": r["d"],
            "hrv_roll": r["hrv_roll"],
            "joy_roll": r["joy_roll"],
            "energy_roll": r["energy_roll"],
        }
        for r in rows
    ]


@mcp.tool()
def fitness_correlate_sleep_mood(
    start: str,
    end: str,
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Daily-grain sleep score × mood (energy & joy).

    Q1 from fitness-schema.md §8. Each row is one day in
    ``[start, end]`` that has a ``fitness_daily`` row, joined to that
    day's journal entries' mood scores. Days with sleep but no
    journal entry have ``energy``/``joy`` = ``null``.

    Args:
        start: Inclusive start date (ISO 8601).
        end: Inclusive end date (ISO 8601).

    Returns:
        ``{"rows": [{"local_date", "sleep_score",
        "sleep_efficiency_pct", "energy", "joy"}, ...]}``
    """
    log.info(
        "Tool call: fitness_correlate_sleep_mood(start=%s, end=%s)",
        start, end,
    )
    conn = _get_db_conn(ctx)
    user_id = _user_id(ctx)
    return {"rows": _q1_sleep_mood(conn, user_id=user_id, start=start, end=end)}


@mcp.tool()
def fitness_correlate_weekly_runs_stress(
    start: str,
    end: str,
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Weekly running distance × stress proxy.

    Q2 from fitness-schema.md §8. Buckets by Monday-of-week. The
    ``stress_proxy`` is the average ``frustration`` mood-dimension
    score for entries in that week (closest dimension to "stress" —
    see schema doc §8 header).

    Args:
        start: Inclusive start date (ISO 8601).
        end: Inclusive end date (ISO 8601).

    Returns:
        ``{"rows": [{"week_start", "distance_km",
        "stress_proxy"}, ...]}``. ``stress_proxy`` is ``null`` for
        weeks where the user ran but didn't journal.
    """
    log.info(
        "Tool call: fitness_correlate_weekly_runs_stress(start=%s, end=%s)",
        start, end,
    )
    conn = _get_db_conn(ctx)
    user_id = _user_id(ctx)
    return {
        "rows": _q2_weekly_runs_stress(conn, user_id=user_id, start=start, end=end),
    }


@mcp.tool()
def fitness_correlate_hrv_mood(
    start: str,
    end: str,
    window: int = 7,
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Rolling-window HRV × mood (joy & energy).

    Q3 from fitness-schema.md §8. Materialises a date series so the
    rolling window is *calendar* days, not row-count days — missing
    days neither corrupt the rolling mean nor shorten the window
    (``AVG()`` ignores NULLs).

    Args:
        start: Inclusive start date (ISO 8601).
        end: Inclusive end date (ISO 8601).
        window: Rolling-window size in calendar days. Defaults to 7;
            the schema doc recommends 7 or 14.

    Returns:
        ``{"rows": [{"d", "hrv_roll", "joy_roll",
        "energy_roll"}, ...]}`` — one row per calendar day.
    """
    log.info(
        "Tool call: fitness_correlate_hrv_mood(start=%s, end=%s, window=%d)",
        start, end, window,
    )
    if window < 1:
        return {"error": "window must be >= 1", "rows": []}
    conn = _get_db_conn(ctx)
    user_id = _user_id(ctx)
    return {
        "rows": _q3_hrv_mood(
            conn, user_id=user_id, start=start, end=end, window=window,
        ),
    }
