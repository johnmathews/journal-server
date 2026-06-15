"""Tests for chapter-editing helpers in storyline_repository.

Phase A, Task 1: date helper functions and the _shift_seqs resequencer.
"""

from __future__ import annotations

from journal.db.storyline_repository import _day_after, _day_before


def test_day_before_and_after() -> None:
    assert _day_before("2026-03-01") == "2026-02-28"
    assert _day_after("2026-02-28") == "2026-03-01"
    # leap year
    assert _day_after("2024-02-28") == "2024-02-29"
