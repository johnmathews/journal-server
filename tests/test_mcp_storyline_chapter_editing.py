"""Tests for the storyline chapter editing MCP tools.

Five new tools:
  * journal_add_storyline_chapter
  * journal_split_storyline_chapter
  * journal_merge_storyline_chapters
  * journal_update_storyline_chapter
  * journal_delete_storyline_chapter

All tests follow the harness pattern from test_mcp_tools_storylines.py:
  - ctx is a MagicMock with ctx.request_context.lifespan_context as a dict
  - storyline repo is a MagicMock(spec=SQLiteStorylineRepository)
  - user_id is monkeypatched via patched_user_id fixture
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from journal.db.storyline_repository import SQLiteStorylineRepository

# ── shared helpers (mirror test_mcp_tools_storylines.py) ──────────


@pytest.fixture
def patched_user_id(monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setattr(
        "journal.mcp_server.tools._ctx.get_current_user_id",
        lambda: 1,
    )
    return 1


def _make_ctx(**lifespan: Any) -> MagicMock:
    ctx = MagicMock()
    ctx.request_context.lifespan_context = dict(lifespan)
    return ctx


def _storyline_repo_mock() -> MagicMock:
    """MagicMock that passes _get_storyline_repository's isinstance check."""
    return MagicMock(spec=SQLiteStorylineRepository)


def _make_storyline(
    storyline_id: int = 1,
    name: str = "Running",
) -> MagicMock:
    s = MagicMock()
    s.id = storyline_id
    s.name = name
    s.status = "active"
    s.last_generated_at = None
    return s


def _make_chapter(
    chapter_id: int = 10,
    storyline_id: int = 1,
    seq: int = 1,
    start_date: str | None = "2026-01-01",
    end_date: str | None = None,
    state: str = "open",
    title: str = "Chapter One",
) -> MagicMock:
    ch = MagicMock()
    ch.id = chapter_id
    ch.storyline_id = storyline_id
    ch.seq = seq
    ch.start_date = start_date
    ch.end_date = end_date
    ch.state = state
    ch.title = title
    return ch


def _make_job(
    status: str = "pending",
    job_id: str = "job-1",
) -> MagicMock:
    job = MagicMock()
    job.id = job_id
    job.status = status
    return job


# ── journal_add_storyline_chapter ─────────────────────────────────


