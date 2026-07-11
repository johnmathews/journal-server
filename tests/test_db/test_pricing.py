"""Tests for pricing configuration."""

import sqlite3

import pytest

from journal.db.migrations import run_migrations
from journal.db.pricing import (
    PricingEntry,
    estimate_cost,
    get_all_pricing,
    update_pricing,
)


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    run_migrations(c)
    return c


class TestGetAllPricing:
    """get_all_pricing — seed data and structure."""

    def test_returns_seed_data(self, conn: sqlite3.Connection) -> None:
        entries = get_all_pricing(conn)
        assert len(entries) >= 12
        models = {e.model for e in entries}
        assert "claude-opus-4-6" in models
        assert "gemini-2.5-pro" in models
        assert "text-embedding-3-large" in models
        assert "gpt-4o-transcribe" in models

    def test_all_entries_are_pricing_entry(self, conn: sqlite3.Connection) -> None:
        entries = get_all_pricing(conn)
        for e in entries:
            assert isinstance(e, PricingEntry)

    def test_categories(self, conn: sqlite3.Connection) -> None:
        entries = get_all_pricing(conn)
        categories = {e.category for e in entries}
        assert categories == {"llm", "embedding", "transcription"}

    def test_llm_entries_have_input_and_output(self, conn: sqlite3.Connection) -> None:
        entries = get_all_pricing(conn)
        llm_entries = [e for e in entries if e.category == "llm"]
        assert len(llm_entries) >= 1
        for e in llm_entries:
            assert e.input_cost_per_mtok is not None and e.input_cost_per_mtok > 0
            assert e.output_cost_per_mtok is not None and e.output_cost_per_mtok > 0
            assert e.cost_per_minute is None

    def test_transcription_entries_have_cost_per_minute(
        self, conn: sqlite3.Connection,
    ) -> None:
        entries = get_all_pricing(conn)
        transcription = [e for e in entries if e.category == "transcription"]
        assert len(transcription) >= 1
        for e in transcription:
            assert e.cost_per_minute is not None and e.cost_per_minute > 0
            assert e.input_cost_per_mtok is None
            assert e.output_cost_per_mtok is None

    def test_embedding_entries_have_zero_output(
        self, conn: sqlite3.Connection,
    ) -> None:
        entries = get_all_pricing(conn)
        embeddings = [e for e in entries if e.category == "embedding"]
        assert len(embeddings) >= 1
        for e in embeddings:
            assert e.input_cost_per_mtok is not None and e.input_cost_per_mtok > 0
            assert e.output_cost_per_mtok == 0

    def test_ordered_by_category_then_model(self, conn: sqlite3.Connection) -> None:
        entries = get_all_pricing(conn)
        keys = [(e.category, e.model) for e in entries]
        assert keys == sorted(keys)


class TestUpdatePricing:
    """update_pricing — modification and validation."""

    def test_changes_costs(self, conn: sqlite3.Connection) -> None:
        result = update_pricing(
            conn, "claude-opus-4-6",
            {"input_cost_per_mtok": 6.0, "output_cost_per_mtok": 30.0},
        )
        assert result is not None
        assert result.input_cost_per_mtok == 6.0
        assert result.output_cost_per_mtok == 30.0

    def test_persists_change(self, conn: sqlite3.Connection) -> None:
        update_pricing(conn, "claude-opus-4-6", {"input_cost_per_mtok": 7.0})
        entries = get_all_pricing(conn)
        opus = next(e for e in entries if e.model == "claude-opus-4-6")
        assert opus.input_cost_per_mtok == 7.0

    def test_unknown_model(self, conn: sqlite3.Connection) -> None:
        result = update_pricing(conn, "nonexistent-model", {"input_cost_per_mtok": 1.0})
        assert result is None

    def test_ignores_disallowed_fields(self, conn: sqlite3.Connection) -> None:
        result = update_pricing(
            conn, "claude-opus-4-6",
            {"category": "embedding", "input_cost_per_mtok": 5.0},
        )
        assert result is not None
        assert result.category == "llm"  # category must not change

    def test_empty_dict(self, conn: sqlite3.Connection) -> None:
        result = update_pricing(conn, "claude-opus-4-6", {})
        assert result is None

    def test_only_disallowed_fields(self, conn: sqlite3.Connection) -> None:
        result = update_pricing(conn, "claude-opus-4-6", {"model": "new-name"})
        assert result is None

    def test_updates_last_verified(self, conn: sqlite3.Connection) -> None:
        result = update_pricing(
            conn, "gemini-2.5-pro",
            {"input_cost_per_mtok": 1.5, "last_verified": "2026-05-01"},
        )
        assert result is not None
        assert result.last_verified == "2026-05-01"
        assert result.input_cost_per_mtok == 1.5

    def test_updates_cost_per_minute(self, conn: sqlite3.Connection) -> None:
        result = update_pricing(
            conn, "gpt-4o-transcribe", {"cost_per_minute": 0.01},
        )
        assert result is not None
        assert result.cost_per_minute == 0.01


