"""Strava fitness provider — Protocol + ``stravalib`` adapter.

The adapter wraps ``stravalib.Client`` (2.x, Pydantic 2.x models) and
keeps zero direct DB or HTTP-library imports beyond ``stravalib``
itself. Token persistence is handled by an injected ``persist_tokens``
callback that the fetch service (W6) wires to the fitness repository
— same seam ``ocr.py`` uses to inject pre-built clients rather than
constructing them from config.

Strava's API is metric by default in 2.x. We pass values through
verbatim. ``sport_type`` collapsing to the seven
``FitnessActivityType`` literals happens in normalize (W7), not here:
the adapter exposes Strava's enum string verbatim.

The ``Tokens`` payload that ``persist_tokens`` receives maps 1:1 to the
columns on ``fitness_auth_state`` so the repository can upsert
directly. ``token_expires_at`` is ISO 8601 UTC, matching the column
format on disk; ``stravalib.Client`` uses epoch seconds internally and
we convert at the boundary.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, TypedDict, runtime_checkable

from stravalib import Client
from stravalib.exc import AccessUnauthorized, AuthError

if TYPE_CHECKING:
    from stravalib.model import DetailedActivity, SummaryActivity


logger = logging.getLogger(__name__)


class StravaAuthError(Exception):
    """Raised when Strava returns 401/403 — the fetch service classifies as ``auth_broken``.

    Translates ``stravalib.exc.AccessUnauthorized`` (HTTP 401/403 on data calls)
    and ``stravalib.exc.AuthError`` (refresh-grant rejected) into a single typed
    contract so ``services/fitness/`` depends only on the provider module, not on
    ``stravalib``. Mirrors :class:`journal.providers.garmin.GarminAuthError`.
    """


@dataclass(frozen=True)
class StravaActivitySummary:
    """One Strava activity in the shape downstream services consume.

    All numeric fields are metric. ``sport_type`` is the verbatim Strava
    enum string (e.g. ``"Run"``, ``"TrailRun"``, ``"WeightTraining"``);
    collapsing to the canonical ``FitnessActivityType`` happens in
    normalize. ``raw_payload`` is the model dumped via
    ``model_dump(mode="json")`` — JSON-safe and ready for the raw
    archive table.
    """

    source_id: str
    sport_type: str
    start_time: str
    local_date: str
    duration_s: int
    moving_time_s: int | None
    distance_m: float | None
    elevation_gain_m: float | None
    avg_hr_bpm: int | None
    max_hr_bpm: int | None
    calories_kcal: int | None
    extras: dict[str, Any] = field(default_factory=dict)
    raw_payload: dict[str, Any] = field(default_factory=dict)


class Tokens(TypedDict):
    """Token triple persisted after a successful refresh.

    Field names mirror ``fitness_auth_state`` columns so a repo-side
    wrapper can upsert without renaming.
    """

    access_token: str
    refresh_token: str
    token_expires_at: str  # ISO 8601 UTC


PersistTokensFn = Callable[[Tokens], None]


@runtime_checkable
class StravaProvider(Protocol):
    """Protocol for Strava providers — data-shape only, stable across adapters."""

    def list_activities(
        self, *, after: datetime, before: datetime,
    ) -> Iterator[StravaActivitySummary]: ...

    def get_activity_detail(self, source_id: str) -> StravaActivitySummary: ...

    def refresh_token_if_needed(self) -> None: ...


class StravalibStravaProvider:
    """``stravalib``-backed adapter implementing :class:`StravaProvider`.

    The adapter does not touch the DB. ``persist_tokens`` is invoked
    after every successful refresh with the new access/refresh/expiry
    triple; the fetch service (W6) wires this to the fitness
    repository's auth-state upsert.
    """

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        access_token: str,
        refresh_token: str,
        token_expires_at: str,
        persist_tokens: PersistTokensFn,
        client_factory: Callable[..., Client] = Client,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._token_expires_at = token_expires_at
        self._persist = persist_tokens
        self._client = client_factory(
            access_token=access_token,
            refresh_token=refresh_token,
            token_expires=_iso_to_epoch(token_expires_at),
        )

    def list_activities(
        self, *, after: datetime, before: datetime,
    ) -> Iterator[StravaActivitySummary]:
        try:
            activities = self._client.get_activities(after=after, before=before)
            for activity in activities:
                yield _summary_from_stravalib(activity)
        except (AccessUnauthorized, AuthError) as exc:
            raise StravaAuthError(str(exc)) from exc

    def get_activity_detail(self, source_id: str) -> StravaActivitySummary:
        try:
            activity = self._client.get_activity(int(source_id))
        except (AccessUnauthorized, AuthError) as exc:
            raise StravaAuthError(str(exc)) from exc
        return _summary_from_stravalib(activity)

    def refresh_token_if_needed(self) -> None:
        """Refresh the access token if expired and persist the new triple.

        ``stravalib.Client`` will auto-refresh on the next API call when
        all three token fields are populated; we still call this
        explicitly at fetch-service boundaries so the persisted ISO
        timestamp matches what the in-memory client believes.

        Raises :class:`StravaAuthError` if the refresh grant is rejected
        (revoked refresh token, deauthorised app) — the fetch service
        treats this as ``auth_broken``.
        """
        if not _expired(self._token_expires_at):
            return
        try:
            token = self._client.refresh_access_token(
                client_id=self._client_id,
                client_secret=self._client_secret,
                refresh_token=self._refresh_token,
            )
        except (AccessUnauthorized, AuthError) as exc:
            raise StravaAuthError(str(exc)) from exc
        new_expires_iso = _epoch_to_iso(int(token["expires_at"]))
        self._access_token = token["access_token"]
        self._refresh_token = token["refresh_token"]
        self._token_expires_at = new_expires_iso
        # Re-arm the in-memory client so subsequent calls use the new
        # triple without reconstructing it.
        self._client.access_token = token["access_token"]
        self._client.refresh_token = token["refresh_token"]
        self._client.token_expires = int(token["expires_at"])
        self._persist(
            Tokens(
                access_token=self._access_token,
                refresh_token=self._refresh_token,
                token_expires_at=self._token_expires_at,
            ),
        )


def _summary_from_stravalib(
    activity: SummaryActivity | DetailedActivity,
) -> StravaActivitySummary:
    """Map a ``stravalib`` activity model into :class:`StravaActivitySummary`."""
    start_dt = activity.start_date
    start_iso = (
        start_dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if start_dt else ""
    )

    local_dt = activity.start_date_local
    local_date = (
        local_dt.strftime("%Y-%m-%d") if local_dt is not None else start_iso[:10]
    )

    sport_type_raw: Any = activity.sport_type or activity.type
    if sport_type_raw is None:
        sport_type = "Unknown"
    elif hasattr(sport_type_raw, "root"):
        sport_type = str(sport_type_raw.root)
    else:
        sport_type = str(sport_type_raw)

    return StravaActivitySummary(
        source_id=str(activity.id),
        sport_type=sport_type,
        start_time=start_iso,
        local_date=local_date,
        duration_s=int(activity.elapsed_time) if activity.elapsed_time else 0,
        moving_time_s=int(activity.moving_time) if activity.moving_time else None,
        distance_m=float(activity.distance) if activity.distance is not None else None,
        elevation_gain_m=(
            float(activity.total_elevation_gain)
            if activity.total_elevation_gain is not None
            else None
        ),
        avg_hr_bpm=(
            int(activity.average_heartrate)
            if activity.average_heartrate is not None
            else None
        ),
        max_hr_bpm=(
            int(activity.max_heartrate)
            if activity.max_heartrate is not None
            else None
        ),
        calories_kcal=_int_or_none(getattr(activity, "calories", None)),
        extras={},
        raw_payload=activity.model_dump(mode="json"),
    )


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _expired(token_expires_at_iso: str) -> bool:
    if not token_expires_at_iso:
        return True
    try:
        ts = datetime.fromisoformat(token_expires_at_iso.replace("Z", "+00:00"))
    except ValueError:
        return True
    return ts <= datetime.now(UTC)


def _iso_to_epoch(iso: str) -> int:
    """ISO 8601 → epoch seconds. Returns 0 (= treat-as-expired) on parse failure.

    The constructor passes this into ``stravalib.Client(token_expires=...)``, so
    crashing here would prevent the adapter from ever booting on a corrupt
    persisted timestamp. Returning 0 means stravalib treats the token as
    expired and auto-refreshes on the next API call — the same outcome
    ``_expired`` produces for the explicit refresh path.
    """
    if not iso:
        return 0
    try:
        ts = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return 0
    return int(ts.timestamp())


def _epoch_to_iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
