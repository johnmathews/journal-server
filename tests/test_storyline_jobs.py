"""Tests for the storylines job/integration layer (W5-W7).

* W5: ``submit_storyline_generation`` + the ``run_storyline_generation``
  worker — happy path, missing-service guard.
* W6: ``StorylineExtensionClassifier`` — entity overlap fast path,
  surface-form → LLM decider path, no-match short-circuit.
* W6 decider: ``_parse_decision`` extracts the verdict from a
  ``tool_use`` block; falls back to "maybe" on malformed response.
* W7: ``_queue_post_ingestion_jobs`` queues a storyline_extension_check
  job when the classifier is wired; skips silently when it isn't.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest

from journal.db.jobs_repository import SQLiteJobRepository
from journal.db.storyline_repository import SQLiteStorylineRepository
from journal.entitystore.store import SQLiteEntityStore
from journal.models import Entry
from journal.providers.storyline_extension_decider import (
    AnthropicStorylineExtensionDecider,
    ExtensionDecision,
    _parse_decision,
)
from journal.services.jobs.runner import JobRunner
from journal.services.jobs.workers import WorkerContext
from journal.services.jobs.workers.storyline_extension_check import (
    run_storyline_extension_check,
)
from journal.services.jobs.workers.storyline_generation import (
    run_storyline_generation,
)
from journal.services.storylines.extension import (
    ExtensionResult,
    StorylineExtensionClassifier,
)
from journal.services.storylines.service import GenerationResult

if TYPE_CHECKING:
    from journal.db.factory import ConnectionFactory


# ── Fakes ───────────────────────────────────────────────────────


class _FakeJobNotifier:
    def __init__(self) -> None:
        self.successes: list[tuple[str, Any]] = []
        self.failures: list[tuple[str, str]] = []

    def get_notify_strategy(self, _parent: str | None) -> str:
        return "individual"

    def notify_success(self, _user: int | None, job_type: str, payload: Any) -> None:  # noqa: ANN401
        self.successes.append((job_type, payload))

    def notify_failed(
        self, _user: int | None, job_type: str, msg: str, _exc: Exception | None = None,
    ) -> None:
        self.failures.append((job_type, msg))

    def try_pipeline_notification(
        self, _parent_job_id: str, _user: int | None,
    ) -> None:
        return None


class _FakeGenerationService:
    def __init__(self, result: GenerationResult) -> None:
        self._result = result
        self.calls: list[int] = []
        self.kwargs: list[dict[str, Any]] = []
        self.chapter_calls: list[int] = []
        self.chapter_kwargs: list[dict[str, Any]] = []
        self.resegment_calls: list[int] = []
        self.resegment_kwargs: list[dict[str, Any]] = []

    def regenerate(
        self,
        storyline_id: int,
        **kwargs: Any,  # noqa: ANN401
    ) -> GenerationResult:
        self.calls.append(storyline_id)
        self.kwargs.append(kwargs)
        return self._result

    def regenerate_chapter(
        self,
        chapter_id: int,
        **kwargs: Any,  # noqa: ANN401
    ) -> GenerationResult:
        self.chapter_calls.append(chapter_id)
        self.chapter_kwargs.append(kwargs)
        return self._result

    def resegment_storyline(
        self,
        storyline_id: int,
        **kwargs: Any,  # noqa: ANN401
    ) -> GenerationResult:
        self.resegment_calls.append(storyline_id)
        self.resegment_kwargs.append(kwargs)
        return self._result


class _FakeClassifier:
    def __init__(self, results_by_entry: dict[int, list[ExtensionResult]]) -> None:
        self._by_entry = results_by_entry
        self.calls: list[tuple[int, int]] = []

    def classify_for_entry(
        self, entry_id: int, user_id: int,
    ) -> list[ExtensionResult]:
        self.calls.append((entry_id, user_id))
        return self._by_entry.get(entry_id, [])


# ── W5: run_storyline_generation worker ─────────────────────────


@pytest.fixture
def job_ctx(
    factory: ConnectionFactory,
) -> tuple[SQLiteJobRepository, _FakeJobNotifier]:
    return SQLiteJobRepository(factory), _FakeJobNotifier()


class TestStorylineGenerationWorker:
    def test_happy_path_marks_succeeded(
        self,
        job_ctx: tuple[SQLiteJobRepository, _FakeJobNotifier],
    ) -> None:
        jobs, notifier = job_ctx
        svc = _FakeGenerationService(GenerationResult(
            storyline_id=42, entry_count=3,
            entity_mention_count=3, fts_fallback_count=0,
            narrative_citation_count=5, curation_citation_count=3,
            narrative_model="opus-fake", curation_model="haiku-fake",
        ))
        ctx = _build_minimal_ctx(jobs, notifier, generation=svc)

        job = jobs.create(
            "storyline_generation",
            {"storyline_id": 42, "user_id": 1},
            user_id=1,
        )
        run_storyline_generation(
            ctx, job.id,
            {"storyline_id": 42, "user_id": 1},
        )
        finished = jobs.get(job.id)
        assert finished is not None
        assert finished.status == "succeeded"
        assert finished.result is not None
        assert finished.result["entry_count"] == 3
        assert finished.result["narrative_citation_count"] == 5
        assert svc.calls == [42]
        # storyline_generation is too noisy for Pushover: this fires on
        # every entry that extends an active storyline. Failures still
        # notify; success does not. Mirrors storyline_extension_check.
        assert notifier.successes == []

    def test_passes_date_range_and_mode_through_to_service(
        self,
        job_ctx: tuple[SQLiteJobRepository, _FakeJobNotifier],
    ) -> None:
        """W6: when the job row carries start_date/end_date/mode the
        worker must forward them to ``service.regenerate(...)`` as
        kwargs. The validation layer already ensures only known keys
        land here; this test pins the runtime forwarding."""
        jobs, notifier = job_ctx
        svc = _FakeGenerationService(GenerationResult(storyline_id=11))
        ctx = _build_minimal_ctx(jobs, notifier, generation=svc)

        params = {
            "storyline_id": 11,
            "user_id": 1,
            "start_date": "2099-01-01",
            "end_date": "2099-01-31",
            "mode": "append",
        }
        job = jobs.create("storyline_generation", params, user_id=1)
        run_storyline_generation(ctx, job.id, params)

        finished = jobs.get(job.id)
        assert finished is not None
        assert finished.status == "succeeded"
        assert svc.calls == [11]
        assert svc.kwargs == [{
            "start_date": "2099-01-01",
            "end_date": "2099-01-31",
            "mode": "append",
        }]

    def test_no_extra_kwargs_when_params_absent(
        self,
        job_ctx: tuple[SQLiteJobRepository, _FakeJobNotifier],
    ) -> None:
        """A bare params dict (no date/mode keys) calls regenerate
        with no kwargs — preserving the legacy code path."""
        jobs, notifier = job_ctx
        svc = _FakeGenerationService(GenerationResult(storyline_id=12))
        ctx = _build_minimal_ctx(jobs, notifier, generation=svc)
        params = {"storyline_id": 12, "user_id": 1}
        job = jobs.create("storyline_generation", params, user_id=1)
        run_storyline_generation(ctx, job.id, params)
        assert svc.kwargs == [{}]

    def test_chapter_id_routes_to_regenerate_chapter(
        self,
        job_ctx: tuple[SQLiteJobRepository, _FakeJobNotifier],
    ) -> None:
        """When the payload carries a chapter_id the worker calls
        ``regenerate_chapter`` (forwarding only ``mode``) instead of
        the storyline-level ``regenerate``."""
        jobs, notifier = job_ctx
        svc = _FakeGenerationService(GenerationResult(storyline_id=12))
        ctx = _build_minimal_ctx(jobs, notifier, generation=svc)
        params = {
            "storyline_id": 12, "chapter_id": 7,
            "user_id": 1, "mode": "replace",
        }
        job = jobs.create("storyline_generation", params, user_id=1)
        run_storyline_generation(ctx, job.id, params)

        finished = jobs.get(job.id)
        assert finished is not None
        assert finished.status == "succeeded"
        # Routed to the chapter path, not the storyline path.
        assert svc.chapter_calls == [7]
        assert svc.chapter_kwargs == [{"mode": "replace"}]
        assert svc.calls == []

    def test_resegment_routes_to_resegment_storyline(
        self,
        job_ctx: tuple[SQLiteJobRepository, _FakeJobNotifier],
    ) -> None:
        """W4: when the payload carries ``resegment`` the worker calls
        ``resegment_storyline`` (forwarding ``override_locked``) instead
        of the storyline-level ``regenerate``."""
        jobs, notifier = job_ctx
        svc = _FakeGenerationService(GenerationResult(
            storyline_id=13, chapter_count=4,
        ))
        ctx = _build_minimal_ctx(jobs, notifier, generation=svc)
        params = {
            "storyline_id": 13,
            "user_id": 1,
            "resegment": True,
            "override_locked": True,
        }
        job = jobs.create("storyline_generation", params, user_id=1)
        run_storyline_generation(ctx, job.id, params)

        finished = jobs.get(job.id)
        assert finished is not None
        assert finished.status == "succeeded"
        # Routed to resegment, not regenerate / regenerate_chapter.
        assert svc.resegment_calls == [13]
        assert svc.resegment_kwargs == [{"override_locked": True}]
        assert svc.calls == []
        assert svc.chapter_calls == []
        # chapter_count surfaces in the summary.
        assert finished.result is not None
        assert finished.result["chapter_count"] == 4

    def test_resegment_defaults_override_locked_false(
        self,
        job_ctx: tuple[SQLiteJobRepository, _FakeJobNotifier],
    ) -> None:
        """``resegment`` without ``override_locked`` forwards
        ``override_locked=False``."""
        jobs, notifier = job_ctx
        svc = _FakeGenerationService(GenerationResult(storyline_id=14))
        ctx = _build_minimal_ctx(jobs, notifier, generation=svc)
        params = {"storyline_id": 14, "user_id": 1, "resegment": True}
        job = jobs.create("storyline_generation", params, user_id=1)
        run_storyline_generation(ctx, job.id, params)

        assert svc.resegment_calls == [14]
        assert svc.resegment_kwargs == [{"override_locked": False}]
        assert svc.calls == []

    def test_no_resegment_still_calls_regenerate(
        self,
        job_ctx: tuple[SQLiteJobRepository, _FakeJobNotifier],
    ) -> None:
        """Default path (no ``resegment`` key) is byte-for-byte the old
        behavior: ``regenerate`` is called, ``resegment_storyline`` is
        never touched."""
        jobs, notifier = job_ctx
        svc = _FakeGenerationService(GenerationResult(storyline_id=15))
        ctx = _build_minimal_ctx(jobs, notifier, generation=svc)
        params = {"storyline_id": 15, "user_id": 1}
        job = jobs.create("storyline_generation", params, user_id=1)
        run_storyline_generation(ctx, job.id, params)

        assert svc.calls == [15]
        assert svc.resegment_calls == []

    def test_auto_split_forwarded_to_regenerate(
        self,
        job_ctx: tuple[SQLiteJobRepository, _FakeJobNotifier],
    ) -> None:
        """W5: when the payload carries ``auto_split`` the worker forwards
        ``auto_split=True`` into ``service.regenerate`` (storyline-level
        path only)."""
        jobs, notifier = job_ctx
        svc = _FakeGenerationService(GenerationResult(storyline_id=16))
        ctx = _build_minimal_ctx(jobs, notifier, generation=svc)
        params = {"storyline_id": 16, "user_id": 1, "auto_split": True}
        job = jobs.create("storyline_generation", params, user_id=1)
        run_storyline_generation(ctx, job.id, params)

        assert svc.calls == [16]
        assert svc.kwargs == [{"auto_split": True}]
        assert svc.resegment_calls == []
        assert svc.chapter_calls == []

    def test_auto_split_ignored_on_chapter_path(
        self,
        job_ctx: tuple[SQLiteJobRepository, _FakeJobNotifier],
    ) -> None:
        """``auto_split`` is only meaningful for the storyline-level
        default path. A chapter-scoped run ignores it (no error, not
        forwarded)."""
        jobs, notifier = job_ctx
        svc = _FakeGenerationService(GenerationResult(storyline_id=17))
        ctx = _build_minimal_ctx(jobs, notifier, generation=svc)
        params = {
            "storyline_id": 17, "chapter_id": 3,
            "user_id": 1, "auto_split": True,
        }
        job = jobs.create("storyline_generation", params, user_id=1)
        run_storyline_generation(ctx, job.id, params)

        assert svc.chapter_calls == [3]
        assert svc.chapter_kwargs == [{}]
        assert svc.calls == []

    def test_missing_service_marks_failed(
        self,
        job_ctx: tuple[SQLiteJobRepository, _FakeJobNotifier],
    ) -> None:
        jobs, notifier = job_ctx
        ctx = _build_minimal_ctx(jobs, notifier, generation=None)
        job = jobs.create(
            "storyline_generation", {"storyline_id": 1, "user_id": 1},
            user_id=1,
        )
        run_storyline_generation(
            ctx, job.id, {"storyline_id": 1, "user_id": 1},
        )
        finished = jobs.get(job.id)
        assert finished is not None
        assert finished.status == "failed"
        assert "not configured" in (finished.error_message or "")


# ── W6: extension classifier ────────────────────────────────────


@pytest.fixture
def classifier_world(
    factory: ConnectionFactory,
) -> dict[str, Any]:
    """Seed user, entity, entry, mention; build a classifier."""
    conn = factory.get()
    cur = conn.execute(
        "INSERT INTO users (email, password_hash, display_name)"
        " VALUES (?, ?, ?)", ("c@x.test", "x", "C"),
    )
    user_id = cur.lastrowid
    conn.commit()

    store = SQLiteEntityStore(factory)
    entity = store.create_entity(
        entity_type="activity", canonical_name="Running",
        description="", first_seen="2026-02-15", user_id=user_id,
    )
    storyline_repo = SQLiteStorylineRepository(factory)
    storyline = storyline_repo.create_storyline(
        user_id=user_id, entity_ids=[entity.id], name="Running",
    )

    # An entry that mentions Running via entity_mentions (stage 1)
    body_overlap = "I ran 5km today and it felt great."
    cur = conn.execute(
        "INSERT INTO entries"
        " (entry_date, source_type, raw_text, final_text,"
        "  word_count, user_id) VALUES (?, ?, ?, ?, ?, ?)",
        ("2026-03-01", "text", body_overlap, body_overlap, 8, user_id),
    )
    entry_overlap_id = cur.lastrowid
    conn.execute(
        "INSERT INTO entity_mentions"
        " (entity_id, entry_id, quote, confidence, extraction_run_id)"
        " VALUES (?, ?, ?, ?, ?)",
        (entity.id, entry_overlap_id, body_overlap, 0.95, "r-1"),
    )

    # An entry that contains the surface form "Running" but has no
    # entity_mentions linking it (stage 2 path)
    body_surface = "Running is fun. I do it often."
    cur = conn.execute(
        "INSERT INTO entries"
        " (entry_date, source_type, raw_text, final_text,"
        "  word_count, user_id) VALUES (?, ?, ?, ?, ?, ?)",
        ("2026-03-02", "text", body_surface, body_surface, 7, user_id),
    )
    entry_surface_id = cur.lastrowid

    # An entry with no mention at all (stage 3 / no_match)
    body_nope = "Today I baked sourdough bread."
    cur = conn.execute(
        "INSERT INTO entries"
        " (entry_date, source_type, raw_text, final_text,"
        "  word_count, user_id) VALUES (?, ?, ?, ?, ?, ?)",
        ("2026-03-03", "text", body_nope, body_nope, 5, user_id),
    )
    entry_nope_id = cur.lastrowid
    conn.commit()

    # Build entry repo helpers — load real Entry objects
    class _MiniEntryRepo:
        def get_entry(self, eid: int) -> Entry | None:
            row = conn.execute(
                "SELECT * FROM entries WHERE id = ?", (eid,),
            ).fetchone()
            if row is None:
                return None
            return Entry(
                id=row["id"], entry_date=row["entry_date"],
                source_type=row["source_type"],
                raw_text=row["raw_text"],
                final_text=row["final_text"] or "",
                word_count=row["word_count"] or 0,
                user_id=row["user_id"] or 0,
            )

        # Used by the generation service, not the classifier; here
        # so the entry repo is interface-complete for any consumer
        # that might pick it up later.
        def search_text(self, **_: Any) -> list[Any]:  # noqa: ANN401
            return []

    @dataclass
    class _CannedDecider:
        verdict: ExtensionDecision
        calls: list[dict[str, Any]] = field(default_factory=list)
        model: str = "haiku-fake"

        def decide(self, **kwargs: Any) -> ExtensionDecision:  # noqa: ANN401
            self.calls.append(kwargs)
            return self.verdict

    decider = _CannedDecider(
        verdict=ExtensionDecision(
            decision="yes", reasoning="Surface form matches.",
            model_used="haiku-fake",
        ),
    )
    classifier = StorylineExtensionClassifier(
        entity_store=store,
        entry_repository=_MiniEntryRepo(),  # type: ignore[arg-type]
        storyline_repository=storyline_repo,
        decider=decider,
    )
    return {
        "user_id": user_id,
        "storyline_id": storyline.id,
        "entity_id": entity.id,
        "entry_overlap_id": entry_overlap_id,
        "entry_surface_id": entry_surface_id,
        "entry_nope_id": entry_nope_id,
        "classifier": classifier,
        "decider": decider,
        "storyline_repo": storyline_repo,
    }


class TestExtensionClassifier:
    def test_entity_overlap_yields_yes_without_llm(
        self, classifier_world: dict[str, Any],
    ) -> None:
        results = classifier_world["classifier"].classify_for_entry(
            entry_id=classifier_world["entry_overlap_id"],
            user_id=classifier_world["user_id"],
        )
        assert len(results) == 1
        assert results[0].decision == "yes"
        assert results[0].stage == "entity_overlap"
        # Decider was not called
        assert classifier_world["decider"].calls == []
        # last_extension_check_at recorded
        s = classifier_world["storyline_repo"].get_storyline(
            classifier_world["storyline_id"],
        )
        assert s is not None
        assert s.last_extension_check_at is not None

    def test_surface_form_invokes_decider(
        self, classifier_world: dict[str, Any],
    ) -> None:
        results = classifier_world["classifier"].classify_for_entry(
            entry_id=classifier_world["entry_surface_id"],
            user_id=classifier_world["user_id"],
        )
        assert len(results) == 1
        assert results[0].stage == "surface_form_llm"
        assert results[0].decision == "yes"  # canned verdict
        assert len(classifier_world["decider"].calls) == 1

    def test_no_match_short_circuits(
        self, classifier_world: dict[str, Any],
    ) -> None:
        results = classifier_world["classifier"].classify_for_entry(
            entry_id=classifier_world["entry_nope_id"],
            user_id=classifier_world["user_id"],
        )
        assert len(results) == 1
        assert results[0].decision == "no"
        assert results[0].stage == "no_match"
        assert classifier_world["decider"].calls == []


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


# ── W7: extension-check worker + ingestion hook ────────────────


class TestExtensionCheckWorker:
    def test_yes_decisions_queue_regenerations(
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
        ctx = _build_minimal_ctx(jobs, notifier, classifier=classifier)
        regen_calls: list[int] = []

        def fake_regen(storyline_id: int, **_: Any) -> Any:  # noqa: ANN401
            regen_calls.append(storyline_id)
            return type("J", (), {"id": f"regen-{storyline_id}"})()

        job = jobs.create(
            "storyline_extension_check",
            {"entry_id": 42, "user_id": 1}, user_id=1,
        )
        run_storyline_extension_check(
            ctx, job.id,
            {"entry_id": 42, "user_id": 1},
            fake_regen,
        )

        finished = jobs.get(job.id)
        assert finished is not None
        assert finished.status == "succeeded"
        assert regen_calls == [1]  # only the "yes" got a regen
        assert finished.result is not None
        classifications = finished.result["classifications"]
        decisions = sorted(c["decision"] for c in classifications)
        assert decisions == ["maybe", "no", "yes"]

    def test_yes_decisions_queue_regenerations_with_auto_split(
        self,
        job_ctx: tuple[SQLiteJobRepository, _FakeJobNotifier],
    ) -> None:
        """W5: the ingest path opts into auto-split — the extension-check
        worker queues regenerations with ``auto_split=True`` so an
        over-budget open chapter is re-segmented automatically."""
        jobs, notifier = job_ctx
        classifier = _FakeClassifier({
            42: [
                ExtensionResult(
                    storyline_id=1, decision="yes",
                    reasoning="ok", stage="entity_overlap",
                ),
            ],
        })
        ctx = _build_minimal_ctx(jobs, notifier, classifier=classifier)
        regen_kwargs: list[dict[str, Any]] = []

        def fake_regen(storyline_id: int, **kwargs: Any) -> Any:  # noqa: ANN401, ARG001
            regen_kwargs.append(kwargs)
            return type("J", (), {"id": "regen"})()

        job = jobs.create(
            "storyline_extension_check",
            {"entry_id": 42, "user_id": 1}, user_id=1,
        )
        run_storyline_extension_check(
            ctx, job.id,
            {"entry_id": 42, "user_id": 1},
            fake_regen,
        )

        assert len(regen_kwargs) == 1
        assert regen_kwargs[0].get("auto_split") is True
        assert regen_kwargs[0].get("user_id") == 1

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


# ── _build_minimal_ctx helper ───────────────────────────────────


def _build_minimal_ctx(
    jobs: SQLiteJobRepository,
    notifier: _FakeJobNotifier,
    *,
    generation: Any = None,  # noqa: ANN401
    classifier: Any = None,  # noqa: ANN401
) -> WorkerContext:
    """Build a WorkerContext with only the fields the storyline
    workers touch. Other fields are set to placeholder objects that
    will blow up if anything else tries to read them (intentional —
    if they get read, the test should fail loudly)."""
    sentinel = object()
    ctx = WorkerContext(
        jobs=jobs,
        notifier=notifier,  # type: ignore[arg-type]
        extraction=sentinel,  # type: ignore[arg-type]
        reembedder=sentinel,  # type: ignore[arg-type]
        mood_backfill=sentinel,  # type: ignore[arg-type]
        mood_scoring=sentinel,  # type: ignore[arg-type]
        entries=sentinel,  # type: ignore[arg-type]
        ingestion=None,
        pop_pending_images=lambda _: [],
        pop_pending_audio=lambda _: [],
        queue_post_ingestion_jobs=lambda *_a, **_k: {},
        storyline_generation=generation,
        storyline_extension_classifier=classifier,
    )
    return ctx


# ── JobRunner-level integration ─────────────────────────────────


class TestJobRunnerStorylineSubmit:
    def test_submit_generation_refuses_when_service_missing(
        self,
        factory: ConnectionFactory,
    ) -> None:
        runner = _build_minimal_runner(factory, generation=None)
        with pytest.raises(RuntimeError, match="not configured"):
            runner.submit_storyline_generation(1, user_id=1)
        runner.shutdown(wait=True, cancel_futures=False)

    def test_submit_generation_queues_when_service_present(
        self,
        factory: ConnectionFactory,
    ) -> None:
        svc = _FakeGenerationService(GenerationResult(storyline_id=7))
        runner = _build_minimal_runner(factory, generation=svc)
        job = runner.submit_storyline_generation(7, user_id=1)
        runner.shutdown(wait=True, cancel_futures=False)
        # Run completed: storyline_id 7 was passed through
        assert svc.calls == [7]
        finished = runner._jobs.get(job.id)  # type: ignore[attr-defined]
        assert finished is not None
        assert finished.status == "succeeded"

    def test_submit_generation_with_date_range_and_mode_persists_params(
        self,
        factory: ConnectionFactory,
    ) -> None:
        """W6: submit_storyline_generation accepts start_date/end_date/
        mode kwargs and persists them into the job params dict, which
        the worker then forwards to the service."""
        svc = _FakeGenerationService(GenerationResult(storyline_id=8))
        runner = _build_minimal_runner(factory, generation=svc)
        job = runner.submit_storyline_generation(
            8,
            user_id=1,
            start_date="2099-04-01",
            end_date="2099-04-30",
            mode="append",
        )
        runner.shutdown(wait=True, cancel_futures=False)

        finished = runner._jobs.get(job.id)  # type: ignore[attr-defined]
        assert finished is not None
        assert finished.params["start_date"] == "2099-04-01"
        assert finished.params["end_date"] == "2099-04-30"
        assert finished.params["mode"] == "append"
        assert svc.calls == [8]
        assert svc.kwargs == [{
            "start_date": "2099-04-01",
            "end_date": "2099-04-30",
            "mode": "append",
        }]

    def test_submit_generation_rejects_invalid_mode(
        self,
        factory: ConnectionFactory,
    ) -> None:
        svc = _FakeGenerationService(GenerationResult(storyline_id=9))
        runner = _build_minimal_runner(factory, generation=svc)
        try:
            with pytest.raises(ValueError, match="Invalid mode"):
                runner.submit_storyline_generation(
                    9, user_id=1, mode="lolnope",
                )
        finally:
            runner.shutdown(wait=True, cancel_futures=False)

    def test_submit_resegment_persists_params_and_worker_resegments(
        self,
        factory: ConnectionFactory,
    ) -> None:
        """W4: ``resegment=True`` (plus ``override_locked=True``) is
        persisted into the job params and the worker re-segments."""
        svc = _FakeGenerationService(GenerationResult(storyline_id=20))
        runner = _build_minimal_runner(factory, generation=svc)
        job = runner.submit_storyline_generation(
            20, user_id=1, resegment=True, override_locked=True,
        )
        runner.shutdown(wait=True, cancel_futures=False)

        finished = runner._jobs.get(job.id)  # type: ignore[attr-defined]
        assert finished is not None
        assert finished.params["resegment"] is True
        assert finished.params["override_locked"] is True
        assert svc.resegment_calls == [20]
        assert svc.resegment_kwargs == [{"override_locked": True}]
        assert svc.calls == []

    def test_submit_resegment_false_omits_params(
        self,
        factory: ConnectionFactory,
    ) -> None:
        """The default (no resegment) keeps the params dict clean — no
        ``resegment``/``override_locked`` keys leak in — and calls
        regenerate."""
        svc = _FakeGenerationService(GenerationResult(storyline_id=21))
        runner = _build_minimal_runner(factory, generation=svc)
        job = runner.submit_storyline_generation(21, user_id=1)
        runner.shutdown(wait=True, cancel_futures=False)

        finished = runner._jobs.get(job.id)  # type: ignore[attr-defined]
        assert finished is not None
        assert "resegment" not in finished.params
        assert "override_locked" not in finished.params
        assert svc.calls == [21]
        assert svc.resegment_calls == []

    def test_submit_generation_auto_split_persists_param_and_forwards(
        self,
        factory: ConnectionFactory,
    ) -> None:
        """W5: ``submit_storyline_generation(auto_split=True)`` stores
        ``auto_split`` in the job params and the worker forwards it into
        ``regenerate``."""
        svc = _FakeGenerationService(GenerationResult(storyline_id=24))
        runner = _build_minimal_runner(factory, generation=svc)
        job = runner.submit_storyline_generation(24, user_id=1, auto_split=True)
        runner.shutdown(wait=True, cancel_futures=False)

        finished = runner._jobs.get(job.id)  # type: ignore[attr-defined]
        assert finished is not None
        assert finished.params["auto_split"] is True
        assert svc.calls == [24]
        assert svc.kwargs == [{"auto_split": True}]

    def test_submit_generation_auto_split_false_omits_param(
        self,
        factory: ConnectionFactory,
    ) -> None:
        """The default (no ``auto_split``) keeps the params dict clean and
        forwards no ``auto_split`` kwarg — manual refresh stays opt-out."""
        svc = _FakeGenerationService(GenerationResult(storyline_id=25))
        runner = _build_minimal_runner(factory, generation=svc)
        job = runner.submit_storyline_generation(25, user_id=1)
        runner.shutdown(wait=True, cancel_futures=False)

        finished = runner._jobs.get(job.id)  # type: ignore[attr-defined]
        assert finished is not None
        assert "auto_split" not in finished.params
        assert svc.kwargs == [{}]

    def test_submit_resegment_with_chapter_id_raises(
        self,
        factory: ConnectionFactory,
    ) -> None:
        """A chapter-scoped run cannot re-segment the whole storyline."""
        svc = _FakeGenerationService(GenerationResult(storyline_id=22))
        runner = _build_minimal_runner(factory, generation=svc)
        try:
            with pytest.raises(ValueError, match="resegment"):
                runner.submit_storyline_generation(
                    22, user_id=1, chapter_id=5, resegment=True,
                )
        finally:
            runner.shutdown(wait=True, cancel_futures=False)

    def test_submit_resegment_with_append_mode_raises(
        self,
        factory: ConnectionFactory,
    ) -> None:
        """resegment is incompatible with append mode."""
        svc = _FakeGenerationService(GenerationResult(storyline_id=23))
        runner = _build_minimal_runner(factory, generation=svc)
        try:
            with pytest.raises(ValueError, match="resegment"):
                runner.submit_storyline_generation(
                    23, user_id=1, mode="append", resegment=True,
                )
        finally:
            runner.shutdown(wait=True, cancel_futures=False)

    def test_submit_extension_check_refuses_when_classifier_missing(
        self,
        factory: ConnectionFactory,
    ) -> None:
        runner = _build_minimal_runner(factory, classifier=None)
        with pytest.raises(RuntimeError, match="not configured"):
            runner.submit_storyline_extension_check(1, user_id=1)
        runner.shutdown(wait=True, cancel_futures=False)


def _build_minimal_runner(
    factory: ConnectionFactory,
    *,
    generation: Any = None,  # noqa: ANN401
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
        storyline_generation_service=generation,
        storyline_extension_classifier=classifier,
    )
