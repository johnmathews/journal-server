"""Passage selection + truncation helpers for conversation replies."""

from __future__ import annotations

from journal.models import ChunkMatch, SearchResult
from journal.services.conversations.passages import window_passage


def _result(text: str, *, chunks=None, snippet=None) -> SearchResult:
    return SearchResult(
        entry_id=1, entry_date="2026-01-01", text=text, score=1.0,
        matching_chunks=chunks or [], snippet=snippet,
    )


def test_window_centers_on_matching_chunk() -> None:
    text = "A" * 1000 + "TARGET" + "B" * 1000
    chunk = ChunkMatch(text="TARGET", score=0.9, chunk_index=1,
                       char_start=1000, char_end=1006)
    out = window_passage(_result(text, chunks=[chunk]), max_chars=100)
    assert "TARGET" in out
    assert len(out) <= 100


def test_window_falls_back_to_head_when_no_offsets() -> None:
    text = "C" * 500
    out = window_passage(_result(text), max_chars=100)
    assert out == "C" * 100


def test_window_returns_short_text_unchanged() -> None:
    out = window_passage(_result("short"), max_chars=100)
    assert out == "short"
