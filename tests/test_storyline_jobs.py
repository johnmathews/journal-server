"""Tests for the storylines job/integration layer (storylines-redesign
Task 8).

* ``run_storyline_update`` — the worker driving ``StorylineEngine``:
  default steady-state ``update``, ``bootstrap``/``refresh_only``/
  ``unpublish`` routing, the publish notification (fires once, only on
  an actual publish), missing-engine guard, engine-exception failure
  handling.
* ``submit_storyline_update`` — param validation (at most one of
  bootstrap/refresh_only/unpublish), missing-engine guard, Pool B
  submission.
* ``run_storyline_extension_check`` — classifies an entry against each
  active storyline; a ``yes`` records the entry as pending
  (``storyline_repository.add_pending_entry``) and queues a coalesced
  ``storyline_update`` unless one is already queued
  (``find_pending_storyline_update``).
* W6 decider parser (``_parse_decision``) — unrelated to Task 8, kept
  alongside the rest of the storyline job-layer tests.
* W1: the storyline_extension_check is queued by the entity-extraction
  worker after mentions are committed (not as a racing sibling), so the
  classifier's entity-overlap signal is reliable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest

from journal.db.jobs_repository import SQLiteJobRepository
from journal.providers.storyline_extension_decider import (
    AnthropicStorylineExtensionDecider,
    _parse_decision,
)
from journal.services.jobs.runner import JobRunner
from journal.services.jobs.workers import WorkerContext
from journal.services.jobs.workers.entity_extraction import (
    run_entity_extraction,
)
from journal.services.jobs.workers.storyline_extension_check import (
    run_storyline_extension_check,
)
from journal.services.jobs.workers.storyline_update import run_storyline_update
from journal.services.storylines.engine import PublishedInfo, UpdateResult
from journal.services.storylines.extension import ExtensionResult

if TYPE_CHECKING:
    from journal.db.factory import ConnectionFactory


# ── Fakes ───────────────────────────────────────────────────────


class _FakeJobNotifier:
    def __init__(self) -> None:
        self.successes: list[tuple[str, Any]] = []
        self.failures: list[tuple[str, str]] = []
        self.chapter_published_calls: list[tuple[int | None, str, str]] = []

    def get_notify_strategy(self, _parent: str | None) -> str:
        return "individual"

    def notify_success(self, _user: int | None, job_type: str, payload: Any) -> None:  # noqa: ANN401
        self.successes.append((job_type, payload))

    def notify_failed(
        self, _user: int | None, job_type: str, msg: str, _exc: Exception | None = None,
    ) -> None:
        self.failures.append((job_type, msg))

    def notify_chapter_published(
        self, user_id: int | None, storyline_name: str, chapter_title: str,
    ) -> None:
        self.chapter_published_calls.append((user_id, storyline_name, chapter_title))

    def try_pipeline_notification(
        self, _parent_job_id: str, _user: int | None,
    ) -> None:
        return None


class _FakeStorylineEngine:
    """Records every call; returns a per-method configurable result.

    Each of ``update_result``/``bootstrap_result``/``refresh_result``
    defaults to an empty ``UpdateResult`` (no publish, nothing new) so
    tests that don't care about the return value don't have to
    configure one.
    """

    def __init__(
        self,
        *,
        update_result: UpdateResult | None = None,
        bootstrap_result: UpdateResult | None = None,
        refresh_result: UpdateResult | None = None,
        raise_exc: Exception | None = None,
        call_order: list[str] | None = None,
    ) -> None:
        self.update_result = update_result or UpdateResult(storyline_id=0)
        self.bootstrap_result = bootstrap_result or UpdateResult(storyline_id=0)
        self.refresh_result = refresh_result or UpdateResult(storyline_id=0)
        self._raise = raise_exc
        self._order = call_order
        self.update_calls: list[int] = []
        self.bootstrap_calls: list[int] = []
        self.refresh_calls: list[int] = []

    def update(self, storyline_id: int) -> UpdateResult:
        self.update_calls.append(storyline_id)
        if self._raise is not None:
            raise self._raise
        return self.update_result

    def bootstrap(self, storyline_id: int, *, mark_read: bool = False) -> UpdateResult:  # noqa: ARG002
        self.bootstrap_calls.append(storyline_id)
        return self.bootstrap_result

    def refresh_draft(self, storyline_id: int) -> UpdateResult:
        self.refresh_calls.append(storyline_id)
        if self._order is not None:
            self._order.append("refresh")
        return self.refresh_result


@dataclass
class _FakeStoryline:
    id: int
    name: str


class _FakeStorylineRepository:
    """Records ``add_pending_entry``/``unpublish_newest`` calls; resolves
    ``get_storyline`` from a name lookup table configured per test."""

    def __init__(
        self,
        *,
        names: dict[int, str] | None = None,
        call_order: list[str] | None = None,
    ) -> None:
        self._names = names or {}
        self._order = call_order
        self.pending_calls: list[tuple[int, int]] = []
        self.unpublish_calls: list[int] = []

    def add_pending_entry(self, storyline_id: int, entry_id: int) -> None:
        self.pending_calls.append((storyline_id, entry_id))

    def get_storyline(self, storyline_id: int) -> _FakeStoryline | None:
        name = self._names.get(storyline_id)
        return None if name is None else _FakeStoryline(id=storyline_id, name=name)

    def unpublish_newest(self, storyline_id: int) -> None:
        self.unpublish_calls.append(storyline_id)
        if self._order is not None:
            self._order.append("unpublish")


class _FakeClassifier:
    def __init__(self, results_by_entry: dict[int, list[ExtensionResult]]) -> None:
        self._by_entry = results_by_entry
        self.calls: list[tuple[int, int]] = []

    def classify_for_entry(
        self, entry_id: int, user_id: int,
    ) -> list[ExtensionResult]:
        self.calls.append((entry_id, user_id))
        return self._by_entry.get(entry_id, [])


@dataclass
class _FakeExtractionResult:
    """Minimal stand-in for the object ``EntityExtractionService``
    returns; ``run_entity_extraction`` only reads these count fields."""

    entities_created: int = 0
    entities_matched: int = 0
    entities_deleted: int = 0
    mentions_created: int = 0
    relationships_created: int = 0
    warnings: list[str] = field(default_factory=list)


class _FakeExtraction:
    def __init__(self) -> None:
        self.entry_calls: list[int] = []
        self.batch_calls: int = 0

    def extract_from_entry(self, entry_id: int) -> _FakeExtractionResult:
        self.entry_calls.append(entry_id)
        return _FakeExtractionResult()

    def extract_batch(self, **_kwargs: Any) -> list[_FakeExtractionResult]:  # noqa: ANN401
        self.batch_calls += 1
        return [_FakeExtractionResult()]


# ── run_storyline_update worker ─────────────────────────────────


@pytest.fixture
def job_ctx(
    factory: ConnectionFactory,
) -> tuple[SQLiteJobRepository, _FakeJobNotifier]:
    return SQLiteJobRepository(factory), _FakeJobNotifier()


class TestStorylineUpdateWorker:
    def test_worker_update_calls_engine_and_records_summary(
        self,
        job_ctx: tuple[SQLiteJobRepository, _FakeJobNotifier],
    ) -> None:
        jobs, notifier = job_ctx
        engine = _FakeStorylineEngine(
            update_result=UpdateResult(
                storyline_id=42,
                new_entry_count=3,
                draft_entry_count=1,
                published=PublishedInfo(chapter_id=5, title="T"),
                reasoning="continues the arc",
            ),
        )
        ctx = _build_minimal_ctx(jobs, notifier, engine=engine)

        job = jobs.create(
            "storyline_update", {"storyline_id": 42, "user_id": 1}, user_id=1,
        )
        run_storyline_update(ctx, job.id, {"storyline_id": 42, "user_id": 1})

        finished = jobs.get(job.id)
        assert finished is not None
        assert finished.status == "succeeded"
        assert finished.result is not None
        assert finished.result["published_chapter_id"] == 5
        assert finished.result["published_title"] == "T"
        assert finished.result["reasoning"] == "continues the arc"
        assert engine.update_calls == [42]
        assert engine.bootstrap_calls == []
        assert engine.refresh_calls == []

    def test_worker_publish_fires_pushover(
        self,
        job_ctx: tuple[SQLiteJobRepository, _FakeJobNotifier],
    ) -> None:
        jobs, notifier = job_ctx
        engine = _FakeStorylineEngine(
            update_result=UpdateResult(
                storyline_id=7, published=PublishedInfo(chapter_id=9, title="T"),
            ),
        )
        repo = _FakeStorylineRepository(names={7: "Marathon Training"})
        ctx = _build_minimal_ctx(jobs, notifier, engine=engine, storyline_repo=repo)

        job = jobs.create(
            "storyline_update", {"storyline_id": 7, "user_id": 1}, user_id=1,
        )
        run_storyline_update(ctx, job.id, {"storyline_id": 7, "user_id": 1})

        assert notifier.chapter_published_calls == [
            (1, "Marathon Training", "T"),
        ]

    def test_worker_no_publish_does_not_fire_pushover(
        self,
        job_ctx: tuple[SQLiteJobRepository, _FakeJobNotifier],
    ) -> None:
        jobs, notifier = job_ctx
        engine = _FakeStorylineEngine(update_result=UpdateResult(storyline_id=8))
        ctx = _build_minimal_ctx(jobs, notifier, engine=engine)

        job = jobs.create(
            "storyline_update", {"storyline_id": 8, "user_id": 1}, user_id=1,
        )
        run_storyline_update(ctx, job.id, {"storyline_id": 8, "user_id": 1})

        finished = jobs.get(job.id)
        assert finished is not None
        assert finished.status == "succeeded"
        assert notifier.chapter_published_calls == []

    def test_worker_bootstrap_param_routes_to_bootstrap(
        self,
        job_ctx: tuple[SQLiteJobRepository, _FakeJobNotifier],
    ) -> None:
        jobs, notifier = job_ctx
        engine = _FakeStorylineEngine(
            bootstrap_result=UpdateResult(storyline_id=10, chapter_count=4),
        )
        ctx = _build_minimal_ctx(jobs, notifier, engine=engine)

        params = {"storyline_id": 10, "user_id": 1, "bootstrap": True}
        job = jobs.create("storyline_update", params, user_id=1)
        run_storyline_update(ctx, job.id, params)

        finished = jobs.get(job.id)
        assert finished is not None
        assert finished.status == "succeeded"
        assert finished.result is not None
        assert finished.result["chapter_count"] == 4
        assert engine.bootstrap_calls == [10]
        assert engine.update_calls == []
        assert engine.refresh_calls == []

    def test_worker_refresh_only_routes_to_refresh_draft(
        self,
        job_ctx: tuple[SQLiteJobRepository, _FakeJobNotifier],
    ) -> None:
        jobs, notifier = job_ctx
        engine = _FakeStorylineEngine(
            refresh_result=UpdateResult(storyline_id=11, draft_entry_count=6),
        )
        ctx = _build_minimal_ctx(jobs, notifier, engine=engine)

        params = {"storyline_id": 11, "user_id": 1, "refresh_only": True}
        job = jobs.create("storyline_update", params, user_id=1)
        run_storyline_update(ctx, job.id, params)

        finished = jobs.get(job.id)
        assert finished is not None
        assert finished.status == "succeeded"
        assert finished.result is not None
        assert finished.result["draft_entry_count"] == 6
        assert engine.refresh_calls == [11]
        assert engine.update_calls == []
        assert engine.bootstrap_calls == []

    def test_worker_unpublish_folds_then_refreshes(
        self,
        job_ctx: tuple[SQLiteJobRepository, _FakeJobNotifier],
    ) -> None:
        """The unpublish branch must fold the newest published chapter
        back into the draft BEFORE re-narrating it — refresh_draft reads
        the draft's current membership, so the fold has to land first."""
        jobs, notifier = job_ctx
        order: list[str] = []
        engine = _FakeStorylineEngine(
            refresh_result=UpdateResult(storyline_id=12), call_order=order,
        )
        repo = _FakeStorylineRepository(call_order=order)
        ctx = _build_minimal_ctx(jobs, notifier, engine=engine, storyline_repo=repo)

        params = {"storyline_id": 12, "user_id": 1, "unpublish": True}
        job = jobs.create("storyline_update", params, user_id=1)
        run_storyline_update(ctx, job.id, params)

        finished = jobs.get(job.id)
        assert finished is not None
        assert finished.status == "succeeded"
        assert repo.unpublish_calls == [12]
        assert engine.refresh_calls == [12]
        assert order == ["unpublish", "refresh"]

    def test_worker_unconfigured_engine_fails_job(
        self,
        job_ctx: tuple[SQLiteJobRepository, _FakeJobNotifier],
    ) -> None:
        jobs, notifier = job_ctx
        ctx = _build_minimal_ctx(jobs, notifier, engine=None)
        job = jobs.create(
            "storyline_update", {"storyline_id": 1, "user_id": 1}, user_id=1,
        )
        run_storyline_update(ctx, job.id, {"storyline_id": 1, "user_id": 1})

        finished = jobs.get(job.id)
        assert finished is not None
        assert finished.status == "failed"
        assert "not configured" in (finished.error_message or "")
        assert notifier.failures  # failure notification fired
        assert notifier.chapter_published_calls == []

    def test_worker_engine_exception_marks_failed(
        self,
        job_ctx: tuple[SQLiteJobRepository, _FakeJobNotifier],
    ) -> None:
        jobs, notifier = job_ctx
        engine = _FakeStorylineEngine(raise_exc=RuntimeError("judge API down"))
        ctx = _build_minimal_ctx(jobs, notifier, engine=engine)

        job = jobs.create(
            "storyline_update", {"storyline_id": 2, "user_id": 1}, user_id=1,
        )
        run_storyline_update(ctx, job.id, {"storyline_id": 2, "user_id": 1})

        finished = jobs.get(job.id)
        assert finished is not None
        assert finished.status == "failed"
        assert finished.error_message
        assert notifier.chapter_published_calls == []


