"""Tests for the Garmin provider (W5 of fitness-tier-plan)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from garminconnect import GarminConnectAuthenticationError

from journal.providers.garmin import (
    GarminActivitySummary,
    GarminAuthError,
    GarminConnectGarminProvider,
    GarminDailyMetrics,
    GarminProvider,
)

FIXTURES = Path(__file__).parent / "fixtures" / "garmin"


def _load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


class _FakeClient:
    """Stand-in for ``garminconnect.Garmin.client`` with the loads/dumps surface."""

    def __init__(self) -> None:
        self.loaded_blob: str | None = None
        self.dump_value: str = '{"di_token":"d","di_refresh_token":"r","di_client_id":"c"}'

    def loads(self, blob: str) -> None:
        self.loaded_blob = blob

    def dumps(self) -> str:
        return self.dump_value


class _FakeGarmin:
    """Stand-in for ``garminconnect.Garmin`` covering the surface the adapter uses.

    Captures init kwargs, login args, and per-method calls so tests can
    assert on the contract without spinning up the real HTTP-bearing client.
    """

    def __init__(self, **kwargs: Any) -> None:
        self.init_kwargs = kwargs
        self.username = kwargs.get("email")
        self.password = kwargs.get("password")
        self.prompt_mfa = kwargs.get("prompt_mfa")
        self.client = _FakeClient()
        self.login_calls: list[str | None] = []
        self.login_returns: tuple[str | None, str | None] = (None, None)
        self.login_invokes_mfa = False
        self.login_raises: BaseException | None = None
        self._payloads: dict[str, Any] = {}
        self._raises_per_method: dict[str, BaseException] = {}
        self._activities_to_return: list[dict[str, Any]] = []

    # Setup helpers ----------------------------------------------------
    def queue_payload(self, method: str, payload: Any) -> None:
        self._payloads[method] = payload

    def queue_error(self, method: str, exc: BaseException) -> None:
        self._raises_per_method[method] = exc

    def queue_activities(self, items: list[dict[str, Any]]) -> None:
        self._activities_to_return = items

    # Garmin SDK surface ----------------------------------------------
    def login(self, tokenstore: str | None = None) -> tuple[str | None, str | None]:
        self.login_calls.append(tokenstore)
        if self.login_raises is not None:
            raise self.login_raises
        if self.login_invokes_mfa and self.prompt_mfa is not None:
            self.prompt_mfa()
        return self.login_returns

    def _get(self, method: str, default: Any) -> Any:
        if method in self._raises_per_method:
            raise self._raises_per_method[method]
        return self._payloads.get(method, default)

    def get_sleep_data(self, cdate: str) -> dict[str, Any]:
        return self._get("get_sleep_data", {})

    def get_hrv_data(self, cdate: str) -> dict[str, Any] | None:
        return self._get("get_hrv_data", None)

    def get_body_battery(
        self, startdate: str, enddate: str | None = None,
    ) -> list[dict[str, Any]]:
        return self._get("get_body_battery", [])

    def get_stress_data(self, cdate: str) -> dict[str, Any]:
        return self._get("get_stress_data", {})

    def get_training_status(self, cdate: str) -> dict[str, Any]:
        return self._get("get_training_status", {})

    def get_training_readiness(self, cdate: str) -> list[dict[str, Any]]:
        return self._get("get_training_readiness", [])

    def get_activities_by_date(
        self, startdate: str, enddate: str | None = None,
    ) -> list[dict[str, Any]]:
        if "get_activities_by_date" in self._raises_per_method:
            raise self._raises_per_method["get_activities_by_date"]
        return list(self._activities_to_return)


def _make_provider(
    *,
    fake: _FakeGarmin | None = None,
    tokens_blob: str | None = None,
    tokens_path: Path | None = None,
    persist_calls: list[str] | None = None,
) -> tuple[GarminConnectGarminProvider, _FakeGarmin, list[str]]:
    captured: list[str] = persist_calls if persist_calls is not None else []
    holder: dict[str, _FakeGarmin] = {}

    def factory(**kwargs: Any) -> _FakeGarmin:
        if fake is not None:
            fake.init_kwargs = kwargs
            fake.username = kwargs.get("email")
            fake.password = kwargs.get("password")
            fake.prompt_mfa = kwargs.get("prompt_mfa")
            holder["c"] = fake
            return fake
        c = _FakeGarmin(**kwargs)
        holder["c"] = c
        return c

    provider = GarminConnectGarminProvider(
        username="user@example.com",
        password="hunter2",
        tokens_blob=tokens_blob,
        tokens_path=tokens_path,
        persist_tokens=captured.append,
        client_factory=factory,  # type: ignore[arg-type]
    )
    return provider, holder.get("c", fake or _FakeGarmin()), captured


# 1. Replay-driven happy path -----------------------------------------


def test_get_daily_aggregates_all_fields_from_fixtures() -> None:
    """All seven endpoints flow into one fully-populated GarminDailyMetrics."""
    fake = _FakeGarmin()
    fake.queue_payload("get_sleep_data", _load("sleep.json"))
    fake.queue_payload("get_hrv_data", _load("hrv.json"))
    fake.queue_payload("get_body_battery", _load("body_battery.json"))
    fake.queue_payload("get_stress_data", _load("stress.json"))
    fake.queue_payload("get_training_status", _load("training_status.json"))
    fake.queue_payload("get_training_readiness", _load("training_readiness.json"))
    provider, _, _ = _make_provider(fake=fake)
    provider.login()

    metrics = provider.get_daily("2026-04-15")

    assert isinstance(metrics, GarminDailyMetrics)
    assert metrics.local_date == "2026-04-15"
    assert metrics.sleep_score == 84
    assert metrics.sleep_duration_s == 27180
    assert metrics.sleep_efficiency_pct == pytest.approx(92.7)
    assert metrics.resting_hr_bpm == 51
    assert metrics.hrv_overnight_ms == pytest.approx(47.5)
    assert metrics.body_battery_high == 78
    assert metrics.body_battery_low == 41
    assert metrics.stress_avg == 31
    assert metrics.training_load_acute == pytest.approx(412.5)
    assert metrics.training_load_chronic == pytest.approx(380.0)
    assert metrics.training_readiness == 78

    # All seven endpoint payloads are preserved verbatim for the raw archive.
    assert set(metrics.raw_payloads_per_endpoint.keys()) == {
        "sleep", "hrv", "body_battery", "stress",
        "training_status", "training_readiness",
    }
    for payload in metrics.raw_payloads_per_endpoint.values():
        # Every payload round-trips through json.dumps — what gets archived.
        json.dumps(payload)


def test_provider_implements_protocol() -> None:
    provider, _, _ = _make_provider()
    assert isinstance(provider, GarminProvider)


# 2. Partial-data resilience ------------------------------------------


def test_get_daily_handles_missing_hrv_reading() -> None:
    """User took the watch off → HRV endpoint returns empty payload, others survive."""
    fake = _FakeGarmin()
    fake.queue_payload("get_sleep_data", _load("sleep.json"))
    fake.queue_payload("get_hrv_data", None)  # SDK returns None when no reading
    fake.queue_payload("get_body_battery", _load("body_battery.json"))
    fake.queue_payload("get_stress_data", _load("stress.json"))
    fake.queue_payload("get_training_status", _load("training_status.json"))
    fake.queue_payload("get_training_readiness", _load("training_readiness.json"))
    provider, _, _ = _make_provider(fake=fake)
    provider.login()

    metrics = provider.get_daily("2026-04-15")

    assert metrics.hrv_overnight_ms is None
    # Other fields still populated.
    assert metrics.sleep_score == 84
    assert metrics.body_battery_high == 78
    assert metrics.training_readiness == 78
    # The (empty) HRV response is still recorded in raw_payloads_per_endpoint.
    assert "hrv" in metrics.raw_payloads_per_endpoint
    assert metrics.raw_payloads_per_endpoint["hrv"] in (None, {})


def test_get_daily_tolerates_completely_empty_payloads() -> None:
    """All endpoints return empty/None — every metric becomes None, no exception."""
    fake = _FakeGarmin()
    # No queued payloads → defaults to {} / [] / None
    provider, _, _ = _make_provider(fake=fake)
    provider.login()

    metrics = provider.get_daily("2026-04-15")

    assert metrics.local_date == "2026-04-15"
    assert metrics.sleep_score is None
    assert metrics.sleep_duration_s is None
    assert metrics.hrv_overnight_ms is None
    assert metrics.body_battery_high is None
    assert metrics.body_battery_low is None
    assert metrics.stress_avg is None
    assert metrics.training_load_acute is None
    assert metrics.training_load_chronic is None
    assert metrics.training_readiness is None
    assert metrics.resting_hr_bpm is None


# 3. MFA callback wiring ----------------------------------------------


def test_login_invokes_mfa_callback_when_garmin_prompts() -> None:
    """Stub Garmin.login to invoke prompt_mfa; assert callback fired and login succeeded."""
    fake = _FakeGarmin()
    fake.login_invokes_mfa = True
    provider, _, _ = _make_provider(fake=fake)

    invocations: list[str] = []

    def mfa() -> str:
        invocations.append("called")
        return "123456"

    provider.login(mfa_callback=mfa)

    assert invocations == ["called"]
    # The adapter passed our callback through to the SDK constructor.
    assert fake.init_kwargs["prompt_mfa"] is mfa
    # Network login path executed (no tokenstore arg → username/password flow).
    assert fake.login_calls == [None]


def test_login_falls_back_to_password_when_no_tokens() -> None:
    fake = _FakeGarmin()
    provider, _, _ = _make_provider(fake=fake)
    provider.login()

    # No blob, no path → SDK invoked with tokenstore=None.
    assert fake.login_calls == [None]
    assert fake.init_kwargs["email"] == "user@example.com"
    assert fake.init_kwargs["password"] == "hunter2"


def test_login_loads_tokens_blob_from_db_first(tmp_path: Path) -> None:
    """D4: DB blob is the source of truth — preferred over filesystem cache."""
    blob = '{"di_token":"FROM_DB","di_refresh_token":"r","di_client_id":"c"}'
    cache = tmp_path / "garmin_tokens.json"
    cache.write_text('{"di_token":"FROM_FILE"}')  # should NOT be read
    fake = _FakeGarmin()
    provider, _, _ = _make_provider(
        fake=fake, tokens_blob=blob, tokens_path=cache,
    )
    provider.login()

    # DB blob was hydrated directly into the SDK client, no network login.
    assert fake.client.loaded_blob == blob
    assert fake.login_calls == []


def test_login_falls_back_to_filesystem_cache_when_no_blob(tmp_path: Path) -> None:
    """No DB row yet → use filesystem cache; SDK handles it via tokenstore=path."""
    cache_dir = tmp_path / "garmin_tokens"
    cache_dir.mkdir()
    fake = _FakeGarmin()
    provider, _, _ = _make_provider(fake=fake, tokens_path=cache_dir)
    provider.login()

    # SDK's login was called with the path string.
    assert fake.login_calls == [str(cache_dir)]


def test_login_persists_tokens_after_network_login(tmp_path: Path) -> None:
    """After a network login, the JSON blob is mirrored back to the persist callback."""
    fake = _FakeGarmin()
    fake.client.dump_value = '{"di_token":"NEW","di_refresh_token":"R","di_client_id":"C"}'
    provider, _, captured = _make_provider(fake=fake)
    provider.login()

    assert captured == [
        '{"di_token":"NEW","di_refresh_token":"R","di_client_id":"C"}',
    ]


def test_login_does_not_persist_when_blob_already_valid() -> None:
    """DB blob path skips network login, so no persist callback (already in DB)."""
    blob = '{"di_token":"FROM_DB","di_refresh_token":"r","di_client_id":"c"}'
    fake = _FakeGarmin()
    provider, _, captured = _make_provider(fake=fake, tokens_blob=blob)
    provider.login()

    assert captured == []


# 4. activity_type_str is verbatim ------------------------------------


@pytest.mark.parametrize(
    "type_key",
    [
        "running",
        "treadmill_running",
        "cycling",
        "mountain_biking",
        "lap_swimming",
        "strength_training",
        "yoga",
    ],
)
def test_activity_type_str_passes_through_verbatim(type_key: str) -> None:
    """Garmin's typeKey strings flow through unchanged — normalize collapses, not provider."""
    activity = {
        "activityId": 1,
        "activityType": {"typeId": 1, "typeKey": type_key, "parentTypeId": 17},
        "startTimeGMT": "2026-04-21 07:15:00",
        "startTimeLocal": "2026-04-21 09:15:00",
        "duration": 1000.0,
        "movingDuration": 950.0,
        "distance": 1000.0,
        "elevationGain": 0.0,
    }
    fake = _FakeGarmin()
    fake.queue_activities([activity])
    provider, _, _ = _make_provider(fake=fake)
    provider.login()

    [summary] = list(
        provider.list_activities(
            after=datetime(2026, 4, 1, tzinfo=UTC),
            before=datetime(2026, 5, 1, tzinfo=UTC),
        ),
    )
    assert summary.activity_type_str == type_key


