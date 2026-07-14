"""W3 — REST API tests for the per-user Strava OAuth flow.

The endpoints under test:

- ``GET  /api/fitness/strava/authorize_url``
- ``POST /api/fitness/strava/exchange``
- ``POST /api/fitness/strava/disconnect``

Auth shape mirrors ``test_api_fitness_garmin_auth.py``: a fake auth middleware
injects an :class:`AuthenticatedUser` so the per-route ``get_authenticated_user``
call sees a consistent user. The cross-user replay test (D4) builds two
clients with two injected users so a state token issued under user A is
consumed under user B's auth context.

The Strava SDK boundary is stubbed via the ``strava_exchange_code`` services-
dict entry — production wiring resolves it to
``providers.strava.exchange_code``. The fake takes ``client_id``,
``client_secret``, ``code`` kwargs (matching the real signature) and returns
``(Tokens, athlete_id_str | None)`` per the W3 shape change.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.testclient import TestClient
from stravalib.exc import AccessUnauthorized

from journal.auth import AuthenticatedUser, _current_user_id
from journal.db.factory import ConnectionFactory
from journal.db.fitness_repository import FitnessRepository
from journal.db.migrations import run_migrations
from journal.models import FitnessAuthState
from journal.providers.strava import Tokens
from journal.services.fitness.strava_pending import StravaPendingStore

if TYPE_CHECKING:
    from pathlib import Path


# ── Fake exchange_code factory ────────────────────────────────────────


class FakeExchangeCode:
    """Stand-in for ``providers.strava.exchange_code``.

    Configurable per test to return either a successful ``(Tokens,
    athlete_id)`` tuple or to raise an SDK-side exception. Records the
    call kwargs so tests can assert correct wiring (client_id /
    client_secret / code reach the SDK).
    """

    def __init__(
        self,
        *,
        athlete_id: str | None = "11111",
        tokens: Tokens | None = None,
        raises: BaseException | None = None,
    ) -> None:
        self._athlete_id = athlete_id
        self._tokens = tokens or Tokens(
            access_token="acc_FAKE",
            refresh_token="ref_FAKE",
            token_expires_at="2026-12-01T00:00:00Z",
        )
        self._raises = raises
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        *,
        client_id: str,
        client_secret: str,
        code: str,
    ) -> tuple[Tokens, str | None]:
        self.calls.append(
            {"client_id": client_id, "client_secret": client_secret, "code": code},
        )
        if self._raises is not None:
            raise self._raises
        return self._tokens, self._athlete_id


# ── Auth + test client ───────────────────────────────────────────────


class _UserAuthMiddleware:
    """Injects a configurable :class:`AuthenticatedUser` into every request."""

    def __init__(self, app: Any, user_id: int) -> None:
        self.app = app
        self._user_id = user_id

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] in ("http", "websocket"):
            scope["user"] = AuthenticatedUser(
                user_id=self._user_id,
                email=f"user{self._user_id}@example.com",
                display_name=f"User {self._user_id}",
                is_admin=False,
                is_active=True,
                email_verified=True,
            )
            token = _current_user_id.set(self._user_id)
            try:
                await self.app(scope, receive, send)
            finally:
                _current_user_id.reset(token)
        else:
            await self.app(scope, receive, send)


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def fitness_factory(tmp_path: Path) -> ConnectionFactory:
    db_path = tmp_path / "strava-auth.db"
    f = ConnectionFactory(db_path)
    run_migrations(f.get())
    return f


@pytest.fixture
def fitness_repo(fitness_factory: ConnectionFactory) -> FitnessRepository:
    return FitnessRepository(fitness_factory)


@pytest.fixture
def pending_store() -> StravaPendingStore:
    return StravaPendingStore()


def _build_services(
    *,
    fitness_factory: ConnectionFactory,
    fitness_repo: FitnessRepository,
    pending_store: StravaPendingStore,
    exchange: Any,
    strava_client_id: str = "12345",
    strava_client_secret: str = "secret-XYZ",
    strava_redirect_uri: str = "https://webapp.example.com/settings/fitness/strava/callback",
    strava_enabled: bool = True,
) -> dict:
    class _StubConfig:
        pass

    cfg = _StubConfig()
    cfg.strava_client_id = strava_client_id
    cfg.strava_client_secret = strava_client_secret
    cfg.strava_redirect_uri = strava_redirect_uri
    # W1 strava-mothball: tests that exercise the live OAuth flow opt in
    # explicitly — production defaults to disabled (STRAVA_ENABLED=false).
    cfg.strava_enabled = strava_enabled

    return {
        "fitness_repo": fitness_repo,
        "db_factory": fitness_factory,
        "strava_pending": pending_store,
        "strava_exchange_code": exchange,
        "config": cfg,
    }


def _build_client(services: dict, *, user_id: int = 1) -> TestClient:
    from mcp.server.fastmcp import FastMCP

    from journal.api import register_api_routes

    test_mcp = FastMCP(f"test-strava-auth-user-{user_id}")
    register_api_routes(test_mcp, lambda: services)
    app = _UserAuthMiddleware(test_mcp.streamable_http_app(), user_id=user_id)
    return TestClient(app, raise_server_exceptions=False)


# ── Tests: authorize_url ─────────────────────────────────────────────


def test_authorize_url_returns_url_state_and_expiry(
    fitness_factory: ConnectionFactory,
    fitness_repo: FitnessRepository,
    pending_store: StravaPendingStore,
) -> None:
    services = _build_services(
        fitness_factory=fitness_factory, fitness_repo=fitness_repo,
        pending_store=pending_store, exchange=FakeExchangeCode(),
    )
    with _build_client(services, user_id=1) as client:
        resp = client.get("/api/fitness/strava/authorize_url")

    assert resp.status_code == 200
    body = resp.json()
    assert "authorize_url" in body
    assert "state" in body
    assert body["expires_at"].endswith("Z")

    parsed = urlparse(body["authorize_url"])
    assert parsed.scheme == "https"
    assert parsed.netloc == "www.strava.com"
    assert parsed.path == "/oauth/authorize"
    qs = parse_qs(parsed.query)
    assert qs["client_id"] == ["12345"]
    assert qs["redirect_uri"] == [
        "https://webapp.example.com/settings/fitness/strava/callback",
    ]
    assert qs["response_type"] == ["code"]
    assert qs["scope"] == ["read,activity:read_all"]
    # State must be the same token returned in the body.
    assert qs["state"] == [body["state"]]

    # State entry was stored under the calling user's id.
    entry = pending_store.peek(body["state"])
    assert entry is not None
    assert entry.user_id == 1


def test_authorize_url_missing_client_id_returns_500(
    fitness_factory: ConnectionFactory,
    fitness_repo: FitnessRepository,
    pending_store: StravaPendingStore,
) -> None:
    services = _build_services(
        fitness_factory=fitness_factory, fitness_repo=fitness_repo,
        pending_store=pending_store, exchange=FakeExchangeCode(),
        strava_client_id="",
    )
    with _build_client(services) as client:
        resp = client.get("/api/fitness/strava/authorize_url")
    assert resp.status_code == 500
    assert "STRAVA_CLIENT_ID" in resp.json()["error"]


# ── Tests: exchange happy path ───────────────────────────────────────


def test_exchange_happy_path_persists_tokens_and_athlete_id(
    fitness_factory: ConnectionFactory,
    fitness_repo: FitnessRepository,
    pending_store: StravaPendingStore,
) -> None:
    fake = FakeExchangeCode(athlete_id="98765432")
    services = _build_services(
        fitness_factory=fitness_factory, fitness_repo=fitness_repo,
        pending_store=pending_store, exchange=fake,
    )
    with _build_client(services, user_id=1) as client:
        auth = client.get("/api/fitness/strava/authorize_url")
        state = auth.json()["state"]
        resp = client.post(
            "/api/fitness/strava/exchange",
            json={"code": "AUTH_CODE_XYZ", "state": state},
        )

    assert resp.status_code == 200
    assert resp.json() == {"connected": True, "upstream_user_id": "98765432"}

    # SDK was called with the configured app credentials and the user's code.
    assert len(fake.calls) == 1
    assert fake.calls[0]["code"] == "AUTH_CODE_XYZ"
    assert fake.calls[0]["client_id"] == "12345"
    assert fake.calls[0]["client_secret"] == "secret-XYZ"

    state_row = fitness_repo.get_auth_state(user_id=1, source="strava")
    assert state_row is not None
    assert state_row.access_token == "acc_FAKE"
    assert state_row.refresh_token == "ref_FAKE"
    assert state_row.token_expires_at == "2026-12-01T00:00:00Z"
    assert state_row.auth_status == "ok"
    assert state_row.auth_broken_since is None
    assert state_row.last_successful_login_at is not None
    # Stored as a string for parity with Garmin's D8 mismatch comparison.
    assert state_row.extra_state.get("upstream_user_id") == "98765432"

    # State was consumed.
    assert pending_store.peek(state) is None


def test_exchange_missing_body_fields_returns_400(
    fitness_factory: ConnectionFactory,
    fitness_repo: FitnessRepository,
    pending_store: StravaPendingStore,
) -> None:
    services = _build_services(
        fitness_factory=fitness_factory, fitness_repo=fitness_repo,
        pending_store=pending_store, exchange=FakeExchangeCode(),
    )
    with _build_client(services) as client:
        resp = client.post(
            "/api/fitness/strava/exchange", json={"code": "ABC"},
        )
    assert resp.status_code == 400


# ── Tests: state validation ──────────────────────────────────────────


def test_exchange_unknown_state_returns_410(
    fitness_factory: ConnectionFactory,
    fitness_repo: FitnessRepository,
    pending_store: StravaPendingStore,
) -> None:
    services = _build_services(
        fitness_factory=fitness_factory, fitness_repo=fitness_repo,
        pending_store=pending_store, exchange=FakeExchangeCode(),
    )
    with _build_client(services) as client:
        resp = client.post(
            "/api/fitness/strava/exchange",
            json={"code": "ABC", "state": "no-such-state"},
        )
    assert resp.status_code == 410
    assert resp.json().get("reason") == "expired_pending_state"


def test_exchange_expired_state_returns_410(
    fitness_factory: ConnectionFactory,
    fitness_repo: FitnessRepository,
) -> None:
    """Roll the pending store's clock past TTL between issue and exchange."""

    class _Clock:
        t = 5000.0

        def __call__(self) -> float:
            return self.t

    clock = _Clock()
    pending = StravaPendingStore(time_func=clock)
    services = _build_services(
        fitness_factory=fitness_factory, fitness_repo=fitness_repo,
        pending_store=pending, exchange=FakeExchangeCode(),
    )
    with _build_client(services) as client:
        auth = client.get("/api/fitness/strava/authorize_url")
        state = auth.json()["state"]
        clock.t += 999_999  # forward past the 10-min TTL
        resp = client.post(
            "/api/fitness/strava/exchange",
            json={"code": "ABC", "state": state},
        )
    assert resp.status_code == 410


