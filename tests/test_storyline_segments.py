"""Unit tests for storyline segment helpers and the curation builder.

Specifically asserts that citation segments carry the optional
``entry_date`` field — the webapp uses it for the absolute-date
toggle on the curation panel and the date eyebrows on the narrative
panel.
"""

from __future__ import annotations

from journal.models import DatedEntryExcerpt
from journal.services.storylines.segments import (
    citation_segment,
    is_valid_segment,
)
from journal.services.storylines.service import _build_curation_segments


def test_citation_segment_omits_entry_date_when_not_provided() -> None:
    seg = citation_segment(42, "a quote")
    assert seg == {"kind": "citation", "entry_id": 42, "quote": "a quote"}
    assert "entry_date" not in seg


def test_citation_segment_includes_entry_date_when_provided() -> None:
    seg = citation_segment(42, "a quote", entry_date="2026-03-15")
    assert seg["entry_date"] == "2026-03-15"
    assert is_valid_segment(seg)


def test_is_valid_segment_rejects_non_string_entry_date() -> None:
    assert not is_valid_segment(
        {"kind": "citation", "entry_id": 1, "quote": "q", "entry_date": 20260315}
    )


def test_is_valid_segment_accepts_citation_without_entry_date() -> None:
    # Older stored panels predate the field — they must still validate.
    assert is_valid_segment({"kind": "citation", "entry_id": 1, "quote": "q"})


def test_build_curation_segments_stamps_each_citation_with_entry_date() -> None:
    excerpts = [
        DatedEntryExcerpt(
            entry_id=10,
            entry_date="2026-02-15",
            final_text="body 1",
            quotes=["q1"],
        ),
        DatedEntryExcerpt(
            entry_id=11,
            entry_date="2026-03-01",
            final_text="body 2",
            quotes=["q2"],
        ),
    ]
    segments = _build_curation_segments(excerpts, transitions=["Two weeks later:"])
    citations = [s for s in segments if s["kind"] == "citation"]
    assert citations == [
        {
            "kind": "citation",
            "entry_id": 10,
            "quote": "q1",
            "entry_date": "2026-02-15",
        },
        {
            "kind": "citation",
            "entry_id": 11,
            "quote": "q2",
            "entry_date": "2026-03-01",
        },
    ]