# 5. list_activities replay-driven happy path -------------------------


def test_list_activities_replays_fixture_into_summary_shape() -> None:
    fake = _FakeGarmin()
    fake.queue_activities(_load("list_activities_response.json"))
    provider, _, _ = _make_provider(fake=fake)
    provider.login()

    summaries = list(
        provider.list_activities(
            after=datetime(2026, 4, 1, tzinfo=UTC),
            before=datetime(2026, 5, 1, tzinfo=UTC),
        ),
    )

    assert len(summaries) == 8
    assert all(isinstance(s, GarminActivitySummary) for s in summaries)

    by_type = {s.activity_type_str: s for s in summaries}

    morning_run = by_type["running"]
    assert morning_run.source_id == "22000000001"
    assert morning_run.start_time == "2026-04-21T07:15:00Z"
    assert morning_run.local_date == "2026-04-21"
    assert morning_run.duration_s == 1830
    assert morning_run.moving_time_s == 1790
    assert morning_run.distance_m == pytest.approx(5612.4)
    assert morning_run.elevation_gain_m == pytest.approx(42.1)
    assert morning_run.avg_hr_bpm == 148
    assert morning_run.max_hr_bpm == 169
    assert morning_run.calories_kcal == 412

    strength = by_type["strength_training"]
    assert strength.distance_m is None
    assert strength.moving_time_s is None
    assert strength.elevation_gain_m is None
    assert strength.duration_s == 2700

    yoga = by_type["yoga"]
    assert yoga.avg_hr_bpm is None
    assert yoga.max_hr_bpm is None


