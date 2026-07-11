"""Tests for the storyline recheck catch-up (W6).

``recheck_storylines`` re-runs the extension classifier over a set of
entries and regenerates every storyline that any of them extends. It is
the manual catch-up for entries ingested while auto-extension was broken
(or before the feature existed): unlike the live path it runs
synchronously in the CLI process, and it coalesces — each matched
storyline is regenerated once regardless of how many entries hit it.
"""

from __future__ import annotations

from dataclasses import dataclass

from journal.services.storylines.recheck import recheck_storylines
from journal.services.storylines.service import GenerationResult


@dataclass
class _R:
    storyline_id: int
    decision: str


class _FakeClassifier:
    def __init__(self, by_entry: dict[int, list[_R]]) -> None:
        self._by_entry = by_entry
        self.calls: list[int] = []

    def classify_for_entry(self, entry_id: int, user_id: int) -> list[_R]:
        self.calls.append(entry_id)
        return self._by_entry.get(entry_id, [])


class _FakeGen:
    def __init__(self) -> None:
        self.calls: list[tuple[int, bool]] = []

    def regenerate(
        self, storyline_id: int, *, auto_split: bool = False,
    ) -> GenerationResult:
        self.calls.append((storyline_id, auto_split))
        return GenerationResult(storyline_id=storyline_id)


def test_dry_run_reports_matches_without_regenerating() -> None:
    classifier = _FakeClassifier({
        10: [_R(1, "yes"), _R(2, "no")],
        11: [_R(1, "yes")],
    })
    gen = _FakeGen()

    result = recheck_storylines(
        classifier=classifier,
        generation_service=gen,
        entry_ids=[10, 11],
        user_id=1,
        dry_run=True,
    )

    assert result.dry_run is True
    assert result.entries_checked == 2
    assert result.matched_storyline_ids == [1]
    assert result.regenerated_storyline_ids == []
    assert gen.calls == []


def test_execute_regenerates_each_matched_storyline_once() -> None:
    classifier = _FakeClassifier({
        10: [_R(1, "yes"), _R(3, "yes")],
        11: [_R(1, "yes")],  # storyline 1 matched twice → one regen
        12: [_R(2, "maybe")],  # maybe is not a match
    })
    gen = _FakeGen()

    result = recheck_storylines(
        classifier=classifier,
        generation_service=gen,
        entry_ids=[10, 11, 12],
        user_id=1,
        dry_run=False,
    )

    assert result.entries_checked == 3
    assert result.matched_storyline_ids == [1, 3]
    assert result.regenerated_storyline_ids == [1, 3]
    # Coalesced: storyline 1 regenerated once, with auto_split.
    assert sorted(gen.calls) == [(1, True), (3, True)]


def test_no_matches_regenerates_nothing() -> None:
    classifier = _FakeClassifier({10: [_R(1, "no")]})
    gen = _FakeGen()

    result = recheck_storylines(
        classifier=classifier,
        generation_service=gen,
        entry_ids=[10],
        user_id=1,
        dry_run=False,
    )

    assert result.matched_storyline_ids == []
    assert gen.calls == []
