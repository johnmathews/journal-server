"""Migration 0035 backfills two pricing rows referenced in code but missing
from the 0017 seed: ``claude-opus-4-7`` (storyline narrator default) and
``whisper-1`` (transcription fallback).
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


def test_claude_opus_4_7_row_backfilled(tmp_path: Path) -> None:
    conn = _migrated(tmp_path).get()
    row = conn.execute(
        "SELECT category, input_cost_per_mtok, output_cost_per_mtok, "
        "cost_per_minute FROM pricing WHERE model = 'claude-opus-4-7'"
    ).fetchone()
    assert row is not None
    assert row["category"] == "llm"
    assert row["input_cost_per_mtok"] == 5.0
    assert row["output_cost_per_mtok"] == 25.0
    assert row["cost_per_minute"] is None


def test_whisper_1_row_backfilled(tmp_path: Path) -> None:
    conn = _migrated(tmp_path).get()
    row = conn.execute(
        "SELECT category, input_cost_per_mtok, output_cost_per_mtok, "
        "cost_per_minute FROM pricing WHERE model = 'whisper-1'"
    ).fetchone()
    assert row is not None
    assert row["category"] == "transcription"
    assert row["input_cost_per_mtok"] is None
    assert row["output_cost_per_mtok"] is None
    assert row["cost_per_minute"] == 0.006
