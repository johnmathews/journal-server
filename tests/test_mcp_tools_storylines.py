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
        # The job id must be rendered literally so the caller can paste
        # the suggested follow-up call verbatim — not a placeholder.
        assert "journal_get_job_status('job-slow')" in out
        assert "(...)" not in out
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


# ── journal_create_storyline (W7: auto-kicks generation) ───────


class TestCreateStoryline:
    @staticmethod
    def _make_entity(
        entity_id: int = 59, canonical_name: str = "Running",
    ) -> MagicMock:
        entity = MagicMock()
        entity.id = entity_id
        entity.canonical_name = canonical_name
        return entity

    def test_create_success_returns_panels_generated_message(
        self, patched_user_id: int,
    ) -> None:
        """Happy path: tool creates the storyline, kicks the
        generation job, polls until succeeded, returns a message
        pointing at journal_get_storyline."""
        from journal.mcp_server import journal_create_storyline

        storyline_repo = _storyline_repo_mock()
        storyline_repo.find_by_entity.return_value = None
        created = _make_storyline(storyline_id=17, name="Running", entity_id=59)
        storyline_repo.create_storyline.return_value = created

        entity_store = MagicMock()
        entity_store.get_entity.return_value = self._make_entity(
            entity_id=59, canonical_name="Running",
        )

        job_runner = MagicMock()
        job_runner.submit_storyline_generation.return_value = _make_job(
            status="pending", job_id="job-create-7",
        )
        job_repository = MagicMock()
        job_repository.get.return_value = _make_job(
            status="succeeded",
            job_id="job-create-7",
            result={"entry_count": 5},
        )

        ctx = _make_ctx(
            storyline_repository=storyline_repo,
            entity_store=entity_store,
            job_runner=job_runner,
            job_repository=job_repository,
        )

        out = journal_create_storyline(
            entity_id=59, name="Running", ctx=ctx,
        )

        assert "Created storyline 17" in out
        assert "Panels generated" in out
        assert "journal_get_storyline(17)" in out
        storyline_repo.create_storyline.assert_called_once()
        job_runner.submit_storyline_generation.assert_called_once_with(
            17, user_id=patched_user_id,
        )

    def test_already_exists_does_not_kick_job(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        """Duplicate (user, entity, name) returns the existing-id
        message and does NOT submit any generation job."""
        from journal.mcp_server import journal_create_storyline

        storyline_repo = _storyline_repo_mock()
        existing = _make_storyline(storyline_id=42, name="Running", entity_id=59)
        storyline_repo.find_by_entity.return_value = existing

        entity_store = MagicMock()
        entity_store.get_entity.return_value = self._make_entity()

        job_runner = MagicMock()
        ctx = _make_ctx(
            storyline_repository=storyline_repo,
            entity_store=entity_store,
            job_runner=job_runner,
            job_repository=MagicMock(),
        )

        out = journal_create_storyline(
            entity_id=59, name="Running", ctx=ctx,
        )

        assert "already exists" in out.lower()
        assert "id=42" in out
        job_runner.submit_storyline_generation.assert_not_called()
        storyline_repo.create_storyline.assert_not_called()

    def test_entity_not_found_does_not_create_or_kick(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        from journal.mcp_server import journal_create_storyline

        storyline_repo = _storyline_repo_mock()
        entity_store = MagicMock()
        entity_store.get_entity.return_value = None
        job_runner = MagicMock()

        ctx = _make_ctx(
            storyline_repository=storyline_repo,
            entity_store=entity_store,
            job_runner=job_runner,
            job_repository=MagicMock(),
        )

        out = journal_create_storyline(
            entity_id=999, name="Running", ctx=ctx,
        )

        assert "Entity 999 not found for this user" in out
        assert "journal_list_entities" in out
        job_runner.submit_storyline_generation.assert_not_called()
        storyline_repo.create_storyline.assert_not_called()

    def test_not_configured_short_circuits(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        from journal.mcp_server import journal_create_storyline

        ctx = _make_ctx()  # no storyline_repository in lifespan
        out = journal_create_storyline(
            entity_id=1, name="X", ctx=ctx,
        )
        assert "not configured" in out.lower()

    def test_generation_timeout_returns_actionable_message(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        """Storyline IS created, but the generation job stays running
        past the timeout — the message tells the caller how to follow
        up via journal_get_job_status."""
        from journal.mcp_server import journal_create_storyline

        storyline_repo = _storyline_repo_mock()
        storyline_repo.find_by_entity.return_value = None
        created = _make_storyline(storyline_id=23, name="Running")
        storyline_repo.create_storyline.return_value = created

        entity_store = MagicMock()
        entity_store.get_entity.return_value = self._make_entity()

        job_runner = MagicMock()
        job_runner.submit_storyline_generation.return_value = _make_job(
            status="pending", job_id="job-slow-23",
        )
        job_repository = MagicMock()
        # Stays running indefinitely; timeout_seconds=0 exits poll
        # loop on first check.
        job_repository.get.return_value = _make_job(
            status="running", job_id="job-slow-23",
        )

        ctx = _make_ctx(
            storyline_repository=storyline_repo,
            entity_store=entity_store,
            job_runner=job_runner,
            job_repository=job_repository,
        )

        out = journal_create_storyline(
            entity_id=59, name="Running", timeout_seconds=0, ctx=ctx,
        )

        assert "Created storyline 23" in out
        assert "job-slow-23" in out
        assert "journal_get_job_status('job-slow-23')" in out
        assert "journal_get_storyline(23)" in out

    def test_generation_failure_surfaces_error_message(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        """Storyline IS created, but the worker fails — message must
        surface the error and point at the retry tool."""
        from journal.mcp_server import journal_create_storyline

        storyline_repo = _storyline_repo_mock()
        storyline_repo.find_by_entity.return_value = None
        created = _make_storyline(storyline_id=31, name="Running")
        storyline_repo.create_storyline.return_value = created

        entity_store = MagicMock()
        entity_store.get_entity.return_value = self._make_entity()

        job_runner = MagicMock()
        job_runner.submit_storyline_generation.return_value = _make_job(
            status="pending", job_id="job-fail-31",
        )
        job_repository = MagicMock()
        job_repository.get.return_value = _make_job(
            status="failed",
            job_id="job-fail-31",
            error_message="Anthropic returned 529 overloaded",
        )

        ctx = _make_ctx(
            storyline_repository=storyline_repo,
            entity_store=entity_store,
            job_runner=job_runner,
            job_repository=job_repository,
        )

        out = journal_create_storyline(
            entity_id=59, name="Running", ctx=ctx,
        )

        assert "Created storyline 31" in out
        assert "Anthropic returned 529 overloaded" in out
        assert "journal_regenerate_storyline(31)" in out

    def test_runner_runtime_error_keeps_storyline_pointers(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        """If submit_storyline_generation raises (feature disabled),
        the storyline is still created and the message points at
        journal_regenerate_storyline for a manual retry."""
        from journal.mcp_server import journal_create_storyline

        storyline_repo = _storyline_repo_mock()
        storyline_repo.find_by_entity.return_value = None
        created = _make_storyline(storyline_id=44, name="Running")
        storyline_repo.create_storyline.return_value = created

        entity_store = MagicMock()
        entity_store.get_entity.return_value = self._make_entity()

        job_runner = MagicMock()
        job_runner.submit_storyline_generation.side_effect = RuntimeError(
            "StorylineGenerationService not configured",
        )

        ctx = _make_ctx(
            storyline_repository=storyline_repo,
            entity_store=entity_store,
            job_runner=job_runner,
            job_repository=MagicMock(),
        )

        out = journal_create_storyline(
            entity_id=59, name="Running", ctx=ctx,
        )

        assert "Created storyline 44" in out
        assert "could not be queued" in out
        assert "journal_regenerate_storyline(44)" in out


# ── journal_storylines_guide ────────────────────────────────────


class TestStorylinesGuide:
    def test_returns_non_empty_guide(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        from journal.mcp_server import journal_storylines_guide

        out = journal_storylines_guide(ctx=_make_ctx())

        assert isinstance(out, str)
        assert len(out) > 200

    def test_mentions_all_other_tool_names(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        """A guide that doesn't reference its peers can't be a guide."""
        from journal.mcp_server import journal_storylines_guide

        out = journal_storylines_guide(ctx=_make_ctx())

        assert "journal_list_storylines" in out
        assert "journal_get_storyline" in out
        assert "journal_create_storyline" in out
        assert "journal_regenerate_storyline" in out
        assert "journal_delete_storyline" in out

    def test_mentions_both_panel_kinds(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        from journal.mcp_server import journal_storylines_guide

        out = journal_storylines_guide(ctx=_make_ctx())

        assert "curation" in out.lower()
        assert "narrative" in out.lower()

    def test_returns_guide_when_not_configured(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        """The guide must be discoverable even when the feature isn't
        wired — a client with no ANTHROPIC_API_KEY still needs to learn
        what storylines are and why the other tools error."""
        from journal.mcp_server import journal_storylines_guide

        # Empty lifespan — no storyline_repository, mirrors the
        # not-configured short-circuit used by the other tools.
        ctx = _make_ctx()

        out = journal_storylines_guide(ctx=ctx)

        assert isinstance(out, str)
        assert len(out) > 200
        assert "journal_list_storylines" in out
        # The guide itself should explain the configuration gating
        assert "ANTHROPIC_API_KEY" in out


# ── journal_delete_storyline ────────────────────────────────────


class TestDeleteStoryline:
    def test_successful_delete_reports_id(
        self, patched_user_id: int,
    ) -> None:
        from journal.mcp_server import journal_delete_storyline

        storyline_repo = _storyline_repo_mock()
        storyline_repo.delete_storyline.return_value = True
        ctx = _make_ctx(storyline_repository=storyline_repo)

        out = journal_delete_storyline(storyline_id=17, ctx=ctx)

        assert out == "Deleted storyline 17."
        storyline_repo.delete_storyline.assert_called_once_with(
            17, user_id=patched_user_id,
        )

    def test_not_found_returns_friendly_message(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        from journal.mcp_server import journal_delete_storyline

        storyline_repo = _storyline_repo_mock()
        storyline_repo.delete_storyline.return_value = False
        ctx = _make_ctx(storyline_repository=storyline_repo)

        out = journal_delete_storyline(storyline_id=999, ctx=ctx)

        assert out == "Storyline 999 not found for this user."

    def test_not_configured_short_circuits(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        from journal.mcp_server import journal_delete_storyline

        ctx = _make_ctx()  # no storyline_repository in lifespan

        out = journal_delete_storyline(storyline_id=1, ctx=ctx)

        assert "not configured" in out.lower()


# ── annotations sanity check ────────────────────────────────────


class TestStorylineToolAnnotations:
    """The four storylines tools advertise MCP behavior hints
    (readOnlyHint / idempotentHint / destructiveHint) so clients can
    reason about side effects. Lock the wiring in."""

    def test_annotations_match_plan(self) -> None:
        from journal.mcp_server import mcp

        tools = mcp._tool_manager._tools
        expected: dict[str, dict[str, bool]] = {
            "journal_list_storylines": {"readOnlyHint": True},
            "journal_get_storyline": {"readOnlyHint": True},
            "journal_create_storyline": {},
            "journal_regenerate_storyline": {"idempotentHint": True},
            "journal_storylines_guide": {"readOnlyHint": True},
            "journal_delete_storyline": {"destructiveHint": True},
        }

        for name, hints in expected.items():
            assert name in tools, f"{name} not registered with FastMCP"
            ann = tools[name].annotations
            for hint_name, hint_value in hints.items():
                assert ann is not None, (
                    f"{name} has no annotations but expected {hint_name}"
                )
                assert getattr(ann, hint_name) is hint_value, (
                    f"{name}.{hint_name} should be {hint_value}, got "
                    f"{getattr(ann, hint_name)}"
                )
            # Tools with no expected hints should not advertise any of
            # the three behavior hints (title-only annotations are OK).
            if not hints and ann is not None:
                assert ann.readOnlyHint is None
                assert ann.idempotentHint is None
                assert ann.destructiveHint is None
