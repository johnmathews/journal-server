"""Manual catch-up: re-classify entries and regenerate matched storylines.

The live path fires an extension check per newly-ingested entry. When that
path was broken (or predates a storyline), the affected storylines never
updated. ``recheck_storylines`` is the recovery tool: it re-runs the
classifier over a caller-supplied set of entries and regenerates every
storyline any of them extends.

Unlike the live path it runs **synchronously** (the CLI process has no job
runner) and it **coalesces** — a storyline matched by several entries is
regenerated once. Callers default to ``dry_run=True`` so the (LLM-costed)
regenerations are always explicit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Iterable


class _ClassifierLike(Protocol):
    def classify_for_entry(self, entry_id: int, user_id: int) -> list: ...


class _GenServiceLike(Protocol):
    def regenerate(self, storyline_id: int, *, auto_split: bool = ...): ...


@dataclass
class RecheckResult:
    entries_checked: int
    matched_storyline_ids: list[int]
    regenerated_storyline_ids: list[int]
    dry_run: bool


def recheck_storylines(
    *,
    classifier: _ClassifierLike,
    generation_service: _GenServiceLike,
    entry_ids: Iterable[int],
    user_id: int,
    dry_run: bool = True,
    auto_split: bool = True,
) -> RecheckResult:
    """Re-classify ``entry_ids`` and regenerate every matched storyline.

    Args:
        entry_ids: entries to re-check (e.g. everything since a date).
        user_id: owner whose active storylines are classified against.
        dry_run: when True (default) report matches without regenerating.
        auto_split: forwarded to ``regenerate`` so an over-budget open
            chapter is re-segmented after the refresh (matches the live
            ingest path).
    """
    entries = list(entry_ids)
    matched: set[int] = set()
    for entry_id in entries:
        for result in classifier.classify_for_entry(
            entry_id=entry_id, user_id=user_id,
        ):
            if result.decision == "yes":
                matched.add(result.storyline_id)

    matched_ids = sorted(matched)
    regenerated: list[int] = []
    if not dry_run:
        for storyline_id in matched_ids:
            generation_service.regenerate(storyline_id, auto_split=auto_split)
            regenerated.append(storyline_id)

    return RecheckResult(
        entries_checked=len(entries),
        matched_storyline_ids=matched_ids,
        regenerated_storyline_ids=regenerated,
        dry_run=dry_run,
    )
