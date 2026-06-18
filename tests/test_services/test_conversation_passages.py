"""Passage selection + truncation helpers for conversation replies."""

from __future__ import annotations

from journal.models import ChunkMatch, SearchResult
from journal.providers.answerer import AnswerPassage
from journal.services.conversations.passages import (
    build_citations,
    select_passages,
    window_passage,
)


def _result(
    text: str,
    *,
    chunks: list[ChunkMatch] | None = None,
    snippet: str | None = None,
) -> SearchResult:
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
    idx = out.index("TARGET")
    assert 35 <= idx <= 65  # TARGET sits near the center of the window, not the head


def test_window_falls_back_to_head_when_no_offsets() -> None:
    text = "C" * 500
    out = window_passage(_result(text), max_chars=100)
    assert out == "C" * 100


def test_window_returns_short_text_unchanged() -> None:
    out = window_passage(_result("short"), max_chars=100)
    assert out == "short"


def _scored(entry_id: int, score: float) -> SearchResult:
    return SearchResult(
        entry_id=entry_id, entry_date="2026-01-01", text="t" * 50,
        score=score, matching_chunks=[], snippet=None,
    )


def test_select_keeps_floor_when_one_strong_result() -> None:
    results = [_scored(1, 0.9)] + [_scored(i, 0.05) for i in range(2, 10)]
    out = select_passages(results, max_chars=800, floor=3, ceiling=15, band=0.5)
    # one dominant score -> only the floor survives the band cut
    assert [p.entry_id for p in out] == [1, 2, 3]


def test_select_clamps_to_ceiling_when_many_close() -> None:
    results = [_scored(i, 0.9) for i in range(1, 30)]
    out = select_passages(results, max_chars=800, floor=3, ceiling=15, band=0.5)
    assert len(out) == 15


def test_select_returns_answer_passages_with_windowed_text() -> None:
    out = select_passages([_scored(1, 0.9)], max_chars=10, floor=1, ceiling=5,
                          band=0.5)
    assert isinstance(out[0], AnswerPassage)
    assert out[0].entry_id == 1
    assert len(out[0].text) <= 10


def test_build_citations_resolves_known_ids_and_drops_unknown() -> None:
    by_id = {7: ("2026-03-01", "Back better now, much less pain today.")}
    cites = build_citations([7, 999], by_id, snippet_chars=10)
    assert len(cites) == 1
    assert cites[0]["entry_id"] == 7
    assert cites[0]["entry_date"] == "2026-03-01"
    assert cites[0]["snippet"] == "Back bette"
