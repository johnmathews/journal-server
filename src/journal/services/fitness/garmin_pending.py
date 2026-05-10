"""W2 — in-memory pending Garmin MFA sessions and per-email cool-down.

Two small data structures that bridge the two-step Garmin connect flow
implemented by ``api/fitness.py`` (W2 of the multi-user plan, see
``docs/fitness-multiuser-plan.md`` §5 W2 and §3 D2).

- :class:`GarminPendingStore` holds the live ``garminconnect.Garmin`` client
  between ``POST /api/fitness/garmin/connect`` (which calls ``login()`` with
  ``return_on_mfa=True`` and sees the ``("needs_mfa", _)`` early-return) and
  ``POST /api/fitness/garmin/connect/mfa`` (which calls ``resume_login`` on
  the same client). Entries are keyed by a 256-bit CSPRNG token, bound to
  the originating ``user_id``, and expire after 10 minutes. Single-process
  server, in-memory only — restart drops every in-flight challenge and the
  user repeats the connect form.
- :class:`GarminCooldownTracker` records recent connect failures per upstream
  email. Garmin's auth rate-limiter keys on ``clientId + email`` (per
  ``python-garminconnect`` issue #344), so a user mistyping their password
  twice in quick succession can trigger an account-wide 429 lockout. The
  local tracker refuses retries for the same email after the threshold is
  reached, surfacing a "too many attempts" error before any upstream call.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

# 10-minute TTL is generous enough that a user can leave the MFA form open
# while reading their phone, and short enough that a leaked token from a
# screenshot or log line ages out before the next dev session.
PENDING_TTL_SECONDS = 10 * 60

DEFAULT_COOLDOWN_THRESHOLD = 5
DEFAULT_COOLDOWN_WINDOW_S = 15 * 60


@dataclass(frozen=True)
class PendingSession:
    """One in-flight Garmin MFA challenge."""

    user_id: int
    client: Any
    state_token: Any
    expires_at: float


class GarminPendingStore:
    """Thread-safe map of CSPRNG-token → :class:`PendingSession`.

    Lazy expiry: every read or write sweeps expired entries before doing its
    own work, so memory stays bounded without a background sweeper thread.
    """

    def __init__(
        self,
        *,
        time_func: Callable[[], float] = time.monotonic,
        ttl_seconds: float = PENDING_TTL_SECONDS,
    ) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, PendingSession] = {}
        self._time = time_func
        self._ttl = ttl_seconds

    def issue(
        self, *, user_id: int, client: Any, state_token: Any,
    ) -> tuple[str, str]:
        """Mint a new pending session.

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
            self._entries[token] = PendingSession(
                user_id=user_id,
                client=client,
                state_token=state_token,
                expires_at=expires_at_mono,
            )
        return token, expires_at_iso

    def consume(self, token: str) -> PendingSession | None:
        """Look up + remove the entry. Returns ``None`` if missing/expired."""
        with self._lock:
            self._sweep_locked()
            return self._entries.pop(token, None)

    def peek(self, token: str) -> PendingSession | None:
        """Look up without removing. Mainly for tests + the cross-user
        rejection path, which inspects the entry to compare ``user_id`` but
        must not consume it on a 403 — the legitimate user can still retry."""
        with self._lock:
            self._sweep_locked()
            return self._entries.get(token)

    def _sweep_locked(self) -> None:
        now = self._time()
        expired = [t for t, s in self._entries.items() if s.expires_at <= now]
        for t in expired:
            del self._entries[t]


class GarminCooldownTracker:
    """Per-email failure counter for connect attempts.

    Failures within the rolling window are kept in a list; once the count
    hits ``threshold`` the email is "locked" until the oldest failure ages
    out. ``record_failure`` and ``check`` both prune expired failures, so
    no separate sweep is needed.
    """

    def __init__(
        self,
        *,
        window_s: float = DEFAULT_COOLDOWN_WINDOW_S,
        threshold: int = DEFAULT_COOLDOWN_THRESHOLD,
        time_func: Callable[[], float] = time.monotonic,
    ) -> None:
        self._lock = threading.Lock()
        self._failures: dict[str, list[float]] = {}
        self._window_s = window_s
        self._threshold = threshold
        self._time = time_func

    def check(self, email: str) -> float | None:
        """Return seconds until next allowed attempt, or ``None`` if allowed
        immediately. Threshold-1 failures returns None; the threshold-th
        attempt is what trips the lockout."""
        norm = self._normalise(email)
        with self._lock:
            failures = self._prune_locked(norm)
            if len(failures) < self._threshold:
                return None
            oldest = failures[0]
            retry_after = max(1.0, oldest + self._window_s - self._time())
            return retry_after

    def record_failure(self, email: str) -> None:
        norm = self._normalise(email)
        with self._lock:
            self._failures.setdefault(norm, []).append(self._time())
            self._prune_locked(norm)

    def reset(self, email: str) -> None:
        norm = self._normalise(email)
        with self._lock:
            self._failures.pop(norm, None)

    @staticmethod
    def _normalise(email: str) -> str:
        return email.strip().lower()

    def _prune_locked(self, email: str) -> list[float]:
        cutoff = self._time() - self._window_s
        kept = [t for t in self._failures.get(email, []) if t > cutoff]
        if kept:
            self._failures[email] = kept
        else:
            self._failures.pop(email, None)
        return kept