def test_exchange_cross_user_state_rejected_403(
    fitness_factory: ConnectionFactory,
    fitness_repo: FitnessRepository,
    pending_store: StravaPendingStore,
) -> None:
    """A state token issued under user 1 cannot be consumed under user 2.

    Without this binding, an attacker could craft an authorize URL embedding
    a pre-issued state and trick a logged-in journal user into attaching the
    attacker's Strava account to the victim's journal account (D4).
    """
    services = _build_services(
        fitness_factory=fitness_factory, fitness_repo=fitness_repo,
        pending_store=pending_store, exchange=FakeExchangeCode(),
    )

    with _build_client(services, user_id=1) as client_a:
        auth = client_a.get("/api/fitness/strava/authorize_url")
        state = auth.json()["state"]

    with _build_client(services, user_id=2) as client_b:
        resp = client_b.post(
            "/api/fitness/strava/exchange",
            json={"code": "ABC", "state": state},
        )

    assert resp.status_code == 403
    assert resp.json().get("reason") == "cross_user_pending_session"
    # Entry preserved so the legitimate user 1 can still complete its flow.
    assert pending_store.peek(state) is not None


# ── Tests: D8 reconnect-with-different-account ───────────────────────


def _seed_existing_strava_auth(
    repo: FitnessRepository, *, user_id: int, upstream_user_id: str,
) -> None:
    repo.upsert_auth_state(
        FitnessAuthState(
            user_id=user_id,
            source="strava",
            access_token="OLD_ACCESS",
            refresh_token="OLD_REFRESH",
            token_expires_at="2026-04-01T00:00:00Z",
            extra_state={"upstream_user_id": upstream_user_id},
            last_successful_login_at="2026-04-01T00:00:00Z",
            auth_status="ok",
        ),
    )


