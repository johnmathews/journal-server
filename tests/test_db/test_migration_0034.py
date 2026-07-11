"""Migration 0034 adds input_tokens/output_tokens/cost_usd columns to the
jobs table (per-job LLM usage; all nullable, cost_usd unfilled until W3).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from journal.db.factory import ConnectionFactory
from journal.db.migrations import run_migrations

if TYPE_CHECKING:
    from pathlib import Path


def _migrated(tmp_path: Path) -> ConnectionFactory:
    factory = ConnectionFactory(tmp_path / "m.db")
    run_migrations(factory.get())
    return factory


def test_jobs_has_token_usage_columns(tmp_path: Path) -> None:
    conn = _migrated(tmp_path).get()
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
    assert "input_tokens" in cols
    assert "output_tokens" in cols
    assert "cost_usd" in cols


def test_token_usage_columns_default_null(tmp_path: Path) -> None:
    conn = _migrated(tmp_path).get()
    conn.execute(
        "INSERT INTO jobs (id, type, status, params_json, progress_current, "
        "progress_total, created_at) "
        "VALUES ('j1', 'mood_backfill', 'queued', '{}', 0, 0, '2026-01-01')"
    )
    row = conn.execute(
        "SELECT input_tokens, output_tokens, cost_usd FROM jobs WHERE id = 'j1'"
    ).fetchone()
    assert row["input_tokens"] is None
    assert row["output_tokens"] is None
    assert row["cost_usd"] is None


def test_token_usage_columns_accept_values(tmp_path: Path) -> None:
    conn = _migrated(tmp_path).get()
    conn.execute(
        "INSERT INTO jobs (id, type, status, params_json, progress_current, "
        "progress_total, created_at, input_tokens, output_tokens, cost_usd) "
        "VALUES ('j2', 'mood_backfill', 'succeeded', '{}', 0, 0, '2026-01-01', "
        "1200, 340, NULL)"
    )
    row = conn.execute(
        "SELECT input_tokens, output_tokens, cost_usd FROM jobs WHERE id = 'j2'"
    ).fetchone()
    assert row["input_tokens"] == 1200
    assert row["output_tokens"] == 340
    assert row["cost_usd"] is None
