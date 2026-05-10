"""W3 — in-memory pending Strava OAuth state store.

A small data structure that bridges the two halves of the per-user Strava
OAuth flow implemented by ``api/fitness.py`` (W3 of the multi-user plan,
see ``docs/fitness-multiuser-plan.md`` §5 W3 and §3 D4).

:class:`StravaPendingStore` holds the ``(user_id, expires_at)`` issued at
``GET /api/fitness/strava/authorize_url`` between that endpoint and the
matching ``POST /api/fitness/strava/exchange`` consume. Entries are keyed
by a 256-bit CSPRNG token, bound to the originating ``user_id``, and
expire after 10 minutes. Single-process server, in-memory only — restart
drops every in-flight authorize_url and the user repeats the connect form.

Parallel to :mod:`journal.services.fitness.garmin_pending` rather than
derived from it: the Garmin store parks a *live SDK client* between
the two endpoints (the MFA flow is a single SDK session split across
two HTTP calls); the Strava store has no SDK client to park (Strava's
callback is a single-shot exchange). The TTL / lazy-sweep / CSPRNG-token
/ user-binding contract is the same shape, but factoring a generic
helper at two users would obscure both modules without removing real
duplication — see the W3 journal entry for the rationale.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

# 10-minute TTL matches the Garmin pending store. Long enough for a user to
# walk through Strava's authorize page (which can include a 2FA challenge),
# short enough that a leaked token from a screenshot or log line ages out
# before the next dev session.
PENDING_STATE_TTL_SECONDS = 10 * 60


@dataclass(frozen=True)
class StravaPendingState:
    """One in-flight Strava authorize-url state token."""

    user_id: int
    expires_at: float  # monotonic, for TTL comparison


class StravaPendingStore:
    """Thread-safe map of CSPRNG-token → :class:`StravaPendingState`.

    Lazy expiry: every read or write sweeps expired entries before doing
    its own work, so memory stays bounded without a background sweeper
    thread.
    """

    def __init__(
        self,
        *,
        time_func: Callable[[], float] = time.monotonic,
        ttl_seconds: float = PENDING_STATE_TTL_SECONDS,
    ) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, StravaPendingState] = {}
        self._time = time_func
        self._ttl = ttl_seconds

    def issue(self, *, user_id: int) -> tuple[str, str]:
        """Mint a new pending state.

        Returns ``(token, expires_at_iso)``. The token is a 256-bit CSPRNG
        value (43 chars of URL-safe base64) and the ISO timestamp is
        wall-clock — what the response payload returns to the client.
        """
        token = secrets.token_urlsafe(32)
        expires_at_mono = self._time() + self._ttl
        expires_at_iso = (
            datetime.now(UTC) + timedelta(seconds=self._ttl)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._lock:
            self._sweep_locked()
            self._entries[token] = StravaPendingState(
                user_id=user_id, expires_at=expires_at_mono,
            )
        return token, expires_at_iso

    def consume(self, token: str) -> StravaPendingState | None:
        """Look up + remove the entry. Returns ``None`` if missing/expired."""
        with self._lock:
            self._sweep_locked()
            return self._entries.pop(token, None)

    def peek(self, token: str) -> StravaPendingState | None:
        """Look up without removing. Used by the cross-user rejection path
        which inspects the entry to compare ``user_id`` but must not
        consume it on a 403 — the legitimate user can still complete
        their flow."""
        with self._lock:
            self._sweep_locked()
            return self._entries.get(token)

    def _sweep_locked(self) -> None:
        now = self._time()
        expired = [t for t, s in self._entries.items() if s.expires_at <= now]
        for t in expired:
            del self._entries[t]