def test_reconnect_with_same_athlete_id_is_allowed(
    fitness_factory: ConnectionFactory,
    fitness_repo: FitnessRepository,
    pending_store: StravaPendingStore,
) -> None:
    _seed_existing_strava_auth(
        fitness_repo, user_id=1, upstream_user_id="98765432",
    )
    services = _build_services(
        fitness_factory=fitness_factory, fitness_repo=fitness_repo,
        pending_store=pending_store,
        exchange=FakeExchangeCode(athlete_id="98765432"),
    )
    with _build_client(services, user_id=1) as client:
        auth = client.get("/api/fitness/strava/authorize_url")
        resp = client.post(
            "/api/fitness/strava/exchange",
            json={"code": "ABC", "state": auth.json()["state"]},
        )
    assert resp.status_code == 200
    state_row = fitness_repo.get_auth_state(user_id=1, source="strava")
    assert state_row is not None
    # Tokens refreshed; upstream_user_id unchanged.
    assert state_row.access_token == "acc_FAKE"
    assert state_row.extra_state.get("upstream_user_id") == "98765432"


def test_reconnect_with_different_athlete_id_is_rejected_409(
    fitness_factory: ConnectionFactory,
    fitness_repo: FitnessRepository,
    pending_store: StravaPendingStore,
) -> None:
    _seed_existing_strava_auth(
        fitness_repo, user_id=1, upstream_user_id="98765432",
    )
    services = _build_services(
        fitness_factory=fitness_factory, fitness_repo=fitness_repo,
        pending_store=pending_store,
        exchange=FakeExchangeCode(athlete_id="11111111"),
    )
    with _build_client(services, user_id=1) as client:
        auth = client.get("/api/fitness/strava/authorize_url")
        resp = client.post(
            "/api/fitness/strava/exchange",
            json={"code": "ABC", "state": auth.json()["state"]},
        )
    assert resp.status_code == 409
    body = resp.json()
    assert body.get("reason") == "upstream_account_mismatch"
    assert body.get("stored_upstream_user_id") == "98765432"
    assert body.get("incoming_upstream_user_id") == "11111111"

    # Existing tokens untouched.
    state_row = fitness_repo.get_auth_state(user_id=1, source="strava")
    assert state_row is not None
    assert state_row.access_token == "OLD_ACCESS"
    assert state_row.extra_state.get("upstream_user_id") == "98765432"


