"""Bulk re-section existing storylines into word-sized chapters (W5).

Storylines generated before the chapter feature (migrations 0030/0031)
were backfilled by 0030 into a single ``open`` chapter spanning the whole
timeline, and there is no automatic path that re-carves them — normal
``regenerate`` refreshes the one chapter without splitting, and the
ingest-time auto-split only fires when a *new* matching entry extends the
storyline. This module provides the missing one-off: iterate a user's
storylines and call ``resegment_storyline`` on each.

Re-sectioning costs one sectioning-narrator LLM call per unlocked span,
so callers default to ``dry_run=True`` — a real run must be explicit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from journal.services.storylines.service import GenerationResult


class _StorylineRepoLike(Protocol):
    def list_storylines(
        self, user_id: int, status: str | None = ...,
        limit: int = ..., offset: int = ...,
    ) -> list: ...

    def get_storyline(
        self, storyline_id: int, user_id: int | None = ...,
    ) -> object | None: ...

    def list_chapters(self, storyline_id: int) -> list: ...


class _GenServiceLike(Protocol):
    def resegment_storyline(
        self, storyline_id: int, *, override_locked: bool = ...,
    ) -> GenerationResult: ...


@dataclass
class StorylineBackfillItem:
    """Outcome for one storyline in a backfill run."""

    storyline_id: int
    name: str
    chapters_before: int
    chapters_after: int | None  # None in dry-run (nothing was rebuilt)
    resegmented: bool
    warnings: list[str] = field(default_factory=list)


@dataclass
class StorylineBackfillResult:
    items: list[StorylineBackfillItem]
    dry_run: bool


# Page size when sweeping a user's storylines; list_storylines caps at 50
# by default, so we page explicitly to cover users with many storylines.
_PAGE = 50


def backfill_storyline_chapters(
    *,
    storyline_repository: _StorylineRepoLike,
    generation_service: _GenServiceLike,
    user_id: int,
    status: str | None = None,
    storyline_id: int | None = None,
    dry_run: bool = True,
    only_single_chapter: bool = True,
    override_locked: bool = False,
) -> StorylineBackfillResult:
    """Re-section a user's storylines into chapters.

    Args:
        user_id: owner whose storylines are swept.
        status: optional storyline status filter (e.g. ``"active"``).
        storyline_id: if set, restrict to this single storyline.
        dry_run: when True (default) only report current chapter counts;
            no LLM calls, no mutation.
        only_single_chapter: when True (default) skip storylines that
            already have more than one chapter — those have been carved
            already. Set False to force a re-carve of every storyline.
        override_locked: forwarded to ``resegment_storyline`` — re-carve
            across hand-painted boundary-locked chapters.
    """
    storylines = _select_storylines(
        storyline_repository, user_id, status, storyline_id,
    )

    items: list[StorylineBackfillItem] = []
    for s in storylines:
        before = len(storyline_repository.list_chapters(s.id))
        if only_single_chapter and before > 1:
            continue
        if dry_run:
            items.append(
                StorylineBackfillItem(
                    storyline_id=s.id, name=s.name,
                    chapters_before=before, chapters_after=None,
                    resegmented=False,
                )
            )
            continue
        result = generation_service.resegment_storyline(
            s.id, override_locked=override_locked,
        )
        after = len(storyline_repository.list_chapters(s.id))
        items.append(
            StorylineBackfillItem(
                storyline_id=s.id, name=s.name,
                chapters_before=before, chapters_after=after,
                resegmented=True, warnings=list(result.warnings),
            )
        )
    return StorylineBackfillResult(items=items, dry_run=dry_run)


def _select_storylines(
    repo: _StorylineRepoLike,
    user_id: int,
    status: str | None,
    storyline_id: int | None,
) -> list:
    if storyline_id is not None:
        one = repo.get_storyline(storyline_id, user_id=user_id)
        return [one] if one is not None else []
    collected: list = []
    offset = 0
    while True:
        page = repo.list_storylines(
            user_id, status=status, limit=_PAGE, offset=offset,
        )
        collected.extend(page)
        if len(page) < _PAGE:
            break
        offset += _PAGE
    return collected
