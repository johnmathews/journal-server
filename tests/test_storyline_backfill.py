"""Tests for the storyline chapter backfill (W5).

``backfill_storyline_chapters`` re-sections existing storylines into
word-sized chapters. It exists because storylines generated before the
chapter feature (migrations 0030/0031) are stuck as a single long chapter
and there is no other bulk path to re-carve them. Dry-run is the default
so a real run — which costs one sectioning LLM call per unlocked span —
is always explicit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from journal.services.storylines.backfill import backfill_storyline_chapters
from journal.services.storylines.service import GenerationResult


@dataclass
class _FakeStoryline:
    id: int
    name: str
    user_id: int = 1
    status: str = "active"


class _FakeStorylineRepo:
    def __init__(
        self,
        storylines: list[_FakeStoryline],
        chapters_by_id: dict[int, int],
    ) -> None:
        self._storylines = storylines
        # id → current chapter count (list_chapters returns that many objs)
        self._chapters = chapters_by_id

    def list_storylines(
        self, user_id: int, status: str | None = None,
        limit: int = 50, offset: int = 0,
    ) -> list[_FakeStoryline]:
        items = [
            s for s in self._storylines
            if s.user_id == user_id and (status is None or s.status == status)
        ]
        return items[offset:offset + limit]

    def get_storyline(
        self, storyline_id: int, user_id: int | None = None,
    ) -> _FakeStoryline | None:
        for s in self._storylines:
            if s.id == storyline_id and (user_id is None or s.user_id == user_id):
                return s
        return None

    def list_chapters(self, storyline_id: int) -> list[Any]:
        return [object()] * self._chapters.get(storyline_id, 0)


class _FakeGenService:
    """Re-sectioning bumps the storyline to ``sections`` chapters."""

    def __init__(self, repo: _FakeStorylineRepo, *, sections: int = 3) -> None:
        self._repo = repo
        self._sections = sections
        self.calls: list[int] = []

    def resegment_storyline(
        self, storyline_id: int, *, override_locked: bool = False,
    ) -> GenerationResult:
        self.calls.append(storyline_id)
        self._repo._chapters[storyline_id] = self._sections  # noqa: SLF001
        return GenerationResult(storyline_id=storyline_id)


def test_dry_run_reports_without_calling_resegment() -> None:
    storylines = [_FakeStoryline(1, "Fitness"), _FakeStoryline(2, "Family")]
    repo = _FakeStorylineRepo(storylines, {1: 1, 2: 1})
    gen = _FakeGenService(repo)

    result = backfill_storyline_chapters(
        storyline_repository=repo,
        generation_service=gen,
        user_id=1,
        dry_run=True,
    )

    assert result.dry_run is True
    assert gen.calls == []  # no LLM calls in dry run
    ids = sorted(i.storyline_id for i in result.items)
    assert ids == [1, 2]
    assert all(i.chapters_before == 1 for i in result.items)
    assert all(i.chapters_after is None for i in result.items)
    assert all(i.resegmented is False for i in result.items)


def test_execute_resegments_single_chapter_storylines() -> None:
    storylines = [_FakeStoryline(1, "Fitness"), _FakeStoryline(2, "Family")]
    repo = _FakeStorylineRepo(storylines, {1: 1, 2: 1})
    gen = _FakeGenService(repo, sections=3)

    result = backfill_storyline_chapters(
        storyline_repository=repo,
        generation_service=gen,
        user_id=1,
        dry_run=False,
    )

    assert result.dry_run is False
    assert sorted(gen.calls) == [1, 2]
    for item in result.items:
        assert item.chapters_before == 1
        assert item.chapters_after == 3
        assert item.resegmented is True


def test_skips_already_multichapter_by_default() -> None:
    storylines = [_FakeStoryline(1, "Fitness"), _FakeStoryline(2, "Family")]
    repo = _FakeStorylineRepo(storylines, {1: 1, 2: 4})  # 2 already carved
    gen = _FakeGenService(repo)

    result = backfill_storyline_chapters(
        storyline_repository=repo,
        generation_service=gen,
        user_id=1,
        dry_run=False,
    )

    assert gen.calls == [1]  # only the single-chapter storyline
    assert {i.storyline_id for i in result.items} == {1}


def test_include_multichapter_when_requested() -> None:
    storylines = [_FakeStoryline(1, "Fitness"), _FakeStoryline(2, "Family")]
    repo = _FakeStorylineRepo(storylines, {1: 1, 2: 4})
    gen = _FakeGenService(repo)

    result = backfill_storyline_chapters(
        storyline_repository=repo,
        generation_service=gen,
        user_id=1,
        dry_run=False,
        only_single_chapter=False,
    )

    assert sorted(gen.calls) == [1, 2]
    assert {i.storyline_id for i in result.items} == {1, 2}


def test_target_single_storyline() -> None:
    storylines = [_FakeStoryline(1, "Fitness"), _FakeStoryline(2, "Family")]
    repo = _FakeStorylineRepo(storylines, {1: 1, 2: 1})
    gen = _FakeGenService(repo)

    result = backfill_storyline_chapters(
        storyline_repository=repo,
        generation_service=gen,
        user_id=1,
        storyline_id=2,
        dry_run=False,
    )

    assert gen.calls == [2]
    assert {i.storyline_id for i in result.items} == {2}