def test_exchange_returns_502_when_athlete_id_missing(
    fitness_factory: ConnectionFactory,
    fitness_repo: FitnessRepository,
    pending_store: StravaPendingStore,
) -> None:
    """Strava's ``return_athlete=True`` is documented as best-effort —
    Strava can omit the athlete payload. Failing closed (502, no row
    written) is safer than persisting tokens without an upstream id we
    can verify on later reconnects (D8 retrofit is impossible)."""
    services = _build_services(
        fitness_factory=fitness_factory, fitness_repo=fitness_repo,
        pending_store=pending_store,
        exchange=FakeExchangeCode(athlete_id=None),
    )
    with _build_client(services) as client:
        auth = client.get("/api/fitness/strava/authorize_url")
        resp = client.post(
            "/api/fitness/strava/exchange",
            json={"code": "ABC", "state": auth.json()["state"]},
        )
    assert resp.status_code == 502
    assert resp.json().get("reason") == "missing_upstream_identity"
    # No row persisted.
    assert fitness_repo.get_auth_state(user_id=1, source="strava") is None


# ── Tests: Strava-side errors ────────────────────────────────────────


def test_exchange_strava_rejected_returns_502(
    fitness_factory: ConnectionFactory,
    fitness_repo: FitnessRepository,
    pending_store: StravaPendingStore,
) -> None:
    services = _build_services(
        fitness_factory=fitness_factory, fitness_repo=fitness_repo,
        pending_store=pending_store,
        exchange=FakeExchangeCode(
            raises=AccessUnauthorized("Strava rejected the code"),
        ),
    )
    with _build_client(services) as client:
        auth = client.get("/api/fitness/strava/authorize_url")
        state = auth.json()["state"]
        resp = client.post(
            "/api/fitness/strava/exchange",
            json={"code": "BAD", "state": state},
        )
    assert resp.status_code == 502
    assert resp.json().get("reason") == "upstream_error"
    # State was consumed — replaying the same state with a fresh code is a
    # one-shot CSRF guarantee. The user must repeat the connect flow.
    assert pending_store.peek(state) is None


# ── Tests: disconnect ────────────────────────────────────────────────