def test_list_activities_raw_payload_is_json_safe() -> None:
    fake = _FakeGarmin()
    fake.queue_activities(_load("list_activities_response.json"))
    provider, _, _ = _make_provider(fake=fake)
    provider.login()

    summary = next(provider.list_activities(
        after=datetime(2026, 4, 1, tzinfo=UTC),
        before=datetime(2026, 5, 1, tzinfo=UTC),
    ))
    assert isinstance(summary.raw_payload, dict)
    json.dumps(summary.raw_payload)


# 6. 401/403 → typed GarminAuthError ----------------------------------


def test_get_daily_translates_auth_error_to_typed_exception() -> None:
    """401 from the SDK propagates as GarminAuthError so the fetch service can act."""
    fake = _FakeGarmin()
    fake.queue_error(
        "get_sleep_data",
        GarminConnectAuthenticationError("401 Unauthorized"),
    )
    provider, _, _ = _make_provider(fake=fake)
    provider.login()

    with pytest.raises(GarminAuthError):
        provider.get_daily("2026-04-15")


def test_list_activities_translates_auth_error_to_typed_exception() -> None:
    fake = _FakeGarmin()
    fake.queue_error(
        "get_activities_by_date",
        GarminConnectAuthenticationError("401 Unauthorized"),
    )
    provider, _, _ = _make_provider(fake=fake)
    provider.login()

    with pytest.raises(GarminAuthError):
        list(provider.list_activities(
            after=datetime(2026, 4, 1, tzinfo=UTC),
            before=datetime(2026, 5, 1, tzinfo=UTC),
        ))


def test_login_translates_auth_error_to_typed_exception() -> None:
    fake = _FakeGarmin()
    fake.login_raises = GarminConnectAuthenticationError("invalid credentials")
    provider, _, _ = _make_provider(fake=fake)

    with pytest.raises(GarminAuthError):
        provider.login()
