"""Per-IP rate limiting for the anonymous authentication endpoints.

Login, register, forgot-password, and reset-password are the only
unauthenticated POST routes, which makes them the natural target for
credential stuffing and password-reset spam. This module provides:

- :class:`FixedWindowRateLimiter` — an in-process, thread-safe,
  fixed-window counter keyed by ``(ip, path)``.
- :class:`AuthRateLimitMiddleware` — pure-ASGI middleware (same style as
  :class:`journal.auth.RequireAuthMiddleware`) that consults the limiter
  for POSTs to :data:`RATE_LIMITED_AUTH_PATHS` and returns **429** when
  the budget is exhausted.

The limiter is deliberately in-process (a dict + lock, no Redis): the
server is a single-process deployment, and losing counters on restart is
acceptable for this threat model. Production wiring happens in
``mcp_server/runserver.py`` via ``Config.auth_rate_limit_*`` (enabled by
default); test apps simply do not pass a limiter to
``build_auth_middleware_stack`` and are unaffected.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import TYPE_CHECKING

from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.types import ASGIApp, Receive, Scope, Send

log = logging.getLogger(__name__)

#: POST paths subject to per-IP rate limiting. These are exactly the
#: anonymous credential-bearing endpoints; authenticated routes are
#: already gated by sessions/API keys and account lockout.
RATE_LIMITED_AUTH_PATHS: frozenset[str] = frozenset(
    {
        "/api/auth/login",
        "/api/auth/register",
        "/api/auth/forgot-password",
        "/api/auth/reset-password",
    }
)


class FixedWindowRateLimiter:
    """Thread-safe fixed-window request counter keyed by ``(ip, path)``.

    Each key holds a deque of request timestamps; timestamps older than
    ``window_seconds`` are evicted on access, and entire stale buckets
    are pruned on insert so the dict cannot grow without bound under
    address-rotating abuse.

    ``clock`` is injectable for deterministic window-expiry tests.
    """

    def __init__(
        self,
        max_requests: int = 10,
        window_seconds: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_requests = max_requests
        self._window = window_seconds
        self._clock = clock
        self._buckets: dict[tuple[str, str], deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, ip: str, path: str) -> bool:
        """Record a request for ``(ip, path)`` and return whether it is
        within the rate limit."""
        now = self._clock()
        cutoff = now - self._window
        key = (ip, path)
        with self._lock:
            # Prune buckets whose newest timestamp predates the window —
            # they hold no live budget and would otherwise leak memory.
            stale = [
                k
                for k, bucket in self._buckets.items()
                if k != key and (not bucket or bucket[-1] <= cutoff)
            ]
            for k in stale:
                del self._buckets[k]

            bucket = self._buckets.setdefault(key, deque())
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= self._max_requests:
                return False
            bucket.append(now)
            return True


class AuthRateLimitMiddleware:
    """Pure-ASGI middleware that 429s over-limit POSTs to auth endpoints.

    Applied **outermost** (before the authentication middleware) so a
    rate-limited request is rejected before any session/API-key lookup
    work. Only ``POST`` requests to ``paths`` are counted; every other
    method/path flows through untouched.

    The client is identified by the ``X-Real-IP`` header when present
    (set by the nginx reverse proxy in production), falling back to the
    transport client host.
    """

    def __init__(
        self,
        app: ASGIApp,
        limiter: FixedWindowRateLimiter,
        paths: frozenset[str] = RATE_LIMITED_AUTH_PATHS,
    ) -> None:
        self.app = app
        self._limiter = limiter
        self._paths = paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or scope["path"] not in self._paths
        ):
            await self.app(scope, receive, send)
            return

        ip = self._client_ip(scope)
        if not self._limiter.allow(ip, scope["path"]):
            log.warning(
                "Rate limit exceeded for %s on %s", ip, scope["path"]
            )
            response = JSONResponse(
                {
                    "error": "rate_limited",
                    "message": "Too many attempts — try again later",
                },
                status_code=429,
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)

    @staticmethod
    def _client_ip(scope: Scope) -> str:
        """``X-Real-IP`` header if set, else the transport client host."""
        for name, value in scope.get("headers", []):
            if name == b"x-real-ip":
                ip = value.decode("latin-1").strip()
                if ip:
                    return ip
        client = scope.get("client")
        if client:
            return str(client[0])
        return "unknown"