def test_disconnect_when_not_connected_returns_disconnected_false(
    fitness_factory: ConnectionFactory,
    fitness_repo: FitnessRepository,
    pending_store: StravaPendingStore,
) -> None:
    services = _build_services(
        fitness_factory=fitness_factory, fitness_repo=fitness_repo,
        pending_store=pending_store, exchange=FakeExchangeCode(),
    )
    with _build_client(services) as client:
        resp = client.post("/api/fitness/strava/disconnect")
    assert resp.status_code == 200
    assert resp.json() == {"disconnected": False}


def test_disconnect_after_connect_deletes_row(
    fitness_factory: ConnectionFactory,
    fitness_repo: FitnessRepository,
    pending_store: StravaPendingStore,
) -> None:
    _seed_existing_strava_auth(
        fitness_repo, user_id=1, upstream_user_id="98765432",
    )
    assert fitness_repo.get_auth_state(user_id=1, source="strava") is not None

    services = _build_services(
        fitness_factory=fitness_factory, fitness_repo=fitness_repo,
        pending_store=pending_store, exchange=FakeExchangeCode(),
    )
    with _build_client(services) as client:
        resp = client.post("/api/fitness/strava/disconnect")
    assert resp.status_code == 200
    assert resp.json() == {"disconnected": True}
    assert fitness_repo.get_auth_state(user_id=1, source="strava") is None


def test_disconnect_only_affects_calling_user(
    fitness_factory: ConnectionFactory,
    fitness_repo: FitnessRepository,
    pending_store: StravaPendingStore,
) -> None:
    """User 1 disconnects; user 2's row is untouched."""
    conn = fitness_factory.get()
    conn.execute(
        "INSERT OR IGNORE INTO users (id, email, display_name, "
        "password_hash, created_at) VALUES "
        "(2, 'u2@example.com', 'User 2', 'x', '2026-01-01T00:00:00Z')",
    )
    conn.commit()
    _seed_existing_strava_auth(
        fitness_repo, user_id=1, upstream_user_id="98765432",
    )
    _seed_existing_strava_auth(
        fitness_repo, user_id=2, upstream_user_id="11111111",
    )

    services = _build_services(
        fitness_factory=fitness_factory, fitness_repo=fitness_repo,
        pending_store=pending_store, exchange=FakeExchangeCode(),
    )
    with _build_client(services, user_id=1) as client_a:
        resp = client_a.post("/api/fitness/strava/disconnect")
    assert resp.status_code == 200
    assert fitness_repo.get_auth_state(user_id=1, source="strava") is None
    other = fitness_repo.get_auth_state(user_id=2, source="strava")
    assert other is not None
    assert other.extra_state.get("upstream_user_id") == "11111111"


# ── Tests: STRAVA_ENABLED mothball (W1) ──────────────────────────────


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/fitness/strava/authorize_url"),
        ("POST", "/api/fitness/strava/exchange"),
        ("POST", "/api/fitness/strava/disconnect"),
    ],
)
def test_strava_routes_404_when_disabled(
    fitness_factory: ConnectionFactory,
    fitness_repo: FitnessRepository,
    pending_store: StravaPendingStore,
    method: str,
    path: str,
) -> None:
    """With STRAVA_ENABLED=false all three OAuth routes are unreachable —
    404 with a clear reason, even when OAuth creds are configured."""
    services = _build_services(
        fitness_factory=fitness_factory, fitness_repo=fitness_repo,
        pending_store=pending_store, exchange=FakeExchangeCode(),
        strava_enabled=False,
    )
    with _build_client(services) as client:
        if method == "GET":
            resp = client.get(path)
        else:
            resp = client.post(path, json={"state": "s", "code": "c"})
    assert resp.status_code == 404
    assert resp.json() == {
        "error": "Strava integration is disabled on this server",
    }


def test_strava_routes_404_when_config_missing(
    fitness_factory: ConnectionFactory,
    fitness_repo: FitnessRepository,
    pending_store: StravaPendingStore,
) -> None:
    """Fail closed: no config in the services dict means Strava is dark."""
    services = _build_services(
        fitness_factory=fitness_factory, fitness_repo=fitness_repo,
        pending_store=pending_store, exchange=FakeExchangeCode(),
    )
    services.pop("config")
    with _build_client(services) as client:
        resp = client.get("/api/fitness/strava/authorize_url")
    assert resp.status_code == 404
    assert "disabled" in resp.json()["error"]
