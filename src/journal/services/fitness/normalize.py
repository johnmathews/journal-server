"""Fitness normalize service — raw rows → ``fitness_activities`` / ``fitness_daily``.

Per W7 of ``docs/fitness-tier-plan.md``. Two entry points
(:func:`normalize_strava`, :func:`normalize_garmin`) read raw rows
from the per-source raw archive, project them into the cross-source
normalized tables, and ``INSERT OR REPLACE`` so re-runs are
idempotent. The ``activity_type`` collapsing table lives in
:mod:`journal.services.fitness._activity_type_map`; the verbatim
source enum is preserved as ``source_subtype``.

**Watermark.** :meth:`FitnessRepository.max_normalized_fetched_at`
returns the largest raw ``fetched_at`` already referenced by a
normalized row. The next pass reads raw rows where ``fetched_at >
watermark``. On first run the watermark is ``None`` and we read all
rows. Garmin runs the daily and activities watermarks separately
since they project into different tables.

**Authoritativeness.** Garmin re-publishes the same ``(endpoint,
source_id)`` with a new ``payload_sha256`` when corrections land.
Normalize keeps the row with the largest ``fetched_at`` per
``(endpoint, source_id)`` group; the older row remains in raw as an
audit trail. ``raw_ref_ids_json`` on the produced ``fitness_daily``
row therefore lists exactly one raw-id per contributing endpoint.

**Drift.** A raw row with a missing required field (e.g. Strava
without ``start_date_local``, Garmin activity without ``activityId``)
is **skipped**, not raised. Drift is logged loudly; if the batch
ended with any drifts, a single ``fitness_sync_runs`` row is inserted
with status ``normalize_drift`` and the admin-only Pushover topic
:func:`PushoverNotificationService.notify_fitness_normalize_drift`
fires *once* with the batch's drift count. The successful rows are
still upserted — drift never aborts the batch.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from journal.models import FitnessActivity, FitnessDaily
from journal.services.fitness._activity_type_map import (
    coarse_garmin,
    coarse_strava,
)

if TYPE_CHECKING:
    from journal.db.fitness_repository import FitnessRepository
    from journal.models import FitnessRawRow

log = logging.getLogger(__name__)


_GARMIN_DAILY_ENDPOINTS = frozenset({
    "sleep", "hrv", "body_battery", "stress",
    "training_load", "training_readiness",
})


@dataclass(frozen=True)
class NormalizeResult:
    """Outcome of one normalize pass.

    Workers (W8) serialise this via ``dataclasses.asdict``. ``drift_count``
    is the number of raw rows that could not be normalized (logged and
    skipped). ``rows_normalized`` counts every upsert performed —
    including idempotent re-upserts on already-normalized rows on the
    first run with no watermark.
    """

    source: str
    rows_normalized: int
    drift_count: int


class NormalizeDriftNotifier(Protocol):
    """The notification surface :func:`normalize_*` depends on.

    Structurally satisfied by
    :class:`journal.services.notifications.PushoverNotificationService`.
    """

    def notify_fitness_normalize_drift(
        self, source: str, drift_count: int,
    ) -> None: ...


# ── Strava ──────────────────────────────────────────────────────────


def normalize_strava(
    repo: FitnessRepository,
    *,
    user_id: int,
    since: str | None = None,
    notifier: NormalizeDriftNotifier | None = None,
    sync_run_id: int | None = None,
) -> NormalizeResult:
    """Normalize Strava activity raw rows into ``fitness_activities``.

    Parameters
    ----------
    repo
        ``FitnessRepository`` connected to the journal DB.
    user_id
        Owner of the rows to normalize.
    since
        Watermark override (ISO 8601 UTC). Defaults to
        :meth:`FitnessRepository.max_normalized_fetched_at` for
        ``(strava, activities)`` — the routine incremental case.
    notifier
        Optional admin-only Pushover service for drift alerts. If
        ``None`` and drift occurs, the drift is still recorded in
        ``fitness_sync_runs`` but no Pushover fires.
    sync_run_id
        Id of the fetch's ``fitness_sync_runs`` row. When provided,
        normalize amends that row's ``rows_normalized`` count so the
        webapp's last-runs panel reflects what was actually persisted.
        ``None`` for code paths that don't tie a normalize pass to a
        specific fetch run (CLI manual normalize, backfill batches).
    """
    # Watermark shape: composite (fetched_at, id) per W3 (the W7 race fix).
    # A caller-supplied `since` is still accepted as a bare ISO string for
    # the force-renormalize one-liner in fitness-operations.md §3; wrap it as
    # ``(s, 0)`` because `id` is a positive AUTOINCREMENT rowid.
    if since is not None:
        watermark: tuple[str, int] | None = (since, 0)
    else:
        watermark = repo.max_normalized_fetched_at(
            source="strava", user_id=user_id, kind="activities",
        )
    rows_normalized = 0
    drift_count = 0
    for raw in repo.list_raw_since(
        source="strava", user_id=user_id, since=watermark,
    ):
        if raw.endpoint not in {"activities", "activity_detail"}:
            continue
        try:
            activity = _strava_raw_to_activity(raw, user_id)
        except _Drift as exc:
            log.warning(
                "Strava normalize drift on raw row %s: %s",
                raw.id, exc, exc_info=False,
            )
            drift_count += 1
            continue
        repo.upsert_activity(activity)
        rows_normalized += 1

    if sync_run_id is not None:
        # Strava is workouts-only — every normalized row is a workout.
        repo.record_normalized_rows(
            sync_run_id, rows_normalized,
            workouts_normalized=rows_normalized,
            wellness_normalized=0,
        )
    _record_drift_if_any(
        repo=repo, source="strava", user_id=user_id,
        drift_count=drift_count, notifier=notifier,
    )
    return NormalizeResult(
        source="strava", rows_normalized=rows_normalized,
        drift_count=drift_count,
    )


def _strava_raw_to_activity(
    raw: FitnessRawRow, user_id: int,
) -> FitnessActivity:
    """Project one Strava raw payload into a :class:`FitnessActivity`.

    Mirrors the field set
    :func:`journal.providers.strava._summary_from_stravalib` builds —
    the provider's ``raw_payload`` is the ``stravalib.SummaryActivity``
    /``DetailedActivity`` model dump, so the keys here match those
    Pydantic field names.

    Raises :class:`_Drift` if a required field is missing — the caller
    catches and records.
    """
    payload = json.loads(raw.payload_json)
    if not isinstance(payload, dict):
        raise _Drift(f"payload is not a JSON object: {type(payload).__name__}")

    activity_id = payload.get("id")
    if activity_id is None:
        raise _Drift("missing required field: id")

    sport_type = payload.get("sport_type") or payload.get("type")
    if not sport_type:
        raise _Drift("missing required field: sport_type")
    sport_type = str(sport_type)

    start_date = payload.get("start_date")
    if not start_date:
        raise _Drift("missing required field: start_date")
    start_iso = _normalize_iso(str(start_date))

    start_local = payload.get("start_date_local")
    if not start_local:
        raise _Drift("missing required field: start_date_local")
    local_date = str(start_local)[:10]

    elapsed = payload.get("elapsed_time")
    if elapsed is None:
        raise _Drift("missing required field: elapsed_time")
    duration_s = int(elapsed)

    distance_m = _float_or_none(payload.get("distance"))
    moving_time_s = _int_or_none(payload.get("moving_time"))

    # Strava's `perceived_exertion` is the athlete's manual RPE, already on
    # the 1–10 scale the fitness_activities CHECK expects (migration 0025), so
    # it maps straight into the column (bounds-coerced defensively). Mostly
    # NULL in reality — RPE is opt-in per activity.
    #
    # `suffer_score` (Strava's "Relative Effort", HR-derived, unbounded ~0–300+)
    # is a DIFFERENT scale and must NOT go in the 1–10 column. Park it in
    # extras_json so the signal is captured without violating the CHECK.
    extras: dict[str, Any] = {}
    suffer_score = _int_or_none(payload.get("suffer_score"))
    if suffer_score is not None:
        extras["suffer_score"] = suffer_score

    return FitnessActivity(
        user_id=user_id,
        source="strava",
        source_id=str(activity_id),
        activity_type=coarse_strava(sport_type),
        source_subtype=sport_type,
        start_time=start_iso,
        local_date=local_date,
        duration_s=duration_s,
        moving_time_s=moving_time_s,
        distance_m=distance_m,
        elevation_gain_m=_float_or_none(payload.get("total_elevation_gain")),
        avg_hr_bpm=_int_or_none(payload.get("average_heartrate")),
        max_hr_bpm=_int_or_none(payload.get("max_heartrate")),
        avg_pace_s_per_km=_avg_pace(
            duration_s=duration_s,
            distance_m=distance_m,
            moving_time_s=moving_time_s,
            activity_type=coarse_strava(sport_type),
        ),
        calories_kcal=_int_or_none(payload.get("calories")),
        perceived_exertion=_bounded_int_or_none(
            payload.get("perceived_exertion"), lo=1, hi=10,
        ),
        extras=extras,
        raw_ref_id=raw.id or 0,
    )


# ── Garmin ──────────────────────────────────────────────────────────


def normalize_garmin(
    repo: FitnessRepository,
    *,
    user_id: int,
    since: str | None = None,
    notifier: NormalizeDriftNotifier | None = None,
    sync_run_id: int | None = None,
) -> NormalizeResult:
    """Normalize Garmin raw rows into ``fitness_daily`` and ``fitness_activities``.

    Garmin contributes both daily wellness metrics (six endpoints fan
    in to one ``fitness_daily`` row per ``local_date``) and discrete
    activities. Watermarks are computed separately for each kind
    because ``max_normalized_fetched_at`` joins differently against
    ``raw_ref_ids_json`` (daily) vs ``raw_ref_id`` (activity).

    ``sync_run_id`` — see :func:`normalize_strava`.
    """
    # Watermark shape: composite (fetched_at, id) per W3 (the W7 race fix).
    # A caller-supplied `since` is still accepted as a bare ISO string for
    # the force-renormalize one-liner in fitness-operations.md §3; wrap it
    # as ``(s, 0)`` so both daily and activity passes start strictly after
    # any real row with that fetched_at value.
    if since is not None:
        daily_watermark: tuple[str, int] | None = (since, 0)
        activity_watermark: tuple[str, int] | None = (since, 0)
    else:
        daily_watermark = repo.max_normalized_fetched_at(
            source="garmin", user_id=user_id, kind="daily",
        )
        activity_watermark = repo.max_normalized_fetched_at(
            source="garmin", user_id=user_id, kind="activities",
        )

    workouts_normalized = 0
    wellness_normalized = 0
    drift_count = 0

    daily_raws_by_date: dict[str, dict[str, FitnessRawRow]] = defaultdict(dict)
    for raw in repo.list_raw_since(
        source="garmin", user_id=user_id, since=daily_watermark,
    ):
        if raw.endpoint not in _GARMIN_DAILY_ENDPOINTS:
            continue
        # Authoritativeness: keep the row with the largest fetched_at
        # for each (endpoint, local_date) pair. raw rows arrive ordered
        # by fetched_at ASC from list_raw_since, so a later iteration
        # for the same key naturally overwrites an earlier one.
        existing = daily_raws_by_date[raw.source_id].get(raw.endpoint)
        if existing is None or raw.fetched_at >= existing.fetched_at:
            daily_raws_by_date[raw.source_id][raw.endpoint] = raw

    for local_date, by_endpoint in daily_raws_by_date.items():
        try:
            daily = _garmin_daily_from_raws(local_date, by_endpoint, user_id)
            repo.upsert_daily(daily)
        except _Drift as exc:
            log.warning(
                "Garmin daily normalize drift on %s: %s", local_date, exc,
            )
            drift_count += 1
            continue
        except sqlite3.IntegrityError as exc:
            # Defense in depth: sentinels are bounds-coerced above, but any
            # other CHECK violation must not abort the batch (or the
            # activity pass that follows). Skip this date, count as drift.
            log.warning(
                "Garmin daily upsert rejected by CHECK constraint on %s: %s",
                local_date, exc,
            )
            drift_count += 1
            continue
        wellness_normalized += 1

    for raw in repo.list_raw_since(
        source="garmin", user_id=user_id, since=activity_watermark,
    ):
        if raw.endpoint not in {"activities", "activity_detail"}:
            continue
        try:
            activity = _garmin_raw_to_activity(raw, user_id)
            repo.upsert_activity(activity)
        except _Drift as exc:
            log.warning(
                "Garmin activity normalize drift on raw row %s: %s",
                raw.id, exc,
            )
            drift_count += 1
            continue
        except sqlite3.IntegrityError as exc:
            # Defense in depth: a CHECK violation on one activity must not
            # abort the rest of the pass.
            log.warning(
                "Garmin activity upsert rejected by CHECK constraint on raw row %s: %s",
                raw.id, exc,
            )
            drift_count += 1
            continue
        workouts_normalized += 1

    rows_normalized = workouts_normalized + wellness_normalized

    if sync_run_id is not None:
        repo.record_normalized_rows(
            sync_run_id, rows_normalized,
            workouts_normalized=workouts_normalized,
            wellness_normalized=wellness_normalized,
        )
    _record_drift_if_any(
        repo=repo, source="garmin", user_id=user_id,
        drift_count=drift_count, notifier=notifier,
    )
    return NormalizeResult(
        source="garmin", rows_normalized=rows_normalized,
        drift_count=drift_count,
    )


def _garmin_daily_from_raws(
    local_date: str,
    by_endpoint: dict[str, FitnessRawRow],
    user_id: int,
) -> FitnessDaily:
    """Fan ``by_endpoint`` (max-fetched_at row per endpoint) into one daily row."""
    payloads = {
        endpoint: json.loads(raw.payload_json) if raw.payload_json else None
        for endpoint, raw in by_endpoint.items()
    }
    sleep = payloads.get("sleep") or {}
    hrv = payloads.get("hrv") or {}
    bb_list = payloads.get("body_battery") or []
    stress = payloads.get("stress") or {}
    training_load = payloads.get("training_load") or {}
    readiness = payloads.get("training_readiness") or []

    if not isinstance(sleep, dict):
        sleep = {}
    if not isinstance(hrv, dict):
        hrv = {}
    if not isinstance(stress, dict):
        stress = {}
    if not isinstance(training_load, dict):
        training_load = {}
    bb_first = bb_list[0] if isinstance(bb_list, list) and bb_list else {}
    if not isinstance(bb_first, dict):
        bb_first = {}
    readiness_first = readiness[0] if isinstance(readiness, list) and readiness else {}
    if not isinstance(readiness_first, dict):
        readiness_first = {}

    sleep_dto = sleep.get("dailySleepDTO") or {}
    sleep_scores = sleep_dto.get("sleepScores") or {}
    sleep_overall = sleep_scores.get("overall") or {}
    tlb = training_load.get("mostRecentTrainingLoadBalance") or {}

    return FitnessDaily(
        user_id=user_id,
        source="garmin",
        local_date=local_date,
        # Range-checked fields use ``_bounded_*`` so Garmin's
        # insufficient-wear sentinels (-1 / -2) collapse to NULL instead of
        # tripping the fitness_daily CHECK constraints (migration 0025).
        sleep_score=_bounded_int_or_none(sleep_overall.get("value"), lo=0, hi=100),
        sleep_duration_s=_int_or_none(sleep_dto.get("sleepTimeSeconds")),
        sleep_efficiency_pct=_bounded_float_or_none(
            sleep_dto.get("sleepEfficiencyPercentage"), lo=0, hi=100,
        ),
        # Schema requires hrv_overnight_ms > 0; a tiny positive lower bound
        # nulls the <= 0 sentinels while passing every realistic reading.
        hrv_overnight_ms=_bounded_float_or_none(
            (hrv.get("hrvSummary") or {}).get("lastNightAvg"),
            lo=1e-9, hi=float("inf"),
        ),
        resting_hr_bpm=_bounded_int_or_none(
            sleep.get("restingHeartRate"), lo=20, hi=200,
        ),
        # NOTE (semantics — do not misread these as battery LEVELS): Garmin's
        # `charged`/`drained` are the day's TOTAL Body Battery points GAINED
        # and LOST, not the day's high/low battery level. So `body_battery_high`
        # ("charged") is total charge accrued and `body_battery_low` ("drained")
        # is total charge spent — both are cumulative daily deltas in 0–100
        # points, and `high` is NOT guaranteed >= `low`. Consumers computing a
        # divergence/fatigue signal (W5) must treat these as charge/drain
        # totals, not min/max levels.
        body_battery_high=_bounded_int_or_none(bb_first.get("charged"), lo=0, hi=100),
        body_battery_low=_bounded_int_or_none(bb_first.get("drained"), lo=0, hi=100),
        stress_avg=_bounded_int_or_none(stress.get("avgStressLevel"), lo=0, hi=100),
        training_load_acute=_float_or_none(tlb.get("metricsTrainingLoadAcute")),
        training_load_chronic=_float_or_none(
            tlb.get("metricsTrainingLoadChronic"),
        ),
        training_readiness=_bounded_int_or_none(
            readiness_first.get("score"), lo=0, hi=100,
        ),
        extras={},
        raw_ref_ids=sorted(
            raw.id for raw in by_endpoint.values() if raw.id is not None
        ),
    )


def _garmin_raw_to_activity(
    raw: FitnessRawRow, user_id: int,
) -> FitnessActivity:
    """Project one Garmin activity raw payload into a :class:`FitnessActivity`."""
    payload = json.loads(raw.payload_json)
    if not isinstance(payload, dict):
        raise _Drift(f"payload is not a JSON object: {type(payload).__name__}")

    activity_id = payload.get("activityId")
    if activity_id is None:
        raise _Drift("missing required field: activityId")

    activity_type = payload.get("activityType") or {}
    type_key = str(activity_type.get("typeKey") or "unknown")

    start_gmt = payload.get("startTimeGMT") or ""
    start_local = payload.get("startTimeLocal") or ""
    if not start_gmt and not start_local:
        raise _Drift("missing both startTimeGMT and startTimeLocal")
    start_iso = _gmt_str_to_iso(str(start_gmt)) if start_gmt else ""
    local_date = (
        str(start_local)[:10] if len(str(start_local)) >= 10 else start_iso[:10]
    )
    if not start_iso or not local_date:
        raise _Drift("could not derive start_time/local_date")

    duration = payload.get("duration")
    if duration is None:
        raise _Drift("missing required field: duration")
    duration_s = int(duration)

    distance_m = _float_or_none(payload.get("distance"))
    moving_time_s = _int_or_none(payload.get("movingDuration"))

    # Garmin has no manual RPE field, so perceived_exertion stays NULL — do
    # NOT synthesise one from training effect (a different, physiological
    # scale). Capture Garmin's training-effect signals in extras_json when the
    # payload carries them, verbatim under their source key names, so the
    # divergence/fatigue consumers can use them without us inventing an RPE.
    extras: dict[str, Any] = {}
    for src_key in (
        "aerobicTrainingEffect",
        "anaerobicTrainingEffect",
        "activityTrainingLoad",
    ):
        value = _float_or_none(payload.get(src_key))
        if value is not None:
            extras[src_key] = value

    return FitnessActivity(
        user_id=user_id,
        source="garmin",
        source_id=str(activity_id),
        activity_type=coarse_garmin(type_key),
        source_subtype=type_key,
        start_time=start_iso,
        local_date=local_date,
        duration_s=duration_s,
        moving_time_s=moving_time_s,
        distance_m=distance_m,
        elevation_gain_m=_float_or_none(payload.get("elevationGain")),
        avg_hr_bpm=_int_or_none(payload.get("averageHR")),
        max_hr_bpm=_int_or_none(payload.get("maxHR")),
        avg_pace_s_per_km=_avg_pace(
            duration_s=duration_s,
            distance_m=distance_m,
            moving_time_s=moving_time_s,
            activity_type=coarse_garmin(type_key),
        ),
        calories_kcal=_int_or_none(payload.get("calories")),
        perceived_exertion=None,
        extras=extras,
        raw_ref_id=raw.id or 0,
    )


# ── Drift handling ──────────────────────────────────────────────────


class _Drift(Exception):  # noqa: N818  internal sentinel; never crosses the public surface
    """Raised internally when a raw row can't be normalized.

    Caught by the entry-point loop. Never propagates out of
    ``normalize_*``.
    """


def _record_drift_if_any(
    *,
    repo: FitnessRepository,
    source: str,
    user_id: int,
    drift_count: int,
    notifier: NormalizeDriftNotifier | None,
) -> None:
    if drift_count == 0:
        return
    run_id = repo.start_sync_run(user_id=user_id, source=source)
    repo.finish_sync_run(
        run_id, status="normalize_drift",
        error_class="NormalizeDrift",
        error_message=f"{drift_count} raw rows skipped during normalize",
        notes={"drift_count": drift_count},
    )
    if notifier is not None:
        notifier.notify_fitness_normalize_drift(source, drift_count)


# ── Helpers ─────────────────────────────────────────────────────────


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _bounded_int_or_none(value: Any, *, lo: int, hi: int) -> int | None:
    """Coerce to ``int`` but collapse out-of-range values to ``None``.

    Garmin publishes sentinel values (``-1`` / ``-2``) for wellness metrics
    such as stress, sleep score, resting HR, body battery, and training
    readiness on days with insufficient wear. ``_int_or_none`` would pass
    those straight through, but the ``fitness_daily`` CHECK constraints
    (migration 0025) reject out-of-range values, so the ``upsert_daily``
    that follows would raise ``sqlite3.IntegrityError`` and abort the whole
    pass. Anything ``None`` or outside the inclusive ``[lo, hi]`` window —
    which the sentinels always are — becomes ``None`` (a legitimate
    "no reading" value the schema accepts).
    """
    result = _int_or_none(value)
    if result is None or result < lo or result > hi:
        return None
    return result


def _bounded_float_or_none(value: Any, *, lo: float, hi: float) -> float | None:
    """Float counterpart of :func:`_bounded_int_or_none`.

    Same Garmin insufficient-wear sentinel rationale: values ``None`` or
    outside the inclusive ``[lo, hi]`` window collapse to ``None`` so they
    never trip a ``fitness_daily`` CHECK constraint. Used for
    ``sleep_efficiency_pct`` (0–100) and ``hrv_overnight_ms`` (the schema
    requires ``> 0``; callers pass a lower bound above zero to enforce it).
    """
    result = _float_or_none(value)
    if result is None or result < lo or result > hi:
        return None
    return result


def _normalize_iso(value: str) -> str:
    """Normalize a Strava ``start_date`` into ``YYYY-MM-DDTHH:MM:SSZ``."""
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value  # let SQL CHECK / downstream fail loudly if misformed
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _gmt_str_to_iso(gmt: str) -> str:
    """Parse Garmin's ``YYYY-MM-DD HH:MM:SS`` GMT string into ISO 8601 UTC."""
    if not gmt:
        return ""
    try:
        dt = datetime.strptime(gmt, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    except ValueError:
        return ""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _avg_pace(
    *,
    duration_s: int,
    distance_m: float | None,
    moving_time_s: int | None,
    activity_type: str,
) -> float | None:
    """Pace in s/km — only meaningful for foot-locomotion activities.

    Uses ``moving_time`` when present (matches what Strava considers
    "pace"), falls back to ``duration_s``. ``None`` for cycling, swimming,
    strength, etc. and whenever distance is missing or zero.
    """
    if activity_type not in {"run", "walk", "hike"}:
        return None
    if distance_m is None or distance_m <= 0:
        return None
    seconds = moving_time_s if moving_time_s is not None else duration_s
    if seconds <= 0:
        return None
    return seconds / (distance_m / 1000.0)


