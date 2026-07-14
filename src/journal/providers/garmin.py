"""Garmin fitness provider — Protocol + ``garminconnect`` adapter.

The adapter wraps ``garminconnect.Garmin`` (0.3.x, ``garth``-backed) and
keeps zero direct DB or HTTP-library imports beyond ``garminconnect``
itself. Token persistence is handled by an injected ``persist_tokens``
callback that the fetch service (W6) wires to the fitness repository
— same seam ``providers/strava.py`` uses to inject pre-built clients
rather than constructing them from config.

Token-loading sequence on ``login()`` (D4 — "one login per token
lifetime"):

1. **DB blob first** (``tokens_blob`` constructor arg, sourced from
   ``fitness_auth_state.extra_state_json`` — the source of truth). If
   present, hydrate the SDK's internal ``client`` directly via
   ``client.loads`` and skip the network roundtrip.
2. **Filesystem cache** (``tokens_path``). Belt-and-braces; useful
   when running the bare provider outside the journal-server process,
   e.g. in a debugging notebook.
3. **Username/password + MFA callback**. After a successful network
   login, the resulting JSON blob is mirrored back to the fetch
   service via ``persist_tokens`` so the next sync starts from the DB
   row rather than the file cache.

Daily metrics are aggregated from seven endpoints (sleep, hrv, body
battery, stress, training status, training readiness, plus the resting
HR field carried inside ``get_sleep_data``). Endpoints that return
``None`` or empty payloads do not crash the aggregation — the
corresponding metric becomes ``None`` and ``raw_payloads_per_endpoint``
still records the (empty) response so the raw archive is faithful.

Activities are listed via ``get_activities_by_date``;
``activity_type_str`` is passed through verbatim. Collapsing to the
canonical ``FitnessActivityType`` literals happens in normalize (W7),
not here. Authentication failures from any endpoint surface as a typed
:class:`GarminAuthError` so the fetch service can classify them as
``auth_broken`` rather than transient.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectTooManyRequestsError,
)

if TYPE_CHECKING:
    from pathlib import Path


logger = logging.getLogger(__name__)


class GarminAuthError(Exception):
    """Raised when Garmin returns 401/403 — the fetch service classifies as ``auth_broken``."""


class GarminRateLimitError(Exception):
    """An unattended re-login was refused upstream (429 / Cloudflare block).

    Deliberately *not* a :class:`GarminAuthError` subclass — the remedy
    is "stop and wait" (arm the shared
    :class:`~journal.services.fitness.garmin_pending.GarminUpstreamCooldown`),
    not "the credentials are bad". Only raised by
    :meth:`GarminConnectGarminProvider.relogin_with_password`.
    """


_MFA_UNATTENDED_MESSAGE = (
    "Garmin requested MFA — unattended re-login cannot complete"
)

# Substrings that mark a Garmin login failure as a rate-limit / Cloudflare
# bot-challenge rather than genuinely wrong credentials. Lower-cased match.
# Kept deliberately broad: a false positive only turns "bad password" into
# "try again later", never the reverse. Shared with ``api/fitness_garmin.py``
# so the connect UI and the unattended re-login classify identically.
RATE_LIMIT_SIGNALS = (
    "429",
    "rate limit",
    "rate-limit",
    "rate limiting",
    "too many",
    "cloudflare",
    "bot challenge",
    "captcha",
    "unexpected title",
    "strategies exhausted",
    "ip rate limited",
    "blocking this request",
)


def looks_rate_limited(*texts: str) -> bool:
    """True when any text carries a rate-limit / bot-challenge signal."""
    blob = " ".join(t for t in texts if t).lower()
    return any(signal in blob for signal in RATE_LIMIT_SIGNALS)


@dataclass(frozen=True)
class GarminDailyMetrics:
    """One day of Garmin wellness metrics, aggregated across endpoints.

    Each scalar field is ``None`` when the corresponding endpoint
    returned no data (watch off, feature not enabled, etc.). The
    ``raw_payloads_per_endpoint`` mapping always carries one entry per
    endpoint queried, even on empty responses, for the raw archive.
    """

    local_date: str
    sleep_score: int | None
    sleep_duration_s: int | None
    sleep_efficiency_pct: float | None
    hrv_overnight_ms: float | None
    resting_hr_bpm: int | None
    body_battery_high: int | None
    body_battery_low: int | None
    stress_avg: int | None
    training_load_acute: float | None
    training_load_chronic: float | None
    training_readiness: int | None
    extras: dict[str, Any] = field(default_factory=dict)
    raw_payloads_per_endpoint: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GarminActivitySummary:
    """One Garmin activity in the shape downstream services consume.

    All numeric fields are metric. ``activity_type_str`` is the
    verbatim Garmin ``typeKey`` (e.g. ``"running"``,
    ``"treadmill_running"``, ``"strength_training"``); collapsing to
    the canonical ``FitnessActivityType`` happens in normalize.
    """

    source_id: str
    activity_type_str: str
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


PersistTokensFn = Callable[[str], None]
"""Persist callback. Receives the JSON blob produced by ``garminconnect.client.dumps``;
the fetch service mirrors it into ``fitness_auth_state.extra_state_json``."""


@runtime_checkable
class GarminProvider(Protocol):
    """Protocol for Garmin providers — data-shape only, stable across adapters."""

    def login(self, *, mfa_callback: Callable[[], str] | None = None) -> None: ...

    def get_daily(self, date: str) -> GarminDailyMetrics: ...

    def list_activities(
        self, *, after: datetime, before: datetime,
    ) -> Iterator[GarminActivitySummary]: ...


@runtime_checkable
class SupportsUnattendedRelogin(Protocol):
    """Optional provider capability: unattended password re-login (W6).

    The fetch service duck-checks this via ``isinstance`` before
    attempting recovery from a dead token blob, so providers (and test
    fakes) that don't carry saved credentials simply skip the retry.
    """

    def can_relogin_with_password(self) -> bool: ...

    def relogin_with_password(self) -> None: ...


class GarminConnectGarminProvider:
    """``garminconnect``-backed adapter implementing :class:`GarminProvider`.

    The adapter does not touch the DB. ``persist_tokens`` is invoked
    after every successful network login with the new token blob; the
    fetch service (W6) wires this to the fitness repository's
    auth-state upsert. The filesystem cache is *not* the source of
    truth — D4 requires that fresh providers boot from the DB row,
    which is what ``tokens_blob`` carries.
    """

    def __init__(
        self,
        *,
        username: str,
        password: str,
        tokens_blob: str | None = None,
        tokens_path: Path | None = None,
        persist_tokens: PersistTokensFn | None = None,
        client_factory: Callable[..., Garmin] = Garmin,
    ) -> None:
        self._username = username
        self._password = password
        self._tokens_blob = tokens_blob
        self._tokens_path = tokens_path
        self._persist = persist_tokens
        self._client_factory = client_factory
        self._client: Garmin | None = None

    # -- Login + token-loading sequence -----------------------------

    def login(self, *, mfa_callback: Callable[[], str] | None = None) -> None:
        client = self._client_factory(
            email=self._username,
            password=self._password,
            prompt_mfa=mfa_callback,
        )
        self._client = client

        if self._tokens_blob:
            try:
                client.client.loads(self._tokens_blob)
                return
            except Exception as exc:  # noqa: BLE001  fall through to next strategy
                logger.warning(
                    "Garmin token blob from DB failed to load (%s); "
                    "falling back to filesystem cache or password",
                    exc,
                )

        tokenstore = str(self._tokens_path) if self._tokens_path is not None else None
        try:
            client.login(tokenstore)
        except GarminConnectAuthenticationError as exc:
            # W6 guard: with no MFA callback wired (the unattended sync
            # path), an MFA challenge must surface as a typed auth error
            # with an actionable message — never an interactive prompt.
            # garminconnect 0.3.x already raises (rather than blocking on
            # stdin) when ``prompt_mfa`` is None; we translate its message.
            if mfa_callback is None and "mfa" in str(exc).lower():
                raise GarminAuthError(_MFA_UNATTENDED_MESSAGE) from exc
            raise GarminAuthError(str(exc)) from exc

        if self._persist is not None:
            try:
                blob = client.client.dumps()
            except Exception:  # noqa: BLE001  defensive — never fail login on persist
                logger.exception("Garmin client.dumps() failed; tokens not mirrored to DB")
                return
            self._persist(blob)

    # -- W6: unattended password re-login ---------------------------

    def can_relogin_with_password(self) -> bool:
        """Whether saved credentials are available for an unattended re-login.

        True only when the factory (bootstrap / CLI) injected a real
        username *and* password — i.e. ``FITNESS_CREDENTIAL_KEY`` is set
        and the auth row carried decryptable saved credentials.
        """
        return bool(self._username and self._password)

    def relogin_with_password(self) -> None:
        """One unattended tier-3 login that bypasses the (dead) token blob.

        Builds a **fresh** SDK client — no ``tokens_blob`` hydration, no
        filesystem tokenstore — and logs in with the saved username /
        password. On success the provider adopts the new client, replaces
        its own ``tokens_blob`` with the fresh dump (so a retried fetch
        phase that calls :meth:`login` again hydrates the *new* blob), and
        mirrors it to the persist callback.

        Raises:
            GarminRateLimitError: upstream 429 / Cloudflare bot-challenge —
                the caller must arm the shared upstream cooldown, not retry.
            GarminAuthError: bad credentials, an MFA challenge (no callback
                is wired on this path, by design — non-goal 3), or no saved
                credentials at all.
        """
        if not self.can_relogin_with_password():
            raise GarminAuthError(
                "No saved Garmin credentials available for unattended re-login",
            )
        client = self._client_factory(
            email=self._username,
            password=self._password,
            prompt_mfa=None,
        )
        try:
            client.login(None)
        except GarminConnectTooManyRequestsError as exc:
            raise GarminRateLimitError(str(exc)) from exc
        except GarminConnectAuthenticationError as exc:
            if looks_rate_limited(str(exc)):
                # The strategy chain can misreport a Cloudflare block as an
                # auth failure (see api/fitness_garmin.py) — classify by text.
                raise GarminRateLimitError(str(exc)) from exc
            if "mfa" in str(exc).lower():
                raise GarminAuthError(_MFA_UNATTENDED_MESSAGE) from exc
            raise GarminAuthError(str(exc)) from exc
        except Exception as exc:
            # Terminal "all strategies exhausted" failures surface as
            # connection errors; rate-limit-looking ones gate the cooldown,
            # anything else propagates for transient classification.
            if looks_rate_limited(str(exc)):
                raise GarminRateLimitError(str(exc)) from exc
            raise

        self._client = client
        try:
            blob = client.client.dumps()
        except Exception:  # noqa: BLE001  defensive — the login itself succeeded
            logger.exception(
                "Garmin client.dumps() failed after unattended re-login; "
                "fresh tokens not mirrored to DB",
            )
            return
        self._tokens_blob = blob
        if self._persist is not None:
            self._persist(blob)

    # -- Daily aggregation ------------------------------------------

    def get_daily(self, date: str) -> GarminDailyMetrics:
        client = self._require_client()

        sleep = _call(client.get_sleep_data, date)
        hrv = _call(client.get_hrv_data, date)
        body_battery = _call(client.get_body_battery, date)
        stress = _call(client.get_stress_data, date)
        training_status = _call(client.get_training_status, date)
        training_readiness = _call(client.get_training_readiness, date)

        sleep_dto = (sleep or {}).get("dailySleepDTO") or {}
        sleep_scores = sleep_dto.get("sleepScores") or {}
        sleep_overall = sleep_scores.get("overall") or {}
        tlb = (training_status or {}).get("mostRecentTrainingLoadBalance") or {}
        readiness_first: dict[str, Any] = {}
        if isinstance(training_readiness, list) and training_readiness:
            first = training_readiness[0]
            if isinstance(first, dict):
                readiness_first = first
        bb_first: dict[str, Any] = {}
        if isinstance(body_battery, list) and body_battery:
            first = body_battery[0]
            if isinstance(first, dict):
                bb_first = first

        return GarminDailyMetrics(
            local_date=date,
            sleep_score=_int_or_none(sleep_overall.get("value")),
            sleep_duration_s=_int_or_none(sleep_dto.get("sleepTimeSeconds")),
            sleep_efficiency_pct=_float_or_none(
                sleep_dto.get("sleepEfficiencyPercentage"),
            ),
            hrv_overnight_ms=_float_or_none(
                ((hrv or {}).get("hrvSummary") or {}).get("lastNightAvg"),
            ),
            resting_hr_bpm=_int_or_none((sleep or {}).get("restingHeartRate")),
            body_battery_high=_int_or_none(bb_first.get("charged")),
            body_battery_low=_int_or_none(bb_first.get("drained")),
            stress_avg=_int_or_none((stress or {}).get("avgStressLevel")),
            training_load_acute=_float_or_none(tlb.get("metricsTrainingLoadAcute")),
            training_load_chronic=_float_or_none(
                tlb.get("metricsTrainingLoadChronic"),
            ),
            training_readiness=_int_or_none(readiness_first.get("score")),
            extras={},
            raw_payloads_per_endpoint={
                "sleep": sleep,
                "hrv": hrv,
                "body_battery": body_battery,
                "stress": stress,
                "training_load": training_status,
                "training_readiness": training_readiness,
            },
        )

    # -- Activity listing -------------------------------------------

    def list_activities(
        self, *, after: datetime, before: datetime,
    ) -> Iterator[GarminActivitySummary]:
        client = self._require_client()
        startdate = after.astimezone(UTC).strftime("%Y-%m-%d")
        enddate = before.astimezone(UTC).strftime("%Y-%m-%d")
        try:
            activities = client.get_activities_by_date(startdate, enddate)
        except GarminConnectAuthenticationError as exc:
            raise GarminAuthError(str(exc)) from exc
        for activity in activities or []:
            yield _summary_from_garmin(activity)

    # -- Internals --------------------------------------------------

    def _require_client(self) -> Garmin:
        if self._client is None:
            raise RuntimeError(
                "GarminConnectGarminProvider.login() must be called before data calls",
            )
        return self._client


def _call(fn: Callable[[str], Any], date: str) -> Any:
    """Invoke a per-day SDK getter, translating auth errors to GarminAuthError."""
    try:
        return fn(date)
    except GarminConnectAuthenticationError as exc:
        raise GarminAuthError(str(exc)) from exc


def _summary_from_garmin(activity: dict[str, Any]) -> GarminActivitySummary:
    """Map a Garmin ``get_activities_by_date`` element into :class:`GarminActivitySummary`."""
    activity_type = activity.get("activityType") or {}
    type_key = str(activity_type.get("typeKey") or "unknown")

    start_gmt_raw = activity.get("startTimeGMT") or ""
    start_iso = _gmt_to_iso(start_gmt_raw)

    start_local_raw = activity.get("startTimeLocal") or ""
    local_date = (
        start_local_raw[:10] if len(start_local_raw) >= 10 else start_iso[:10]
    )

    return GarminActivitySummary(
        source_id=str(activity.get("activityId", "")),
        activity_type_str=type_key,
        start_time=start_iso,
        local_date=local_date,
        duration_s=_int_or_none(activity.get("duration")) or 0,
        moving_time_s=_int_or_none(activity.get("movingDuration")),
        distance_m=_float_or_none(activity.get("distance")),
        elevation_gain_m=_float_or_none(activity.get("elevationGain")),
        avg_hr_bpm=_int_or_none(activity.get("averageHR")),
        max_hr_bpm=_int_or_none(activity.get("maxHR")),
        calories_kcal=_int_or_none(activity.get("calories")),
        extras={},
        raw_payload=dict(activity),
    )


def _gmt_to_iso(gmt: str) -> str:
    """Parse Garmin's ``"YYYY-MM-DD HH:MM:SS"`` GMT format into ISO 8601 UTC."""
    if not gmt:
        return ""
    try:
        dt = datetime.strptime(gmt, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    except ValueError:
        return ""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


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
