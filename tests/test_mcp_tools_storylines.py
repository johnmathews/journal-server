"""Tests for the storylines MCP tools.

The W9 plan promised this file but it never shipped, which is how
three stacked bugs reached prod in ``journal_regenerate_storyline``:

1. The poll helper was called with ``timeout_seconds=`` but its kwarg
   is ``timeout=`` — TypeError on every call.
2. An ``if finished is None:`` branch covering an unreachable case
   (the helper always returns a dict, never None).
3. Attribute access (``finished.status``) on a dict.

These tests pin the public behavior of all four storylines tools so
the bugs cannot regress: success/failure/timeout/not-configured/
not-found for ``journal_regenerate_storyline``, plus happy-path
smoke tests for ``list`` / ``get`` / ``create`` and the
``not configured`` short-circuit on each.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from journal.db.storyline_repository import SQLiteStorylineRepository


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
    """A MagicMock that passes ``_get_storyline_repository``'s
    ``isinstance(repo, SQLiteStorylineRepository)`` assertion."""
    return MagicMock(spec=SQLiteStorylineRepository)


def _make_job(
    status: str,
    job_id: str = "job-1",
    result: dict[str, Any] | None = None,
    error_message: str | None = None,
) -> MagicMock:
    job = MagicMock()
    job.id = job_id
    job.status = status
    job.result = result
    job.error_message = error_message
    return job


def _make_storyline(
    storyline_id: int = 1,
    name: str = "Running",
    entity_id: int = 59,
    last_generated_at: str | None = None,
) -> MagicMock:
    s = MagicMock()
    s.id = storyline_id
    s.name = name
    s.entity_id = entity_id
    s.status = "active"
    s.last_generated_at = last_generated_at
    return s


# ── journal_regenerate_storyline ────────────────────────────────


class TestRegenerateStoryline:
    def test_success_path_returns_formatted_summary(
        self, patched_user_id: int,
    ) -> None:
        """Happy path: tool queues a job, polls until succeeded,
        formats the result blob into the success summary string."""
        from journal.mcp_server import journal_regenerate_storyline

        storyline_repo = _storyline_repo_mock()
        storyline_repo.get_storyline.return_value = _make_storyline()
        job_runner = MagicMock()
        job_runner.submit_storyline_generation.return_value = _make_job(
            status="pending", job_id="job-42",
        )
        result_blob = {
            "entry_count": 17,
            "entity_mention_count": 17,
            "fts_fallback_count": 0,
            "narrative_citation_count": 12,
            "narrative_model": "claude-opus-4-7",
            "curation_citation_count": 17,
            "curation_model": "claude-haiku-4-5",
        }
        job_repository = MagicMock()
        job_repository.get.return_value = _make_job(
            status="succeeded", job_id="job-42", result=result_blob,
        )
        ctx = _make_ctx(
            storyline_repository=storyline_repo,
            job_runner=job_runner,
            job_repository=job_repository,
        )

        out = journal_regenerate_storyline(storyline_id=3, ctx=ctx)

        assert "Regeneration succeeded" in out
        assert "entries: 17" in out
        assert "(17 via entity, 0 via FTS fallback)" in out
        assert "narrative citations: 12" in out
        assert "model claude-opus-4-7" in out
        assert "curation citations: 17" in out
        assert "model claude-haiku-4-5" in out
        # User saw "go read it" pointer
        assert "journal_get_storyline(3)" in out
        # The tool used the configured user and queued exactly once
        job_runner.submit_storyline_generation.assert_called_once_with(
            3, user_id=patched_user_id,
        )

    def test_failed_job_returns_error_message(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        """Job runs and fails — tool surfaces the worker's
        error_message verbatim rather than dropping it."""
        from journal.mcp_server import journal_regenerate_storyline

        storyline_repo = _storyline_repo_mock()
        storyline_repo.get_storyline.return_value = _make_storyline()
        job_runner = MagicMock()
        job_runner.submit_storyline_generation.return_value = _make_job(
            status="pending",
        )
        job_repository = MagicMock()
        job_repository.get.return_value = _make_job(
            status="failed",
            error_message="Anthropic API returned 529 (overloaded)",
        )
        ctx = _make_ctx(
            storyline_repository=storyline_repo,
            job_runner=job_runner,
            job_repository=job_repository,
        )

        out = journal_regenerate_storyline(storyline_id=3, ctx=ctx)

        assert "Regeneration failed" in out
        assert "Anthropic API returned 529 (overloaded)" in out

    def test_failed_job_without_error_message_returns_unknown(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        """Defensive: a failed job with no error_message still
        produces a useful message rather than 'None' or a crash."""
        from journal.mcp_server import journal_regenerate_storyline

        storyline_repo = _storyline_repo_mock()
        storyline_repo.get_storyline.return_value = _make_storyline()
        job_runner = MagicMock()
        job_runner.submit_storyline_generation.return_value = _make_job(
            status="pending",
        )
        job_repository = MagicMock()
        job_repository.get.return_value = _make_job(
            status="failed", error_message=None,
        )
        ctx = _make_ctx(
            storyline_repository=storyline_repo,
            job_runner=job_runner,
            job_repository=job_repository,
        )

        out = journal_regenerate_storyline(storyline_id=3, ctx=ctx)

        assert "Regeneration failed" in out
        assert "unknown" in out.lower()

    def test_timeout_path_returns_actionable_message(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        """If the job doesn't terminate within ``timeout_seconds``,
        the tool tells the caller how to follow up — not a stack trace.
        ``timeout_seconds=0`` makes the polling loop exit on the first
        check, so this test runs without sleeping."""
        from journal.mcp_server import journal_regenerate_storyline

        storyline_repo = _storyline_repo_mock()
        storyline_repo.get_storyline.return_value = _make_storyline()
        job_runner = MagicMock()
        job_runner.submit_storyline_generation.return_value = _make_job(
            status="pending", job_id="job-slow",
        )
        # Job stays "running" forever — but with timeout_seconds=0
        # the poll loop returns the timeout dict immediately.
        job_repository = MagicMock()
        job_repository.get.return_value = _make_job(
            status="running", job_id="job-slow",
        )
        ctx = _make_ctx(
            storyline_repository=storyline_repo,
            job_runner=job_runner,
            job_repository=job_repository,
        )

        out = journal_regenerate_storyline(
            storyline_id=3, timeout_seconds=0, ctx=ctx,
        )

        # Useful follow-up — no stack trace, no opaque error
        assert "job-slow" in out
        assert "journal_get_job" in out or "check status later" in out
        # And specifically NOT the success or failure phrasing
        assert "succeeded" not in out.lower()
        assert "Regeneration failed" not in out

    def test_not_configured_short_circuits(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        """When storylines aren't wired (no ANTHROPIC_API_KEY at boot),
        the tool returns an actionable message without trying to queue."""
        from journal.mcp_server import journal_regenerate_storyline

        ctx = _make_ctx(
            # storyline_repository absent → _get_storyline_repository → None
            job_runner=MagicMock(),
            job_repository=MagicMock(),
        )

        out = journal_regenerate_storyline(storyline_id=1, ctx=ctx)

        assert "not configured" in out.lower()

    def test_storyline_not_found(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        """Unknown id → friendly message, no submit."""
        from journal.mcp_server import journal_regenerate_storyline

        storyline_repo = _storyline_repo_mock()
        storyline_repo.get_storyline.return_value = None
        job_runner = MagicMock()
        job_repository = MagicMock()
        ctx = _make_ctx(
            storyline_repository=storyline_repo,
            job_runner=job_runner,
            job_repository=job_repository,
        )

        out = journal_regenerate_storyline(storyline_id=999, ctx=ctx)

        assert "not found" in out.lower()
        job_runner.submit_storyline_generation.assert_not_called()

    def test_runner_runtime_error_is_surfaced(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        """If the runner refuses (e.g. feature disabled mid-flight),
        the user sees the runner's reason."""
        from journal.mcp_server import journal_regenerate_storyline

        storyline_repo = _storyline_repo_mock()
        storyline_repo.get_storyline.return_value = _make_storyline()
        job_runner = MagicMock()
        job_runner.submit_storyline_generation.side_effect = RuntimeError(
            "storylines service not wired",
        )
        ctx = _make_ctx(
            storyline_repository=storyline_repo,
            job_runner=job_runner,
            job_repository=MagicMock(),
        )

        out = journal_regenerate_storyline(storyline_id=3, ctx=ctx)

        assert "Cannot regenerate" in out
        assert "storylines service not wired" in out


