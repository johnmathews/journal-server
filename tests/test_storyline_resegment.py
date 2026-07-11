"""Tests for the storyline re-segmentation service (W3).

``resegment_storyline`` re-carves a storyline into titled, word-sized
chapters derived from one sectioning-narrator call per unlocked span.
boundary_locked chapters are preserved untouched; title_locked chapters
contribute their title to the overlapping replacement chapter. The
rebuild is atomic (``rebuild_chapters``) and never transiently violates
the single-open partial unique index or ``UNIQUE(storyline_id, seq)``.

The fakes here mirror the style of ``test_storyline_generation.py`` /
``test_storyline_sectioning.py`` but drive the SECTIONED path: the fake
sectioned narrator splits the incoming excerpts into a configurable
number of sections whose citation segments reference the entry_ids/dates
of the excerpts in each group, so the service's window-derivation logic
gets real per-section date ranges to work with.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from journal.db.storyline_repository import SQLiteStorylineRepository
from journal.entitystore.store import SQLiteEntityStore
from journal.providers.storyline_glue import GlueResult
from journal.providers.storyline_narrator import (
    NarrativeResult,
    NarrativeSection,
    SectionedNarrativeResult,
)
from journal.services.storylines.service import StorylineGenerationService

if TYPE_CHECKING:
    from journal.db.factory import ConnectionFactory
    from journal.models import DatedEntryExcerpt


# ── Fakes ────────────────────────────────────────────────────────


class _FakeGlue:
    def __init__(self) -> None:
        self.model = "claude-haiku-4-5-fake"
        self.calls: int = 0

    def generate_transitions(
        self, excerpts: list[DatedEntryExcerpt],
    ) -> GlueResult:
        self.calls += 1
        if len(excerpts) < 2:
            return GlueResult(model_used=self.model)
        return GlueResult(
            transitions=[f"step {i}:" for i in range(len(excerpts) - 1)],
            model_used=self.model,
        )


def _section_from_excerpts(
    title: str, group: list[DatedEntryExcerpt],
) -> NarrativeSection:
    """Build a NarrativeSection whose citation segments reference the
    given excerpts (entry_id + entry_date). The window for this section
    derives from the min/max entry_date over these citations."""
    segments: list[dict[str, Any]] = [
        {"kind": "text", "text": f"{title} prose about the subject."}
    ]
    source_ids: list[int] = []
    for ex in group:
        segments.append(
            {
                "kind": "citation",
                "entry_id": ex.entry_id,
                "quote": ex.final_text[:40],
                "entry_date": str(ex.entry_date),
            }
        )
        source_ids.append(ex.entry_id)
    return NarrativeSection(
        title=title,
        segments=segments,
        source_entry_ids=source_ids,
        citation_count=len(group),
        word_count=4,
    )


class _FakeSectionedNarrator:
    """Splits the incoming excerpts into ``n_sections`` contiguous groups.

    Each generate_sectioned_narrative call records the excerpts it saw
    (so tests can assert call count + per-span corpus). Sections carry
    citation segments pointing back at the group's excerpts, so the
    service derives each section's window from real entry dates.
    """

    def __init__(
        self,
        n_sections: int = 1,
        *,
        titles: list[str] | None = None,
        zero_sections: bool = False,
    ) -> None:
        self.model = "claude-opus-4-7-fake"
        self._n = n_sections
        self._titles = titles
        self._zero = zero_sections
        self.calls: list[list[DatedEntryExcerpt]] = []

    # The flat method exists on the protocol; resegment never calls it,
    # but keep it so the object duck-types as a narrator.
    def generate_narrative(self, *args: Any, **kwargs: Any) -> NarrativeResult:
        raise AssertionError("resegment must use generate_sectioned_narrative")

    def generate_sectioned_narrative(
        self,
        excerpts: list[DatedEntryExcerpt],
        storyline_name: str,  # noqa: ARG002
        storyline_description: str = "",  # noqa: ARG002
    ) -> SectionedNarrativeResult:
        self.calls.append(list(excerpts))
        if self._zero or not excerpts:
            return SectionedNarrativeResult(model_used=self.model)
        n = min(self._n, len(excerpts))
        # Contiguous chronological groups.
        groups: list[list[DatedEntryExcerpt]] = [[] for _ in range(n)]
        per = max(1, len(excerpts) // n)
        for i, ex in enumerate(excerpts):
            idx = min(i // per, n - 1)
            groups[idx].append(ex)
        sections: list[NarrativeSection] = []
        for gi, group in enumerate(groups):
            if not group:
                continue
            title = (
                self._titles[gi]
                if self._titles and gi < len(self._titles)
                else f"Section {gi + 1}"
            )
            sections.append(_section_from_excerpts(title, group))
        return SectionedNarrativeResult(
            sections=sections, model_used=self.model,
        )


# ── Seed helpers ─────────────────────────────────────────────────


def _seed(
    factory: ConnectionFactory,
    dates: list[str],
) -> tuple[SQLiteStorylineRepository, SQLiteEntityStore, int, int]:
    """Seed a user + Atlas entity + one mention per date. Returns
    (storyline_repo, entity_store, user_id, storyline_id) with NO
    chapters yet (tests create the chapter layout they need)."""
    conn = factory.get()
    cur = conn.execute(
        "INSERT INTO users (email, password_hash, display_name)"
        " VALUES (?, ?, ?)",
        ("u@x.test", "x", "U"),
    )
    user_id = cur.lastrowid
    conn.commit()
    store = SQLiteEntityStore(factory)
    entity = store.create_entity(
        entity_type="person", canonical_name="Atlas",
        description="", first_seen=dates[0], user_id=user_id,
    )
    for d in dates:
        quote = f"Atlas did something on {d}."
        cur = conn.execute(
            "INSERT INTO entries"
            " (entry_date, source_type, raw_text, final_text,"
            "  word_count, user_id)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (d, "text", quote, quote, len(quote.split()), user_id),
        )
        entry_id = cur.lastrowid
        conn.execute(
            "INSERT INTO entity_mentions"
            " (entity_id, entry_id, quote, confidence, extraction_run_id)"
            " VALUES (?, ?, ?, ?, ?)",
            (entity.id, entry_id, quote, 0.95, "run-1"),
        )
    conn.commit()
    repo = SQLiteStorylineRepository(factory)
    storyline = repo.create_storyline(
        user_id=user_id, entity_ids=[entity.id], name="Atlas",
        start_date=dates[0], end_date=None,
    )
    return repo, store, user_id, storyline.id


class _NoSearchEntryRepo:
    def search_text(self, *args: Any, **kwargs: Any) -> list[Any]:
        return []


def _svc(
    repo: SQLiteStorylineRepository,
    store: SQLiteEntityStore,
    narrator: _FakeSectionedNarrator,
    glue: _FakeGlue | None = None,
    *,
    max_chapter_words: int | None = None,
) -> StorylineGenerationService:
    kwargs: dict[str, Any] = {}
    if max_chapter_words is not None:
        kwargs["max_chapter_words"] = max_chapter_words
    return StorylineGenerationService(
        entity_store=store,
        entry_repository=_NoSearchEntryRepo(),
        storyline_repository=repo,
        narrator=narrator,
        glue=glue or _FakeGlue(),
        **kwargs,
    )


def _assert_invariants(
    repo: SQLiteStorylineRepository, storyline_id: int,
) -> None:
    chapters = repo.list_chapters(storyline_id)
    # Exactly one open chapter.
    open_states = [c for c in chapters if c.state == "open"]
    assert len(open_states) == 1
    assert repo.get_open_chapter(storyline_id) is not None
    assert repo.get_open_chapter(storyline_id).id == open_states[0].id
    # Seqs contiguous 1..N with no duplicates.
    seqs = sorted(c.seq for c in chapters)
    assert seqs == list(range(1, len(chapters) + 1))
    # The open chapter is the final one in time (end_date is None).
    assert open_states[0].end_date is None
    # Windows tile contiguously in seq order.
    ordered = sorted(chapters, key=lambda c: c.seq)
    for prev, nxt in zip(ordered, ordered[1:], strict=False):
        assert prev.end_date is not None
        # next starts the day after prev ends.
        from datetime import date as _d
        from datetime import timedelta as _td
        expected = (_d.fromisoformat(prev.end_date) + _td(days=1)).isoformat()
        assert nxt.start_date == expected


# ── Tests ────────────────────────────────────────────────────────


class TestResegmentAllUnlocked:
    def test_full_recarve_into_three_chapters(
        self, factory: ConnectionFactory,
    ) -> None:
        dates = [
            "2026-01-05", "2026-01-12",
            "2026-02-05", "2026-02-12",
            "2026-03-05", "2026-03-12",
        ]
        repo, store, _user, sid = _seed(factory, dates)
        # One big open chapter covering everything.
        repo.create_chapter(
            storyline_id=sid, seq=1, title="Everything",
            start_date="2026-01-05", end_date=None, state="open",
        )
        narrator = _FakeSectionedNarrator(
            n_sections=3, titles=["Winter", "February", "March"],
        )
        svc = _svc(repo, store, narrator)
        result = svc.resegment_storyline(sid)

        chapters = repo.list_chapters(sid)
        assert len(chapters) == 3
        assert [c.title for c in chapters] == ["Winter", "February", "March"]
        # Earlier chapters closed, last open.
        assert [c.state for c in chapters] == ["closed", "closed", "open"]
        # The sectioned narrator was called exactly once.
        assert len(narrator.calls) == 1
        # First chapter starts at span start; last is open.
        assert chapters[0].start_date == "2026-01-05"
        assert chapters[-1].end_date is None
        # Panels written + word counts set on every chapter.
        for ch in chapters:
            assert repo.get_panel(ch.id, "narrative") is not None
            assert repo.get_panel(ch.id, "curation") is not None
            refreshed = repo.get_chapter(ch.id)
            assert refreshed.narrative_word_count > 0
        _assert_invariants(repo, sid)
        assert result.chapter_count == 3

    def test_bucketing_splits_when_narrative_is_long(
        self, factory: ConnectionFactory,
    ) -> None:
        """The sectioning narrator won't split a long narrative on its own,
        so resegment time-buckets deterministically: with an estimated
        1000-word narrative and a 250-word chapter target, an 8-entry span
        becomes 4 chapters, narrating one bucket at a time."""
        dates = [
            "2026-01-05", "2026-01-20",
            "2026-02-05", "2026-02-20",
            "2026-03-05", "2026-03-20",
            "2026-04-05", "2026-04-20",
        ]
        repo, store, _user, sid = _seed(factory, dates)
        ch = repo.create_chapter(
            storyline_id=sid, seq=1, title="Everything",
            start_date="2026-01-05", end_date=None, state="open",
        )
        # Estimated total narrative length → k = round(1000 / 250) = 4.
        repo.set_chapter_word_count(ch.id, 1000)
        narrator = _FakeSectionedNarrator(n_sections=1)
        svc = _svc(repo, store, narrator, max_chapter_words=250)

        result = svc.resegment_storyline(sid)

        chapters = repo.list_chapters(sid)
        assert len(chapters) == 4
        assert result.chapter_count == 4
        # One narrator call per bucket (not one over the whole span), each
        # bucket carrying 2 of the 8 entries.
        assert len(narrator.calls) == 4
        assert [len(c) for c in narrator.calls] == [2, 2, 2, 2]
        assert [c.state for c in chapters] == [
            "closed", "closed", "closed", "open",
        ]
        _assert_invariants(repo, sid)

    def test_no_bucketing_when_narrative_is_short(
        self, factory: ConnectionFactory,
    ) -> None:
        """A short narrative (est below one chapter's worth) stays a single
        chapter, narrated once — no fan-out."""
        dates = ["2026-01-05", "2026-02-05", "2026-03-05"]
        repo, store, _user, sid = _seed(factory, dates)
        ch = repo.create_chapter(
            storyline_id=sid, seq=1, title="Everything",
            start_date="2026-01-05", end_date=None, state="open",
        )
        repo.set_chapter_word_count(ch.id, 120)  # k = round(120/250) = 0 → 1
        narrator = _FakeSectionedNarrator(n_sections=1)
        svc = _svc(repo, store, narrator, max_chapter_words=250)

        svc.resegment_storyline(sid)

        chapters = repo.list_chapters(sid)
        assert len(chapters) == 1
        assert len(narrator.calls) == 1

    def test_missing_storyline_raises(
        self, factory: ConnectionFactory,
    ) -> None:
        repo = SQLiteStorylineRepository(factory)
        store = SQLiteEntityStore(factory)
        svc = _svc(repo, store, _FakeSectionedNarrator())
        with pytest.raises(ValueError, match="not found"):
            svc.resegment_storyline(9999)


class TestResegmentBoundaryLocked:
    def test_boundary_locked_chapter_preserved(
        self, factory: ConnectionFactory,
    ) -> None:
        dates = [
            "2026-01-05", "2026-01-20",   # span A (unlocked)
            "2026-02-10", "2026-02-20",   # locked chapter B window
            "2026-03-05", "2026-03-20",   # span C (unlocked, open)
        ]
        repo, store, _user, sid = _seed(factory, dates)
        # Three chapters: unlocked / boundary_locked / unlocked-open.
        repo.create_chapter(
            storyline_id=sid, seq=1, title="A",
            start_date="2026-01-05", end_date="2026-01-31", state="closed",
        )
        b = repo.create_chapter(
            storyline_id=sid, seq=2, title="Locked B",
            start_date="2026-02-01", end_date="2026-02-28", state="closed",
        )
        repo.create_chapter(
            storyline_id=sid, seq=3, title="C",
            start_date="2026-03-01", end_date=None, state="open",
        )
        # Lock B's boundary and give it a panel we expect to survive.
        conn = factory.get()
        conn.execute(
            "UPDATE storyline_chapters SET boundary_locked = 1, title_locked = 1"
            " WHERE id = ?",
            (b.id,),
        )
        conn.commit()
        repo.upsert_panel(
            chapter_id=b.id, panel_kind="narrative",
            segments=[{"kind": "text", "text": "B preserved narrative"}],
            source_entry_ids=[], citation_count=0, model_used="orig",
        )

        narrator = _FakeSectionedNarrator(n_sections=1)
        svc = _svc(repo, store, narrator)
        svc.resegment_storyline(sid)

        chapters = repo.list_chapters(sid)
        # B survives with same id/window/title/panel.
        b_after = repo.get_chapter(b.id)
        assert b_after is not None
        assert b_after.start_date == "2026-02-01"
        assert b_after.end_date == "2026-02-28"
        assert b_after.title == "Locked B"
        b_panel = repo.get_panel(b.id, "narrative")
        assert b_panel is not None
        assert b_panel.segments == [
            {"kind": "text", "text": "B preserved narrative"}
        ]
        assert b_panel.model_used == "orig"
        # Two unlocked spans → narrator called once per span = twice.
        assert len(narrator.calls) == 2
        # Exactly one open chapter, contiguous tiling, no chapter crosses B.
        _assert_invariants(repo, sid)
        # B's window is still bounded by [2026-02-01, 2026-02-28]; the
        # chapter before B ends 2026-01-31, the chapter after B starts
        # 2026-03-01.
        ordered = sorted(chapters, key=lambda c: c.seq)
        b_idx = next(i for i, ch in enumerate(ordered) if ch.id == b.id)
        assert ordered[b_idx - 1].end_date == "2026-01-31"
        assert ordered[b_idx + 1].start_date == "2026-03-01"

    def test_override_locked_recarves_across_boundary(
        self, factory: ConnectionFactory,
    ) -> None:
        dates = [
            "2026-01-05", "2026-01-20",
            "2026-02-10", "2026-02-20",
            "2026-03-05", "2026-03-20",
        ]
        repo, store, _user, sid = _seed(factory, dates)
        repo.create_chapter(
            storyline_id=sid, seq=1, title="A",
            start_date="2026-01-05", end_date="2026-01-31", state="closed",
        )
        locked_b = repo.create_chapter(
            storyline_id=sid, seq=2, title="Locked B",
            start_date="2026-02-01", end_date="2026-02-28", state="closed",
        )
        repo.create_chapter(
            storyline_id=sid, seq=3, title="C",
            start_date="2026-03-01", end_date=None, state="open",
        )
        conn = factory.get()
        conn.execute(
            "UPDATE storyline_chapters SET boundary_locked = 1 WHERE id = ?",
            (locked_b.id,),
        )
        conn.commit()

        narrator = _FakeSectionedNarrator(n_sections=2)
        svc = _svc(repo, store, narrator)
        svc.resegment_storyline(sid, override_locked=True)

        # Whole timeline is one span → narrator called exactly once.
        assert len(narrator.calls) == 1
        _assert_invariants(repo, sid)


class _FrontLoadedNarrator:
    """Returns 3 sections where a NON-last section cites the span's final
    date — the shape that triggered the window-clamp inversion bug.

    Section 0 cites the earliest AND the latest excerpt (so its raw end
    equals span_end); section 1 cites the middle excerpt; section 2 (the
    last) cites the latest. Called once (single bounded span).
    """

    model = "claude-opus-4-7-fake"

    def __init__(self) -> None:
        self.calls: list[list[DatedEntryExcerpt]] = []

    def generate_narrative(self, *a: Any, **k: Any) -> NarrativeResult:
        raise AssertionError("must use generate_sectioned_narrative")

    def generate_sectioned_narrative(
        self,
        excerpts: list[DatedEntryExcerpt],
        storyline_name: str,  # noqa: ARG002
        storyline_description: str = "",  # noqa: ARG002
    ) -> SectionedNarrativeResult:
        self.calls.append(list(excerpts))
        ordered = sorted(excerpts, key=lambda e: (str(e.entry_date), e.entry_id))
        earliest, middle, latest = ordered[0], ordered[1], ordered[-1]
        sections = [
            _section_from_excerpts("Front", [earliest, latest]),
            _section_from_excerpts("Mid", [middle]),
            _section_from_excerpts("Tail", [latest]),
        ]
        return SectionedNarrativeResult(sections=sections, model_used=self.model)


class TestResegmentBoundedSpanClamping:
    """Regression: a bounded unlocked span (created by a boundary_locked
    open chapter after it) whose sections cite the span's final date must
    NOT produce inverted (start > end) chapter windows."""

    def test_no_inverted_windows_in_bounded_span(
        self, factory: ConnectionFactory,
    ) -> None:
        dates = ["2026-01-10", "2026-01-20", "2026-01-31"]
        repo, store, _user, sid = _seed(factory, dates)
        # Unlocked closed span A, then a boundary_locked OPEN chapter B.
        # The only unlocked span is A, bounded by [2026-01-01, 2026-01-31].
        repo.create_chapter(
            storyline_id=sid, seq=1, title="A",
            start_date="2026-01-01", end_date="2026-01-31", state="closed",
        )
        b = repo.create_chapter(
            storyline_id=sid, seq=2, title="Locked Open B",
            start_date="2026-02-01", end_date=None, state="open",
        )
        conn = factory.get()
        conn.execute(
            "UPDATE storyline_chapters SET boundary_locked = 1 WHERE id = ?",
            (b.id,),
        )
        conn.commit()

        svc = _svc(repo, store, _FrontLoadedNarrator())
        svc.resegment_storyline(sid)

        chapters = repo.list_chapters(sid)
        # No chapter may have start_date > end_date.
        for ch in chapters:
            if ch.end_date is not None:
                assert ch.start_date is not None
                assert ch.start_date <= ch.end_date, (
                    f"inverted window: {ch.start_date}..{ch.end_date}"
                )
        # The re-carved span stays within [2026-01-01, 2026-01-31]; B is
        # preserved as the open chapter. Contiguity + single-open hold.
        _assert_invariants(repo, sid)
        b_after = repo.get_chapter(b.id)
        assert b_after.state == "open"
        assert b_after.boundary_locked is True
        # The latest date's content is attributed to a real chapter, not
        # silently dropped: some non-B chapter ends at the span end.
        recarved = [c for c in chapters if c.id != b.id]
        assert any(c.end_date == "2026-01-31" for c in recarved)


class TestTitleLockedInheritance:
    def test_locked_title_inherited_by_majority_overlap(
        self, factory: ConnectionFactory,
    ) -> None:
        dates = ["2026-01-05", "2026-01-10", "2026-01-15", "2026-01-20"]
        repo, store, _user, sid = _seed(factory, dates)
        # Two unlocked chapters; the first is title_locked.
        locked = repo.create_chapter(
            storyline_id=sid, seq=1, title="My Custom Title",
            start_date="2026-01-05", end_date="2026-01-12", state="closed",
        )
        repo.create_chapter(
            storyline_id=sid, seq=2, title="Second",
            start_date="2026-01-13", end_date=None, state="open",
        )
        conn = factory.get()
        conn.execute(
            "UPDATE storyline_chapters SET title_locked = 1 WHERE id = ?",
            (locked.id,),
        )
        conn.commit()

        # Narrator splits into 2 sections; the first new section's window
        # ([2026-01-05, 2026-01-10]) overlaps the locked chapter's window
        # ([2026-01-05, 2026-01-12]) by a majority of days.
        narrator = _FakeSectionedNarrator(
            n_sections=2, titles=["Auto One", "Auto Two"],
        )
        svc = _svc(repo, store, narrator)
        svc.resegment_storyline(sid)

        chapters = repo.list_chapters(sid)
        assert len(chapters) == 2
        # First chapter keeps the locked title; title_locked stays 1.
        assert chapters[0].title == "My Custom Title"
        assert chapters[0].title_locked is True
        # Second chapter uses the auto title, unlocked.
        assert chapters[1].title == "Auto Two"
        assert chapters[1].title_locked is False
        _assert_invariants(repo, sid)


class TestEmptyAndFailure:
    def test_empty_corpus_collapses_to_single_open_chapter(
        self, factory: ConnectionFactory,
    ) -> None:
        # Seed a user/entity/storyline but NO mentions in the window.
        conn = factory.get()
        cur = conn.execute(
            "INSERT INTO users (email, password_hash, display_name)"
            " VALUES (?, ?, ?)",
            ("e@x.test", "x", "E"),
        )
        user_id = cur.lastrowid
        conn.commit()
        store = SQLiteEntityStore(factory)
        entity = store.create_entity(
            entity_type="person", canonical_name="Ghost",
            description="", first_seen="2026-01-01", user_id=user_id,
        )
        repo = SQLiteStorylineRepository(factory)
        storyline = repo.create_storyline(
            user_id=user_id, entity_ids=[entity.id], name="Ghost",
            start_date="2026-01-01", end_date=None,
        )
        # One open chapter covering an empty window.
        repo.create_chapter(
            storyline_id=storyline.id, seq=1, title="Empty",
            start_date="2026-01-01", end_date=None, state="open",
        )
        narrator = _FakeSectionedNarrator(n_sections=3)
        svc = _svc(repo, store, narrator)
        result = svc.resegment_storyline(storyline.id)

        chapters = repo.list_chapters(storyline.id)
        assert len(chapters) == 1
        assert chapters[0].state == "open"
        assert chapters[0].start_date == "2026-01-01"
        assert chapters[0].end_date is None
        # Empty panels written, no crash. Narrator NOT called (no excerpts).
        assert narrator.calls == []
        narrative = repo.get_panel(chapters[0].id, "narrative")
        assert narrative is not None
        assert narrative.segments == []
        _assert_invariants(repo, storyline.id)
        assert result.chapter_count == 1

    def test_zero_sections_preserves_span(
        self, factory: ConnectionFactory,
    ) -> None:
        dates = ["2026-01-05", "2026-01-12", "2026-01-20"]
        repo, store, _user, sid = _seed(factory, dates)
        ch = repo.create_chapter(
            storyline_id=sid, seq=1, title="Original",
            start_date="2026-01-05", end_date=None, state="open",
        )
        # Pre-existing panel that must survive the narrator failure.
        repo.upsert_panel(
            chapter_id=ch.id, panel_kind="narrative",
            segments=[{"kind": "text", "text": "good narrative"}],
            source_entry_ids=[], citation_count=0, model_used="orig",
        )
        narrator = _FakeSectionedNarrator(zero_sections=True)
        svc = _svc(repo, store, narrator)
        result = svc.resegment_storyline(sid)

        # The span is preserved: same chapter id + panel intact.
        chapters = repo.list_chapters(sid)
        assert len(chapters) == 1
        assert chapters[0].id == ch.id
        panel = repo.get_panel(ch.id, "narrative")
        assert panel is not None
        assert panel.segments == [{"kind": "text", "text": "good narrative"}]
        # A warning was recorded.
        assert any("section" in w.lower() or "narrat" in w.lower()
                   for w in result.warnings)
        _assert_invariants(repo, sid)
