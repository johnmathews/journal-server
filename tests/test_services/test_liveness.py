"""Tests for per-component liveness checks."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from journal.services.liveness import (
    check_api_key,
    check_chromadb,
    check_fitness_freshness,
    check_sqlite,
    overall_status,
)


class TestSQLiteCheck:
    def test_ok_on_working_connection(self) -> None:
        conn = sqlite3.connect(":memory:")
        result = check_sqlite(conn)
        assert result.name == "sqlite"
        assert result.status == "ok"
        assert result.error is None

    def test_error_on_closed_connection(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.close()
        result = check_sqlite(conn)
        assert result.status == "error"
        assert result.error is not None


class TestChromaDBCheck:
    def test_ok_with_count(self) -> None:
        store = MagicMock()
        store.count.return_value = 42
        result = check_chromadb(store)
        assert result.status == "ok"
        assert "42" in result.detail

    def test_error_when_count_raises(self) -> None:
        store = MagicMock()
        store.count.side_effect = RuntimeError("connection refused")
        result = check_chromadb(store)
        assert result.status == "error"
        assert result.error == "connection refused"


class TestAPIKeyCheck:
    def test_degraded_when_missing(self) -> None:
        result = check_api_key("anthropic", None)
        assert result.status == "degraded"

    def test_degraded_when_empty(self) -> None:
        result = check_api_key("anthropic", "")
        assert result.status == "degraded"

    def test_degraded_when_too_short(self) -> None:
        result = check_api_key("anthropic", "short", min_length=20)
        assert result.status == "degraded"
        assert "shorter" in result.detail

    def test_ok_with_plausible_key(self) -> None:
        # 40-char string — Anthropic keys in reality are longer
        # but the check only enforces min_length.
        result = check_api_key("anthropic", "a" * 40)
        assert result.status == "ok"
        assert "40 chars" in result.detail


class TestOverallStatus:
    def test_all_ok_is_ok(self) -> None:
        from journal.services.liveness import ComponentCheck

        checks = [
            ComponentCheck("a", "ok", "fine"),
            ComponentCheck("b", "ok", "fine"),
        ]
        assert overall_status(checks) == "ok"

    def test_any_degraded_is_degraded(self) -> None:
        from journal.services.liveness import ComponentCheck

        checks = [
            ComponentCheck("a", "ok", "fine"),
            ComponentCheck("b", "degraded", "meh"),
        ]
        assert overall_status(checks) == "degraded"

    def test_any_error_wins_over_degraded(self) -> None:
        from journal.services.liveness import ComponentCheck

        checks = [
            ComponentCheck("a", "degraded", "meh"),
            ComponentCheck("b", "error", "bad"),
        ]
        assert overall_status(checks) == "error"

    def test_empty_list_defaults_to_ok(self) -> None:
        assert overall_status([]) == "ok"


class TestFitnessFreshnessCheck:
    """`check_fitness_freshness` rolls up a list of per-source health
    summary dicts (as returned by `FitnessRepository.get_health_summary`)
    into a single `ComponentCheck`. The threshold is the number of hours
    a source must have been broken before the check returns `degraded`.
    """

    NOW = datetime(2026, 5, 9, 4, 0, 0, tzinfo=UTC)

    def _hours_ago(self, hours: float) -> str:
        return (self.NOW - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def test_empty_summary_is_ok(self) -> None:
        result = check_fitness_freshness(
            summary=[], threshold_hours=48, now=self.NOW,
        )
        assert result.name == "fitness"
        assert result.status == "ok"
        assert result.error is None

    def test_all_sources_ok_is_ok(self) -> None:
        summary = [
            {"source": "strava", "auth_status": "ok",
             "auth_broken_since": None, "last_success_at": self._hours_ago(1)},
            {"source": "garmin", "auth_status": "ok",
             "auth_broken_since": None, "last_success_at": self._hours_ago(1)},
        ]
        result = check_fitness_freshness(
            summary=summary, threshold_hours=48, now=self.NOW,
        )
        assert result.status == "ok"

    def test_recently_broken_under_threshold_is_ok(self) -> None:
        """`auth_status='broken'` with `auth_broken_since` < threshold ago
        is operator information, not a degradation. Stays `ok` so the
        public probe doesn't flap on every failed token refresh."""
        summary = [
            {"source": "strava", "auth_status": "broken",
             "auth_broken_since": self._hours_ago(2),
             "last_success_at": self._hours_ago(3)},
        ]
        result = check_fitness_freshness(
            summary=summary, threshold_hours=48, now=self.NOW,
        )
        assert result.status == "ok"

    def test_broken_over_threshold_is_degraded(self) -> None:
        summary = [
            {"source": "strava", "auth_status": "ok",
             "auth_broken_since": None,
             "last_success_at": self._hours_ago(1)},
            {"source": "garmin", "auth_status": "broken",
             "auth_broken_since": self._hours_ago(72),
             "last_success_at": self._hours_ago(80)},
        ]
        result = check_fitness_freshness(
            summary=summary, threshold_hours=48, now=self.NOW,
        )
        assert result.status == "degraded"
        assert "garmin" in result.detail

    def test_threshold_boundary_uses_strict_greater_than(self) -> None:
        """Exactly 48h ago is *not yet* over a 48h threshold (use `>`,
        not `>=`). One second past 48h is. Pinning the boundary so a
        future tweak doesn't silently flip flap behaviour."""
        summary_at_threshold = [
            {"source": "strava", "auth_status": "broken",
             "auth_broken_since": self._hours_ago(48),
             "last_success_at": self._hours_ago(49)},
        ]
        assert check_fitness_freshness(
            summary=summary_at_threshold, threshold_hours=48, now=self.NOW,
        ).status == "ok"

        summary_past_threshold = [
            {"source": "strava", "auth_status": "broken",
             "auth_broken_since": self._hours_ago(48.5),
             "last_success_at": self._hours_ago(49)},
        ]
        assert check_fitness_freshness(
            summary=summary_past_threshold, threshold_hours=48, now=self.NOW,
        ).status == "degraded"

    def test_broken_without_since_is_ok(self) -> None:
        """An `auth_status='broken'` row with `auth_broken_since=None`
        shouldn't happen in practice (W6 always sets the timestamp on
        transition) but the check must not crash if it does. Treat
        as `ok` since we can't compute the duration."""
        summary = [
            {"source": "strava", "auth_status": "broken",
             "auth_broken_since": None,
             "last_success_at": None},
        ]
        result = check_fitness_freshness(
            summary=summary, threshold_hours=48, now=self.NOW,
        )
        assert result.status == "ok"

    def test_custom_threshold_one_hour(self) -> None:
        """Tunable `FITNESS_HEALTH_BROKEN_DEGRADED_HOURS` flows through
        as the `threshold_hours` arg."""
        summary = [
            {"source": "strava", "auth_status": "broken",
             "auth_broken_since": self._hours_ago(2),
             "last_success_at": self._hours_ago(3)},
        ]
        assert check_fitness_freshness(
            summary=summary, threshold_hours=1, now=self.NOW,
        ).status == "degraded"

    def test_multiple_broken_sources_named_in_detail(self) -> None:
        summary = [
            {"source": "strava", "auth_status": "broken",
             "auth_broken_since": self._hours_ago(72),
             "last_success_at": self._hours_ago(80)},
            {"source": "garmin", "auth_status": "broken",
             "auth_broken_since": self._hours_ago(60),
             "last_success_at": self._hours_ago(70)},
        ]
        result = check_fitness_freshness(
            summary=summary, threshold_hours=48, now=self.NOW,
        )
        assert result.status == "degraded"
        assert "strava" in result.detail and "garmin" in result.detail
