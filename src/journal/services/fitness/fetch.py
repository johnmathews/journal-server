"""Fitness fetch service — orchestrates one sync run per source.

Per W6 of ``docs/fitness-tier-plan.md``. The fetch service owns the
state machine: load auth → single-run guard → start run → fetch (via
the W4/W5 Protocol) → classify outcome (success / auth_broken /
transient_failure) → transition auth / fire notifications / finish run.
Normalization is W7's concern; raw payloads are persisted here, derived
rows come later.

The service has zero references to ``stravalib`` / ``garminconnect``
types — only the Protocols from
:mod:`journal.providers.strava` and :mod:`journal.providers.garmin` and
the typed :class:`StravaAuthError` / :class:`GarminAuthError`. Anything
else raised by a provider call is treated as a transient failure (with
loud logging, per the plan's "unknown error" path).

Threshold counter for ``notif_fitness_sync_failure`` reads the most
recent N+1 sync-run rows after finishing the current run. If the
streak of consecutive ``transient_failure`` statuses (counting from
the most recent) is *exactly* N, the notification fires — fire-on-Nth,
not fire-every-failure. See ``_consecutive_transient_streak`` for the
streak logic.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol

from journal.providers.garmin import (
    GarminActivitySummary,
    GarminAuthError,
    GarminDailyMetrics,
    GarminProvider,
)
from journal.providers.strava import (
    StravaActivitySummary,
    StravaAuthError,
    StravaProvider,
)
from journal.services.fitness.errors import FitnessAuthError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from journal.config import Config
    from journal.db.fitness_repository import FitnessRepository
    from journal.models import FitnessAuthState

log = logging.getLogger(__name__)


SyncStatus = Literal["success", "auth_broken", "transient_failure", "running"]


@dataclass(frozen=True)
class FitnessSyncResult:
    """Outcome of one ``run_sync`` call.

    Workers (W8) serialise this via ``dataclasses.asdict``. ``status``
    matches the terminal states recorded on the ``fitness_sync_runs``
    row, plus ``"running"`` which is only returned when the single-run
    guard short-circuits (caller hit a sync that's already in flight).
    """

    status: SyncStatus
    run_id: int
    rows_fetched: int
    rows_normalized: int


class FitnessNotifier(Protocol):
    """The notification surface the fetch service depends on.

    Structurally satisfied by
    :class:`journal.services.notifications.PushoverNotificationService`.
    Decoupling the fetch service from the full notification class keeps
    tests trivial and means swapping the notification backend doesn't
    reach into ``services/fitness/``.
    """

    def notify_fitness_auth_broken(self, user_id: int, source: str) -> None: ...

    def notify_fitness_sync_failure(
        self, user_id: int, source: str, attempts: int,
    ) -> None: ...


class _FetchServiceBase:
    """Shared lifecycle for both source services.

    Subclasses fill in the source name, provider construction, and the
    fetch-and-persist body. Everything else — guards, run-row
    bookkeeping, error classification, threshold counting, notifications
    — lives here so the two source classes are differentiated only by
    what they need to be different on.
    """

    SOURCE: str = ""

    def __init__(
        self,
        *,
        repo: FitnessRepository,
        notifier: FitnessNotifier,
        config: Config,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repo = repo
        self._notifier = notifier
        self._config = config
        self._clock: Callable[[], datetime] = clock or (lambda: datetime.now(UTC))

    def run_sync(
        self,
        *,
        user_id: int,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> FitnessSyncResult:
        existing = self._repo.find_running_sync_run(
            user_id=user_id, source=self.SOURCE,
        )
        if existing is not None:
            log.info(
                "Skipping %s sync for user %d — run %d already in flight",
                self.SOURCE, user_id, existing,
            )
            return FitnessSyncResult(
                status="running", run_id=existing,
                rows_fetched=0, rows_normalized=0,
            )

        auth = self._repo.get_auth_state(user_id=user_id, source=self.SOURCE)
        run_id = self._repo.start_sync_run(user_id=user_id, source=self.SOURCE)

        if auth is None or not self._has_credentials(auth):
            self._repo.finish_sync_run(
                run_id, status="auth_broken",
                error_class="MissingAuthState",
                error_message="No fitness_auth_state row or credentials for user",
            )
            return FitnessSyncResult(
                status="auth_broken", run_id=run_id,
                rows_fetched=0, rows_normalized=0,
            )

        provider = self._build_provider(auth)
        since_dt, until_dt = self._derive_window(user_id, since, until)

        try:
            rows_fetched = self._do_fetch_and_persist(
                provider=provider,
                since=since_dt,
                until=until_dt,
                sync_run_id=run_id,
                user_id=user_id,
            )
        except FitnessAuthError as exc:
            transitioned = self._repo.transition_auth(
                user_id=user_id, source=self.SOURCE,
                status="broken", at=_now_iso(),
            )
            self._repo.finish_sync_run(
                run_id, status="auth_broken",
                error_class=exc.__class__.__name__,
                error_message=str(exc),
            )
            if transitioned:
                self._notifier.notify_fitness_auth_broken(user_id, self.SOURCE)
            return FitnessSyncResult(
                status="auth_broken", run_id=run_id,
                rows_fetched=0, rows_normalized=0,
            )
        except Exception as exc:  # noqa: BLE001  treat unknown as transient (test 7)
            log.warning(
                "Fitness sync %s for user %d failed transiently: %s",
                self.SOURCE, user_id, exc, exc_info=True,
            )
            self._repo.finish_sync_run(
                run_id, status="transient_failure",
                error_class=exc.__class__.__name__,
                error_message=str(exc),
            )
            self._maybe_fire_threshold_alert(user_id)
            return FitnessSyncResult(
                status="transient_failure", run_id=run_id,
                rows_fetched=0, rows_normalized=0,
            )

        self._repo.transition_auth(
            user_id=user_id, source=self.SOURCE,
            status="ok", at=_now_iso(),
        )
        self._repo.finish_sync_run(
            run_id, status="success",
            rows_fetched=rows_fetched,
        )
        return FitnessSyncResult(
            status="success", run_id=run_id,
            rows_fetched=rows_fetched, rows_normalized=0,
        )

    # ── Subclass hooks ──────────────────────────────────────────────

    def _has_credentials(self, auth: FitnessAuthState) -> bool:
        """Whether ``auth`` carries the credential this source needs.

        Default: ``access_token`` is set (the OAuth pattern Strava
        uses). Garmin overrides this — its W11 re-auth persists the
        live credential as ``extra_state["tokens_blob"]`` and leaves
        ``access_token`` as ``None``, so the default check would
        spuriously short-circuit to ``MissingAuthState``.

        Decoupling the credential check from a fixed column lets us
        add per-source auth shapes without reaching back into
        :meth:`run_sync`. Each subclass owns the answer to "do I have
        what I need to call the provider?".
        """
        return bool(auth.access_token)

    def _build_provider(self, auth: FitnessAuthState) -> Any:
        raise NotImplementedError

    def _do_fetch_and_persist(
        self,
        *,
        provider: Any,
        since: datetime,
        until: datetime,
        sync_run_id: int,
        user_id: int,
    ) -> int:
        raise NotImplementedError

    # ── Internals ───────────────────────────────────────────────────

    def _derive_window(
        self,
        user_id: int,
        since: datetime | None,
        until: datetime | None,
    ) -> tuple[datetime, datetime]:
        if until is None:
            until = self._clock()
        if since is None:
            last_ok = self._repo.last_successful_sync_at(
                user_id=user_id, source=self.SOURCE,
            )
            backfill = self._config.fitness_backfill_start
            since = max(_parse_iso(last_ok), _parse_date(backfill))
        return since, until

    def _maybe_fire_threshold_alert(self, user_id: int) -> None:
        threshold = self._config.fitness_transient_failure_threshold
        recent = self._repo.list_recent_sync_runs(
            user_id=user_id, source=self.SOURCE, limit=threshold + 1,
        )
        streak = _consecutive_transient_streak(recent)
        if streak == threshold:
            self._notifier.notify_fitness_sync_failure(
                user_id, self.SOURCE, attempts=threshold,
            )


class StravaFetchService(_FetchServiceBase):
    """Fetch service for Strava."""

    SOURCE = "strava"

    def __init__(
        self,
        *,
        repo: FitnessRepository,
        notifier: FitnessNotifier,
        config: Config,
        provider_factory: Callable[[FitnessAuthState], StravaProvider],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(repo=repo, notifier=notifier, config=config, clock=clock)
        self._provider_factory = provider_factory

    def _build_provider(self, auth: FitnessAuthState) -> StravaProvider:
        return self._provider_factory(auth)

    def _do_fetch_and_persist(
        self,
        *,
        provider: StravaProvider,
        since: datetime,
        until: datetime,
        sync_run_id: int,
        user_id: int,
    ) -> int:
        try:
            provider.refresh_token_if_needed()
            activities: Iterable[StravaActivitySummary] = provider.list_activities(
                after=since, before=until,
            )
            return _persist_activity_rows(
                repo=self._repo, source="strava",
                user_id=user_id, sync_run_id=sync_run_id,
                summaries=(_strava_raw_row(a) for a in activities),
            )
        except StravaAuthError as exc:
            raise FitnessAuthError(str(exc)) from exc


class GarminFetchService(_FetchServiceBase):
    """Fetch service for Garmin."""

    SOURCE = "garmin"

    def __init__(
        self,
        *,
        repo: FitnessRepository,
        notifier: FitnessNotifier,
        config: Config,
        provider_factory: Callable[[FitnessAuthState], GarminProvider],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(repo=repo, notifier=notifier, config=config, clock=clock)
        self._provider_factory = provider_factory

    def _has_credentials(self, auth: FitnessAuthState) -> bool:
        """Garmin's live credential is the ``tokens_blob`` from W11 re-auth.

        ``access_token`` stays ``None`` for Garmin rows (the OAuth-style
        triple isn't applicable to ``garminconnect``'s session model);
        the real credential is the persisted blob in ``extra_state``.
        """
        return bool(auth.extra_state and auth.extra_state.get("tokens_blob"))

    def _build_provider(self, auth: FitnessAuthState) -> GarminProvider:
        return self._provider_factory(auth)

    def _do_fetch_and_persist(
        self,
        *,
        provider: GarminProvider,
        since: datetime,
        until: datetime,
        sync_run_id: int,
        user_id: int,
    ) -> int:
        try:
            provider.login()
            rows = 0
            for date_str in _dates_in_window(since, until):
                metrics = provider.get_daily(date_str)
                rows += _persist_garmin_daily_rows(
                    repo=self._repo, user_id=user_id,
                    sync_run_id=sync_run_id, metrics=metrics,
                )
            activities: Iterable[GarminActivitySummary] = provider.list_activities(
                after=since, before=until,
            )
            rows += _persist_activity_rows(
                repo=self._repo, source="garmin",
                user_id=user_id, sync_run_id=sync_run_id,
                summaries=(_garmin_activity_raw_row(a) for a in activities),
            )
            return rows
        except GarminAuthError as exc:
            raise FitnessAuthError(str(exc)) from exc


# ── Helpers ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _RawRowSpec:
    endpoint: str
    source_id: str
    payload: dict[str, Any]


def _strava_raw_row(activity: StravaActivitySummary) -> _RawRowSpec:
    return _RawRowSpec(
        endpoint="activities",
        source_id=activity.source_id,
        payload=activity.raw_payload,
    )


def _garmin_activity_raw_row(activity: GarminActivitySummary) -> _RawRowSpec:
    return _RawRowSpec(
        endpoint="activities",
        source_id=activity.source_id,
        payload=activity.raw_payload,
    )


def _persist_activity_rows(
    *,
    repo: FitnessRepository,
    source: Literal["strava", "garmin"],
    user_id: int,
    sync_run_id: int,
    summaries: Iterable[_RawRowSpec],
) -> int:
    rows = 0
    for spec in summaries:
        new_id = repo.insert_raw(
            source=source, user_id=user_id,
            endpoint=spec.endpoint, source_id=spec.source_id,
            payload_json=json.dumps(spec.payload, default=str, sort_keys=True),
            sync_run_id=sync_run_id,
        )
        if new_id is not None:
            rows += 1
    return rows


def _persist_garmin_daily_rows(
    *,
    repo: FitnessRepository,
    user_id: int,
    sync_run_id: int,
    metrics: GarminDailyMetrics,
) -> int:
    """One row per endpoint per day, source_id = local_date.

    Matches the schema's ``fitness_raw_garmin (user_id, endpoint,
    source_id, payload_sha256)`` UNIQUE constraint. An empty / None
    payload is still persisted so the raw archive is faithful — the
    SHA256 differs from a populated one, so re-fetches that get more
    data later append a new row rather than no-op.
    """
    rows = 0
    for endpoint, payload in metrics.raw_payloads_per_endpoint.items():
        new_id = repo.insert_raw(
            source="garmin", user_id=user_id,
            endpoint=endpoint, source_id=metrics.local_date,
            payload_json=json.dumps(payload, default=str, sort_keys=True),
            sync_run_id=sync_run_id,
        )
        if new_id is not None:
            rows += 1
    return rows


def _dates_in_window(since: datetime, until: datetime) -> Iterable[str]:
    """Yield ``YYYY-MM-DD`` for each calendar day in ``[since, until]``."""
    if since > until:
        return
    cur = since.astimezone(UTC).date()
    last = until.astimezone(UTC).date()
    while cur <= last:
        yield cur.isoformat()
        cur = cur.fromordinal(cur.toordinal() + 1)


def _consecutive_transient_streak(recent: list[Any]) -> int:
    """Count consecutive ``transient_failure`` statuses from the head of the list.

    The list is ordered most-recent-first by
    ``list_recent_sync_runs``. Stops counting at the first non-transient
    entry (or end of list).
    """
    streak = 0
    for run in recent:
        if run.status == "transient_failure":
            streak += 1
        else:
            break
    return streak


def _parse_iso(value: str | None) -> datetime:
    if not value:
        return datetime.fromtimestamp(0, tz=UTC)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.fromtimestamp(0, tz=UTC)


def _parse_date(value: str) -> datetime:
    """Parse ``YYYY-MM-DD`` (the config default for ``fitness_backfill_start``)."""
    return datetime.fromisoformat(value + "T00:00:00+00:00")


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
