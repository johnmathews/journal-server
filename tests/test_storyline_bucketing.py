"""Unit tests for the deterministic excerpt bucketer used by resegment.

``_split_excerpts_contiguous`` partitions date-ordered excerpts into ~k
contiguous groups without splitting a same-date run across a boundary —
the basis for turning one storyline into multiple word-sized chapters when
the sectioning narrator refuses to split on its own.
"""

from __future__ import annotations

from dataclasses import dataclass

from journal.services.storylines.service import _split_excerpts_contiguous


@dataclass
class _Ex:
    entry_id: int
    entry_date: str


def _mk(dates: list[str]) -> list[_Ex]:
    return [_Ex(entry_id=i, entry_date=d) for i, d in enumerate(dates)]


def test_even_split() -> None:
    ex = _mk([f"2026-01-{d:02d}" for d in range(1, 9)])  # 8 distinct dates
    groups = _split_excerpts_contiguous(ex, 4)
    assert [len(g) for g in groups] == [2, 2, 2, 2]
    # Contiguous and order-preserving.
    assert [e.entry_id for g in groups for e in g] == list(range(8))


def test_uneven_split_front_loads_remainder() -> None:
    ex = _mk([f"2026-01-{d:02d}" for d in range(1, 8)])  # 7 distinct dates
    groups = _split_excerpts_contiguous(ex, 3)
    # ceil distribution: 3, 2, 2
    assert [len(g) for g in groups] == [3, 2, 2]


def test_k_one_returns_single_group() -> None:
    ex = _mk(["2026-01-01", "2026-01-02", "2026-01-03"])
    assert _split_excerpts_contiguous(ex, 1) == [ex]


def test_k_larger_than_n_caps_at_n() -> None:
    ex = _mk(["2026-01-01", "2026-01-02"])
    groups = _split_excerpts_contiguous(ex, 10)
    assert [len(g) for g in groups] == [1, 1]


def test_same_date_run_not_split_across_boundary() -> None:
    # Six entries, but four share 2026-01-02. A naive count split at index 3
    # would cut the same-date run; the bucketer snaps the boundary forward.
    ex = _mk([
        "2026-01-01",
        "2026-01-02", "2026-01-02", "2026-01-02", "2026-01-02",
        "2026-01-03",
    ])
    groups = _split_excerpts_contiguous(ex, 3)
    # No date appears in two different groups.
    for i, g in enumerate(groups):
        for other in groups[i + 1:]:
            assert not ({e.entry_date for e in g} & {e.entry_date for e in other})
    # All excerpts preserved, in order.
    assert [e.entry_id for g in groups for e in g] == list(range(6))


def test_empty_returns_single_empty_group() -> None:
    assert _split_excerpts_contiguous([], 3) == [[]]
