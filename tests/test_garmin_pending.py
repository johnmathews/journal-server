"""Unit tests for the W2 Garmin pending-session store + cool-down tracker.

These cover the data-shape primitives directly. End-to-end coverage of how
the connect/MFA endpoints use them lives in
``tests/test_api_fitness_garmin_auth.py``.
"""

from __future__ import annotations

import pytest

from journal.services.fitness.garmin_pending import (
    DEFAULT_COOLDOWN_THRESHOLD,
    DEFAULT_COOLDOWN_WINDOW_S,
    DEFAULT_UPSTREAM_BLOCK_S,
    PENDING_TTL_SECONDS,
    GarminCooldownTracker,
    GarminPendingStore,
    GarminUpstreamCooldown,
)


class _FakeClock:
    """Monotonic-clock stand-in. Tests advance ``self.t`` directly."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


# ── Pending store ────────────────────────────────────────────────────


def test_pending_issue_returns_token_and_iso_expiry() -> None:
    store = GarminPendingStore()
    token, expires_iso = store.issue(user_id=1, client="client", state_token="state")
    assert isinstance(token, str)
    # 256-bit base64url ≈ 43 chars; secrets.token_urlsafe(32) is 43 chars.
    assert len(token) >= 32
    assert expires_iso.endswith("Z")


def test_pending_consume_returns_entry_and_removes_it() -> None:
    store = GarminPendingStore()
    token, _ = store.issue(user_id=7, client="C", state_token="S")

    entry = store.consume(token)
    assert entry is not None
    assert entry.user_id == 7
    assert entry.client == "C"
    assert entry.state_token == "S"

    # Second consume returns None — the entry is gone.
    assert store.consume(token) is None


def test_pending_unknown_token_returns_none() -> None:
    store = GarminPendingStore()
    assert store.consume("nope") is None
    assert store.peek("nope") is None


def test_pending_entry_expires_after_ttl() -> None:
    clock = _FakeClock()
    store = GarminPendingStore(time_func=clock)
    token, _ = store.issue(user_id=1, client="C", state_token="S")

    # Just before expiry — still there.
    clock.t += PENDING_TTL_SECONDS - 1
    assert store.peek(token) is not None

    # At TTL — gone (boundary is "expires_at <= now").
    clock.t += 1
    assert store.peek(token) is None
    assert store.consume(token) is None


def test_pending_session_defaults_carry_no_credentials() -> None:
    """W5: sessions issued without credential kwargs (key-unset mode, and
    every pre-W5 call site) default to empty username / no ciphertext."""
    store = GarminPendingStore()
    token, _ = store.issue(user_id=1, client="C", state_token="S")
    entry = store.consume(token)
    assert entry is not None
    assert entry.username == ""
    assert entry.enc_password is None


def test_pending_issue_carries_username_and_ciphertext() -> None:
    """W5: the connect handler passes the username and the *encrypted*
    password through the pending session so MFA completion can persist
    them. The store is a dumb carrier — it never sees plaintext."""
    store = GarminPendingStore()
    token, _ = store.issue(
        user_id=3,
        client="C",
        state_token="S",
        username="alice@example.com",
        enc_password="gAAAAA-ciphertext",
    )
    entry = store.consume(token)
    assert entry is not None
    assert entry.username == "alice@example.com"
    assert entry.enc_password == "gAAAAA-ciphertext"


def test_pending_two_distinct_tokens_dont_collide() -> None:
    store = GarminPendingStore()
    t1, _ = store.issue(user_id=1, client="A", state_token="sa")
    t2, _ = store.issue(user_id=2, client="B", state_token="sb")
    assert t1 != t2

    e1 = store.consume(t1)
    e2 = store.consume(t2)
    assert e1 is not None and e1.client == "A"
    assert e2 is not None and e2.client == "B"


# ── Cool-down tracker ────────────────────────────────────────────────


def test_cooldown_default_thresholds_are_documented() -> None:
    # Sanity: the public knobs match the ones called out in the W2 plan
    # (5 failures within 15 minutes per email keys on clientId+email).
    assert DEFAULT_COOLDOWN_THRESHOLD == 5
    assert DEFAULT_COOLDOWN_WINDOW_S == 15 * 60


def test_cooldown_clean_state_allows_attempts() -> None:
    tracker = GarminCooldownTracker()
    assert tracker.check("alice@example.com") is None


def test_cooldown_under_threshold_still_allows_attempts() -> None:
    tracker = GarminCooldownTracker()
    for _ in range(DEFAULT_COOLDOWN_THRESHOLD - 1):
        tracker.record_failure("alice@example.com")
    assert tracker.check("alice@example.com") is None


def test_cooldown_threshold_failures_locks_out_for_window() -> None:
    clock = _FakeClock()
    tracker = GarminCooldownTracker(time_func=clock)
    for _ in range(DEFAULT_COOLDOWN_THRESHOLD):
        tracker.record_failure("alice@example.com")
    retry_after = tracker.check("alice@example.com")
    assert retry_after is not None
    assert retry_after > 0
    assert retry_after <= DEFAULT_COOLDOWN_WINDOW_S


def test_cooldown_window_expiry_releases_lockout() -> None:
    clock = _FakeClock()
    tracker = GarminCooldownTracker(time_func=clock)
    for _ in range(DEFAULT_COOLDOWN_THRESHOLD):
        tracker.record_failure("alice@example.com")
    assert tracker.check("alice@example.com") is not None

    # Roll forward past the window — the failures age out, attempts allowed.
    clock.t += DEFAULT_COOLDOWN_WINDOW_S + 1
    assert tracker.check("alice@example.com") is None


def test_cooldown_keys_per_email_independently() -> None:
    tracker = GarminCooldownTracker()
    for _ in range(DEFAULT_COOLDOWN_THRESHOLD):
        tracker.record_failure("alice@example.com")
    assert tracker.check("alice@example.com") is not None
    # bob isn't affected.
    assert tracker.check("bob@example.com") is None


def test_cooldown_reset_clears_failures() -> None:
    tracker = GarminCooldownTracker()
    for _ in range(DEFAULT_COOLDOWN_THRESHOLD):
        tracker.record_failure("alice@example.com")
    assert tracker.check("alice@example.com") is not None
    tracker.reset("alice@example.com")
    assert tracker.check("alice@example.com") is None


def test_cooldown_normalises_email_case_and_whitespace() -> None:
    # Garmin's own rate-limiter is email-keyed and case-insensitive; our
    # local tracker must agree, otherwise "Alice@example.com " and
    # "alice@example.com" would each accrue separate failure budgets and
    # the protective effect halves.
    tracker = GarminCooldownTracker()
    for _ in range(DEFAULT_COOLDOWN_THRESHOLD):
        tracker.record_failure("Alice@Example.com")
    assert tracker.check("  alice@example.com  ") is not None


@pytest.mark.parametrize("failures_before_reset", [1, 3, 4])
def test_cooldown_reset_after_partial_failures_preserves_clean_state(
    failures_before_reset: int,
) -> None:
    tracker = GarminCooldownTracker()
    for _ in range(failures_before_reset):
        tracker.record_failure("alice@example.com")
    tracker.reset("alice@example.com")
    # After reset, threshold-1 more failures should not trigger lockout.
    for _ in range(DEFAULT_COOLDOWN_THRESHOLD - 1):
        tracker.record_failure("alice@example.com")
    assert tracker.check("alice@example.com") is None


# ── Upstream (global) cooldown ───────────────────────────────────────


def test_upstream_cooldown_default_block_documented() -> None:
    assert DEFAULT_UPSTREAM_BLOCK_S == 5 * 60


def test_upstream_cooldown_clean_state_allows_attempts() -> None:
    gate = GarminUpstreamCooldown()
    assert gate.check() is None


def test_upstream_cooldown_single_block_trips_the_gate() -> None:
    # Unlike the per-email tracker, ONE block is enough — there is no benign
    # reason to retry into a live Cloudflare block.
    clock = _FakeClock()
    gate = GarminUpstreamCooldown(time_func=clock)
    gate.record_block()
    remaining = gate.check()
    assert remaining is not None
    assert 0 < remaining <= DEFAULT_UPSTREAM_BLOCK_S


def test_upstream_cooldown_expires_after_block_window() -> None:
    clock = _FakeClock()
    gate = GarminUpstreamCooldown(time_func=clock)
    gate.record_block()
    assert gate.check() is not None

    clock.t += DEFAULT_UPSTREAM_BLOCK_S + 1
    assert gate.check() is None


def test_upstream_cooldown_reset_clears_block() -> None:
    gate = GarminUpstreamCooldown()
    gate.record_block()
    assert gate.check() is not None
    gate.reset()
    assert gate.check() is None


def test_upstream_cooldown_re_arm_extends_window() -> None:
    clock = _FakeClock()
    gate = GarminUpstreamCooldown(time_func=clock)
    gate.record_block()

    # Half-way through, a second block pushes expiry out to a fresh full window.
    clock.t += DEFAULT_UPSTREAM_BLOCK_S / 2
    gate.record_block()
    clock.t += DEFAULT_UPSTREAM_BLOCK_S / 2 + 1
    # Would have expired under the first block; the re-arm keeps it hot.
    assert gate.check() is not None