def _insert_row(
    conn: sqlite3.Connection,
    model: str,
    category: str,
    input_cost: float | None,
    output_cost: float | None,
    cost_per_minute: float | None = None,
) -> None:
    conn.execute(
        "INSERT INTO pricing (model, category, input_cost_per_mtok, "
        "output_cost_per_mtok, cost_per_minute, last_verified) "
        "VALUES (?, ?, ?, ?, ?, '2026-01-01')",
        (model, category, input_cost, output_cost, cost_per_minute),
    )
    conn.commit()


class TestEstimateCost:
    """estimate_cost — best-effort USD cost from captured tokens."""

    def test_single_priced_model(self, conn: sqlite3.Connection) -> None:
        # claude-opus-4-6 is seeded at 5.0 in / 25.0 out per Mtok.
        per_model = {
            "claude-opus-4-6": {"input_tokens": 1_000_000, "output_tokens": 1_000_000},
        }
        cost = estimate_cost(conn, per_model)
        assert cost is not None
        assert cost == pytest.approx(5.0 + 25.0)

    def test_fractional_tokens(self, conn: sqlite3.Connection) -> None:
        # claude-haiku-4-5 seeded at 1.0 in / 5.0 out per Mtok.
        per_model = {
            "claude-haiku-4-5": {"input_tokens": 500_000, "output_tokens": 200_000},
        }
        cost = estimate_cost(conn, per_model)
        assert cost is not None
        # 0.5 * 1.0 + 0.2 * 5.0 = 0.5 + 1.0
        assert cost == pytest.approx(0.5 + 1.0)

    def test_multi_model_sum(self, conn: sqlite3.Connection) -> None:
        per_model = {
            "claude-opus-4-6": {"input_tokens": 1_000_000, "output_tokens": 0},
            "claude-haiku-4-5": {"input_tokens": 0, "output_tokens": 1_000_000},
        }
        cost = estimate_cost(conn, per_model)
        assert cost is not None
        # opus input 5.0 + haiku output 5.0
        assert cost == pytest.approx(5.0 + 5.0)

    def test_unknown_model_excluded_but_others_priced(
        self, conn: sqlite3.Connection,
    ) -> None:
        per_model = {
            "claude-opus-4-6": {"input_tokens": 1_000_000, "output_tokens": 0},
            "no-such-model": {"input_tokens": 9_999_999, "output_tokens": 9_999_999},
        }
        cost = estimate_cost(conn, per_model)
        assert cost is not None
        assert cost == pytest.approx(5.0)

    def test_unknown_model_only_returns_none(
        self, conn: sqlite3.Connection,
    ) -> None:
        per_model = {
            "no-such-model": {"input_tokens": 1_000_000, "output_tokens": 1_000_000},
        }
        assert estimate_cost(conn, per_model) is None

    def test_transcription_category_excluded(
        self, conn: sqlite3.Connection,
    ) -> None:
        # gpt-4o-transcribe is priced per audio-minute, not per token.
        per_model = {
            "gpt-4o-transcribe": {"input_tokens": 1_000_000, "output_tokens": 0},
        }
        assert estimate_cost(conn, per_model) is None

    def test_transcription_excluded_but_llm_priced(
        self, conn: sqlite3.Connection,
    ) -> None:
        per_model = {
            "gpt-4o-transcribe": {"input_tokens": 1_000_000, "output_tokens": 0},
            "claude-opus-4-6": {"input_tokens": 1_000_000, "output_tokens": 0},
        }
        cost = estimate_cost(conn, per_model)
        assert cost is not None
        assert cost == pytest.approx(5.0)

    def test_null_output_cost_still_prices_input(
        self, conn: sqlite3.Connection,
    ) -> None:
        _insert_row(conn, "in-only", "llm", 10.0, None)
        per_model = {
            "in-only": {"input_tokens": 1_000_000, "output_tokens": 1_000_000},
        }
        cost = estimate_cost(conn, per_model)
        assert cost is not None
        assert cost == pytest.approx(10.0)  # output term skipped

    def test_null_input_cost_still_prices_output(
        self, conn: sqlite3.Connection,
    ) -> None:
        _insert_row(conn, "out-only", "llm", None, 20.0)
        per_model = {
            "out-only": {"input_tokens": 1_000_000, "output_tokens": 1_000_000},
        }
        cost = estimate_cost(conn, per_model)
        assert cost is not None
        assert cost == pytest.approx(20.0)  # input term skipped

    def test_empty_per_model_returns_none(self, conn: sqlite3.Connection) -> None:
        assert estimate_cost(conn, {}) is None

    def test_embedding_model_priced(self, conn: sqlite3.Connection) -> None:
        # text-embedding-3-large: 0.13 in, 0 out — embeddings are in scope.
        per_model = {
            "text-embedding-3-large": {
                "input_tokens": 1_000_000,
                "output_tokens": 0,
            },
        }
        cost = estimate_cost(conn, per_model)
        assert cost is not None
        assert cost == pytest.approx(0.13)
