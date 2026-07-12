"""Tests for the storylines MCP tools (draft/published chapter model).

The storylines-redesign (docs/superpowers/specs/
2026-07-12-storylines-redesign-design.md) replaced the old
open/closed-chapter + two-panel model with draft/published chapters
and deleted the manual chapter-editing tools (add/split/merge/window-
update/delete-chapter) along with ``journal_regenerate_storyline``
(replaced by ``journal_refresh_storyline`` / bootstrap-on-create /
``journal_unpublish_storyline_chapter``). These tests pin the new
tool surface and specifically guard against the old
``journal_get_storyline`` bug where ``list_panels(storyline.id)`` was
called with a storyline id where a chapter id was expected — that
whole panels concept is gone here by construction.
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
    status: str = "active",
) -> MagicMock:
    s = MagicMock()
    s.id = storyline_id
    s.name = name
    s.status = status
    return s


def _make_chapter(
    chapter_id: int = 31,
    seq: int = 1,
    title: str = "Early days",
    state: str = "draft",
    storyline_id: int = 1,
    read_at: str | None = None,
    entry_count: int = 3,
    first_entry_date: str | None = "2026-01-01",
    last_entry_date: str | None = "2026-01-10",
    segments: list[dict[str, Any]] | None = None,
    addenda: list[dict[str, Any]] | None = None,
    citation_count: int = 2,
    model_used: str = "claude-opus-4-7",
    generated_at: str | None = "2026-01-11T00:00:00Z",
) -> MagicMock:
    ch = MagicMock()
    ch.id = chapter_id
    ch.seq = seq
    ch.title = title
    ch.state = state
    ch.storyline_id = storyline_id
    ch.read_at = read_at
    ch.entry_count = entry_count
    ch.first_entry_date = first_entry_date
    ch.last_entry_date = last_entry_date
    ch.segments = segments if segments is not None else []
    ch.addenda = addenda if addenda is not None else []
    ch.citation_count = citation_count
    ch.model_used = model_used
    ch.generated_at = generated_at
    return ch


# ── journal_list_storylines ─────────────────────────────────────


class TestListStorylines:
    def test_lists_user_storylines_with_unread_and_chapter_counts(
        self, patched_user_id: int,
    ) -> None:
        from journal.mcp_server import journal_list_storylines

        storyline_repo = _storyline_repo_mock()
        storyline_repo.list_storylines.return_value = [
            _make_storyline(1, "Running"),
            _make_storyline(2, "Atlas"),
        ]
        storyline_repo.list_anchors.side_effect = lambda sid: (
            [59] if sid == 1 else [3]
        )
        storyline_repo.unread_counts.return_value = {1: 2, 2: 0}
        storyline_repo.chapter_counts.return_value = {1: 5, 2: 1}
        entity_store = MagicMock()
        entity_store.get_entity.side_effect = (
            lambda eid: MagicMock(canonical_name=f"E{eid}")
        )
        ctx = _make_ctx(
            storyline_repository=storyline_repo,
            entity_store=entity_store,
        )

        out = journal_list_storylines(ctx=ctx)

        assert "Running" in out
        assert "Atlas" in out
        assert "id=59" in out
        assert "id=3" in out
        assert "unread=2/5 chapters" in out
        assert "unread=0/1 chapters" in out
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


# ── journal_get_storyline ───────────────────────────────────────


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

    def test_get_storyline_renders_chapter_meta_with_state_and_read_at(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        """This is the regression guard for the old panel bug: the
        tool must render chapter metadata (including state and
        read_at) without ever calling ``list_panels`` at all."""
        from journal.mcp_server import journal_get_storyline

        storyline_repo = _storyline_repo_mock()
        storyline_repo.get_storyline.return_value = _make_storyline(
            storyline_id=7, name="Running",
        )
        storyline_repo.list_anchors.return_value = []
        published = _make_chapter(
            chapter_id=30, seq=1, title="Early days", state="published",
            storyline_id=7, read_at="2026-02-01T00:00:00Z",
        )
        draft = _make_chapter(
            chapter_id=31, seq=2, title="", state="draft",
            storyline_id=7, read_at=None,
        )
        storyline_repo.list_chapters.return_value = [published, draft]
        entity_store = MagicMock()
        ctx = _make_ctx(
            storyline_repository=storyline_repo,
            entity_store=entity_store,
        )

        out = journal_get_storyline(storyline_id=7, ctx=ctx)

        assert "chapters" in out
        assert "[30]" in out
        assert "Early days" in out
        assert "state=published" in out
        assert "read_at=2026-02-01T00:00:00Z" in out
        assert "[31]" in out
        assert "state=draft" in out
        assert "read_at=None" in out
        # No panel concept survives the redesign.
        assert not hasattr(storyline_repo, "list_panels") or (
            not storyline_repo.list_panels.called
        )
        assert "panel" not in out.lower()

    def test_no_chapters_yet_returns_actionable_message(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        from journal.mcp_server import journal_get_storyline

        storyline_repo = _storyline_repo_mock()
        storyline_repo.get_storyline.return_value = _make_storyline(
            storyline_id=7,
        )
        storyline_repo.list_anchors.return_value = []
        storyline_repo.list_chapters.return_value = []
        ctx = _make_ctx(
            storyline_repository=storyline_repo,
            entity_store=MagicMock(),
        )

        out = journal_get_storyline(storyline_id=7, ctx=ctx)

        assert "no chapters yet" in out.lower()
        assert "journal_refresh_storyline" in out


# ── journal_get_storyline_chapter ───────────────────────────────


class TestGetStorylineChapter:
    def test_not_configured(self, patched_user_id: int) -> None:  # noqa: ARG002
        from journal.mcp_server import journal_get_storyline_chapter

        ctx = _make_ctx()
        out = journal_get_storyline_chapter(
            storyline_id=1, chapter_id=1, ctx=ctx,
        )
        assert "not configured" in out.lower()

    def test_storyline_not_found(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        from journal.mcp_server import journal_get_storyline_chapter

        storyline_repo = _storyline_repo_mock()
        storyline_repo.get_storyline.return_value = None
        ctx = _make_ctx(storyline_repository=storyline_repo)
        out = journal_get_storyline_chapter(
            storyline_id=999, chapter_id=1, ctx=ctx,
        )
        assert "not found" in out.lower()

    def test_chapter_not_owned_by_storyline_is_rejected(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        from journal.mcp_server import journal_get_storyline_chapter

        storyline_repo = _storyline_repo_mock()
        storyline_repo.get_storyline.return_value = _make_storyline(
            storyline_id=7,
        )
        other = _make_chapter(chapter_id=88, storyline_id=99)
        storyline_repo.get_chapter.return_value = other
        ctx = _make_ctx(storyline_repository=storyline_repo)

        out = journal_get_storyline_chapter(
            storyline_id=7, chapter_id=88, ctx=ctx,
        )

        assert "not found" in out.lower()

    def test_renders_segments_and_addenda(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        from journal.mcp_server import journal_get_storyline_chapter

        storyline_repo = _storyline_repo_mock()
        storyline_repo.get_storyline.return_value = _make_storyline(
            storyline_id=7,
        )
        chapter = _make_chapter(
            chapter_id=30,
            storyline_id=7,
            state="published",
            title="Early days",
            segments=[
                {"kind": "text", "text": "It started in January."},
                {
                    "kind": "citation",
                    "entry_id": 5,
                    "quote": "Went for a run",
                },
            ],
            addenda=[
                {
                    "added_at": "2026-03-01T00:00:00Z",
                    "segments": [
                        {"kind": "text", "text": "A late note appeared."},
                    ],
                    "entry_ids": [9],
                },
            ],
        )
        storyline_repo.get_chapter.return_value = chapter
        ctx = _make_ctx(storyline_repository=storyline_repo)

        out = journal_get_storyline_chapter(
            storyline_id=7, chapter_id=30, ctx=ctx,
        )

        assert "It started in January." in out
        assert "[entry 5]" in out
        assert "Went for a run" in out
        assert "Addenda" in out
        assert "A late note appeared." in out


# ── journal_create_storyline (bootstrap on create) ──────────────


class TestCreateStoryline:
    @staticmethod
    def _make_entity(
        entity_id: int = 59, canonical_name: str = "Running",
    ) -> MagicMock:
        entity = MagicMock()
        entity.id = entity_id
        entity.canonical_name = canonical_name
        return entity

    def test_create_success_bootstraps_and_returns_summary(
        self, patched_user_id: int,
    ) -> None:
        from journal.mcp_server import journal_create_storyline

        storyline_repo = _storyline_repo_mock()
        storyline_repo.find_by_anchor_set.return_value = None
        created = _make_storyline(storyline_id=17, name="Running")
        storyline_repo.create_storyline.return_value = created

        entity_store = MagicMock()
        entity_store.get_entity.return_value = self._make_entity(
            entity_id=59, canonical_name="Running",
        )

        job_runner = MagicMock()
        job_runner.submit_storyline_update.return_value = _make_job(
            status="pending", job_id="job-create-7",
        )
        job_repository = MagicMock()
        job_repository.get.return_value = _make_job(
            status="succeeded",
            job_id="job-create-7",
            result={"chapter_count": 3},
        )

        ctx = _make_ctx(
            storyline_repository=storyline_repo,
            entity_store=entity_store,
            job_runner=job_runner,
            job_repository=job_repository,
        )

        out = journal_create_storyline(
            entity_ids=[59], name="Running", ctx=ctx,
        )

        assert "Created storyline 17" in out
        assert "Bootstrap finished" in out
        assert "journal_get_storyline(17)" in out
        storyline_repo.create_storyline.assert_called_once_with(
            user_id=patched_user_id, entity_ids=[59], name="Running",
            description="",
        )
        job_runner.submit_storyline_update.assert_called_once_with(
            17, user_id=patched_user_id, bootstrap=True,
        )

    def test_already_exists_does_not_kick_job(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        from journal.mcp_server import journal_create_storyline

        storyline_repo = _storyline_repo_mock()
        existing = _make_storyline(storyline_id=42, name="Running")
        storyline_repo.find_by_anchor_set.return_value = existing

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
            entity_ids=[59], name="Running", ctx=ctx,
        )

        assert "already exists" in out.lower()
        assert "id=42" in out
        job_runner.submit_storyline_update.assert_not_called()
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
            entity_ids=[999], name="Running", ctx=ctx,
        )

        assert "[999]" in out
        assert "not found for this user" in out
        assert "journal_list_entities" in out
        job_runner.submit_storyline_update.assert_not_called()
        storyline_repo.create_storyline.assert_not_called()

    def test_not_configured_short_circuits(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        from journal.mcp_server import journal_create_storyline

        ctx = _make_ctx()  # no storyline_repository in lifespan
        out = journal_create_storyline(
            entity_ids=[1], name="X", ctx=ctx,
        )
        assert "not configured" in out.lower()

    def test_bootstrap_timeout_returns_actionable_message(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        from journal.mcp_server import journal_create_storyline

        storyline_repo = _storyline_repo_mock()
        storyline_repo.find_by_anchor_set.return_value = None
        created = _make_storyline(storyline_id=23, name="Running")
        storyline_repo.create_storyline.return_value = created

        entity_store = MagicMock()
        entity_store.get_entity.return_value = self._make_entity()

        job_runner = MagicMock()
        job_runner.submit_storyline_update.return_value = _make_job(
            status="pending", job_id="job-slow-23",
        )
        job_repository = MagicMock()
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
            entity_ids=[59], name="Running", timeout_seconds=0, ctx=ctx,
        )

        assert "Created storyline 23" in out
        assert "job-slow-23" in out
        assert "journal_get_job_status('job-slow-23')" in out
        assert "journal_get_storyline(23)" in out

    def test_bootstrap_failure_surfaces_error_message(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        from journal.mcp_server import journal_create_storyline

        storyline_repo = _storyline_repo_mock()
        storyline_repo.find_by_anchor_set.return_value = None
        created = _make_storyline(storyline_id=31, name="Running")
        storyline_repo.create_storyline.return_value = created

        entity_store = MagicMock()
        entity_store.get_entity.return_value = self._make_entity()

        job_runner = MagicMock()
        job_runner.submit_storyline_update.return_value = _make_job(
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
            entity_ids=[59], name="Running", ctx=ctx,
        )

        assert "Created storyline 31" in out
        assert "Anthropic returned 529 overloaded" in out
        assert "journal_refresh_storyline(31)" in out

    def test_runner_runtime_error_keeps_storyline_pointers(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        from journal.mcp_server import journal_create_storyline

        storyline_repo = _storyline_repo_mock()
        storyline_repo.find_by_anchor_set.return_value = None
        created = _make_storyline(storyline_id=44, name="Running")
        storyline_repo.create_storyline.return_value = created

        entity_store = MagicMock()
        entity_store.get_entity.return_value = self._make_entity()

        job_runner = MagicMock()
        job_runner.submit_storyline_update.side_effect = RuntimeError(
            "StorylineEngine not configured",
        )

        ctx = _make_ctx(
            storyline_repository=storyline_repo,
            entity_store=entity_store,
            job_runner=job_runner,
            job_repository=MagicMock(),
        )

        out = journal_create_storyline(
            entity_ids=[59], name="Running", ctx=ctx,
        )

        assert "Created storyline 44" in out
        assert "could not be" in out.lower()
        assert "journal_refresh_storyline(44)" in out


# ── journal_refresh_storyline ───────────────────────────────────


class TestRefreshStoryline:
    def test_success_path_returns_formatted_summary(
        self, patched_user_id: int,
    ) -> None:
        from journal.mcp_server import journal_refresh_storyline

        storyline_repo = _storyline_repo_mock()
        storyline_repo.get_storyline.return_value = _make_storyline()
        job_runner = MagicMock()
        job_runner.submit_storyline_update.return_value = _make_job(
            status="pending", job_id="job-42",
        )
        job_repository = MagicMock()
        job_repository.get.return_value = _make_job(
            status="succeeded",
            job_id="job-42",
            result={"draft_entry_count": 5},
        )
        ctx = _make_ctx(
            storyline_repository=storyline_repo,
            job_runner=job_runner,
            job_repository=job_repository,
        )

        out = journal_refresh_storyline(storyline_id=3, ctx=ctx)

        assert "Refresh succeeded" in out
        assert "draft entries: 5" in out
        assert "journal_get_storyline(3)" in out
        job_runner.submit_storyline_update.assert_called_once_with(
            3, user_id=patched_user_id, refresh_only=True,
        )

    def test_failed_job_returns_error_message(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        from journal.mcp_server import journal_refresh_storyline

        storyline_repo = _storyline_repo_mock()
        storyline_repo.get_storyline.return_value = _make_storyline()
        job_runner = MagicMock()
        job_runner.submit_storyline_update.return_value = _make_job(
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

        out = journal_refresh_storyline(storyline_id=3, ctx=ctx)

        assert "Refresh failed" in out
        assert "Anthropic API returned 529 (overloaded)" in out

    def test_timeout_path_returns_actionable_message(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        from journal.mcp_server import journal_refresh_storyline

        storyline_repo = _storyline_repo_mock()
        storyline_repo.get_storyline.return_value = _make_storyline()
        job_runner = MagicMock()
        job_runner.submit_storyline_update.return_value = _make_job(
            status="pending", job_id="job-slow",
        )
        job_repository = MagicMock()
        job_repository.get.return_value = _make_job(
            status="running", job_id="job-slow",
        )
        ctx = _make_ctx(
            storyline_repository=storyline_repo,
            job_runner=job_runner,
            job_repository=job_repository,
        )

        out = journal_refresh_storyline(
            storyline_id=3, timeout_seconds=0, ctx=ctx,
        )

        assert "job-slow" in out
        assert "journal_get_job_status('job-slow')" in out
        assert "succeeded" not in out.lower()
        assert "Refresh failed" not in out

    def test_not_configured_short_circuits(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        from journal.mcp_server import journal_refresh_storyline

        ctx = _make_ctx(
            job_runner=MagicMock(),
            job_repository=MagicMock(),
        )

        out = journal_refresh_storyline(storyline_id=1, ctx=ctx)

        assert "not configured" in out.lower()

    def test_storyline_not_found(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        from journal.mcp_server import journal_refresh_storyline

        storyline_repo = _storyline_repo_mock()
        storyline_repo.get_storyline.return_value = None
        job_runner = MagicMock()
        ctx = _make_ctx(
            storyline_repository=storyline_repo,
            job_runner=job_runner,
            job_repository=MagicMock(),
        )

        out = journal_refresh_storyline(storyline_id=999, ctx=ctx)

        assert "not found" in out.lower()
        job_runner.submit_storyline_update.assert_not_called()

    def test_runner_runtime_error_is_surfaced(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        from journal.mcp_server import journal_refresh_storyline

        storyline_repo = _storyline_repo_mock()
        storyline_repo.get_storyline.return_value = _make_storyline()
        job_runner = MagicMock()
        job_runner.submit_storyline_update.side_effect = RuntimeError(
            "StorylineEngine not configured",
        )
        ctx = _make_ctx(
            storyline_repository=storyline_repo,
            job_runner=job_runner,
            job_repository=MagicMock(),
        )

        out = journal_refresh_storyline(storyline_id=3, ctx=ctx)

        assert "Cannot refresh" in out
        assert "StorylineEngine not configured" in out


# ── journal_unpublish_storyline_chapter ─────────────────────────


class TestUnpublishStorylineChapter:
    def test_not_configured(self, patched_user_id: int) -> None:  # noqa: ARG002
        from journal.mcp_server import journal_unpublish_storyline_chapter

        ctx = _make_ctx()
        out = journal_unpublish_storyline_chapter(storyline_id=1, ctx=ctx)
        assert "not configured" in out.lower()

    def test_storyline_not_found(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        from journal.mcp_server import journal_unpublish_storyline_chapter

        storyline_repo = _storyline_repo_mock()
        storyline_repo.get_storyline.return_value = None
        ctx = _make_ctx(storyline_repository=storyline_repo)

        out = journal_unpublish_storyline_chapter(storyline_id=999, ctx=ctx)

        assert "not found" in out.lower()

    def test_no_published_chapter_rejected(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        from journal.mcp_server import journal_unpublish_storyline_chapter

        storyline_repo = _storyline_repo_mock()
        storyline_repo.get_storyline.return_value = _make_storyline(
            storyline_id=3,
        )
        storyline_repo.list_chapters.return_value = [
            _make_chapter(state="draft", storyline_id=3),
        ]
        job_runner = MagicMock()
        ctx = _make_ctx(
            storyline_repository=storyline_repo, job_runner=job_runner,
        )

        out = journal_unpublish_storyline_chapter(storyline_id=3, ctx=ctx)

        assert "no published chapter" in out.lower()
        job_runner.submit_storyline_update.assert_not_called()

    def test_success_path_folds_and_reports(
        self, patched_user_id: int,
    ) -> None:
        from journal.mcp_server import journal_unpublish_storyline_chapter

        storyline_repo = _storyline_repo_mock()
        storyline_repo.get_storyline.return_value = _make_storyline(
            storyline_id=3,
        )
        storyline_repo.list_chapters.return_value = [
            _make_chapter(state="published", storyline_id=3),
        ]
        job_runner = MagicMock()
        job_runner.submit_storyline_update.return_value = _make_job(
            status="pending", job_id="job-unpub",
        )
        job_repository = MagicMock()
        job_repository.get.return_value = _make_job(
            status="succeeded",
            job_id="job-unpub",
            result={"draft_entry_count": 4},
        )
        ctx = _make_ctx(
            storyline_repository=storyline_repo,
            job_runner=job_runner,
            job_repository=job_repository,
        )

        out = journal_unpublish_storyline_chapter(storyline_id=3, ctx=ctx)

        assert "Unpublish succeeded" in out
        job_runner.submit_storyline_update.assert_called_once_with(
            3, user_id=patched_user_id, unpublish=True,
        )


# ── journal_rename_storyline_chapter ────────────────────────────


class TestRenameStorylineChapter:
    def test_not_configured(self, patched_user_id: int) -> None:  # noqa: ARG002
        from journal.mcp_server import journal_rename_storyline_chapter

        ctx = _make_ctx()
        out = journal_rename_storyline_chapter(
            storyline_id=1, chapter_id=1, title="X", ctx=ctx,
        )
        assert "not configured" in out.lower()

    def test_empty_title_rejected(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        from journal.mcp_server import journal_rename_storyline_chapter

        storyline_repo = _storyline_repo_mock()
        storyline_repo.get_storyline.return_value = _make_storyline(
            storyline_id=3,
        )
        storyline_repo.get_chapter.return_value = _make_chapter(
            chapter_id=30, storyline_id=3,
        )
        ctx = _make_ctx(storyline_repository=storyline_repo)

        out = journal_rename_storyline_chapter(
            storyline_id=3, chapter_id=30, title="   ", ctx=ctx,
        )

        assert "non-empty" in out.lower()
        storyline_repo.rename_chapter.assert_not_called()

    def test_happy_path_renames(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        from journal.mcp_server import journal_rename_storyline_chapter

        storyline_repo = _storyline_repo_mock()
        storyline_repo.get_storyline.return_value = _make_storyline(
            storyline_id=3,
        )
        storyline_repo.get_chapter.return_value = _make_chapter(
            chapter_id=30, storyline_id=3,
        )
        ctx = _make_ctx(storyline_repository=storyline_repo)

        out = journal_rename_storyline_chapter(
            storyline_id=3, chapter_id=30, title="New Title", ctx=ctx,
        )

        storyline_repo.rename_chapter.assert_called_once_with(30, "New Title")
        assert "Renamed chapter [30]" in out
        assert "New Title" in out


# ── journal_set_storyline_anchors ───────────────────────────────


class TestSetStorylineAnchors:
    @staticmethod
    def _make_entity(eid: int, name: str) -> MagicMock:
        entity = MagicMock()
        entity.id = eid
        entity.canonical_name = name
        return entity

    def test_not_configured(self, patched_user_id: int) -> None:  # noqa: ARG002
        from journal.mcp_server import journal_set_storyline_anchors

        ctx = _make_ctx()
        out = journal_set_storyline_anchors(
            storyline_id=1, entity_ids=[2], ctx=ctx,
        )
        assert "not configured" in out.lower()

    def test_storyline_not_found(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        from journal.mcp_server import journal_set_storyline_anchors

        storyline_repo = _storyline_repo_mock()
        storyline_repo.get_storyline.return_value = None
        ctx = _make_ctx(storyline_repository=storyline_repo)
        out = journal_set_storyline_anchors(
            storyline_id=999, entity_ids=[1], ctx=ctx,
        )
        assert "not found" in out.lower()

    def test_empty_entity_ids_rejected(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        from journal.mcp_server import journal_set_storyline_anchors

        storyline_repo = _storyline_repo_mock()
        storyline_repo.get_storyline.return_value = _make_storyline()
        ctx = _make_ctx(storyline_repository=storyline_repo)
        out = journal_set_storyline_anchors(
            storyline_id=1, entity_ids=[], ctx=ctx,
        )
        assert "at least one" in out.lower()
        storyline_repo.set_anchors.assert_not_called()

    def test_above_cap_rejected(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        from journal.mcp_server import journal_set_storyline_anchors

        storyline_repo = _storyline_repo_mock()
        storyline_repo.get_storyline.return_value = _make_storyline()
        ctx = _make_ctx(storyline_repository=storyline_repo)
        out = journal_set_storyline_anchors(
            storyline_id=1,
            entity_ids=list(range(1, 17)),  # 16 anchors
            ctx=ctx,
        )
        assert "cap" in out.lower()
        storyline_repo.set_anchors.assert_not_called()

    def test_missing_entity_reported_and_no_write(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        from journal.mcp_server import journal_set_storyline_anchors

        storyline_repo = _storyline_repo_mock()
        storyline_repo.get_storyline.return_value = _make_storyline()
        entity_store = MagicMock()
        entity_store.get_entity.return_value = None
        ctx = _make_ctx(
            storyline_repository=storyline_repo,
            entity_store=entity_store,
        )
        out = journal_set_storyline_anchors(
            storyline_id=1, entity_ids=[42, 99], ctx=ctx,
        )
        assert "[42, 99]" in out
        storyline_repo.set_anchors.assert_not_called()

    def test_happy_path_updates_anchors_and_returns_summary(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        from journal.mcp_server import journal_set_storyline_anchors

        storyline_repo = _storyline_repo_mock()
        storyline_repo.get_storyline.return_value = _make_storyline(
            storyline_id=7,
        )
        storyline_repo.list_anchors.return_value = [42, 99]
        entity_store = MagicMock()
        entity_store.get_entity.side_effect = lambda eid, **_: (
            self._make_entity(eid, f"Name{eid}")
        )
        ctx = _make_ctx(
            storyline_repository=storyline_repo,
            entity_store=entity_store,
        )

        out = journal_set_storyline_anchors(
            storyline_id=7, entity_ids=[99, 42], ctx=ctx,
        )

        storyline_repo.set_anchors.assert_called_once_with(7, [42, 99])
        assert "Updated anchors for storyline 7" in out
        assert "Name42" in out
        assert "Name99" in out
        assert "journal_refresh_storyline(7)" in out


# ── journal_storylines_guide ────────────────────────────────────


class TestStorylinesGuide:
    def test_returns_non_empty_guide(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        from journal.mcp_server import journal_storylines_guide

        out = journal_storylines_guide(ctx=_make_ctx())

        assert isinstance(out, str)
        assert len(out) > 200

    def test_mentions_current_tool_names(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        """A guide that doesn't reference its peers can't be a guide."""
        from journal.mcp_server import journal_storylines_guide

        out = journal_storylines_guide(ctx=_make_ctx())

        assert "journal_list_storylines" in out
        assert "journal_get_storyline" in out
        assert "journal_get_storyline_chapter" in out
        assert "journal_create_storyline" in out
        assert "journal_refresh_storyline" in out
        assert "journal_unpublish_storyline_chapter" in out
        assert "journal_rename_storyline_chapter" in out
        assert "journal_set_storyline_anchors" in out
        assert "journal_delete_storyline" in out

    def test_mentions_draft_and_published_and_judge(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        from journal.mcp_server import journal_storylines_guide

        out = journal_storylines_guide(ctx=_make_ctx())

        assert "draft" in out.lower()
        assert "published" in out.lower()
        assert "judge" in out.lower()
        assert "unread" in out.lower()
        assert "unpublish" in out.lower()
        assert "bootstrap" in out.lower()

    def test_mentions_no_stale_concepts(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        """The old two-panel / manual-chaptering vocabulary must not
        survive the redesign into the primer text."""
        from journal.mcp_server import journal_storylines_guide

        out = journal_storylines_guide(ctx=_make_ctx())

        for stale_word in (
            "panel", "curation", "resegment", "window", "split", "merge",
        ):
            assert stale_word not in out.lower(), (
                f"guide still mentions stale concept {stale_word!r}"
            )

    def test_returns_guide_when_not_configured(
        self, patched_user_id: int,  # noqa: ARG002
    ) -> None:
        """The guide must be discoverable even when the feature isn't
        wired — a client with no ANTHROPIC_API_KEY still needs to learn
        what storylines are and why the other tools error."""
        from journal.mcp_server import journal_storylines_guide

        ctx = _make_ctx()

        out = journal_storylines_guide(ctx=ctx)

        assert isinstance(out, str)
        assert len(out) > 200
        assert "journal_list_storylines" in out
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


# ── registration sanity checks ──────────────────────────────────


class TestStorylineToolRegistration:
    """Deleted tools must not be registered; surviving tools must
    advertise the expected MCP behavior hints."""

    def test_deleted_tools_not_registered(self) -> None:
        from journal.mcp_server import mcp

        registered_tool_names = set(mcp._tool_manager._tools)
        for deleted in (
            "journal_add_storyline_chapter",
            "journal_split_storyline_chapter",
            "journal_merge_storyline_chapters",
            "journal_update_storyline_chapter",
            "journal_delete_storyline_chapter",
            "journal_regenerate_storyline",
        ):
            assert deleted not in registered_tool_names

    def test_annotations_match_plan(self) -> None:
        from journal.mcp_server import mcp

        tools = mcp._tool_manager._tools
        expected: dict[str, dict[str, bool]] = {
            "journal_list_storylines": {"readOnlyHint": True},
            "journal_get_storyline": {"readOnlyHint": True},
            "journal_get_storyline_chapter": {"readOnlyHint": True},
            "journal_create_storyline": {},
            "journal_refresh_storyline": {"idempotentHint": True},
            "journal_unpublish_storyline_chapter": {"destructiveHint": True},
            "journal_rename_storyline_chapter": {},
            "journal_set_storyline_anchors": {},
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
            if not hints and ann is not None:
                assert ann.readOnlyHint is None
                assert ann.idempotentHint is None
                assert ann.destructiveHint is None
