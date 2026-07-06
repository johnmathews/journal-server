"""Tests for the Strava provider (W4 of fitness-tier-plan)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from stravalib.exc import AccessUnauthorized, AuthError, Fault
from stravalib.model import DetailedActivity, SummaryActivity

from journal.providers.strava import (
    StravaActivitySummary,
    StravaAuthError,
    StravalibStravaProvider,
    StravaProvider,
    Tokens,
)

FIXTURES = Path(__file__).parent / "fixtures" / "strava"


def _load_summaries() -> list[SummaryActivity]:
    raw = json.loads((FIXTURES / "list_activities_response.json").read_text())
    return [SummaryActivity.model_validate(item) for item in raw]


class _FakeClient:
    """Stand-in for ``stravalib.Client`` used by all provider tests.

    Captures kwargs the adapter passes to the constructor and to
    ``refresh_access_token`` so tests can assert on the contract
    without spinning up the real HTTP-bearing client.
    """

    def __init__(self, **kwargs: Any) -> None:
        self.init_kwargs = kwargs
        self.access_token = kwargs.get("access_token")
        self.refresh_token = kwargs.get("refresh_token")
        self.token_expires = kwargs.get("token_expires")
        self._activities_to_yield: list[Any] = []
        self._detail_to_return: Any = None
        self._refresh_response: dict[str, Any] = {}
        self.last_get_activities_kwargs: dict[str, Any] = {}
        self.last_refresh_kwargs: dict[str, Any] = {}

    # Test setup helpers ------------------------------------------------
    def queue_activities(self, items: list[Any]) -> None:
        self._activities_to_yield = items

    def queue_detail(self, item: Any) -> None:
        self._detail_to_return = item

    def queue_refresh(self, response: dict[str, Any]) -> None:
        self._refresh_response = response

    # stravalib.Client surface used by the adapter ----------------------
    def get_activities(self, **kwargs: Any) -> Any:
        self.last_get_activities_kwargs = kwargs
        return iter(self._activities_to_yield)

    def get_activity(self, activity_id: int) -> Any:
        self._last_get_activity_id = activity_id
        return self._detail_to_return

    def refresh_access_token(self, **kwargs: Any) -> dict[str, Any]:
        self.last_refresh_kwargs = kwargs
        return self._refresh_response


def _make_provider(
    *,
    fake: _FakeClient | None = None,
    persist_calls: list[Tokens] | None = None,
    token_expires_at: str = "2030-01-01T00:00:00Z",
) -> tuple[StravalibStravaProvider, _FakeClient, list[Tokens]]:
    """Construct a provider wired to a ``_FakeClient``.

    Returns the provider, the fake (so the test can configure it
    after construction), and the persist-capture list (so the test
    can assert what was persisted).
    """
    captured: list[Tokens] = persist_calls if persist_calls is not None else []
    holder: dict[str, _FakeClient] = {}

    def factory(**kwargs: Any) -> _FakeClient:
        c = fake or _FakeClient(**kwargs)
        if fake is not None:
            # init_kwargs still records what the adapter passed
            fake.init_kwargs = kwargs
            fake.access_token = kwargs.get("access_token")
            fake.refresh_token = kwargs.get("refresh_token")
            fake.token_expires = kwargs.get("token_expires")
        holder["c"] = c
        return c  # type: ignore[return-value]

    provider = StravalibStravaProvider(
        client_id="cid",
        client_secret="csecret",
        access_token="atok",
        refresh_token="rtok",
        token_expires_at=token_expires_at,
        persist_tokens=captured.append,
        client_factory=factory,  # type: ignore[arg-type]
    )
    return provider, holder["c"], captured


# 1. Replay-driven happy path ------------------------------------------


def test_list_activities_replays_fixture_into_summary_shape() -> None:
    """Each fixture activity becomes a StravaActivitySummary with the right shape."""
    provider, fake, _ = _make_provider()
    fake.queue_activities(_load_summaries())

    after = datetime(2026, 4, 1, tzinfo=UTC)
    before = datetime(2026, 5, 1, tzinfo=UTC)
    summaries = list(provider.list_activities(after=after, before=before))

    assert len(summaries) == 8
    assert all(isinstance(s, StravaActivitySummary) for s in summaries)
    # adapter passed the window through verbatim
    assert fake.last_get_activities_kwargs == {"after": after, "before": before}

    by_sport = {s.sport_type: s for s in summaries}

    morning_run = by_sport["Run"]
    assert morning_run.source_id == "11000000001"
    assert morning_run.start_time == "2026-04-21T07:15:00Z"
    assert morning_run.local_date == "2026-04-21"
    assert morning_run.duration_s == 1830
    assert morning_run.moving_time_s == 1790
    assert morning_run.distance_m == pytest.approx(5612.4)
    assert morning_run.elevation_gain_m == pytest.approx(42.1)
    assert morning_run.avg_hr_bpm == 148  # int truncation of 148.6
    assert morning_run.max_hr_bpm == 169

    weights = by_sport["WeightTraining"]
    assert weights.distance_m is None
    assert weights.moving_time_s is None
    assert weights.elevation_gain_m is None
    assert weights.duration_s == 2700

    yoga = by_sport["Yoga"]
    assert yoga.avg_hr_bpm is None
    assert yoga.max_hr_bpm is None


def test_summaries_implement_protocol() -> None:
    """The adapter satisfies the StravaProvider Protocol."""
    provider, _, _ = _make_provider()
    assert isinstance(provider, StravaProvider)


def test_raw_payload_is_json_safe_dict() -> None:
    """raw_payload round-trips through json.dumps without conversion errors."""
    provider, fake, _ = _make_provider()
    fake.queue_activities(_load_summaries()[:1])
    summary = next(provider.list_activities(after=datetime(2026, 1, 1, tzinfo=UTC),
                                             before=datetime(2026, 12, 31, tzinfo=UTC)))
    # Must be a dict, must serialise — this is what gets archived raw.
    assert isinstance(summary.raw_payload, dict)
    json.dumps(summary.raw_payload)  # would raise on datetime / Pydantic objects


# 2. Token refresh path ------------------------------------------------


def test_refresh_calls_strava_and_persists_new_triple() -> None:
    """Expired token: adapter refreshes, then invokes persist with the new triple."""
    provider, fake, captured = _make_provider(
        token_expires_at="2020-01-01T00:00:00Z",  # well in the past
    )
    new_expires_epoch = int(
        datetime(2030, 6, 15, 12, 0, 0, tzinfo=UTC).timestamp(),
    )
    fake.queue_refresh(
        {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_at": new_expires_epoch,
            "expires_in": 21600,
            "token_type": "Bearer",
        },
    )

    provider.refresh_token_if_needed()

    assert fake.last_refresh_kwargs == {
        "client_id": "cid",
        "client_secret": "csecret",
        "refresh_token": "rtok",
    }
    assert len(captured) == 1
    persisted = captured[0]
    assert persisted["access_token"] == "new-access"
    assert persisted["refresh_token"] == "new-refresh"
    assert persisted["token_expires_at"] == "2030-06-15T12:00:00Z"
    # In-memory client is re-armed so subsequent calls use the new triple.
    assert fake.access_token == "new-access"
    assert fake.refresh_token == "new-refresh"
    assert fake.token_expires == new_expires_epoch


def test_refresh_skips_when_token_not_expired() -> None:
    """Fresh token: no refresh call, no persist call."""
    future = (datetime.now(UTC) + timedelta(hours=2)).strftime(
        "%Y-%m-%dT%H:%M:%SZ",
    )
    provider, fake, captured = _make_provider(token_expires_at=future)

    provider.refresh_token_if_needed()

    assert captured == []
    assert fake.last_refresh_kwargs == {}


def test_refresh_treats_unparseable_expiry_as_expired() -> None:
    """Garbage token_expires_at string forces a refresh — fail-safe direction."""
    provider, fake, captured = _make_provider(token_expires_at="not-a-date")
    fake.queue_refresh(
        {
            "access_token": "x",
            "refresh_token": "y",
            "expires_at": int(
                datetime(2030, 1, 1, tzinfo=UTC).timestamp(),
            ),
        },
    )
    provider.refresh_token_if_needed()
    assert len(captured) == 1


# 3. Units stay metric -------------------------------------------------


def test_distance_is_metres_not_imperial_converted() -> None:
    """Sentinel: a Strava 'distance: 1609.0' (1 mile in metres) stays at 1609.0.

    Catches a future regression where someone enables units='imperial' on the
    stravalib.Client and the adapter silently emits yards/miles instead of
    metres.
    """
    one_mile_m = 1609.0
    activity = SummaryActivity.model_validate(
        {
            "id": 99,
            "sport_type": "Run",
            "type": "Run",
            "start_date": "2026-04-21T07:15:00Z",
            "start_date_local": "2026-04-21T09:15:00",
            "elapsed_time": 600,
            "moving_time": 590,
            "distance": one_mile_m,
            "total_elevation_gain": 0.0,
            "average_heartrate": 140.0,
            "max_heartrate": 160,
        },
    )
    provider, fake, _ = _make_provider()
    fake.queue_activities([activity])

    [summary] = list(
        provider.list_activities(
            after=datetime(2026, 1, 1, tzinfo=UTC),
            before=datetime(2026, 12, 31, tzinfo=UTC),
        ),
    )
    assert summary.distance_m == pytest.approx(one_mile_m)
    # And it's a plain float, not a Distance/pint quantity.
    assert isinstance(summary.distance_m, float)
    assert type(summary.distance_m) is float


# 4. sport_type collapsing happens in normalize, not provider ----------


@pytest.mark.parametrize(
    "raw_sport_type",
    ["Run", "TrailRun", "WeightTraining", "VirtualRide", "Yoga"],
)
def test_sport_type_passes_through_verbatim(raw_sport_type: str) -> None:
    """The adapter does not collapse sport_type — that's normalize's job."""
    activity = SummaryActivity.model_validate(
        {
            "id": 1,
            "sport_type": raw_sport_type,
            "type": "Workout",  # legacy field, intentionally different
            "start_date": "2026-04-21T07:15:00Z",
            "start_date_local": "2026-04-21T09:15:00",
            "elapsed_time": 600,
            "moving_time": 590,
            "distance": 1000.0,
            "total_elevation_gain": 0.0,
        },
    )
    provider, fake, _ = _make_provider()
    fake.queue_activities([activity])

    [summary] = list(
        provider.list_activities(
            after=datetime(2026, 1, 1, tzinfo=UTC),
            before=datetime(2026, 12, 31, tzinfo=UTC),
        ),
    )
    assert summary.sport_type == raw_sport_type


# 5. get_activity_detail ----------------------------------------------


def test_get_activity_detail_returns_summary_shape() -> None:
    """DetailedActivity flows through the same mapper, including calories."""
    detail = DetailedActivity.model_validate(
        {
            "id": 42,
            "sport_type": "Run",
            "type": "Run",
            "start_date": "2026-04-21T07:15:00Z",
            "start_date_local": "2026-04-21T09:15:00",
            "elapsed_time": 1800,
            "moving_time": 1750,
            "distance": 5000.0,
            "total_elevation_gain": 30.0,
            "average_heartrate": 140.0,
            "max_heartrate": 160,
            "calories": 412.0,
        },
    )
    provider, fake, _ = _make_provider()
    fake.queue_detail(detail)

    summary = provider.get_activity_detail("42")
    assert summary.source_id == "42"
    assert summary.calories_kcal == 412


# 6. 401/403 → typed StravaAuthError ----------------------------------


def _raiser(exc: BaseException) -> Any:
    def _f(*args: Any, **kwargs: Any) -> Any:
        raise exc
    return _f


def test_list_activities_translates_access_unauthorized_to_typed_error() -> None:
    """A 401 from stravalib propagates as StravaAuthError, not the SDK type."""
    provider, fake, _ = _make_provider()
    fake.get_activities = _raiser(  # type: ignore[method-assign]
        AccessUnauthorized("Authorization Error: ..."),
    )

    with pytest.raises(StravaAuthError):
        list(provider.list_activities(
            after=datetime(2026, 4, 1, tzinfo=UTC),
            before=datetime(2026, 5, 1, tzinfo=UTC),
        ))


def test_get_activity_detail_translates_access_unauthorized_to_typed_error() -> None:
    provider, fake, _ = _make_provider()
    fake.get_activity = _raiser(AccessUnauthorized("401"))  # type: ignore[method-assign]

    with pytest.raises(StravaAuthError):
        provider.get_activity_detail("42")


def test_refresh_translates_auth_error_to_typed_error() -> None:
    """A revoked refresh token surfaces as AuthError; adapter wraps it for W6."""
    provider, fake, _ = _make_provider(
        token_expires_at="2020-01-01T00:00:00Z",  # forces refresh
    )
    fake.refresh_access_token = _raiser(  # type: ignore[method-assign]
        AuthError("refresh grant rejected"),
    )

    with pytest.raises(StravaAuthError):
        provider.refresh_token_if_needed()


# 6b. 403 Fault (app deactivated / forbidden) → StravaAuthError -------
#
# stravalib maps only 401 to AccessUnauthorized; a 403 surfaces as a bare
# ``Fault``. In prod this is what a subscriber-only-API cutover looks like:
#   403 Forbidden [{'resource': 'Application', 'field': 'Status',
#                   'code': 'Inactive'}]
# It is a permanent, action-required failure, not a transient blip, so the
# adapter must translate it to StravaAuthError (→ FitnessAuthError →
# auth_broken → notify) rather than letting it fall through to the fetch
# service's transient catch-all.


class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


def _fault(status_code: int, msg: str = "boom") -> Fault:
    return Fault(msg, response=_FakeResponse(status_code))  # type: ignore[arg-type]


_APP_INACTIVE_MSG = (
    "403 Client Error: Forbidden "
    "[Forbidden: [{'resource': 'Application', 'field': 'Status', "
    "'code': 'Inactive'}]]"
)


def test_list_activities_translates_403_fault_to_typed_auth_error() -> None:
    """A 403 Fault (app Inactive / forbidden) becomes StravaAuthError, not Fault."""
    provider, fake, _ = _make_provider()
    fake.get_activities = _raiser(_fault(403, _APP_INACTIVE_MSG))  # type: ignore[method-assign]

    with pytest.raises(StravaAuthError):
        list(provider.list_activities(
            after=datetime(2026, 4, 1, tzinfo=UTC),
            before=datetime(2026, 5, 1, tzinfo=UTC),
        ))


def test_get_activity_detail_translates_403_fault_to_typed_auth_error() -> None:
    provider, fake, _ = _make_provider()
    fake.get_activity = _raiser(_fault(403, _APP_INACTIVE_MSG))  # type: ignore[method-assign]

    with pytest.raises(StravaAuthError):
        provider.get_activity_detail("42")


def test_refresh_translates_403_fault_to_typed_auth_error() -> None:
    provider, fake, _ = _make_provider(
        token_expires_at="2020-01-01T00:00:00Z",  # forces refresh
    )
    fake.refresh_access_token = _raiser(_fault(403, _APP_INACTIVE_MSG))  # type: ignore[method-assign]

    with pytest.raises(StravaAuthError):
        provider.refresh_token_if_needed()


def test_list_activities_leaves_5xx_fault_transient() -> None:
    """A 5xx Fault is a genuine transient failure — it must NOT become an auth error.

    It propagates as the raw Fault so the fetch service classifies it as
    transient_failure and retries, exactly as before.
    """
    provider, fake, _ = _make_provider()
    fake.get_activities = _raiser(_fault(503))  # type: ignore[method-assign]

    with pytest.raises(Fault):
        list(provider.list_activities(
            after=datetime(2026, 4, 1, tzinfo=UTC),
            before=datetime(2026, 5, 1, tzinfo=UTC),
        ))