class TestAddStorylineChapter:
    def test_add_chapter_happy(self, patched_user_id: int) -> None:
        """Add a chapter; repo.add_chapter is called and success string returned."""
        from journal.mcp_server import journal_add_storyline_chapter

        repo = _storyline_repo_mock()
        repo.get_storyline.return_value = _make_storyline(storyline_id=1)
        new_ch = _make_chapter(chapter_id=20, storyline_id=1, seq=2,
                               start_date="2026-06-01", end_date=None, state="open")
        repo.add_chapter.return_value = new_ch

        runner = MagicMock()
        runner.submit_storyline_generation.return_value = _make_job()
        job_repo = MagicMock()
        job_repo.get.return_value = _make_job(status="succeeded")

        ctx = _make_ctx(
            storyline_repository=repo,
            job_runner=runner,
            job_repository=job_repo,
        )

        out = journal_add_storyline_chapter(
            storyline_id=1, start_date="2026-06-01", ctx=ctx,
        )

        repo.add_chapter.assert_called_once_with(1, "2026-06-01", None)
        assert "not found" not in out.lower()
        assert "Could not" not in out
        assert "not configured" not in out.lower()

    def test_add_chapter_not_found(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        """Storyline not found returns the expected 'not found' string."""
        from journal.mcp_server import journal_add_storyline_chapter

        repo = _storyline_repo_mock()
        repo.get_storyline.return_value = None
        ctx = _make_ctx(storyline_repository=repo)

        out = journal_add_storyline_chapter(
            storyline_id=999, start_date="2026-06-01", ctx=ctx,
        )

        assert "not found" in out.lower()
        repo.add_chapter.assert_not_called()

    def test_add_chapter_not_configured(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        from journal.mcp_server import journal_add_storyline_chapter

        out = journal_add_storyline_chapter(
            storyline_id=1, start_date="2026-06-01", ctx=_make_ctx(),
        )
        assert "not configured" in out.lower()

    def test_add_chapter_value_error_returns_could_not(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        """repo.add_chapter raises ValueError → 'Could not' returned."""
        from journal.mcp_server import journal_add_storyline_chapter

        repo = _storyline_repo_mock()
        repo.get_storyline.return_value = _make_storyline(storyline_id=1)
        repo.add_chapter.side_effect = ValueError("overlaps an existing chapter")

        ctx = _make_ctx(storyline_repository=repo, job_runner=MagicMock(),
                        job_repository=MagicMock())

        out = journal_add_storyline_chapter(
            storyline_id=1, start_date="2026-01-01", ctx=ctx,
        )

        assert "Could not" in out
        assert "overlaps" in out


# ── journal_split_storyline_chapter ───────────────────────────────


class TestSplitStorylineChapter:
    def test_split_chapter_happy(self, patched_user_id: int) -> None:
        """Happy path: split returns info about both halves."""
        from journal.mcp_server import journal_split_storyline_chapter

        repo = _storyline_repo_mock()
        repo.get_storyline.return_value = _make_storyline(storyline_id=1)
        ch = _make_chapter(chapter_id=10, storyline_id=1, seq=1,
                           start_date="2026-01-01", end_date=None, state="open")
        repo.get_chapter.return_value = ch

        left = _make_chapter(chapter_id=10, storyline_id=1, seq=1,
                             start_date="2026-01-01", end_date="2026-03-31",
                             state="closed")
        right = _make_chapter(chapter_id=11, storyline_id=1, seq=2,
                              start_date="2026-04-01", end_date=None,
                              state="open")
        repo.split_chapter.return_value = (left, right)

        runner = MagicMock()
        runner.submit_storyline_generation.return_value = _make_job()
        job_repo = MagicMock()
        job_repo.get.return_value = _make_job(status="succeeded")

        ctx = _make_ctx(
            storyline_repository=repo,
            job_runner=runner,
            job_repository=job_repo,
        )

        out = journal_split_storyline_chapter(
            storyline_id=1, chapter_id=10, date="2026-04-01", ctx=ctx,
        )

        repo.split_chapter.assert_called_once_with(10, "2026-04-01")
        assert "not found" not in out.lower()
        assert "Could not" not in out
        # Should mention both chapter ids or seqs
        assert "10" in out or "seq 1" in out

    def test_split_chapter_bad_date(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        """Bad split date (before chapter start) → 'Could not ...' string."""
        from journal.mcp_server import journal_split_storyline_chapter

        repo = _storyline_repo_mock()
        repo.get_storyline.return_value = _make_storyline(storyline_id=1)
        ch = _make_chapter(chapter_id=10, storyline_id=1, seq=1,
                           start_date="2026-01-01", end_date=None, state="open")
        repo.get_chapter.return_value = ch
        repo.split_chapter.side_effect = ValueError(
            "split date must be after the chapter start",
        )

        ctx = _make_ctx(storyline_repository=repo, job_runner=MagicMock(),
                        job_repository=MagicMock())

        out = journal_split_storyline_chapter(
            storyline_id=1, chapter_id=10, date="2025-12-01", ctx=ctx,
        )

        assert "Could not" in out

    def test_split_chapter_not_found(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        """Chapter not belonging to storyline → 'not found'."""
        from journal.mcp_server import journal_split_storyline_chapter

        repo = _storyline_repo_mock()
        repo.get_storyline.return_value = _make_storyline(storyline_id=1)
        # chapter belongs to a different storyline
        other_ch = _make_chapter(chapter_id=10, storyline_id=99)
        repo.get_chapter.return_value = other_ch

        ctx = _make_ctx(storyline_repository=repo)

        out = journal_split_storyline_chapter(
            storyline_id=1, chapter_id=10, date="2026-04-01", ctx=ctx,
        )

        assert "not found" in out.lower()
        repo.split_chapter.assert_not_called()

    def test_split_storyline_not_found(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        from journal.mcp_server import journal_split_storyline_chapter

        repo = _storyline_repo_mock()
        repo.get_storyline.return_value = None
        ctx = _make_ctx(storyline_repository=repo)

        out = journal_split_storyline_chapter(
            storyline_id=999, chapter_id=10, date="2026-04-01", ctx=ctx,
        )

        assert "not found" in out.lower()


# ── journal_merge_storyline_chapters ──────────────────────────────


class TestMergeStorylineChapters:
    def test_merge_chapters_happy(self, patched_user_id: int) -> None:
        """Merge two adjacent chapters; success string returned."""
        from journal.mcp_server import journal_merge_storyline_chapters

        repo = _storyline_repo_mock()
        repo.get_storyline.return_value = _make_storyline(storyline_id=1)

        ch_a = _make_chapter(chapter_id=10, storyline_id=1, seq=1,
                             start_date="2026-01-01", end_date="2026-03-31",
                             state="closed")
        ch_b = _make_chapter(chapter_id=11, storyline_id=1, seq=2,
                             start_date="2026-04-01", end_date=None,
                             state="open")

        def get_chapter_side(chapter_id: int) -> MagicMock | None:
            return {10: ch_a, 11: ch_b}.get(chapter_id)

        repo.get_chapter.side_effect = get_chapter_side
        merged = _make_chapter(chapter_id=10, storyline_id=1, seq=1,
                               start_date="2026-01-01", end_date=None,
                               state="open")
        repo.merge_chapters.return_value = merged

        runner = MagicMock()
        runner.submit_storyline_generation.return_value = _make_job()
        job_repo = MagicMock()
        job_repo.get.return_value = _make_job(status="succeeded")

        ctx = _make_ctx(
            storyline_repository=repo,
            job_runner=runner,
            job_repository=job_repo,
        )

        out = journal_merge_storyline_chapters(
            storyline_id=1, chapter_ids=[10, 11], ctx=ctx,
        )

        repo.merge_chapters.assert_called_once_with([10, 11])
        assert "Could not" not in out
        assert "not found" not in out.lower()

    def test_merge_chapters_chapter_not_in_storyline(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        """A chapter_id belonging to a different storyline → error."""
        from journal.mcp_server import journal_merge_storyline_chapters

        repo = _storyline_repo_mock()
        repo.get_storyline.return_value = _make_storyline(storyline_id=1)

        ch_a = _make_chapter(chapter_id=10, storyline_id=1)
        ch_bad = _make_chapter(chapter_id=11, storyline_id=99)  # wrong storyline

        def get_chapter_side(chapter_id: int) -> MagicMock | None:
            return {10: ch_a, 11: ch_bad}.get(chapter_id)

        repo.get_chapter.side_effect = get_chapter_side

        ctx = _make_ctx(storyline_repository=repo)

        out = journal_merge_storyline_chapters(
            storyline_id=1, chapter_ids=[10, 11], ctx=ctx,
        )

        assert "not found" in out.lower()
        repo.merge_chapters.assert_not_called()

    def test_merge_storyline_not_found(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        from journal.mcp_server import journal_merge_storyline_chapters

        repo = _storyline_repo_mock()
        repo.get_storyline.return_value = None
        ctx = _make_ctx(storyline_repository=repo)

        out = journal_merge_storyline_chapters(
            storyline_id=999, chapter_ids=[10, 11], ctx=ctx,
        )

        assert "not found" in out.lower()

    def test_merge_value_error_returned(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        """Non-adjacent chapters → ValueError → 'Could not' string."""
        from journal.mcp_server import journal_merge_storyline_chapters

        repo = _storyline_repo_mock()
        repo.get_storyline.return_value = _make_storyline(storyline_id=1)

        ch_a = _make_chapter(chapter_id=10, storyline_id=1, seq=1)
        ch_b = _make_chapter(chapter_id=12, storyline_id=1, seq=3)

        def get_chapter_side(chapter_id: int) -> MagicMock | None:
            return {10: ch_a, 12: ch_b}.get(chapter_id)

        repo.get_chapter.side_effect = get_chapter_side
        repo.merge_chapters.side_effect = ValueError(
            "chapters to merge must be adjacent (contiguous seq)",
        )

        ctx = _make_ctx(storyline_repository=repo, job_runner=MagicMock(),
                        job_repository=MagicMock())

        out = journal_merge_storyline_chapters(
            storyline_id=1, chapter_ids=[10, 12], ctx=ctx,
        )

        assert "Could not" in out


# ── journal_update_storyline_chapter ──────────────────────────────


class TestUpdateStorylineChapter:
    def test_update_chapter_rename_only(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        """Only title given → repo.rename_chapter called, not update_chapter_window."""
        from journal.mcp_server import journal_update_storyline_chapter

        repo = _storyline_repo_mock()
        repo.get_storyline.return_value = _make_storyline(storyline_id=1)
        ch = _make_chapter(chapter_id=10, storyline_id=1)
        repo.get_chapter.return_value = ch
        renamed = _make_chapter(chapter_id=10, storyline_id=1, title="New Title")
        repo.rename_chapter.return_value = renamed

        ctx = _make_ctx(storyline_repository=repo, job_runner=MagicMock(),
                        job_repository=MagicMock())

        out = journal_update_storyline_chapter(
            storyline_id=1, chapter_id=10, title="New Title", ctx=ctx,
        )

        repo.rename_chapter.assert_called_once_with(10, "New Title")
        repo.update_chapter_window.assert_not_called()
        assert "Could not" not in out
        assert "not found" not in out.lower()

    def test_update_chapter_window(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        """start_date given → repo.update_chapter_window called."""
        from journal.mcp_server import journal_update_storyline_chapter

        repo = _storyline_repo_mock()
        repo.get_storyline.return_value = _make_storyline(storyline_id=1)
        ch = _make_chapter(chapter_id=10, storyline_id=1)
        repo.get_chapter.return_value = ch
        updated = _make_chapter(chapter_id=10, storyline_id=1,
                                start_date="2026-03-01")
        repo.update_chapter_window.return_value = [updated]

        runner = MagicMock()
        runner.submit_storyline_generation.return_value = _make_job()
        job_repo = MagicMock()
        job_repo.get.return_value = _make_job(status="succeeded")

        ctx = _make_ctx(storyline_repository=repo, job_runner=runner,
                        job_repository=job_repo)

        out = journal_update_storyline_chapter(
            storyline_id=1, chapter_id=10, start_date="2026-03-01", ctx=ctx,
        )

        repo.update_chapter_window.assert_called_once_with(
            10, "2026-03-01", None, allow_gap=False,
        )
        repo.rename_chapter.assert_not_called()
        assert "Could not" not in out
        assert "not found" not in out.lower()

    def test_update_chapter_not_found(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        from journal.mcp_server import journal_update_storyline_chapter

        repo = _storyline_repo_mock()
        repo.get_storyline.return_value = _make_storyline(storyline_id=1)
        repo.get_chapter.return_value = None

        ctx = _make_ctx(storyline_repository=repo)

        out = journal_update_storyline_chapter(
            storyline_id=1, chapter_id=999, title="X", ctx=ctx,
        )

        assert "not found" in out.lower()

    def test_update_chapter_storyline_not_found(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        from journal.mcp_server import journal_update_storyline_chapter

        repo = _storyline_repo_mock()
        repo.get_storyline.return_value = None
        ctx = _make_ctx(storyline_repository=repo)

        out = journal_update_storyline_chapter(
            storyline_id=999, chapter_id=10, title="X", ctx=ctx,
        )

        assert "not found" in out.lower()

    def test_update_chapter_not_configured(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        from journal.mcp_server import journal_update_storyline_chapter

        out = journal_update_storyline_chapter(
            storyline_id=1, chapter_id=10, title="X", ctx=_make_ctx(),
        )
        assert "not configured" in out.lower()

    def test_update_chapter_value_error(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        """ValueError from update_chapter_window → 'Could not' string."""
        from journal.mcp_server import journal_update_storyline_chapter

        repo = _storyline_repo_mock()
        repo.get_storyline.return_value = _make_storyline(storyline_id=1)
        ch = _make_chapter(chapter_id=10, storyline_id=1)
        repo.get_chapter.return_value = ch
        repo.update_chapter_window.side_effect = ValueError(
            "the open chapter's end cannot be set",
        )

        ctx = _make_ctx(storyline_repository=repo, job_runner=MagicMock(),
                        job_repository=MagicMock())

        out = journal_update_storyline_chapter(
            storyline_id=1, chapter_id=10, end_date="2026-12-31", ctx=ctx,
        )

        assert "Could not" in out


# ── journal_delete_storyline_chapter ──────────────────────────────


class TestDeleteStorylineChapter:
    def test_delete_chapter_happy(self, patched_user_id: int) -> None:
        """Delete a chapter; repo.delete_chapter is called."""
        from journal.mcp_server import journal_delete_storyline_chapter

        repo = _storyline_repo_mock()
        repo.get_storyline.return_value = _make_storyline(storyline_id=1)
        ch = _make_chapter(chapter_id=10, storyline_id=1, state="closed",
                           end_date="2026-03-31")
        repo.get_chapter.return_value = ch
        repo.delete_chapter.return_value = [9]  # affected neighbor id

        runner = MagicMock()
        runner.submit_storyline_generation.return_value = _make_job()
        job_repo = MagicMock()
        job_repo.get.return_value = _make_job(status="succeeded")

        ctx = _make_ctx(
            storyline_repository=repo,
            job_runner=runner,
            job_repository=job_repo,
        )

        out = journal_delete_storyline_chapter(
            storyline_id=1, chapter_id=10, ctx=ctx,
        )

        repo.delete_chapter.assert_called_once_with(10, allow_gap=False)
        assert "Could not" not in out
        assert "not found" not in out.lower()

    def test_delete_chapter_not_found(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        """Chapter belongs to a different storyline → 'not found'."""
        from journal.mcp_server import journal_delete_storyline_chapter

        repo = _storyline_repo_mock()
        repo.get_storyline.return_value = _make_storyline(storyline_id=1)
        other_ch = _make_chapter(chapter_id=10, storyline_id=99)
        repo.get_chapter.return_value = other_ch

        ctx = _make_ctx(storyline_repository=repo)

        out = journal_delete_storyline_chapter(
            storyline_id=1, chapter_id=10, ctx=ctx,
        )

        assert "not found" in out.lower()
        repo.delete_chapter.assert_not_called()

    def test_delete_chapter_none(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        """get_chapter returns None → 'not found'."""
        from journal.mcp_server import journal_delete_storyline_chapter

        repo = _storyline_repo_mock()
        repo.get_storyline.return_value = _make_storyline(storyline_id=1)
        repo.get_chapter.return_value = None

        ctx = _make_ctx(storyline_repository=repo)

        out = journal_delete_storyline_chapter(
            storyline_id=1, chapter_id=10, ctx=ctx,
        )

        assert "not found" in out.lower()
        repo.delete_chapter.assert_not_called()

    def test_delete_chapter_storyline_not_found(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        from journal.mcp_server import journal_delete_storyline_chapter

        repo = _storyline_repo_mock()
        repo.get_storyline.return_value = None
        ctx = _make_ctx(storyline_repository=repo)

        out = journal_delete_storyline_chapter(
            storyline_id=999, chapter_id=10, ctx=ctx,
        )

        assert "not found" in out.lower()

    def test_delete_chapter_not_configured(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        from journal.mcp_server import journal_delete_storyline_chapter

        out = journal_delete_storyline_chapter(
            storyline_id=1, chapter_id=10, ctx=_make_ctx(),
        )
        assert "not configured" in out.lower()

    def test_delete_chapter_value_error(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        """Only chapter → ValueError → 'Could not' string."""
        from journal.mcp_server import journal_delete_storyline_chapter

        repo = _storyline_repo_mock()
        repo.get_storyline.return_value = _make_storyline(storyline_id=1)
        ch = _make_chapter(chapter_id=10, storyline_id=1)
        repo.get_chapter.return_value = ch
        repo.delete_chapter.side_effect = ValueError(
            "cannot delete a storyline's only chapter",
        )

        ctx = _make_ctx(storyline_repository=repo, job_runner=MagicMock(),
                        job_repository=MagicMock())

        out = journal_delete_storyline_chapter(
            storyline_id=1, chapter_id=10, ctx=ctx,
        )

        assert "Could not" in out

    def test_delete_chapter_allow_gap(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        """allow_gap=True is forwarded to repo.delete_chapter."""
        from journal.mcp_server import journal_delete_storyline_chapter

        repo = _storyline_repo_mock()
        repo.get_storyline.return_value = _make_storyline(storyline_id=1)
        ch = _make_chapter(chapter_id=10, storyline_id=1, state="closed",
                           end_date="2026-03-31")
        repo.get_chapter.return_value = ch
        repo.delete_chapter.return_value = []

        ctx = _make_ctx(storyline_repository=repo, job_runner=MagicMock(),
                        job_repository=MagicMock())

        journal_delete_storyline_chapter(
            storyline_id=1, chapter_id=10, allow_gap=True, ctx=ctx,
        )

        repo.delete_chapter.assert_called_once_with(10, allow_gap=True)


# ── tool annotations sanity check ─────────────────────────────────


class TestChapterToolAnnotations:
    """Ensure the five chapter editing tools are registered with correct hints."""

    def test_annotations_match_plan(self) -> None:
        from journal.mcp_server import mcp

        tools = mcp._tool_manager._tools
        expected: dict[str, dict[str, bool]] = {
            "journal_add_storyline_chapter": {},
            "journal_split_storyline_chapter": {},
            "journal_merge_storyline_chapters": {},
            "journal_update_storyline_chapter": {},
            "journal_delete_storyline_chapter": {"destructiveHint": True},
        }
        for name, hints in expected.items():
            assert name in tools, f"{name} not registered with FastMCP"
            ann = tools[name].annotations
            for hint_name, hint_value in hints.items():
                assert ann is not None, (
                    f"{name} has no annotations but expected {hint_name}"
                )
                assert getattr(ann, hint_name) is hint_value, (
                    f"{name}.{hint_name} should be {hint_value}"
                )
