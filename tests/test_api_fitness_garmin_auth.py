"""W2 — REST API tests for the per-user Garmin connect / MFA / disconnect flow.

The endpoints under test:

- ``POST /api/fitness/garmin/connect``
- ``POST /api/fitness/garmin/connect/mfa``
- ``POST /api/fitness/garmin/disconnect``

Auth shape mirrors ``test_api_fitness.py``: a fake auth middleware injects an
:class:`AuthenticatedUser` so the per-route ``get_authenticated_user`` call
sees a consistent user. The cross-user replay test (D2) builds two separate
clients with two different injected users so a token issued under user A is
consumed under user B's auth context.

The Garmin SDK is stubbed via the ``garmin_client_factory`` services-dict
entry — production wiring resolves it to ``garminconnect.Garmin``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from garminconnect import (
    GarminConnectAuthenticationError,
    GarminConnectTooManyRequestsError,
)
from starlette.testclient import TestClient

from journal.auth import AuthenticatedUser, _current_user_id
from journal.db.connection import get_connection
from journal.db.factory import ConnectionFactory
from journal.db.fitness_repository import FitnessRepository
from journal.db.migrations import run_migrations
from journal.models import FitnessAuthState
from journal.services.fitness.garmin_pending import (
    DEFAULT_COOLDOWN_THRESHOLD,
    GarminCooldownTracker,
    GarminPendingStore,
)

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Generator
    from pathlib import Path


# ── Fake Garmin client ──────────────────────────────────────────────


class FakeGarmin:
    """Stand-in for ``garminconnect.Garmin`` driven by a per-test script.

    The factory is parameterised by the test:

    - ``mfa_required``: first ``login()`` returns ``("needs_mfa", legacy)``.
    - ``profile``: dict returned from ``get_user_profile()`` after
      successful login or successful resume_login.
    - ``profile_after_mfa_raises``: when set, calling ``get_user_profile()``
      after ``resume_login`` raises this exception (post-MFA fetch failure).
    - ``login_raises`` / ``resume_raises``: exception to raise from the
      respective call (used for invalid-credentials, 429, wrong-MFA-code).
    """

    def __init__(
        self,
        *,
        email: str,
        password: str,
        return_on_mfa: bool = False,
        prompt_mfa: Any = None,
        mfa_required: bool = False,
        profile: dict[str, Any] | None = None,
        login_raises: BaseException | None = None,
        resume_raises: BaseException | None = None,
        resume_profile: dict[str, Any] | None = None,
        profile_after_mfa_raises: BaseException | None = None,
        token_blob: str = "FAKE-TOKEN-BLOB",
    ) -> None:
        self._email = email
        self._password = password
        self._return_on_mfa = return_on_mfa
        self._mfa_required = mfa_required
        self._login_raises = login_raises
        self._resume_raises = resume_raises
        self._profile = profile or {"displayName": "alice.j", "fullName": "Alice J"}
        self._resume_profile = resume_profile or self._profile
        self._profile_after_mfa_raises = profile_after_mfa_raises
        self._mfa_resolved = False
        self._token_blob = token_blob
        # The real SDK exposes ``client.client.dumps()``. Match that shape so
        # the connect handler can capture the blob the same way.
        self.client = type(
            "_InnerClient",
            (),
            {"dumps": lambda _self: token_blob, "loads": lambda _self, blob: None},
        )()

    def login(self) -> tuple[Any, Any]:
        if self._login_raises is not None:
            raise self._login_raises
        if self._mfa_required and not self._mfa_resolved and self._return_on_mfa:
            return ("needs_mfa", "legacy-token-noop")
        return (None, "legacy-token-success")

    def resume_login(self, _client_state: Any, _mfa_code: str) -> tuple[Any, Any]:
        if self._resume_raises is not None:
            raise self._resume_raises
        self._mfa_resolved = True
        return (None, "legacy-token-resumed")

    def get_user_profile(self) -> dict[str, Any]:
        if self._mfa_resolved:
            if self._profile_after_mfa_raises is not None:
                raise self._profile_after_mfa_raises
            return self._resume_profile
        return self._profile


class FakeGarminFactory:
    """Holds the per-test FakeGarmin parameters and yields one client per call.

    The factory itself is what the endpoint receives via
    ``services["garmin_client_factory"]``; calling it like ``Garmin(...)``
    constructs a fresh fake.
    """

    def __init__(self, **overrides: Any) -> None:
        self._overrides = overrides
        self.last_client: FakeGarmin | None = None

    def __call__(self, **kwargs: Any) -> FakeGarmin:
        merged = {**kwargs, **self._overrides}
        client = FakeGarmin(**merged)
        self.last_client = client
        return client


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
    db_path = tmp_path / "garmin-auth.db"
    f = ConnectionFactory(db_path)
    run_migrations(f.get())
    return f


@pytest.fixture
def fitness_db(fitness_factory: ConnectionFactory) -> Generator[sqlite3.Connection]:
    """Cross-thread connection for the legacy ``"db_conn"`` services slot."""
    conn = get_connection(fitness_factory.db_path, check_same_thread=False)
    yield conn
    conn.close()


@pytest.fixture
def fitness_repo(fitness_factory: ConnectionFactory) -> FitnessRepository:
    return FitnessRepository(fitness_factory)


@pytest.fixture
def pending_store() -> GarminPendingStore:
    return GarminPendingStore()


@pytest.fixture
def cooldown_tracker() -> GarminCooldownTracker:
    return GarminCooldownTracker()


def _build_services(
    *,
    fitness_db: sqlite3.Connection,
    fitness_repo: FitnessRepository,
    pending_store: GarminPendingStore,
    cooldown_tracker: GarminCooldownTracker,
    garmin_factory: Any,
) -> dict:
    return {
        "fitness_repo": fitness_repo,
        "db_conn": fitness_db,
        "garmin_pending": pending_store,
        "garmin_cooldown": cooldown_tracker,
        "garmin_client_factory": garmin_factory,
    }


def _build_client(services: dict, *, user_id: int = 1) -> TestClient:
    from mcp.server.fastmcp import FastMCP

    from journal.api import register_api_routes

    test_mcp = FastMCP(f"test-garmin-auth-user-{user_id}")
    register_api_routes(test_mcp, lambda: services)
    app = _UserAuthMiddleware(test_mcp.streamable_http_app(), user_id=user_id)
    return TestClient(app, raise_server_exceptions=False)


# ── Tests: connect (no MFA path) ─────────────────────────────────────


def test_connect_no_mfa_persists_tokens_and_upstream_id(
    fitness_db: sqlite3.Connection,
    fitness_repo: FitnessRepository,
    pending_store: GarminPendingStore,
    cooldown_tracker: GarminCooldownTracker,
) -> None:
    factory = FakeGarminFactory(
        mfa_required=False,
        profile={"displayName": "alice.j", "fullName": "Alice J"},
    )
    services = _build_services(
        fitness_db=fitness_db, fitness_repo=fitness_repo,
        pending_store=pending_store, cooldown_tracker=cooldown_tracker,
        garmin_factory=factory,
    )
    with _build_client(services, user_id=1) as client:
        resp = client.post(
            "/api/fitness/garmin/connect",
            json={"username": "alice@example.com", "password": "secret"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body == {"connected": True, "upstream_user_id": "alice.j"}

    # Persisted state: tokens_blob + upstream_user_id, status=ok.
    state = fitness_repo.get_auth_state(user_id=1, source="garmin")
    assert state is not None
    assert state.auth_status == "ok"
    assert state.auth_broken_since is None
    assert state.last_successful_login_at is not None
    assert state.extra_state.get("tokens_blob") == "FAKE-TOKEN-BLOB"
    assert state.extra_state.get("upstream_user_id") == "alice.j"


def test_connect_missing_body_fields_returns_400(
    fitness_db: sqlite3.Connection,
    fitness_repo: FitnessRepository,
    pending_store: GarminPendingStore,
    cooldown_tracker: GarminCooldownTracker,
) -> None:
    factory = FakeGarminFactory()
    services = _build_services(
        fitness_db=fitness_db, fitness_repo=fitness_repo,
        pending_store=pending_store, cooldown_tracker=cooldown_tracker,
        garmin_factory=factory,
    )
    with _build_client(services) as client:
        resp = client.post(
            "/api/fitness/garmin/connect", json={"username": "alice@example.com"},
        )
    assert resp.status_code == 400


def test_connect_invalid_credentials_returns_401_and_records_failure(
    fitness_db: sqlite3.Connection,
    fitness_repo: FitnessRepository,
    pending_store: GarminPendingStore,
    cooldown_tracker: GarminCooldownTracker,
) -> None:
    factory = FakeGarminFactory(
        login_raises=GarminConnectAuthenticationError("Authentication failed"),
    )
    services = _build_services(
        fitness_db=fitness_db, fitness_repo=fitness_repo,
        pending_store=pending_store, cooldown_tracker=cooldown_tracker,
        garmin_factory=factory,
    )
    with _build_client(services) as client:
        resp = client.post(
            "/api/fitness/garmin/connect",
            json={"username": "alice@example.com", "password": "wrong"},
        )
    assert resp.status_code == 401
    body = resp.json()
    assert body.get("reason") == "invalid_credentials"
    # The failure is recorded — the cool-down tracker has one entry now.
    # check() returns None until threshold; record_failure is called once.
    assert cooldown_tracker.check("alice@example.com") is None  # under threshold


def test_connect_upstream_429_surfaces_distinctly(
    fitness_db: sqlite3.Connection,
    fitness_repo: FitnessRepository,
    pending_store: GarminPendingStore,
    cooldown_tracker: GarminCooldownTracker,
) -> None:
    factory = FakeGarminFactory(
        login_raises=GarminConnectTooManyRequestsError("Too many login attempts"),
    )
    services = _build_services(
        fitness_db=fitness_db, fitness_repo=fitness_repo,
        pending_store=pending_store, cooldown_tracker=cooldown_tracker,
        garmin_factory=factory,
    )
    with _build_client(services) as client:
        resp = client.post(
            "/api/fitness/garmin/connect",
            json={"username": "alice@example.com", "password": "x"},
        )
    assert resp.status_code == 429
    body = resp.json()
    assert "retry_after_seconds" in body


def test_connect_per_email_cooldown_blocks_after_threshold(
    fitness_db: sqlite3.Connection,
    fitness_repo: FitnessRepository,
    pending_store: GarminPendingStore,
    cooldown_tracker: GarminCooldownTracker,
) -> None:
    factory = FakeGarminFactory(
        login_raises=GarminConnectAuthenticationError("Authentication failed"),
    )
    services = _build_services(
        fitness_db=fitness_db, fitness_repo=fitness_repo,
        pending_store=pending_store, cooldown_tracker=cooldown_tracker,
        garmin_factory=factory,
    )
    with _build_client(services) as client:
        # Fail enough times to trip the cool-down.
        for _ in range(DEFAULT_COOLDOWN_THRESHOLD):
            resp = client.post(
                "/api/fitness/garmin/connect",
                json={"username": "alice@example.com", "password": "wrong"},
            )
            assert resp.status_code == 401

        # Next attempt is refused with 429 *before* hitting Garmin — the
        # cool-down protects users from upstream account-wide lockouts.
        resp = client.post(
            "/api/fitness/garmin/connect",
            json={"username": "alice@example.com", "password": "wrong"},
        )
        assert resp.status_code == 429
        body = resp.json()
        assert body.get("reason") == "local_cooldown"
        assert body.get("retry_after_seconds") > 0


# ── Tests: connect (MFA path) ────────────────────────────────────────


def test_connect_with_mfa_required_returns_pending_session(
    fitness_db: sqlite3.Connection,
    fitness_repo: FitnessRepository,
    pending_store: GarminPendingStore,
    cooldown_tracker: GarminCooldownTracker,
) -> None:
    factory = FakeGarminFactory(mfa_required=True)
    services = _build_services(
        fitness_db=fitness_db, fitness_repo=fitness_repo,
        pending_store=pending_store, cooldown_tracker=cooldown_tracker,
        garmin_factory=factory,
    )
    with _build_client(services) as client:
        resp = client.post(
            "/api/fitness/garmin/connect",
            json={"username": "alice@example.com", "password": "x"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["mfa_required"] is True
    assert isinstance(body["pending_session"], str)
    assert body["pending_session"]
    assert body["expires_at"].endswith("Z")
    # No row persisted yet.
    assert fitness_repo.get_auth_state(user_id=1, source="garmin") is None


def test_mfa_completes_login_and_persists_tokens(
    fitness_db: sqlite3.Connection,
    fitness_repo: FitnessRepository,
    pending_store: GarminPendingStore,
    cooldown_tracker: GarminCooldownTracker,
) -> None:
    factory = FakeGarminFactory(mfa_required=True)
    services = _build_services(
        fitness_db=fitness_db, fitness_repo=fitness_repo,
        pending_store=pending_store, cooldown_tracker=cooldown_tracker,
        garmin_factory=factory,
    )
    with _build_client(services) as client:
        connect = client.post(
            "/api/fitness/garmin/connect",
            json={"username": "alice@example.com", "password": "x"},
        )
        token = connect.json()["pending_session"]
        mfa = client.post(
            "/api/fitness/garmin/connect/mfa",
            json={"pending_session": token, "code": "123456"},
        )

    assert mfa.status_code == 200
    body = mfa.json()
    assert body["connected"] is True
    state = fitness_repo.get_auth_state(user_id=1, source="garmin")
    assert state is not None
    assert state.extra_state.get("tokens_blob") == "FAKE-TOKEN-BLOB"
    assert state.extra_state.get("upstream_user_id") == "alice.j"


def test_mfa_wrong_code_returns_401_and_preserves_pending_session(
    fitness_db: sqlite3.Connection,
    fitness_repo: FitnessRepository,
    pending_store: GarminPendingStore,
    cooldown_tracker: GarminCooldownTracker,
) -> None:
    factory = FakeGarminFactory(
        mfa_required=True,
        resume_raises=GarminConnectAuthenticationError("Bad MFA"),
    )
    services = _build_services(
        fitness_db=fitness_db, fitness_repo=fitness_repo,
        pending_store=pending_store, cooldown_tracker=cooldown_tracker,
        garmin_factory=factory,
    )
    with _build_client(services) as client:
        connect = client.post(
            "/api/fitness/garmin/connect",
            json={"username": "alice@example.com", "password": "x"},
        )
        token = connect.json()["pending_session"]
        bad = client.post(
            "/api/fitness/garmin/connect/mfa",
            json={"pending_session": token, "code": "000000"},
        )

    assert bad.status_code == 401
    assert bad.json().get("reason") == "invalid_mfa_code"
    # Pending session preserved so the user can retry with a fresh code.
    assert pending_store.peek(token) is not None


def test_mfa_post_login_profile_failure_returns_502(
    fitness_db: sqlite3.Connection,
    fitness_repo: FitnessRepository,
    pending_store: GarminPendingStore,
    cooldown_tracker: GarminCooldownTracker,
) -> None:
    factory = FakeGarminFactory(
        mfa_required=True,
        profile_after_mfa_raises=RuntimeError("Failed to retrieve social profile"),
    )
    services = _build_services(
        fitness_db=fitness_db, fitness_repo=fitness_repo,
        pending_store=pending_store, cooldown_tracker=cooldown_tracker,
        garmin_factory=factory,
    )
    with _build_client(services) as client:
        connect = client.post(
            "/api/fitness/garmin/connect",
            json={"username": "alice@example.com", "password": "x"},
        )
        token = connect.json()["pending_session"]
        mfa = client.post(
            "/api/fitness/garmin/connect/mfa",
            json={"pending_session": token, "code": "123456"},
        )

    assert mfa.status_code == 502
    assert mfa.json().get("reason") == "post_mfa_profile_fetch_failed"
    # No row persisted — we'd rather force a clean retry than write a row
    # without an upstream id we can verify on later reconnects (D8).
    assert fitness_repo.get_auth_state(user_id=1, source="garmin") is None


def test_mfa_unknown_pending_session_returns_410(
    fitness_db: sqlite3.Connection,
    fitness_repo: FitnessRepository,
    pending_store: GarminPendingStore,
    cooldown_tracker: GarminCooldownTracker,
) -> None:
    factory = FakeGarminFactory()
    services = _build_services(
        fitness_db=fitness_db, fitness_repo=fitness_repo,
        pending_store=pending_store, cooldown_tracker=cooldown_tracker,
        garmin_factory=factory,
    )
    with _build_client(services) as client:
        resp = client.post(
            "/api/fitness/garmin/connect/mfa",
            json={"pending_session": "no-such-token", "code": "123456"},
        )
    assert resp.status_code == 410
    assert resp.json().get("reason") == "expired_pending_session"


def test_mfa_expired_pending_session_returns_410(
    fitness_db: sqlite3.Connection,
    fitness_repo: FitnessRepository,
    cooldown_tracker: GarminCooldownTracker,
) -> None:
    # Roll the pending store's clock so a session ages out before the MFA
    # call lands. The endpoint reads `services["garmin_pending"]` once, so
    # we wire a clock-driven store directly into the services dict.
    class _Clock:
        t = 1000.0

        def __call__(self) -> float:
            return self.t

    clock = _Clock()
    pending = GarminPendingStore(time_func=clock)
    factory = FakeGarminFactory(mfa_required=True)
    services = _build_services(
        fitness_db=fitness_db, fitness_repo=fitness_repo,
        pending_store=pending, cooldown_tracker=cooldown_tracker,
        garmin_factory=factory,
    )
    with _build_client(services) as client:
        connect = client.post(
            "/api/fitness/garmin/connect",
            json={"username": "alice@example.com", "password": "x"},
        )
        token = connect.json()["pending_session"]
        clock.t += 999_999  # forward past the 10-min TTL
        resp = client.post(
            "/api/fitness/garmin/connect/mfa",
            json={"pending_session": token, "code": "123456"},
        )
    assert resp.status_code == 410


def test_mfa_cross_user_pending_session_rejected_403(
    fitness_db: sqlite3.Connection,
    fitness_repo: FitnessRepository,
    pending_store: GarminPendingStore,
    cooldown_tracker: GarminCooldownTracker,
) -> None:
    """A token issued under user 1 cannot be consumed under user 2's auth.

    Even though the token is opaque and unguessable, leak channels (logs,
    screenshots, browser-history hand-offs) make the user-binding check the
    only thing protecting in-flight MFAs from cross-user takeover.
    """
    factory = FakeGarminFactory(mfa_required=True)
    services = _build_services(
        fitness_db=fitness_db, fitness_repo=fitness_repo,
        pending_store=pending_store, cooldown_tracker=cooldown_tracker,
        garmin_factory=factory,
    )

    # User 1 starts a connect and gets a pending session.
    with _build_client(services, user_id=1) as client_a:
        connect = client_a.post(
            "/api/fitness/garmin/connect",
            json={"username": "alice@example.com", "password": "x"},
        )
        token = connect.json()["pending_session"]

    # User 2 tries to consume it — must be 403.
    with _build_client(services, user_id=2) as client_b:
        resp = client_b.post(
            "/api/fitness/garmin/connect/mfa",
            json={"pending_session": token, "code": "123456"},
        )
    assert resp.status_code == 403
    assert resp.json().get("reason") == "cross_user_pending_session"
    # The entry stays put — so user 1 can still complete their own flow
    # if they retry within the TTL.
    assert pending_store.peek(token) is not None


def test_mfa_missing_body_fields_returns_400(
    fitness_db: sqlite3.Connection,
    fitness_repo: FitnessRepository,
    pending_store: GarminPendingStore,
    cooldown_tracker: GarminCooldownTracker,
) -> None:
    factory = FakeGarminFactory()
    services = _build_services(
        fitness_db=fitness_db, fitness_repo=fitness_repo,
        pending_store=pending_store, cooldown_tracker=cooldown_tracker,
        garmin_factory=factory,
    )
    with _build_client(services) as client:
        resp = client.post(
            "/api/fitness/garmin/connect/mfa", json={"pending_session": "x"},
        )
    assert resp.status_code == 400


# ── Tests: D8 reconnect-with-different-account ───────────────────────


def _seed_existing_garmin_auth(
    repo: FitnessRepository, *, user_id: int, upstream_user_id: str,
) -> None:
    repo.upsert_auth_state(
        FitnessAuthState(
            user_id=user_id,
            source="garmin",
            extra_state={
                "tokens_blob": "OLD-BLOB",
                "upstream_user_id": upstream_user_id,
            },
            last_successful_login_at="2026-04-01T00:00:00Z",
            auth_status="ok",
        ),
    )


def test_reconnect_with_same_upstream_id_is_allowed(
    fitness_db: sqlite3.Connection,
    fitness_repo: FitnessRepository,
    pending_store: GarminPendingStore,
    cooldown_tracker: GarminCooldownTracker,
) -> None:
    _seed_existing_garmin_auth(fitness_repo, user_id=1, upstream_user_id="alice.j")
    factory = FakeGarminFactory(profile={"displayName": "alice.j"})
    services = _build_services(
        fitness_db=fitness_db, fitness_repo=fitness_repo,
        pending_store=pending_store, cooldown_tracker=cooldown_tracker,
        garmin_factory=factory,
    )
    with _build_client(services) as client:
        resp = client.post(
            "/api/fitness/garmin/connect",
            json={"username": "alice@example.com", "password": "x"},
        )
    assert resp.status_code == 200
    assert resp.json()["connected"] is True
    state = fitness_repo.get_auth_state(user_id=1, source="garmin")
    assert state is not None
    # The blob got refreshed, the upstream id unchanged.
    assert state.extra_state["tokens_blob"] == "FAKE-TOKEN-BLOB"
    assert state.extra_state["upstream_user_id"] == "alice.j"


def test_reconnect_with_different_upstream_id_is_rejected(
    fitness_db: sqlite3.Connection,
    fitness_repo: FitnessRepository,
    pending_store: GarminPendingStore,
    cooldown_tracker: GarminCooldownTracker,
) -> None:
    _seed_existing_garmin_auth(fitness_repo, user_id=1, upstream_user_id="alice.j")
    factory = FakeGarminFactory(profile={"displayName": "bob.k"})
    services = _build_services(
        fitness_db=fitness_db, fitness_repo=fitness_repo,
        pending_store=pending_store, cooldown_tracker=cooldown_tracker,
        garmin_factory=factory,
    )
    with _build_client(services) as client:
        resp = client.post(
            "/api/fitness/garmin/connect",
            json={"username": "bob@example.com", "password": "x"},
        )
    assert resp.status_code == 409
    body = resp.json()
    assert body.get("reason") == "upstream_account_mismatch"
    assert body.get("stored_upstream_user_id") == "alice.j"
    assert body.get("incoming_upstream_user_id") == "bob.k"
    # Existing row untouched: blob stays "OLD-BLOB".
    state = fitness_repo.get_auth_state(user_id=1, source="garmin")
    assert state is not None
    assert state.extra_state.get("tokens_blob") == "OLD-BLOB"
    assert state.extra_state.get("upstream_user_id") == "alice.j"


# ── Tests: disconnect ────────────────────────────────────────────────


def test_disconnect_when_not_connected_returns_disconnected_false(
    fitness_db: sqlite3.Connection,
    fitness_repo: FitnessRepository,
    pending_store: GarminPendingStore,
    cooldown_tracker: GarminCooldownTracker,
) -> None:
    factory = FakeGarminFactory()
    services = _build_services(
        fitness_db=fitness_db, fitness_repo=fitness_repo,
        pending_store=pending_store, cooldown_tracker=cooldown_tracker,
        garmin_factory=factory,
    )
    with _build_client(services) as client:
        resp = client.post("/api/fitness/garmin/disconnect")
    assert resp.status_code == 200
    assert resp.json() == {"disconnected": False}


def test_disconnect_after_connect_deletes_row(
    fitness_db: sqlite3.Connection,
    fitness_repo: FitnessRepository,
    pending_store: GarminPendingStore,
    cooldown_tracker: GarminCooldownTracker,
) -> None:
    _seed_existing_garmin_auth(fitness_repo, user_id=1, upstream_user_id="alice.j")
    assert fitness_repo.get_auth_state(user_id=1, source="garmin") is not None

    factory = FakeGarminFactory()
    services = _build_services(
        fitness_db=fitness_db, fitness_repo=fitness_repo,
        pending_store=pending_store, cooldown_tracker=cooldown_tracker,
        garmin_factory=factory,
    )
    with _build_client(services) as client:
        resp = client.post("/api/fitness/garmin/disconnect")
    assert resp.status_code == 200
    assert resp.json() == {"disconnected": True}
    assert fitness_repo.get_auth_state(user_id=1, source="garmin") is None


def test_disconnect_only_affects_calling_user(
    fitness_db: sqlite3.Connection,
    fitness_repo: FitnessRepository,
    pending_store: GarminPendingStore,
    cooldown_tracker: GarminCooldownTracker,
) -> None:
    """User 1 disconnects; user 2's row is untouched."""
    # Seed both users so we don't trip the FK on user_id=2 when the test DB
    # has no users table population. (Schema has FK ON DELETE CASCADE; we
    # need actual users rows for user_id=2 to exist as an auth_state row.)
    # Migration 0011 seeds user_id=1; we just need user_id=2 to exist
    # so the FitnessAuthState upsert for user 2 doesn't trip the FK.
    fitness_db.execute(
        "INSERT OR IGNORE INTO users (id, email, display_name, password_hash, created_at) "
        "VALUES (2, 'u2@example.com', 'User 2', 'x', '2026-01-01T00:00:00Z')",
    )
    fitness_db.commit()
    _seed_existing_garmin_auth(fitness_repo, user_id=1, upstream_user_id="alice.j")
    _seed_existing_garmin_auth(fitness_repo, user_id=2, upstream_user_id="bob.k")

    factory = FakeGarminFactory()
    services = _build_services(
        fitness_db=fitness_db, fitness_repo=fitness_repo,
        pending_store=pending_store, cooldown_tracker=cooldown_tracker,
        garmin_factory=factory,
    )
    with _build_client(services, user_id=1) as client_a:
        resp = client_a.post("/api/fitness/garmin/disconnect")
    assert resp.status_code == 200

    assert fitness_repo.get_auth_state(user_id=1, source="garmin") is None
    other = fitness_repo.get_auth_state(user_id=2, source="garmin")
    assert other is not None
    assert other.extra_state["upstream_user_id"] == "bob.k"


# ── Sanity: real-time fixture wiring ─────────────────────────────────


def test_factory_kwargs_reach_fake_client(
    fitness_db: sqlite3.Connection,
    fitness_repo: FitnessRepository,
    pending_store: GarminPendingStore,
    cooldown_tracker: GarminCooldownTracker,
) -> None:
    """The connect handler must call ``Garmin(email=..., password=...,
    return_on_mfa=True)``. This guards against accidentally calling the
    factory with positional args or forgetting ``return_on_mfa``."""
    factory = FakeGarminFactory()
    services = _build_services(
        fitness_db=fitness_db, fitness_repo=fitness_repo,
        pending_store=pending_store, cooldown_tracker=cooldown_tracker,
        garmin_factory=factory,
    )
    with _build_client(services) as client:
        client.post(
            "/api/fitness/garmin/connect",
            json={"username": "alice@example.com", "password": "secret"},
        )
    assert factory.last_client is not None
    assert factory.last_client._email == "alice@example.com"
    assert factory.last_client._password == "secret"
    assert factory.last_client._return_on_mfa is True