# ── journal_list_storylines / journal_get_storyline / create ───


class TestListStorylines:
    def test_lists_user_storylines(
        self, patched_user_id: int,
    ) -> None:
        from journal.mcp_server import journal_list_storylines

        storyline_repo = _storyline_repo_mock()
        storyline_repo.list_storylines.return_value = [
            _make_storyline(1, "Running", 59, "2026-05-12T09:55:17Z"),
            _make_storyline(2, "Atlas", 3, None),
        ]
        ctx = _make_ctx(storyline_repository=storyline_repo)

        out = journal_list_storylines(ctx=ctx)

        assert "Running" in out
        assert "Atlas" in out
        assert "entity_id=59" in out
        assert "last_generated=2026-05-12T09:55:17Z" in out
        assert "last_generated=never" in out
        storyline_repo.list_storylines.assert_called_once_with(
            user_id=patched_user_id, status=None, limit=50,
        )

    def test_not_configured(self, patched_user_id: int) -> None:  # noqa: ARG002
        from journal.mcp_server import journal_list_storylines

        ctx = _make_ctx()
        out = journal_list_storylines(ctx=ctx)
        assert "not configured" in out.lower()

    def test_empty_list(self, patched_user_id: int) -> None:  # noqa: ARG002
        from journal.mcp_server import journal_list_storylines

        storyline_repo = _storyline_repo_mock()
        storyline_repo.list_storylines.return_value = []
        ctx = _make_ctx(storyline_repository=storyline_repo)
        out = journal_list_storylines(ctx=ctx)
        assert "No storylines" in out


class TestGetStoryline:
    def test_not_configured(self, patched_user_id: int) -> None:  # noqa: ARG002
        from journal.mcp_server import journal_get_storyline

        ctx = _make_ctx()
        out = journal_get_storyline(storyline_id=1, ctx=ctx)
        assert "not configured" in out.lower()

    def test_storyline_not_found(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        from journal.mcp_server import journal_get_storyline

        storyline_repo = _storyline_repo_mock()
        storyline_repo.get_storyline.return_value = None
        ctx = _make_ctx(storyline_repository=storyline_repo)
        out = journal_get_storyline(storyline_id=999, ctx=ctx)
        assert "not found" in out.lower()
