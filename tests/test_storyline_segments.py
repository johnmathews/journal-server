"""Unit tests for storyline segment helpers.

Specifically asserts that citation segments carry the optional
``entry_date`` field — the webapp uses it for the absolute-date
toggle on the curation panel and the date eyebrows on the narrative
panel.
"""

from __future__ import annotations

from journal.services.storylines.segments import (
    citation_segment,
    is_valid_segment,
)


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
