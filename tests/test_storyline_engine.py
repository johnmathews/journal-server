"""Tests for :mod:`journal.services.storylines.engine`.

Covers the storylines-redesign engine (spec: docs/superpowers/specs/
2026-07-12-storylines-redesign-design.md), Task 6:

* ``update()`` — continue-vs-break steady state: no-op when nothing
  new, judge-driven draft/new-chapter/addendum split, publish guards
  (min entries, at-most-one-publish), failure handling for the judge
  and the narrator (draft/closure/addendum) that always leaves prior
  state untouched and records a warning.
* ``bootstrap()`` — full-history partition into chapters.
* ``refresh_draft()`` — re-narrate the draft without consulting the judge.
* ``SQLiteStorylineRepository.find_entries_mentioning`` — the sparse-recall
  LIKE fallback added in this task.

Uses a real ``SQLiteStorylineRepository``/``SQLiteEntityStore``/
``SQLiteEntryRepository`` over the ``factory`` fixture (cheap, and it
exercises the actual transactions) plus hand-rolled ``FakeNarrator``/
``FakeJudge`` doubles.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest

from journal.db.repository.store import SQLiteEntryRepository
from journal.db.storyline_repository import SQLiteStorylineRepository
from journal.entitystore.store import SQLiteEntityStore
from journal.providers.storyline_judge import (
    EntryAssignment,
    ExtensionJudgment,
    PartitionChapter,
    PartitionResult,
)
from journal.providers.storyline_narrator import NarrativeResult
from journal.services.storylines.engine import (
    StorylineEngine,
    StorylineEngineProtocol,
)
from journal.services.storylines.segments import citation_segment, text_segment

if TYPE_CHECKING:
    from collections.abc import Callable

    from journal.db.factory import ConnectionFactory
    from journal.models import DatedEntryExcerpt, Storyline
    from journal.providers.storyline_judge import EntryForJudge


# ── Fakes ─────────────────────────────────────────────────────────


@dataclass
class _NarratorCall:
    excerpts: list[DatedEntryExcerpt]
    storyline_name: str
    storyline_description: str
    mode: str
    prior_narrative: str | None


class FakeNarrator:
    """Records every call; returns a per-mode configurable result.

    ``results[mode]`` overrides the default for that mode.
    ``default_result`` is returned for any mode with no override —
    non-empty by default so tests that don't care about narration
    content don't have to configure it.
    """

    def __init__(self) -> None:
        self.calls: list[_NarratorCall] = []
        self.results: dict[str, NarrativeResult] = {}
        self.raises: dict[str, Exception] = {}
        self.default_result = NarrativeResult(
            segments=[text_segment("Narrated.")],
            source_entry_ids=[],
            citation_count=0,
            model_used="fake-narrator",
        )

    def generate_narrative(
        self,
        excerpts: list[DatedEntryExcerpt],
        storyline_name: str,
        storyline_description: str = "",
        *,
        mode: str = "draft",
        prior_narrative: str | None = None,
    ) -> NarrativeResult:
        self.calls.append(
            _NarratorCall(
                excerpts=list(excerpts),
                storyline_name=storyline_name,
                storyline_description=storyline_description,
                mode=mode,
                prior_narrative=prior_narrative,
            ),
        )
        if mode in self.raises:
            raise self.raises[mode]
        return self.results.get(mode, self.default_result)


@dataclass
class _JudgeCall:
    storyline_name: str
    draft_entries: list[EntryForJudge]
    new_entries: list[EntryForJudge]
    published_chapters: list[tuple[int, str, str, str]]


class FakeJudge:
    """Records every ``judge_extension``/``partition`` call; returns a
    single configured result for each (set ``.judgment``/
    ``.partition_result`` per test)."""

    def __init__(self) -> None:
        self.calls: list[_JudgeCall] = []
        self.partition_calls: list[list[Any]] = []
        self.judgment = ExtensionJudgment(
            assignments=[], draft_arc_complete=False, reasoning="",
        )
        self.partition_result = PartitionResult(chapters=[])
        #: When set, overrides ``partition_result`` — called with the
        #: per-window entries so a test can react to what's actually
        #: in each window (e.g. the bootstrap-windowing seeding tests).
        self.partition_result_fn: Callable[[list[EntryForJudge]], PartitionResult] | None = None

    def judge_extension(
        self,
        *,
        storyline_name: str,
        storyline_description: str,
        draft_narrative: str,
        draft_entries: list[EntryForJudge],
        new_entries: list[EntryForJudge],
        published_chapters: list[tuple[int, str, str, str]],
    ) -> ExtensionJudgment:
        self.calls.append(
            _JudgeCall(storyline_name, draft_entries, new_entries, published_chapters),
        )
        return self.judgment

    def partition(
        self,
        *,
        storyline_name: str,
        storyline_description: str,
        entries: list[EntryForJudge],
    ) -> PartitionResult:
        self.partition_calls.append(list(entries))
        if self.partition_result_fn is not None:
            return self.partition_result_fn(entries)
        return self.partition_result


# ── Fixtures ──────────────────────────────────────────────────────


@pytest.fixture
def seed_user(factory: ConnectionFactory) -> int:
    conn = factory.get()
    cursor = conn.execute(
        "INSERT INTO users (email, password_hash, display_name)"
        " VALUES (?, ?, ?)",
        ("test@example.com", "x", "Test User"),
    )
    conn.commit()
    user_id = cursor.lastrowid
    assert user_id is not None
    return user_id


@pytest.fixture
def entity_store(factory: ConnectionFactory) -> SQLiteEntityStore:
    return SQLiteEntityStore(factory)


@pytest.fixture
def entry_repository(factory: ConnectionFactory) -> SQLiteEntryRepository:
    return SQLiteEntryRepository(factory)


@pytest.fixture
def repo(factory: ConnectionFactory) -> SQLiteStorylineRepository:
    return SQLiteStorylineRepository(factory)


@pytest.fixture
def seed_entity(entity_store: SQLiteEntityStore, seed_user: int) -> int:
    entity = entity_store.create_entity(
        entity_type="activity",
        canonical_name="Running",
        description="The activity of running",
        first_seen="2026-02-15",
        user_id=seed_user,
    )
    return entity.id


@pytest.fixture
def storyline(
    repo: SQLiteStorylineRepository, seed_user: int, seed_entity: int,
) -> Storyline:
    return repo.create_storyline(seed_user, [seed_entity], "Running")


def _seed_entry_with_mention(
    factory: ConnectionFactory,
    user_id: int,
    entity_id: int,
    entry_date: str,
    text: str,
) -> int:
    conn = factory.get()
    cursor = conn.execute(
        "INSERT INTO entries"
        " (entry_date, source_type, raw_text, final_text, word_count, user_id)"
        " VALUES (?, 'text', ?, ?, ?, ?)",
        (entry_date, text, text, len(text.split()), user_id),
    )
    entry_id = cursor.lastrowid
    assert entry_id is not None
    conn.execute(
        "INSERT INTO entity_mentions"
        " (entity_id, entry_id, quote, confidence, extraction_run_id)"
        " VALUES (?, ?, ?, ?, ?)",
        (entity_id, entry_id, text, 0.95, "run-1"),
    )
    conn.commit()
    return entry_id


def _seed_many_entries(
    factory: ConnectionFactory, user_id: int, entity_id: int, count: int,
) -> list[int]:
    """Seed ``count`` entries mentioning ``entity_id``, one per day
    starting 2026-01-01, in chronological order — used by the
    bootstrap-windowing and update-batching tests, which need corpora
    larger than the window/batch size (50)."""
    from datetime import date, timedelta

    base = date(2026, 1, 1)
    return [
        _seed_entry_with_mention(
            factory, user_id, entity_id,
            (base + timedelta(days=i)).isoformat(),
            f"Entry number {i} about running.",
        )
        for i in range(count)
    ]


@pytest.fixture
def entry_ids(
    factory: ConnectionFactory, seed_user: int, seed_entity: int,
) -> list[int]:
    """Seed three entries mentioning the anchor entity, in date order."""
    rows = [
        ("2026-02-20", "I ran 5km today"),
        ("2026-03-15", "Long Saturday run."),
        ("2026-04-25", "I ran 11km yesterday."),
    ]
    return [
        _seed_entry_with_mention(factory, seed_user, seed_entity, entry_date, text)
        for entry_date, text in rows
    ]


@pytest.fixture
def fake_narrator() -> FakeNarrator:
    return FakeNarrator()


@pytest.fixture
def fake_judge() -> FakeJudge:
    return FakeJudge()


@pytest.fixture
def engine(
    entity_store: SQLiteEntityStore,
    entry_repository: SQLiteEntryRepository,
    repo: SQLiteStorylineRepository,
    fake_narrator: FakeNarrator,
    fake_judge: FakeJudge,
) -> StorylineEngine:
    return StorylineEngine(
        entity_store=entity_store,
        entry_repository=entry_repository,
        storyline_repository=repo,
        narrator=fake_narrator,
        judge=fake_judge,
        embedder=None,
        min_publish_entries=3,
    )


def test_engine_satisfies_protocol(engine: StorylineEngine) -> None:
    assert isinstance(engine, StorylineEngineProtocol)


# ── update(): continue ───────────────────────────────────────────


class TestUpdateContinue:
    def test_new_entries_join_draft_and_draft_renarrated(
        self,
        engine: StorylineEngine,
        repo: SQLiteStorylineRepository,
        storyline: Storyline,
        entry_ids: list[int],
        fake_narrator: FakeNarrator,
        fake_judge: FakeJudge,
    ) -> None:
        fake_judge.judgment = ExtensionJudgment(
            assignments=[EntryAssignment(entry_ids[0], "draft")],
            draft_arc_complete=False, reasoning="continues",
        )
        result = engine.update(storyline.id)
        draft = repo.get_draft(storyline.id)
        assert draft is not None
        assert entry_ids[0] in repo.chapter_entry_ids(draft.id)
        assert fake_narrator.calls[-1].mode == "draft"
        assert draft.segments  # narrative written
        assert result.published is None
        assert result.new_entry_count == 3

    def test_no_new_entries_is_a_noop(
        self, engine: StorylineEngine, storyline: Storyline, fake_judge: FakeJudge,
    ) -> None:
        result = engine.update(storyline.id)
        assert fake_judge.calls == []
        assert result.new_entry_count == 0

    def test_judge_failure_leaves_state_untouched_and_pending(
        self,
        engine: StorylineEngine,
        repo: SQLiteStorylineRepository,
        storyline: Storyline,
        entry_ids: list[int],
        fake_judge: FakeJudge,
    ) -> None:
        fake_judge.judgment = ExtensionJudgment([], False, "", failed=True)
        result = engine.update(storyline.id)
        assert result.warnings
        assert "judge" in result.warnings[0].lower()
        draft = repo.get_draft(storyline.id)
        assert draft is not None
        assert repo.chapter_entry_ids(draft.id) == []
        # candidates remain unassigned → retried next update
        second = engine.update(storyline.id)
        assert second.new_entry_count == 3

    def test_stale_pending_id_swept_even_when_nothing_else_new(
        self,
        engine: StorylineEngine,
        repo: SQLiteStorylineRepository,
        storyline: Storyline,
        entry_ids: list[int],
        fake_judge: FakeJudge,
    ) -> None:
        """A pending id that's already assigned to a chapter (persisted
        by a previous run whose clear step was skipped) is stale — it
        must be swept from the pending table at the start of the next
        update(), even on a run that finds nothing else new (i.e. the
        sweep can't be gated behind the "any new candidates" check)."""
        draft = repo.get_draft(storyline.id)
        assert draft is not None
        repo.add_entries_to_draft(draft.id, entry_ids)  # all 3 assigned
        repo.add_pending_entry(storyline.id, entry_ids[0])  # stale
        assert entry_ids[0] in repo.list_pending_entries(storyline.id)

        result = engine.update(storyline.id)

        assert entry_ids[0] not in repo.list_pending_entries(storyline.id)
        assert result.new_entry_count == 0
        assert fake_judge.calls == []  # no-op path still taken

    def test_narrator_failure_keeps_old_draft_narrative(
        self,
        engine: StorylineEngine,
        repo: SQLiteStorylineRepository,
        storyline: Storyline,
        entry_ids: list[int],
        fake_narrator: FakeNarrator,
        fake_judge: FakeJudge,
    ) -> None:
        draft = repo.get_draft(storyline.id)
        assert draft is not None
        repo.set_draft_narrative(
            draft.id,
            segments=[text_segment("Existing narrative.")],
            source_entry_ids=[], citation_count=0, model_used="prior-model",
            embedding=None,
        )
        fake_judge.judgment = ExtensionJudgment(
            assignments=[EntryAssignment(entry_ids[0], "draft")],
            draft_arc_complete=False, reasoning="continues",
        )
        fake_narrator.results["draft"] = NarrativeResult(segments=[], model_used="fake")

        result = engine.update(storyline.id)

        refreshed = repo.get_draft(storyline.id)
        assert refreshed is not None
        assert refreshed.segments == [{"kind": "text", "text": "Existing narrative."}]
        assert any("narrat" in w.lower() for w in result.warnings)
        # membership still applied even though narration failed
        assert entry_ids[0] in repo.chapter_entry_ids(refreshed.id)


# ── update(): batching (_JUDGE_BATCH) ────────────────────────────


class TestUpdateBatching:
    def test_60_new_entries_batches_50_and_defers_the_rest(
        self,
        engine: StorylineEngine,
        repo: SQLiteStorylineRepository,
        storyline: Storyline,
        factory: ConnectionFactory,
        seed_user: int,
        seed_entity: int,
        fake_judge: FakeJudge,
    ) -> None:
        seeded_ids = _seed_many_entries(factory, seed_user, seed_entity, 60)
        fake_judge.judgment = ExtensionJudgment(
            assignments=[EntryAssignment(eid, "draft") for eid in seeded_ids[:50]],
            draft_arc_complete=False, reasoning="continues",
        )

        result = engine.update(storyline.id)

        assert len(fake_judge.calls[-1].new_entries) == 50
        assert {e.entry_id for e in fake_judge.calls[-1].new_entries} == set(seeded_ids[:50])
        assert result.new_entry_count == 50
        assert any(
            "10" in w and "deferred" in w.lower() for w in result.warnings
        )
        draft = repo.get_draft(storyline.id)
        assert draft is not None
        assert sorted(repo.chapter_entry_ids(draft.id)) == sorted(seeded_ids[:50])

        # Second update() picks up the remaining 10 — nothing was lost.
        fake_judge.judgment = ExtensionJudgment(
            assignments=[EntryAssignment(eid, "draft") for eid in seeded_ids[50:]],
            draft_arc_complete=False, reasoning="continues",
        )
        second = engine.update(storyline.id)

        assert second.new_entry_count == 10
        assert {e.entry_id for e in fake_judge.calls[-1].new_entries} == set(seeded_ids[50:])
        draft = repo.get_draft(storyline.id)
        assert draft is not None
        assert sorted(repo.chapter_entry_ids(draft.id)) == sorted(seeded_ids)


# ── update(): publish ────────────────────────────────────────────


class TestUpdatePublish:
    def test_arc_complete_publishes_with_closure_title(
        self,
        engine: StorylineEngine,
        repo: SQLiteStorylineRepository,
        storyline: Storyline,
        entry_ids: list[int],
        fake_narrator: FakeNarrator,
        fake_judge: FakeJudge,
        factory: ConnectionFactory,
        seed_user: int,
        seed_entity: int,
    ) -> None:
        draft = repo.get_draft(storyline.id)
        assert draft is not None
        repo.add_entries_to_draft(draft.id, entry_ids)  # 3 members, meets the floor
        new_entry_id = _seed_entry_with_mention(
            factory, seed_user, seed_entity, "2026-05-10", "Ran a marathon!",
        )
        fake_judge.judgment = ExtensionJudgment(
            assignments=[EntryAssignment(new_entry_id, "new_chapter")],
            draft_arc_complete=True, reasoning="arc complete",
        )
        fake_narrator.results["closure"] = NarrativeResult(
            segments=[text_segment("It concluded.")],
            source_entry_ids=entry_ids, citation_count=3,
            title="The End of Winter", model_used="fake-narrator",
        )

        result = engine.update(storyline.id)

        chapters = repo.list_chapters(storyline.id)
        assert chapters[-2].state == "published"
        assert chapters[-2].title == "The End of Winter"
        assert chapters[-2].read_at is None  # unread!
        assert result.published is not None
        assert result.published.title == "The End of Winter"
        assert repo.chapter_entry_ids(chapters[-1].id)  # new draft got the new entry
        assert [c.mode for c in fake_narrator.calls] == ["closure", "draft"]

    def test_min_entries_guard_blocks_publish(
        self,
        engine: StorylineEngine,
        repo: SQLiteStorylineRepository,
        storyline: Storyline,
        entry_ids: list[int],
        fake_judge: FakeJudge,
    ) -> None:
        draft = repo.get_draft(storyline.id)
        assert draft is not None
        repo.add_entries_to_draft(draft.id, entry_ids[:1])  # only 1 member
        fake_judge.judgment = ExtensionJudgment(
            assignments=[EntryAssignment(entry_ids[1], "draft")],
            draft_arc_complete=True, reasoning="looks complete but too small",
        )

        result = engine.update(storyline.id)

        current_draft = repo.get_draft(storyline.id)
        assert current_draft is not None
        assert set(repo.chapter_entry_ids(current_draft.id)) == {
            entry_ids[0], entry_ids[1],
        }
        assert result.published is None
        assert not [c for c in repo.list_chapters(storyline.id) if c.state == "published"]
        assert any("min" in w.lower() for w in result.warnings)

    def test_at_most_one_publish_per_run(
        self,
        engine: StorylineEngine,
        repo: SQLiteStorylineRepository,
        storyline: Storyline,
        entry_ids: list[int],
        fake_narrator: FakeNarrator,
        fake_judge: FakeJudge,
        factory: ConnectionFactory,
        seed_user: int,
        seed_entity: int,
    ) -> None:
        draft = repo.get_draft(storyline.id)
        assert draft is not None
        repo.add_entries_to_draft(draft.id, entry_ids)
        new_entry_id = _seed_entry_with_mention(
            factory, seed_user, seed_entity, "2026-05-10", "Ran a marathon!",
        )
        fake_judge.judgment = ExtensionJudgment(
            assignments=[EntryAssignment(new_entry_id, "new_chapter")],
            draft_arc_complete=True, reasoning="arc complete AND more coming",
        )

        engine.update(storyline.id)

        chapters = repo.list_chapters(storyline.id)
        published = [c for c in chapters if c.state == "published"]
        assert len(published) == 1
        assert sum(1 for c in fake_narrator.calls if c.mode == "closure") == 1

    def test_closure_without_title_falls_back(
        self,
        engine: StorylineEngine,
        repo: SQLiteStorylineRepository,
        storyline: Storyline,
        entry_ids: list[int],
        fake_narrator: FakeNarrator,
        fake_judge: FakeJudge,
        factory: ConnectionFactory,
        seed_user: int,
        seed_entity: int,
    ) -> None:
        draft = repo.get_draft(storyline.id)
        assert draft is not None
        repo.add_entries_to_draft(draft.id, entry_ids)
        # A genuinely new entry so update() has something to judge —
        # entry_ids are already draft members, so on their own they
        # wouldn't trigger a judge call at all.
        _seed_entry_with_mention(
            factory, seed_user, seed_entity, "2026-05-10", "Ran a marathon!",
        )
        fake_judge.judgment = ExtensionJudgment(
            assignments=[], draft_arc_complete=True, reasoning="done",
        )
        fake_narrator.results["closure"] = NarrativeResult(
            segments=[text_segment("It concluded.")],
            source_entry_ids=entry_ids, citation_count=3, title=None,
            model_used="fake-narrator",
        )

        engine.update(storyline.id)

        published = [
            c for c in repo.list_chapters(storyline.id) if c.state == "published"
        ][-1]
        assert published.title == f"Chapter {draft.seq}"

    def test_closure_exception_propagates_and_leaves_entries_pending(
        self,
        engine: StorylineEngine,
        repo: SQLiteStorylineRepository,
        storyline: Storyline,
        entry_ids: list[int],
        fake_narrator: FakeNarrator,
        fake_judge: FakeJudge,
        factory: ConnectionFactory,
        seed_user: int,
        seed_entity: int,
    ) -> None:
        """A narrator exception during closure is not caught anywhere in
        the engine, so it propagates out of update(). The binding
        failure policy requires that the pending-clear write never runs
        in that case — verify the about-to-publish id survives."""
        draft = repo.get_draft(storyline.id)
        assert draft is not None
        repo.add_entries_to_draft(draft.id, entry_ids)  # 3 members, meets the floor
        new_entry_id = _seed_entry_with_mention(
            factory, seed_user, seed_entity, "2026-05-10", "Ran a marathon!",
        )
        # Simulate this id already being tracked as pending from an
        # earlier partial run — the exact scenario the ordering fix
        # protects against.
        repo.add_pending_entry(storyline.id, new_entry_id)
        fake_judge.judgment = ExtensionJudgment(
            assignments=[EntryAssignment(new_entry_id, "new_chapter")],
            draft_arc_complete=True, reasoning="arc complete",
        )
        fake_narrator.raises["closure"] = RuntimeError("narrator exploded")

        with pytest.raises(RuntimeError, match="narrator exploded"):
            engine.update(storyline.id)

        # Nothing published, and the entry destined for the new chapter
        # is neither published nor silently dropped from pending.
        assert not [c for c in repo.list_chapters(storyline.id) if c.state == "published"]
        assert new_entry_id in repo.list_pending_entries(storyline.id)

    def test_publish_commits_but_new_draft_narration_empty(
        self,
        engine: StorylineEngine,
        repo: SQLiteStorylineRepository,
        storyline: Storyline,
        entry_ids: list[int],
        fake_narrator: FakeNarrator,
        fake_judge: FakeJudge,
        factory: ConnectionFactory,
        seed_user: int,
        seed_entity: int,
    ) -> None:
        """Closure succeeds and publish_draft commits the new chapter and
        the new draft's membership; the *subsequent* narration call for
        the new draft then returns empty segments. The published chapter
        must still exist with its title, the new draft must still carry
        the membership recorded by publish_draft, and a warning must be
        recorded — nothing about the earlier successful publish is undone."""
        draft = repo.get_draft(storyline.id)
        assert draft is not None
        repo.add_entries_to_draft(draft.id, entry_ids)  # 3 members, meets the floor
        new_entry_id = _seed_entry_with_mention(
            factory, seed_user, seed_entity, "2026-05-10", "Ran a marathon!",
        )
        fake_judge.judgment = ExtensionJudgment(
            assignments=[EntryAssignment(new_entry_id, "new_chapter")],
            draft_arc_complete=True, reasoning="arc complete",
        )
        fake_narrator.results["closure"] = NarrativeResult(
            segments=[text_segment("It concluded.")],
            source_entry_ids=entry_ids, citation_count=3,
            title="The End of Winter", model_used="fake-narrator",
        )
        fake_narrator.results["draft"] = NarrativeResult(segments=[], model_used="fake")

        result = engine.update(storyline.id)

        chapters = repo.list_chapters(storyline.id)
        published = [c for c in chapters if c.state == "published"]
        assert len(published) == 1
        assert published[0].title == "The End of Winter"
        new_draft = repo.get_draft(storyline.id)
        assert new_draft is not None
        assert new_draft.id != published[0].id
        assert new_entry_id in repo.chapter_entry_ids(new_draft.id)
        assert new_draft.segments == []
        assert any("after publish" in w.lower() for w in result.warnings)

    def test_closure_empty_defers_publish_and_folds_new_into_draft(
        self,
        engine: StorylineEngine,
        repo: SQLiteStorylineRepository,
        storyline: Storyline,
        entry_ids: list[int],
        fake_narrator: FakeNarrator,
        fake_judge: FakeJudge,
        factory: ConnectionFactory,
        seed_user: int,
        seed_entity: int,
    ) -> None:
        draft = repo.get_draft(storyline.id)
        assert draft is not None
        repo.add_entries_to_draft(draft.id, entry_ids)
        new_entry_id = _seed_entry_with_mention(
            factory, seed_user, seed_entity, "2026-05-10", "Ran a marathon!",
        )
        fake_judge.judgment = ExtensionJudgment(
            assignments=[EntryAssignment(new_entry_id, "new_chapter")],
            draft_arc_complete=True, reasoning="arc complete",
        )
        fake_narrator.results["closure"] = NarrativeResult(segments=[], model_used="fake")

        result = engine.update(storyline.id)

        assert result.published is None
        assert not [c for c in repo.list_chapters(storyline.id) if c.state == "published"]
        current_draft = repo.get_draft(storyline.id)
        assert current_draft is not None
        assert new_entry_id in repo.chapter_entry_ids(current_draft.id)
        assert any("closure" in w.lower() for w in result.warnings)


# ── update(): addenda ────────────────────────────────────────────


class TestAddenda:
    def test_backdated_entry_becomes_addendum_and_unreads_chapter(
        self,
        engine: StorylineEngine,
        repo: SQLiteStorylineRepository,
        storyline: Storyline,
        entry_ids: list[int],
        fake_narrator: FakeNarrator,
        fake_judge: FakeJudge,
        factory: ConnectionFactory,
    ) -> None:
        draft = repo.get_draft(storyline.id)
        assert draft is not None
        repo.add_entries_to_draft(draft.id, entry_ids[:2])
        published, _new_draft = repo.publish_draft(
            storyline.id,
            title="Winter Running",
            segments=[text_segment("Winter running arc.")],
            source_entry_ids=entry_ids[:2], citation_count=2, model_used="fake",
            new_draft_entry_ids=[],
        )
        repo.set_read(published.id, True)

        fake_judge.judgment = ExtensionJudgment(
            assignments=[
                EntryAssignment(entry_ids[2], "published_chapter", published.id),
            ],
            draft_arc_complete=False, reasoning="backdated entry",
        )
        fake_narrator.results["addendum"] = NarrativeResult(
            segments=[text_segment("A late addition.")],
            source_entry_ids=[entry_ids[2]], citation_count=1,
            model_used="fake-narrator",
        )

        result = engine.update(storyline.id)

        updated = repo.get_chapter(published.id)
        assert updated is not None
        assert updated.addenda
        assert updated.addenda[-1]["entry_ids"] == [entry_ids[2]]
        assert updated.read_at is None
        assert result.addenda_chapter_ids == [published.id]
        addendum_calls = [c for c in fake_narrator.calls if c.mode == "addendum"]
        assert len(addendum_calls) == 1
        assert addendum_calls[0].prior_narrative == "Winter running arc."

        conn = factory.get()
        row = conn.execute(
            "SELECT added_late FROM storyline_chapter_entries"
            " WHERE chapter_id = ? AND entry_id = ?",
            (published.id, entry_ids[2]),
        ).fetchone()
        assert row is not None
        assert row["added_late"] == 1

    def test_addendum_narration_failure_leaves_entries_pending(
        self,
        engine: StorylineEngine,
        repo: SQLiteStorylineRepository,
        storyline: Storyline,
        entry_ids: list[int],
        fake_narrator: FakeNarrator,
        fake_judge: FakeJudge,
    ) -> None:
        draft = repo.get_draft(storyline.id)
        assert draft is not None
        repo.add_entries_to_draft(draft.id, entry_ids[:2])
        published, _new_draft = repo.publish_draft(
            storyline.id,
            title="Winter Running",
            segments=[text_segment("Winter running arc.")],
            source_entry_ids=entry_ids[:2], citation_count=2, model_used="fake",
            new_draft_entry_ids=[],
        )
        fake_judge.judgment = ExtensionJudgment(
            assignments=[
                EntryAssignment(entry_ids[2], "published_chapter", published.id),
            ],
            draft_arc_complete=False, reasoning="backdated entry",
        )
        fake_narrator.results["addendum"] = NarrativeResult(segments=[], model_used="fake")

        result = engine.update(storyline.id)

        updated = repo.get_chapter(published.id)
        assert updated is not None
        assert updated.addenda == []
        assert any("addendum" in w.lower() for w in result.warnings)
        # entry_ids[2] never landed anywhere → still surfaces as new next run
        engine.update(storyline.id)
        assert entry_ids[2] in [c.entry_id for c in fake_judge.calls[-1].new_entries]

    def test_addendum_with_citation_only_prior_is_treated_as_failure(
        self,
        engine: StorylineEngine,
        repo: SQLiteStorylineRepository,
        storyline: Storyline,
        entry_ids: list[int],
        fake_narrator: FakeNarrator,
        fake_judge: FakeJudge,
    ) -> None:
        """A published chapter whose segments are citation-only (no text
        segments) has an empty ``_join_text`` — the narrator's own
        ``mode="addendum"`` guard raises ``ValueError`` on a blank
        ``prior_narrative``. The engine must catch this upstream (never
        call the narrator) and treat it as an ordinary addendum
        failure: a warning, entries left pending, no crash."""
        draft = repo.get_draft(storyline.id)
        assert draft is not None
        repo.add_entries_to_draft(draft.id, entry_ids[:2])
        published, _new_draft = repo.publish_draft(
            storyline.id,
            title="Winter Running",
            segments=[citation_segment(entry_ids[0], "some quoted text")],
            source_entry_ids=entry_ids[:2], citation_count=1, model_used="fake",
            new_draft_entry_ids=[],
        )
        fake_judge.judgment = ExtensionJudgment(
            assignments=[
                EntryAssignment(entry_ids[2], "published_chapter", published.id),
            ],
            draft_arc_complete=False, reasoning="backdated entry",
        )

        result = engine.update(storyline.id)  # must not raise

        updated = repo.get_chapter(published.id)
        assert updated is not None
        assert updated.addenda == []
        assert any("narrative text" in w.lower() for w in result.warnings)
        assert not any(c.mode == "addendum" for c in fake_narrator.calls)
        # entry_ids[2] never landed anywhere → still surfaces as new next run
        engine.update(storyline.id)
        assert entry_ids[2] in [c.entry_id for c in fake_judge.calls[-1].new_entries]


# ── bootstrap() ──────────────────────────────────────────────────


class TestBootstrap:
    def test_partitions_and_publishes_all_but_last(
        self,
        engine: StorylineEngine,
        repo: SQLiteStorylineRepository,
        storyline: Storyline,
        entry_ids: list[int],
        fake_narrator: FakeNarrator,
        fake_judge: FakeJudge,
    ) -> None:
        fake_judge.partition_result = PartitionResult(
            chapters=[
                PartitionChapter([entry_ids[0], entry_ids[1]], "The Build-Up"),
                PartitionChapter([entry_ids[2]], "Now"),
            ],
        )
        fake_narrator.results["closure"] = NarrativeResult(
            segments=[text_segment("Build-up concluded.")],
            source_entry_ids=entry_ids[:2], citation_count=2,
            title="The Build-Up", model_used="fake-narrator",
        )

        result = engine.bootstrap(storyline.id)

        chapters = repo.list_chapters(storyline.id)
        assert [c.state for c in chapters] == ["published", "draft"]
        assert chapters[0].read_at is None  # NEW storyline: unread is correct
        assert chapters[0].title == "The Build-Up"
        assert repo.chapter_entry_ids(chapters[0].id) == entry_ids[:2]
        assert repo.chapter_entry_ids(chapters[1].id) == entry_ids[2:]
        assert result.chapter_count == 2

    def test_bootstrap_mark_read_flag(
        self,
        engine: StorylineEngine,
        repo: SQLiteStorylineRepository,
        storyline: Storyline,
        entry_ids: list[int],
        fake_judge: FakeJudge,
    ) -> None:
        fake_judge.partition_result = PartitionResult(
            chapters=[
                PartitionChapter([entry_ids[0], entry_ids[1]], "The Build-Up"),
                PartitionChapter([entry_ids[2]], "Now"),
            ],
        )

        engine.bootstrap(storyline.id, mark_read=True)

        published = [
            c for c in repo.list_chapters(storyline.id) if c.state == "published"
        ][0]
        assert published.read_at is not None

    def test_partition_failure_makes_no_writes(
        self,
        engine: StorylineEngine,
        repo: SQLiteStorylineRepository,
        storyline: Storyline,
        entry_ids: list[int],
        fake_judge: FakeJudge,
    ) -> None:
        fake_judge.partition_result = PartitionResult(chapters=[], failed=True)
        before = [(c.id, c.state) for c in repo.list_chapters(storyline.id)]

        result = engine.bootstrap(storyline.id)

        after = [(c.id, c.state) for c in repo.list_chapters(storyline.id)]
        assert after == before
        assert any("partition" in w.lower() for w in result.warnings)

    def test_narration_failure_aborts_before_any_write(
        self,
        engine: StorylineEngine,
        repo: SQLiteStorylineRepository,
        storyline: Storyline,
        entry_ids: list[int],
        fake_narrator: FakeNarrator,
        fake_judge: FakeJudge,
    ) -> None:
        fake_judge.partition_result = PartitionResult(
            chapters=[
                PartitionChapter([entry_ids[0], entry_ids[1]], "The Build-Up"),
                PartitionChapter([entry_ids[2]], "Now"),
            ],
        )
        fake_narrator.results["closure"] = NarrativeResult(segments=[], model_used="fake")
        before = [(c.id, c.state) for c in repo.list_chapters(storyline.id)]

        result = engine.bootstrap(storyline.id)

        after = [(c.id, c.state) for c in repo.list_chapters(storyline.id)]
        assert after == before
        assert any("narrat" in w.lower() for w in result.warnings)


class TestBootstrapWindowing:
    """Overlapping-window partitioning (spec §2 Flow 2, _PARTITION_WINDOW=50)."""

    def test_large_corpus_partitions_in_overlapping_windows(
        self,
        engine: StorylineEngine,
        repo: SQLiteStorylineRepository,
        storyline: Storyline,
        factory: ConnectionFactory,
        seed_user: int,
        seed_entity: int,
        fake_narrator: FakeNarrator,
        fake_judge: FakeJudge,
    ) -> None:
        seeded_ids = _seed_many_entries(factory, seed_user, seed_entity, 120)

        def splitting_partition(entries: list[EntryForJudge]) -> PartitionResult:
            """Deterministic fake partitioner: everything but the last
            entry in the window becomes one chapter, the last entry
            becomes a second, single-entry chapter — the one that
            carries forward to seed the next window."""
            ids = [e.entry_id for e in entries]
            if len(ids) == 1:
                return PartitionResult(chapters=[PartitionChapter(ids, "Solo")])
            return PartitionResult(
                chapters=[
                    PartitionChapter(ids[:-1], "Bulk"),
                    PartitionChapter(ids[-1:], "Tail"),
                ],
            )

        fake_judge.partition_result_fn = splitting_partition
        fake_narrator.results["closure"] = NarrativeResult(
            segments=[text_segment("Closed.")], model_used="fake", title="T",
        )

        engine.bootstrap(storyline.id)

        # 120 entries / window 50 → 3 windows, so ≥3 partition calls.
        assert len(fake_judge.partition_calls) >= 3

        # Seeding: the carried (last) chapter of window N is exactly
        # window N+1's leading entry.
        for call_n, call_n1 in zip(
            fake_judge.partition_calls, fake_judge.partition_calls[1:], strict=False,
        ):
            assert call_n1[0].entry_id == call_n[-1].entry_id

        # Every entry id is covered exactly once across the final
        # chapters actually written — nothing lost, nothing duplicated.
        chapters = repo.list_chapters(storyline.id)
        written_ids = [eid for c in chapters for eid in repo.chapter_entry_ids(c.id)]
        assert sorted(written_ids) == sorted(seeded_ids)
        assert len(written_ids) == len(set(written_ids))
        assert chapters[-1].state == "draft"
        assert all(c.state == "published" for c in chapters[:-1])

    def test_small_corpus_uses_a_single_partition_call(
        self,
        engine: StorylineEngine,
        repo: SQLiteStorylineRepository,
        storyline: Storyline,
        entry_ids: list[int],
        fake_judge: FakeJudge,
    ) -> None:
        fake_judge.partition_result = PartitionResult(
            chapters=[PartitionChapter(entry_ids, "One Chapter")],
        )

        engine.bootstrap(storyline.id)

        assert len(fake_judge.partition_calls) == 1
        chapters = repo.list_chapters(storyline.id)
        assert [c.state for c in chapters] == ["draft"]
        assert repo.chapter_entry_ids(chapters[0].id) == entry_ids


# ── refresh_draft() ──────────────────────────────────────────────


class TestRefresh:
    def test_refresh_renarrates_draft_members_only(
        self,
        engine: StorylineEngine,
        repo: SQLiteStorylineRepository,
        storyline: Storyline,
        entry_ids: list[int],
        fake_narrator: FakeNarrator,
        fake_judge: FakeJudge,
    ) -> None:
        draft = repo.get_draft(storyline.id)
        assert draft is not None
        repo.add_entries_to_draft(draft.id, entry_ids[:2])

        result = engine.refresh_draft(storyline.id)

        assert fake_judge.calls == []
        assert fake_narrator.calls[-1].mode == "draft"
        assert {e.entry_id for e in fake_narrator.calls[-1].excerpts} == set(entry_ids[:2])
        assert result.published is None


# ── find_entries_mentioning (sparse-recall LIKE fallback) ────────


class TestFindEntriesMentioning:
    def test_like_scan_matches_and_is_parameterised(
        self,
        repo: SQLiteStorylineRepository,
        factory: ConnectionFactory,
        seed_user: int,
    ) -> None:
        conn = factory.get()
        cursor = conn.execute(
            "INSERT INTO entries"
            " (entry_date, source_type, raw_text, final_text, word_count, user_id)"
            " VALUES (?, 'text', ?, ?, ?, ?)",
            ("2026-05-01", "Saw Alice at the park.", "Saw Alice at the park.", 5, seed_user),
        )
        entry_id = cursor.lastrowid
        conn.commit()

        results = repo.find_entries_mentioning(seed_user, "Alice")
        assert [e.entry_id for e in results] == [entry_id]
        assert results[0].quotes == []

        # A name containing SQL metacharacters is bound as a literal
        # value, never concatenated into the query text.
        assert repo.find_entries_mentioning(seed_user, "'; DROP TABLE entries; --") == []
        assert repo.find_entries_mentioning(seed_user, "Alice") == results

    def test_percent_in_name_is_escaped_not_a_wildcard(
        self,
        repo: SQLiteStorylineRepository,
        factory: ConnectionFactory,
        seed_user: int,
    ) -> None:
        """``%``/``_`` are LIKE wildcards — an entity literally named
        "100%" must match the literal text "100%" but must NOT match
        "100 percent" (which an unescaped ``%...%``  pattern would
        wrongly treat "100%" as "100" + any-chars)."""
        conn = factory.get()
        cursor = conn.execute(
            "INSERT INTO entries"
            " (entry_date, source_type, raw_text, final_text, word_count, user_id)"
            " VALUES (?, 'text', ?, ?, ?, ?)",
            (
                "2026-05-02", "Battery at 100% this morning.",
                "Battery at 100% this morning.", 5, seed_user,
            ),
        )
        literal_entry_id = cursor.lastrowid
        conn.execute(
            "INSERT INTO entries"
            " (entry_date, source_type, raw_text, final_text, word_count, user_id)"
            " VALUES (?, 'text', ?, ?, ?, ?)",
            (
                "2026-05-03", "Gave it 100 percent effort today.",
                "Gave it 100 percent effort today.", 6, seed_user,
            ),
        )
        conn.commit()

        results = repo.find_entries_mentioning(seed_user, "100%")
        assert [e.entry_id for e in results] == [literal_entry_id]


class TestSparseRecallFallback:
    def test_engine_uses_like_fallback_when_mentions_are_sparse(
        self,
        engine: StorylineEngine,
        repo: SQLiteStorylineRepository,
        storyline: Storyline,
        factory: ConnectionFactory,
        seed_user: int,
        fake_judge: FakeJudge,
    ) -> None:
        # No entity_mentions row at all — only a bare surface-form match.
        conn = factory.get()
        cursor = conn.execute(
            "INSERT INTO entries"
            " (entry_date, source_type, raw_text, final_text, word_count, user_id)"
            " VALUES (?, 'text', ?, ?, ?, ?)",
            (
                "2026-05-01", "Went for a Running session today.",
                "Went for a Running session today.", 6, seed_user,
            ),
        )
        entry_id = cursor.lastrowid
        conn.commit()
        fake_judge.judgment = ExtensionJudgment(
            assignments=[EntryAssignment(entry_id, "draft")],
            draft_arc_complete=False, reasoning="continues",
        )

        result = engine.update(storyline.id)

        assert result.new_entry_count == 1
        draft = repo.get_draft(storyline.id)
        assert draft is not None
        assert entry_id in repo.chapter_entry_ids(draft.id)
