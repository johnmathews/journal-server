"""W3 — unit tests for the Strava pending-state store.

Mirrors the shape of ``test_garmin_pending.py`` but tests the simpler
``StravaPendingStore``: the value carries only ``(user_id, expires_at)``,
no live SDK client to park (the Strava callback is a single round-trip).
The CSPRNG token / TTL / lazy-sweep / peek-vs-consume / user-binding
contracts are the same as Garmin's, so the tests track those one-for-one.
"""

from __future__ import annotations

from journal.services.fitness.strava_pending import (
    PENDING_STATE_TTL_SECONDS,
    StravaPendingState,
    StravaPendingStore,
)


class _Clock:
    """Deterministic monotonic clock for TTL tests."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def test_issue_returns_unique_token_and_iso_expiry() -> None:
    store = StravaPendingStore()
    token_a, exp_a = store.issue(user_id=1)
    token_b, exp_b = store.issue(user_id=1)

    assert token_a != token_b
    assert isinstance(token_a, str)
    assert len(token_a) >= 32  # 256 bits of base64url
    assert exp_a.endswith("Z") and "T" in exp_a
    assert exp_b.endswith("Z") and "T" in exp_b


def test_issue_default_ttl_matches_constant() -> None:
    assert PENDING_STATE_TTL_SECONDS == 10 * 60


def test_peek_returns_entry_without_consuming() -> None:
    store = StravaPendingStore()
    token, _ = store.issue(user_id=42)

    entry = store.peek(token)
    assert isinstance(entry, StravaPendingState)
    assert entry.user_id == 42

    # Peek a second time: still there.
    entry2 = store.peek(token)
    assert entry2 is not None
    assert entry2.user_id == 42


def test_consume_removes_entry() -> None:
    store = StravaPendingStore()
    token, _ = store.issue(user_id=7)

    consumed = store.consume(token)
    assert consumed is not None
    assert consumed.user_id == 7

    assert store.peek(token) is None
    assert store.consume(token) is None


def test_peek_unknown_token_returns_none() -> None:
    store = StravaPendingStore()
    assert store.peek("no-such-token") is None
    assert store.consume("no-such-token") is None


def test_expired_entry_is_swept_on_peek() -> None:
    clock = _Clock()
    store = StravaPendingStore(time_func=clock)
    token, _ = store.issue(user_id=1)

    clock.advance(PENDING_STATE_TTL_SECONDS + 1)
    assert store.peek(token) is None
    # Internal map should also be cleared by the lazy sweep.
    assert store.consume(token) is None


def test_expired_entry_is_swept_on_consume() -> None:
    clock = _Clock()
    store = StravaPendingStore(time_func=clock)
    token, _ = store.issue(user_id=1)

    clock.advance(PENDING_STATE_TTL_SECONDS + 1)
    assert store.consume(token) is None


def test_unrelated_entries_are_not_swept_when_one_expires() -> None:
    clock = _Clock()
    store = StravaPendingStore(time_func=clock, ttl_seconds=600)
    token_old, _ = store.issue(user_id=1)
    clock.advance(300)
    token_fresh, _ = store.issue(user_id=2)
    clock.advance(400)  # token_old expired, token_fresh still alive

    assert store.peek(token_old) is None
    fresh = store.peek(token_fresh)
    assert fresh is not None
    assert fresh.user_id == 2


def test_user_id_binding_is_preserved_across_peek_then_consume() -> None:
    """The same user_id surfaces from peek and consume — the binding is
    the load-bearing field for the cross-user replay rejection at the
    endpoint layer."""
    store = StravaPendingStore()
    token, _ = store.issue(user_id=99)

    peek_entry = store.peek(token)
    consume_entry = store.consume(token)

    assert peek_entry is not None
    assert consume_entry is not None
    assert peek_entry.user_id == consume_entry.user_id == 99


def test_custom_ttl_is_respected() -> None:
    clock = _Clock()
    store = StravaPendingStore(time_func=clock, ttl_seconds=30)
    token, _ = store.issue(user_id=1)

    clock.advance(20)
    assert store.peek(token) is not None

    clock.advance(15)  # total 35s, past 30s TTL
    assert store.peek(token) is None
