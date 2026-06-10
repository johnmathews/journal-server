"""Tests for per-IP rate limiting on the anonymous auth endpoints.

Covers `journal.ratelimit` (the fixed-window limiter + pure-ASGI
middleware) and its wiring into `build_auth_middleware_stack`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from starlette.responses import JSONResponse
from starlette.testclient import TestClient

from journal.ratelimit import (
    RATE_LIMITED_AUTH_PATHS,
    AuthRateLimitMiddleware,
    FixedWindowRateLimiter,
)

if TYPE_CHECKING:
    from starlette.types import Receive, Scope, Send


async def _ok_app(scope: Scope, receive: Receive, send: Send) -> None:
    """Trivial inner ASGI app: 200 for every HTTP request."""
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
    response = JSONResponse({"ok": True})
    await response(scope, receive, send)


def _client(limiter: FixedWindowRateLimiter) -> TestClient:
    return TestClient(
        AuthRateLimitMiddleware(_ok_app, limiter),
        raise_server_exceptions=False,
    )


# ---------------------------------------------------------------------------
# Limiter unit tests
# ---------------------------------------------------------------------------


class TestFixedWindowRateLimiter:
    def test_allows_up_to_max_requests(self) -> None:
        limiter = FixedWindowRateLimiter(max_requests=3, window_seconds=60)
        for _ in range(3):
            assert limiter.allow("1.2.3.4", "/api/auth/login") is True
        assert limiter.allow("1.2.3.4", "/api/auth/login") is False

    def test_window_expiry_resets_the_budget(self) -> None:
        now = [1000.0]
        limiter = FixedWindowRateLimiter(
            max_requests=2, window_seconds=300, clock=lambda: now[0]
        )
        assert limiter.allow("ip", "/p") is True
        assert limiter.allow("ip", "/p") is True
        assert limiter.allow("ip", "/p") is False
        # Just inside the window — still denied.
        now[0] += 299.0
        assert limiter.allow("ip", "/p") is False
        # Past the window — old timestamps evicted, budget restored.
        now[0] += 2.0
        assert limiter.allow("ip", "/p") is True

    def test_keys_are_per_ip_and_per_path(self) -> None:
        limiter = FixedWindowRateLimiter(max_requests=1, window_seconds=60)
        assert limiter.allow("a", "/login") is True
        assert limiter.allow("a", "/login") is False
        # Different IP, same path — independent budget.
        assert limiter.allow("b", "/login") is True
        # Same IP, different path — independent budget.
        assert limiter.allow("a", "/register") is True

    def test_stale_buckets_are_pruned_on_insert(self) -> None:
        now = [0.0]
        limiter = FixedWindowRateLimiter(
            max_requests=5, window_seconds=10, clock=lambda: now[0]
        )
        for i in range(50):
            limiter.allow(f"ip-{i}", "/p")
        assert len(limiter._buckets) == 50
        # All 50 buckets are stale once the window has fully passed;
        # the next insert must sweep them.
        now[0] += 11.0
        limiter.allow("fresh-ip", "/p")
        assert len(limiter._buckets) == 1


# ---------------------------------------------------------------------------
# Middleware tests
# ---------------------------------------------------------------------------


class TestAuthRateLimitMiddleware:
    def test_under_limit_requests_pass(self) -> None:
        client = _client(FixedWindowRateLimiter(max_requests=10, window_seconds=300))
        for _ in range(10):
            resp = client.post("/api/auth/login", json={})
            assert resp.status_code == 200

    def test_over_limit_returns_429(self) -> None:
        client = _client(FixedWindowRateLimiter(max_requests=10, window_seconds=300))
        for _ in range(10):
            assert client.post("/api/auth/login", json={}).status_code == 200
        resp = client.post("/api/auth/login", json={})
        assert resp.status_code == 429
        body = resp.json()
        assert body["error"] == "rate_limited"
        assert body["message"] == "Too many attempts — try again later"

    def test_per_ip_independence_via_x_real_ip(self) -> None:
        client = _client(FixedWindowRateLimiter(max_requests=2, window_seconds=300))
        for _ in range(2):
            client.post("/api/auth/login", json={}, headers={"X-Real-IP": "10.0.0.1"})
        # First IP is now exhausted ...
        resp = client.post(
            "/api/auth/login", json={}, headers={"X-Real-IP": "10.0.0.1"}
        )
        assert resp.status_code == 429
        # ... but a different IP has its own budget.
        resp = client.post(
            "/api/auth/login", json={}, headers={"X-Real-IP": "10.0.0.2"}
        )
        assert resp.status_code == 200

    def test_x_real_ip_takes_precedence_over_client_host(self) -> None:
        """All TestClient requests share the same transport client host;
        a fresh X-Real-IP must escape a limit accrued without the
        header — proving the header keys the bucket when present."""
        client = _client(FixedWindowRateLimiter(max_requests=1, window_seconds=300))
        assert client.post("/api/auth/login", json={}).status_code == 200
        # client-host bucket exhausted.
        assert client.post("/api/auth/login", json={}).status_code == 429
        resp = client.post(
            "/api/auth/login", json={}, headers={"X-Real-IP": "203.0.113.7"}
        )
        assert resp.status_code == 200

    def test_each_auth_path_has_its_own_budget(self) -> None:
        client = _client(FixedWindowRateLimiter(max_requests=1, window_seconds=300))
        assert client.post("/api/auth/login", json={}).status_code == 200
        assert client.post("/api/auth/login", json={}).status_code == 429
        for path in sorted(RATE_LIMITED_AUTH_PATHS - {"/api/auth/login"}):
            assert client.post(path, json={}).status_code == 200

    def test_non_auth_routes_are_never_limited(self) -> None:
        client = _client(FixedWindowRateLimiter(max_requests=1, window_seconds=300))
        for _ in range(5):
            assert client.post("/api/entries", json={}).status_code == 200
            assert client.get("/health").status_code == 200

    def test_get_requests_to_auth_paths_are_not_limited(self) -> None:
        """Only POST is limited — e.g. GET /api/auth/verify-reset-token
        style probes flow through other paths/methods."""
        client = _client(FixedWindowRateLimiter(max_requests=1, window_seconds=300))
        for _ in range(5):
            assert client.get("/api/auth/login").status_code == 200


# ---------------------------------------------------------------------------
# Wiring into build_auth_middleware_stack
# ---------------------------------------------------------------------------


class TestMiddlewareStackWiring:
    @pytest.fixture
    def auth_service(self) -> MagicMock:
        svc = MagicMock()
        svc.validate_session.return_value = None
        svc.validate_api_key.return_value = None
        return svc

    def test_stack_applies_rate_limiter_when_provided(
        self, auth_service: MagicMock
    ) -> None:
        from journal.auth import build_auth_middleware_stack

        limiter = FixedWindowRateLimiter(max_requests=2, window_seconds=300)
        app = build_auth_middleware_stack(
            _ok_app, auth_service, rate_limiter=limiter
        )
        with TestClient(app, raise_server_exceptions=False) as tc:
            assert tc.post("/api/auth/login", json={}).status_code == 200
            assert tc.post("/api/auth/login", json={}).status_code == 200
            resp = tc.post("/api/auth/login", json={})
            assert resp.status_code == 429
            assert resp.json()["error"] == "rate_limited"

    def test_stack_without_rate_limiter_is_unlimited(
        self, auth_service: MagicMock
    ) -> None:
        """Default behavior (no limiter) is unchanged — this is what the
        existing auth test fixtures rely on for rapid-fire calls."""
        from journal.auth import build_auth_middleware_stack

        app = build_auth_middleware_stack(_ok_app, auth_service)
        with TestClient(app, raise_server_exceptions=False) as tc:
            for _ in range(25):
                assert tc.post("/api/auth/login", json={}).status_code == 200