# ── W6 decider parser ──────────────────────────────────────────


class TestExtensionDeciderParser:
    def test_parses_tool_use_block(self) -> None:
        response = type("R", (), {"content": [
            {
                "type": "tool_use",
                "name": "record_decision",
                "input": {"decision": "yes", "reasoning": "Direct match."},
            },
        ]})()
        result = _parse_decision(response, model="haiku")
        assert result.decision == "yes"
        assert result.reasoning == "Direct match."
        assert result.model_used == "haiku"

    def test_malformed_response_falls_back_to_maybe(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        response = type("R", (), {"content": [
            {"type": "text", "text": "I think yes"},  # not tool_use
        ]})()
        with caplog.at_level("WARNING"):
            result = _parse_decision(response, model="haiku")
        assert result.decision == "maybe"
        assert "malformed" in result.reasoning.lower()

    def test_invalid_decision_value_falls_back(self) -> None:
        response = type("R", (), {"content": [
            {
                "type": "tool_use",
                "name": "record_decision",
                "input": {"decision": "definitely", "reasoning": "?"},
            },
        ]})()
        result = _parse_decision(response, model="haiku")
        assert result.decision == "maybe"

    def test_api_failure_returns_maybe(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        class _Boom:
            class _M:
                def create(self, **_: Any) -> Any:  # noqa: ANN401
                    raise RuntimeError("sad")
            messages = _M()
        decider = AnthropicStorylineExtensionDecider(
            api_key="x", client=_Boom(),
        )
        with caplog.at_level("ERROR"):
            out = decider.decide(
                storyline_name="x",
                storyline_description="",
                entry_date="2026-01-01",
                entry_text="anything",
            )
        assert out.decision == "maybe"


# ── extension-check worker + ingestion hook ────────────────────


class TestEntityExtractionTriggersStorylineCheck:
    """W1: the storyline extension check is enqueued by the
    entity-extraction worker *after* mentions are committed, not as a
    concurrent sibling that races entity extraction. This fixes the
    root cause of a burst ingest updating zero storylines."""

    def test_single_entry_extraction_queues_storyline_check(
        self,
        job_ctx: tuple[SQLiteJobRepository, _FakeJobNotifier],
    ) -> None:
        jobs, notifier = job_ctx
        spy: list[tuple[int, int | None]] = []
        ctx = _build_minimal_ctx(
            jobs, notifier,
            extraction=_FakeExtraction(),
            queue_storyline_check=lambda eid, uid: spy.append((eid, uid)),
        )
        job = jobs.create(
            "entity_extraction", {"entry_id": 7, "user_id": 1}, user_id=1,
        )
        run_entity_extraction(ctx, job.id, {"entry_id": 7, "user_id": 1})

        finished = jobs.get(job.id)
        assert finished is not None
        assert finished.status == "succeeded"
        # Trigger fired exactly once, for this entry + user.
        assert spy == [(7, 1)]

    def test_batch_extraction_does_not_queue_storyline_check(
        self,
        job_ctx: tuple[SQLiteJobRepository, _FakeJobNotifier],
    ) -> None:
        """Batch extraction (no entry_id) must not fan out one storyline
        check per entry — that would be a regeneration storm."""
        jobs, notifier = job_ctx
        spy: list[Any] = []
        ctx = _build_minimal_ctx(
            jobs, notifier,
            extraction=_FakeExtraction(),
            queue_storyline_check=lambda *a: spy.append(a),  # noqa: ANN401
        )
        job = jobs.create(
            "entity_extraction", {"stale_only": True}, user_id=1,
        )
        run_entity_extraction(
            ctx, job.id, {"stale_only": True, "user_id": 1},
        )

        finished = jobs.get(job.id)
        assert finished is not None
        assert finished.status == "succeeded"
        assert spy == []


class TestExtensionCheckWorker:
    def test_yes_decisions_queue_updates(
        self,
        job_ctx: tuple[SQLiteJobRepository, _FakeJobNotifier],
    ) -> None:
        jobs, notifier = job_ctx
        classifier = _FakeClassifier({
            42: [
                ExtensionResult(
                    storyline_id=1, decision="yes",
                    reasoning="ok", stage="entity_overlap",
                ),
                ExtensionResult(
                    storyline_id=2, decision="no",
                    reasoning="meh", stage="no_match",
                ),
                ExtensionResult(
                    storyline_id=3, decision="maybe",
                    reasoning="borderline", stage="surface_form_llm",
                ),
            ],
        })
        storyline_repo = _FakeStorylineRepository()
        ctx = _build_minimal_ctx(
            jobs, notifier, classifier=classifier, storyline_repo=storyline_repo,
        )
        update_calls: list[int] = []

        def fake_submit_update(storyline_id: int, **_: Any) -> Any:  # noqa: ANN401
            update_calls.append(storyline_id)
            return type("J", (), {"id": f"update-{storyline_id}"})()

        job = jobs.create(
            "storyline_extension_check",
            {"entry_id": 42, "user_id": 1}, user_id=1,
        )
        run_storyline_extension_check(
            ctx, job.id,
            {"entry_id": 42, "user_id": 1},
            fake_submit_update,
        )

        finished = jobs.get(job.id)
        assert finished is not None
        assert finished.status == "succeeded"
        assert update_calls == [1]  # only the "yes" got an update
        assert storyline_repo.pending_calls == [(1, 42)]
        assert finished.result is not None
        classifications = finished.result["classifications"]
        decisions = sorted(c["decision"] for c in classifications)
        assert decisions == ["maybe", "no", "yes"]

    def test_missing_classifier_marks_failed(
        self,
        job_ctx: tuple[SQLiteJobRepository, _FakeJobNotifier],
    ) -> None:
        jobs, notifier = job_ctx
        ctx = _build_minimal_ctx(jobs, notifier, classifier=None)
        job = jobs.create(
            "storyline_extension_check",
            {"entry_id": 1, "user_id": 1}, user_id=1,
        )
        run_storyline_extension_check(
            ctx, job.id,
            {"entry_id": 1, "user_id": 1},
            lambda *_a, **_k: None,
        )
        finished = jobs.get(job.id)
        assert finished is not None
        assert finished.status == "failed"

    def test_extension_check_yes_adds_pending_and_queues_coalesced(
        self,
        job_ctx: tuple[SQLiteJobRepository, _FakeJobNotifier],
    ) -> None:
        """A burst ingest must not fire one full update per entry. Every
        ``yes`` records the entry as pending regardless of coalescing
        (lossless), but if a queued update for the storyline already
        exists, it will pick up the pending entry when it runs, so skip
        queuing another."""
        jobs, notifier = job_ctx
        classifier = _FakeClassifier({
            42: [
                ExtensionResult(
                    storyline_id=1, decision="yes",
                    reasoning="ok", stage="entity_overlap",
                ),
            ],
        })
        storyline_repo = _FakeStorylineRepository()
        ctx = _build_minimal_ctx(
            jobs, notifier, classifier=classifier, storyline_repo=storyline_repo,
        )
        # A prior entry in this batch already queued a plain update for
        # storyline 1 (still queued — not yet run).
        jobs.create(
            "storyline_update",
            {"storyline_id": 1, "user_id": 1},
            user_id=1,
        )
        update_calls: list[int] = []

        def fake_submit_update(storyline_id: int, **_: Any) -> Any:  # noqa: ANN401
            update_calls.append(storyline_id)
            return type("J", (), {"id": "update"})()

        job = jobs.create(
            "storyline_extension_check",
            {"entry_id": 42, "user_id": 1}, user_id=1,
        )
        run_storyline_extension_check(
            ctx, job.id, {"entry_id": 42, "user_id": 1}, fake_submit_update,
        )

        finished = jobs.get(job.id)
        assert finished is not None
        assert finished.status == "succeeded"
        # Pending was recorded even though the update itself coalesced —
        # this is what makes the coalescing lossless.
        assert storyline_repo.pending_calls == [(1, 42)]
        assert update_calls == []  # coalesced onto the queued update
        assert finished.result is not None
        assert finished.result["updates_queued"] == []
        assert finished.result["coalesced_storyline_ids"] == [1]

    def test_queues_update_when_no_pending_update(
        self,
        job_ctx: tuple[SQLiteJobRepository, _FakeJobNotifier],
    ) -> None:
        """With nothing queued, the "yes" still queues an update."""
        jobs, notifier = job_ctx
        classifier = _FakeClassifier({
            42: [
                ExtensionResult(
                    storyline_id=1, decision="yes",
                    reasoning="ok", stage="entity_overlap",
                ),
            ],
        })
        storyline_repo = _FakeStorylineRepository()
        ctx = _build_minimal_ctx(
            jobs, notifier, classifier=classifier, storyline_repo=storyline_repo,
        )
        update_calls: list[int] = []

        def fake_submit_update(storyline_id: int, **_: Any) -> Any:  # noqa: ANN401
            update_calls.append(storyline_id)
            return type("J", (), {"id": "update"})()

        job = jobs.create(
            "storyline_extension_check",
            {"entry_id": 42, "user_id": 1}, user_id=1,
        )
        run_storyline_extension_check(
            ctx, job.id, {"entry_id": 42, "user_id": 1}, fake_submit_update,
        )

        finished = jobs.get(job.id)
        assert finished is not None
        assert update_calls == [1]
        assert finished.result["coalesced_storyline_ids"] == []


# ── _build_minimal_ctx helper ───────────────────────────────────


def _build_minimal_ctx(
    jobs: SQLiteJobRepository,
    notifier: _FakeJobNotifier,
    *,
    engine: Any = None,  # noqa: ANN401
    classifier: Any = None,  # noqa: ANN401
    extraction: Any = None,  # noqa: ANN401
    queue_storyline_check: Any = None,  # noqa: ANN401
    storyline_repo: Any = None,  # noqa: ANN401
) -> WorkerContext:
    """Build a WorkerContext with only the fields the storyline
    workers touch. Other fields are set to placeholder objects that
    will blow up if anything else tries to read them (intentional —
    if they get read, the test should fail loudly)."""
    sentinel = object()
    ctx = WorkerContext(
        jobs=jobs,
        notifier=notifier,  # type: ignore[arg-type]
        extraction=extraction if extraction is not None else sentinel,  # type: ignore[arg-type]
        reembedder=sentinel,  # type: ignore[arg-type]
        mood_backfill=sentinel,  # type: ignore[arg-type]
        mood_scoring=sentinel,  # type: ignore[arg-type]
        entries=sentinel,  # type: ignore[arg-type]
        ingestion=None,
        pop_pending_images=lambda _: [],
        pop_pending_audio=lambda _: [],
        queue_post_ingestion_jobs=lambda *_a, **_k: {},
        queue_storyline_extension_check=queue_storyline_check,
        storyline_engine=engine,
        storyline_extension_classifier=classifier,
        storyline_repository=storyline_repo,
    )
    return ctx


# ── JobRunner-level integration ─────────────────────────────────


class TestJobRunnerStorylineSubmit:
    def test_submit_update_refuses_when_engine_missing(
        self,
        factory: ConnectionFactory,
    ) -> None:
        runner = _build_minimal_runner(factory, engine=None)
        with pytest.raises(RuntimeError, match="not configured"):
            runner.submit_storyline_update(1, user_id=1)
        runner.shutdown(wait=True, cancel_futures=False)

    def test_submit_update_queues_when_engine_present(
        self,
        factory: ConnectionFactory,
    ) -> None:
        engine = _FakeStorylineEngine(update_result=UpdateResult(storyline_id=7))
        runner = _build_minimal_runner(factory, engine=engine)
        job = runner.submit_storyline_update(7, user_id=1)
        runner.shutdown(wait=True, cancel_futures=False)
        assert engine.update_calls == [7]
        finished = runner._jobs.get(job.id)  # type: ignore[attr-defined]
        assert finished is not None
        assert finished.status == "succeeded"

    def test_submit_rejects_bootstrap_plus_refresh(
        self,
        factory: ConnectionFactory,
    ) -> None:
        engine = _FakeStorylineEngine()
        runner = _build_minimal_runner(factory, engine=engine)
        try:
            with pytest.raises(ValueError, match="At most one"):
                runner.submit_storyline_update(
                    9, user_id=1, bootstrap=True, refresh_only=True,
                )
        finally:
            runner.shutdown(wait=True, cancel_futures=False)

    def test_submit_rejects_all_three_modes(
        self,
        factory: ConnectionFactory,
    ) -> None:
        engine = _FakeStorylineEngine()
        runner = _build_minimal_runner(factory, engine=engine)
        try:
            with pytest.raises(ValueError, match="At most one"):
                runner.submit_storyline_update(
                    9, user_id=1,
                    bootstrap=True, refresh_only=True, unpublish=True,
                )
        finally:
            runner.shutdown(wait=True, cancel_futures=False)

    def test_submit_bootstrap_persists_param_and_worker_bootstraps(
        self,
        factory: ConnectionFactory,
    ) -> None:
        engine = _FakeStorylineEngine(
            bootstrap_result=UpdateResult(storyline_id=20, chapter_count=3),
        )
        runner = _build_minimal_runner(factory, engine=engine)
        job = runner.submit_storyline_update(20, user_id=1, bootstrap=True)
        runner.shutdown(wait=True, cancel_futures=False)

        finished = runner._jobs.get(job.id)  # type: ignore[attr-defined]
        assert finished is not None
        assert finished.params["bootstrap"] is True
        assert engine.bootstrap_calls == [20]
        assert engine.update_calls == []

    def test_submit_default_omits_mode_params(
        self,
        factory: ConnectionFactory,
    ) -> None:
        engine = _FakeStorylineEngine(update_result=UpdateResult(storyline_id=21))
        runner = _build_minimal_runner(factory, engine=engine)
        job = runner.submit_storyline_update(21, user_id=1)
        runner.shutdown(wait=True, cancel_futures=False)

        finished = runner._jobs.get(job.id)  # type: ignore[attr-defined]
        assert finished is not None
        assert "bootstrap" not in finished.params
        assert "refresh_only" not in finished.params
        assert "unpublish" not in finished.params
        assert engine.update_calls == [21]

    def test_submit_extension_check_refuses_when_classifier_missing(
        self,
        factory: ConnectionFactory,
    ) -> None:
        runner = _build_minimal_runner(factory, classifier=None)
        with pytest.raises(RuntimeError, match="not configured"):
            runner.submit_storyline_extension_check(1, user_id=1)
        runner.shutdown(wait=True, cancel_futures=False)

    def test_maybe_queue_extension_check_creates_job_when_wired(
        self,
        factory: ConnectionFactory,
    ) -> None:
        """W1: the entity-extraction worker's trigger callable queues a
        real storyline_extension_check job when storylines are wired."""
        classifier = _FakeClassifier({})  # classify → [] (no updates)
        runner = _build_minimal_runner(factory, classifier=classifier)
        runner._maybe_queue_storyline_extension_check(5, 1)  # noqa: SLF001
        runner.shutdown(wait=True, cancel_futures=False)

        jobs = SQLiteJobRepository(factory)
        checks, _ = jobs.list_jobs(job_type="storyline_extension_check")
        assert len(checks) == 1
        assert checks[0].params["entry_id"] == 5
        assert checks[0].params["user_id"] == 1

    def test_maybe_queue_extension_check_noop_when_not_wired(
        self,
        factory: ConnectionFactory,
    ) -> None:
        """Storylines opt-out: the trigger is a safe no-op, no exception,
        no job — so non-storyline servers ingest normally."""
        runner = _build_minimal_runner(factory, classifier=None)
        runner._maybe_queue_storyline_extension_check(5, 1)  # noqa: SLF001
        runner.shutdown(wait=True, cancel_futures=False)

        jobs = SQLiteJobRepository(factory)
        checks, _ = jobs.list_jobs(job_type="storyline_extension_check")
        assert checks == []

    def test_maybe_queue_extension_check_noop_when_user_unknown(
        self,
        factory: ConnectionFactory,
    ) -> None:
        """No silent drop, but no job either: a user-less entry can't be
        scoped to a user's storylines, so we log and skip."""
        classifier = _FakeClassifier({})
        runner = _build_minimal_runner(factory, classifier=classifier)
        runner._maybe_queue_storyline_extension_check(5, None)  # noqa: SLF001
        runner.shutdown(wait=True, cancel_futures=False)

        jobs = SQLiteJobRepository(factory)
        checks, _ = jobs.list_jobs(job_type="storyline_extension_check")
        assert checks == []


def _build_minimal_runner(
    factory: ConnectionFactory,
    *,
    engine: Any = None,  # noqa: ANN401
    classifier: Any = None,  # noqa: ANN401
) -> JobRunner:
    """Construct a JobRunner with stub collaborators sufficient for
    the storyline submit_* paths. Other collaborators are
    placeholder objects that raise if touched."""
    class _StubExtraction:
        def reembed_entity_for_description(
            self, _eid: int, *, user_id: int,  # noqa: ARG002
        ) -> dict[str, Any]:
            return {}

    class _StubMood:
        def score_entry(self, *_a: Any, **_k: Any) -> int:  # noqa: ANN401
            return 0

    return JobRunner(
        job_repository=SQLiteJobRepository(factory),
        entity_extraction_service=_StubExtraction(),  # type: ignore[arg-type]
        mood_backfill_callable=lambda **_: None,
        mood_scoring_service=_StubMood(),  # type: ignore[arg-type]
        entry_repository=object(),  # type: ignore[arg-type]
        storyline_engine=engine,
        storyline_extension_classifier=classifier,
    )
