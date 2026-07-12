"""StorylineEngine — the continue-or-break orchestrator for storylines.

Replaces the round-1 ``StorylineGenerationService`` (deterministic
time-bucketed chaptering) with a judge-driven design: an LLM judge
(``providers/storyline_judge.py``) decides, per update, whether each
newly-mentioned entry continues the draft chapter, starts a new one, or
is a late addendum to an already-published chapter; a narrator
(``providers/storyline_narrator.py``) composes the prose. The engine is
pure orchestration: fetch candidates, ask the judge, apply its verdict,
ask the narrator to (re)write whatever changed. Three entry points, all
returning :class:`UpdateResult`: ``update`` (the idempotent steady-state
call — no-op when nothing new), ``bootstrap`` (one-time full-history
partition, replacing whatever chapters exist), and ``refresh_draft``
(re-narrate the draft from its existing membership, no judge call).

Failure policy (binding, see task brief): every LLM call completes (or
fails) before any repository write for that step. A judge failure
aborts ``update()`` with no writes — candidates are reconsidered next
time. A narrator failure on the draft or an addendum leaves prior state
untouched and records a warning; addendum entries whose narration
failed are NOT cleared from the pending table (unlike every other new
entry that run) so they retry next update. Publishing never happens
with an empty closure, and at most one publish happens per ``update()``
call (there is exactly one call site).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from journal.db.storyline_repository import BootstrapChapterSpec
from journal.models import DatedEntryExcerpt
from journal.providers.storyline_judge import EntryForJudge
from journal.services.storylines.segments import SEGMENT_KIND_TEXT

if TYPE_CHECKING:
    from collections.abc import Callable

    from journal.db.repository.protocol import EntryRepository
    from journal.db.storyline_repository import SQLiteStorylineRepository
    from journal.entitystore.protocol import EntityStore
    from journal.models import Storyline, StorylineChapter
    from journal.providers.storyline_judge import EntryAssignment, StorylineJudgeProtocol
    from journal.providers.storyline_narrator import (
        NarrativeResult,
        StorylineNarratorProtocol,
    )

log = logging.getLogger(__name__)


# Conservative ceiling for the embedder input, ported from the round-1
# service. text-embedding-3-large accepts 8192 tokens; English prose
# averages ~4 chars/token, so 32k chars sits comfortably below the
# limit with headroom for token-density variation.
_EMBED_MAX_CHARS = 32_000

# Context-economy truncation for judge input. Draft-chapter entries are
# already summarised by the draft narrative the judge also sees, so
# they get a tighter cap than the new entries being classified.
_DRAFT_ENTRY_TRUNCATE_CHARS = 2_000
_NEW_ENTRY_TRUNCATE_CHARS = 6_000

# Sparse-storyline recall fallback threshold (spec §3): below this many
# entity-mention-based candidates, supplement with a literal LIKE scan.
_SPARSE_RECALL_THRESHOLD = 3


@dataclass
class PublishedInfo:
    """What got published during one :meth:`StorylineEngine.update` call."""

    chapter_id: int
    title: str


@dataclass
class UpdateResult:
    """Aggregate outcome of one engine call, surfaced to jobs/API/CLI."""

    storyline_id: int
    new_entry_count: int = 0
    draft_entry_count: int = 0
    published: PublishedInfo | None = None
    addenda_chapter_ids: list[int] = field(default_factory=list)
    chapter_count: int = 0  # bootstrap only
    reasoning: str = ""
    warnings: list[str] = field(default_factory=list)


@runtime_checkable
class StorylineEngineProtocol(Protocol):
    def update(self, storyline_id: int) -> UpdateResult: ...

    def bootstrap(
        self, storyline_id: int, *, mark_read: bool = False,
    ) -> UpdateResult: ...

    def refresh_draft(self, storyline_id: int) -> UpdateResult: ...


class StorylineEngine:
    """Orchestrates judge + narrator + repository for one storyline at a time.

    ``embedder`` is optional — when ``None`` the draft embedding is
    left unset. ``min_publish_entries`` guards against publishing a
    one-entry "chapter": below the floor, a would-be publish folds
    everything into the draft instead.
    """

    def __init__(
        self,
        *,
        entity_store: EntityStore,
        entry_repository: EntryRepository,
        storyline_repository: SQLiteStorylineRepository,
        narrator: StorylineNarratorProtocol,
        judge: StorylineJudgeProtocol,
        embedder: Callable[[str], list[float]] | None = None,
        min_publish_entries: int = 3,
    ) -> None:
        self._entity_store = entity_store
        self._entry_repository = entry_repository
        self._repo = storyline_repository
        self._narrator = narrator
        self._judge = judge
        self._embedder = embedder
        self._min_publish_entries = min_publish_entries

    # ── entry points ─────────────────────────────────────────────

    def update(self, storyline_id: int) -> UpdateResult:
        """Steady-state continue-or-break update; a no-op when nothing new
        has been said about this storyline's anchors since the last call."""
        storyline = self._require_storyline(storyline_id)
        draft = self._repo.get_draft(storyline_id)
        if draft is None:
            raise ValueError(f"Storyline {storyline_id} has no draft chapter")
        result = UpdateResult(storyline_id=storyline_id)

        candidates = self._candidate_entries(storyline)
        pending = self._repo.list_pending_entries(storyline_id)
        assigned = self._repo.assigned_entry_ids(storyline_id)
        new_ids = [e.entry_id for e in candidates if e.entry_id not in assigned]
        new_ids += [eid for eid in pending if eid not in assigned and eid not in new_ids]
        if not new_ids:
            return result
        result.new_entry_count = len(new_ids)

        excerpt_by_id = {e.entry_id: e for e in candidates}
        new_entries = [
            self._to_judge_entry(eid, excerpt_by_id, truncate=_NEW_ENTRY_TRUNCATE_CHARS)
            for eid in new_ids
        ]
        draft_member_ids = self._repo.chapter_entry_ids(draft.id)
        draft_entries = [
            self._to_judge_entry(eid, excerpt_by_id, truncate=_DRAFT_ENTRY_TRUNCATE_CHARS)
            for eid in draft_member_ids
        ]
        judgment = self._judge.judge_extension(
            storyline_name=storyline.name, storyline_description=storyline.description,
            draft_narrative=_join_text(draft.segments), draft_entries=draft_entries,
            new_entries=new_entries, published_chapters=self._published_index(storyline_id),
        )
        if judgment.failed:
            result.warnings.append("Judge unavailable; entries left pending for the next run.")
            return result
        result.reasoning = judgment.reasoning

        to_draft, to_new, addenda = _split_assignments(judgment.assignments)

        # Addenda first (independent of the draft's fate); a narration
        # failure leaves those ids out of the pending-clear set below.
        failed_addendum_ids: set[int] = set()
        for chapter_id, eids in addenda.items():
            fold_back = self._apply_addendum(
                storyline, chapter_id, eids, excerpt_by_id, result, failed_addendum_ids,
            )
            to_draft.extend(fold_back)

        # Publish decision + guards.
        draft_total = len(draft_member_ids) + len(to_draft)
        wants_publish = judgment.draft_arc_complete or bool(to_new)
        if wants_publish and draft_total < self._min_publish_entries:
            result.warnings.append(
                f"Draft has {draft_total} entries < min {self._min_publish_entries}; "
                "deferring publish.",
            )
            to_draft, to_new, wants_publish = to_draft + to_new, [], False

        if to_draft:
            self._repo.add_entries_to_draft(draft.id, to_draft)
        # Clear every new id from pending EXCEPT ones whose addendum
        # narration just failed — those must survive to be retried.
        ids_to_clear = [eid for eid in new_ids if eid not in failed_addendum_ids]
        self._repo.clear_pending_entries(storyline_id, ids_to_clear)

        if wants_publish:
            self._publish(storyline, draft.id, to_new, excerpt_by_id, result)
        else:
            self._renarrate_draft(storyline, draft.id, excerpt_by_id, result)
        self._stamp_draft_entry_count(storyline_id, result)
        return result

    def bootstrap(self, storyline_id: int, *, mark_read: bool = False) -> UpdateResult:
        """Partition full history into chapters, replacing any existing ones.

        ``mark_read=True`` lets a bulk migration seed pre-existing
        published content as already-read (no wall of unread badges).
        """
        storyline = self._require_storyline(storyline_id)
        result = UpdateResult(storyline_id=storyline_id)

        candidates = self._candidate_entries(storyline)
        excerpt_by_id = {e.entry_id: e for e in candidates}
        pending = self._repo.list_pending_entries(storyline_id)
        all_ids = [e.entry_id for e in candidates]
        all_ids += [eid for eid in pending if eid not in excerpt_by_id and eid not in all_ids]
        if not all_ids:
            result.warnings.append(
                "No entries found for this storyline's anchors; nothing to bootstrap.",
            )
            return result

        judge_entries = [
            self._to_judge_entry(eid, excerpt_by_id, truncate=_NEW_ENTRY_TRUNCATE_CHARS)
            for eid in all_ids
        ]
        partition = self._judge.partition(
            storyline_name=storyline.name, storyline_description=storyline.description,
            entries=judge_entries,
        )
        if partition.failed or not partition.chapters:
            result.warnings.append("Partition unavailable; bootstrap aborted.")
            return result

        specs: list[BootstrapChapterSpec] = []
        chapter_count = len(partition.chapters)
        for i, chapter in enumerate(partition.chapters):
            is_last = i == chapter_count - 1
            excerpts = self._excerpts_for(chapter.entry_ids, excerpt_by_id)
            narrative = self._narrator.generate_narrative(
                excerpts, storyline.name, storyline.description,
                mode="draft" if is_last else "closure",
            )
            if not narrative.segments:
                result.warnings.append(
                    f"Narration for chapter {i + 1}/{chapter_count} returned no "
                    "content; bootstrap aborted before any write.",
                )
                return result
            specs.append(BootstrapChapterSpec(
                title=narrative.title or chapter.working_title,
                state="draft" if is_last else "published", segments=narrative.segments,
                source_entry_ids=narrative.source_entry_ids,
                citation_count=narrative.citation_count, model_used=narrative.model_used,
                entry_ids=chapter.entry_ids, mark_read=mark_read,
            ))

        self._repo.replace_all_chapters(storyline_id, specs)
        new_draft = self._repo.get_draft(storyline_id)
        if new_draft is not None:
            self._write_draft_narrative(new_draft.id, new_draft, result)
        self._repo.clear_pending_entries(storyline_id, all_ids)
        result.chapter_count = chapter_count
        return result

    def refresh_draft(self, storyline_id: int) -> UpdateResult:
        """Re-narrate the current draft from its existing membership,
        without consulting the judge or changing membership."""
        storyline = self._require_storyline(storyline_id)
        draft = self._repo.get_draft(storyline_id)
        if draft is None:
            raise ValueError(f"Storyline {storyline_id} has no draft chapter")
        result = UpdateResult(storyline_id=storyline_id)
        candidates = self._candidate_entries(storyline)
        excerpt_by_id = {e.entry_id: e for e in candidates}
        self._renarrate_draft(storyline, draft.id, excerpt_by_id, result)
        self._stamp_draft_entry_count(storyline_id, result)
        return result

    # ── publish / renarrate / addendum ──────────────────────────

    def _publish(
        self, storyline: Storyline, draft_id: int, to_new_ids: list[int],
        excerpt_by_id: dict[int, DatedEntryExcerpt], result: UpdateResult,
    ) -> None:
        """Close out the draft as a published chapter, atomically.

        Never publishes an empty closure: if the closure narration
        comes back with no segments, the entries destined for the new
        chapter fold into the still-open draft instead, which is then
        re-narrated — nothing lost, no half-published chapter written.
        """
        draft = self._repo.get_chapter(draft_id)
        assert draft is not None
        member_ids = self._repo.chapter_entry_ids(draft_id)
        excerpts = self._excerpts_for(member_ids, excerpt_by_id)
        closure = self._narrator.generate_narrative(
            excerpts, storyline.name, storyline.description, mode="closure",
        )
        if not closure.segments:
            result.warnings.append(
                "Closure narration returned no content; deferring publish.",
            )
            if to_new_ids:
                self._repo.add_entries_to_draft(draft_id, to_new_ids)
            self._renarrate_draft(storyline, draft_id, excerpt_by_id, result)
            return

        title = closure.title or f"Chapter {draft.seq}"
        published, new_draft = self._repo.publish_draft(
            storyline.id, title=title, segments=closure.segments,
            source_entry_ids=closure.source_entry_ids,
            citation_count=closure.citation_count, model_used=closure.model_used,
            new_draft_entry_ids=to_new_ids,
        )
        result.published = PublishedInfo(chapter_id=published.id, title=published.title)

        if not to_new_ids:
            return
        new_excerpts = self._excerpts_for(to_new_ids, excerpt_by_id)
        new_narrative = self._narrator.generate_narrative(
            new_excerpts, storyline.name, storyline.description, mode="draft",
        )
        if not new_narrative.segments:
            result.warnings.append(
                "New draft narration returned no content after publish; "
                "draft membership was still recorded.",
            )
            return
        self._write_draft_narrative(new_draft.id, new_narrative, result)

    def _renarrate_draft(
        self, storyline: Storyline, draft_id: int,
        excerpt_by_id: dict[int, DatedEntryExcerpt], result: UpdateResult,
    ) -> None:
        member_ids = self._repo.chapter_entry_ids(draft_id)
        excerpts = self._excerpts_for(member_ids, excerpt_by_id)
        narrative = self._narrator.generate_narrative(
            excerpts, storyline.name, storyline.description, mode="draft",
        )
        if not narrative.segments:
            result.warnings.append(
                "Draft narration returned no content; keeping the existing "
                "narrative.",
            )
            return
        self._write_draft_narrative(draft_id, narrative, result)

    def _write_draft_narrative(
        self, draft_id: int, narrative: NarrativeResult | StorylineChapter,
        result: UpdateResult,
    ) -> None:
        """Write segments onto the draft with a best-effort embedding.
        Accepts a fresh ``NarrativeResult`` or an already-persisted
        ``StorylineChapter`` (bootstrap's post-write embed pass) — both
        expose the same segment/citation attributes."""
        embedding = self._safe_embed(narrative.segments, result)
        self._repo.set_draft_narrative(
            draft_id, segments=narrative.segments,
            source_entry_ids=narrative.source_entry_ids,
            citation_count=narrative.citation_count, model_used=narrative.model_used,
            embedding=embedding,
        )

    def _apply_addendum(
        self, storyline: Storyline, chapter_id: int, entry_ids: list[int],
        excerpt_by_id: dict[int, DatedEntryExcerpt], result: UpdateResult,
        failed_ids: set[int],
    ) -> list[int]:
        """Apply one published-chapter addendum. Returns ids to fold into
        ``to_draft`` instead when the chapter isn't actually published
        (defensive fallback). On narration failure, adds ``entry_ids`` to
        ``failed_ids`` (kept pending for a retry) and returns ``[]``."""
        chapter = self._repo.get_chapter(chapter_id)
        if chapter is None or chapter.state != "published":
            result.warnings.append(
                f"Chapter {chapter_id} is not a published chapter; folding "
                f"{len(entry_ids)} entries into the draft instead.",
            )
            return list(entry_ids)

        excerpts = self._excerpts_for(entry_ids, excerpt_by_id)
        narrative = self._narrator.generate_narrative(
            excerpts, storyline.name, storyline.description,
            mode="addendum", prior_narrative=_join_text(chapter.segments),
        )
        if not narrative.segments:
            result.warnings.append(
                f"Addendum narration for chapter {chapter_id} returned no "
                "content; entries left pending for the next run.",
            )
            failed_ids.update(entry_ids)
            return []

        self._repo.append_addendum(chapter_id, segments=narrative.segments, entry_ids=entry_ids)
        result.addenda_chapter_ids.append(chapter_id)
        return []

    # ── candidate / excerpt fetching ────────────────────────────

    def _candidate_entries(self, storyline: Storyline) -> list[DatedEntryExcerpt]:
        """Union of entity-mention-based excerpts across all anchors, no
        date window. Below ``_SPARSE_RECALL_THRESHOLD`` rows, supplements
        with a literal surface-form LIKE scan per anchor (sparse-storyline
        recall fallback, spec §3), catching pronominal/mis-extracted
        mentions."""
        anchor_ids = self._repo.list_anchors(storyline.id)
        by_id: dict[int, DatedEntryExcerpt] = {}
        for entity_id in anchor_ids:
            for ex in self._entity_store.get_dated_entity_excerpts(
                entity_id=entity_id, user_id=storyline.user_id,
            ):
                by_id.setdefault(ex.entry_id, ex)

        if len(by_id) < _SPARSE_RECALL_THRESHOLD:
            for entity_id in anchor_ids:
                entity = self._entity_store.get_entity(entity_id)
                if entity is None:
                    continue
                for ex in self._repo.find_entries_mentioning(
                    storyline.user_id, entity.canonical_name,
                ):
                    by_id.setdefault(ex.entry_id, ex)

        return sorted(by_id.values(), key=lambda ex: (ex.entry_date, ex.entry_id))

    def _excerpts_for(
        self, entry_ids: list[int], excerpt_by_id: dict[int, DatedEntryExcerpt],
    ) -> list[DatedEntryExcerpt]:
        """Resolve ``entry_ids`` to excerpts, chronologically ordered. Ids
        absent from ``excerpt_by_id`` (pending surface-form matches with no
        formal mention, or draft members whose mention has since dropped
        out of the union) fall back to a direct entry lookup."""
        out: list[DatedEntryExcerpt] = []
        for eid in entry_ids:
            ex = excerpt_by_id.get(eid)
            if ex is None:
                entry = self._entry_repository.get_entry(eid)
                if entry is None:
                    log.warning("Entry %d no longer exists — skipping", eid)
                    continue
                ex = DatedEntryExcerpt(
                    entry_id=eid, entry_date=entry.entry_date,
                    final_text=entry.final_text or entry.raw_text, quotes=[],
                )
            out.append(ex)
        out.sort(key=lambda e: (e.entry_date, e.entry_id))
        return out

    def _to_judge_entry(
        self, entry_id: int, excerpt_by_id: dict[int, DatedEntryExcerpt], *, truncate: int,
    ) -> EntryForJudge:
        ex = excerpt_by_id.get(entry_id)
        if ex is not None:
            text, entry_date = ex.final_text, ex.entry_date
        else:
            entry = self._entry_repository.get_entry(entry_id)
            text = (entry.final_text or entry.raw_text) if entry is not None else ""
            entry_date = entry.entry_date if entry is not None else ""
        return EntryForJudge(entry_id=entry_id, entry_date=entry_date, text=text[:truncate])

    def _published_index(self, storyline_id: int) -> list[tuple[int, str, str, str]]:
        return [
            (c.id, c.title, c.first_entry_date or "", c.last_entry_date or "")
            for c in self._repo.list_chapters(storyline_id)
            if c.state == "published"
        ]

    def _safe_embed(
        self, segments: list[dict[str, Any]], result: UpdateResult,
    ) -> list[float] | None:
        """Best-effort embedding: a failure never blocks the narrative
        write it rides along with."""
        if self._embedder is None:
            return None
        try:
            return self._embedder(_join_text(segments))
        except Exception:  # noqa: BLE001 — embedding is best-effort
            log.exception("Storyline draft embedding failed; continuing without one")
            result.warnings.append(
                "Embedding failed; draft saved without an updated embedding.",
            )
            return None

    def _stamp_draft_entry_count(self, storyline_id: int, result: UpdateResult) -> None:
        draft = self._repo.get_draft(storyline_id)
        assert draft is not None
        result.draft_entry_count = len(self._repo.chapter_entry_ids(draft.id))

    def _require_storyline(self, storyline_id: int) -> Storyline:
        storyline = self._repo.get_storyline(storyline_id)
        if storyline is None:
            raise ValueError(f"Storyline {storyline_id} not found")
        return storyline


def _split_assignments(
    assignments: list[EntryAssignment],
) -> tuple[list[int], list[int], dict[int, list[int]]]:
    """Bucket a judgment's per-entry assignments by target."""
    to_draft: list[int] = []
    to_new: list[int] = []
    addenda: dict[int, list[int]] = {}
    for a in assignments:
        if a.target == "draft":
            to_draft.append(a.entry_id)
        elif a.target == "new_chapter":
            to_new.append(a.entry_id)
        elif a.target == "published_chapter":
            assert a.chapter_id is not None
            addenda.setdefault(a.chapter_id, []).append(a.entry_id)
    return to_draft, to_new, addenda


def _join_text(segments: list[dict[str, Any]]) -> str:
    """Flatten narrative *prose* segments into one plain string for
    embedding (ported from the round-1 service's ``_join_narrative_text``).
    Citation segments are excluded — they'd routinely push the join past
    the embedder's token limit. ``_EMBED_MAX_CHARS`` is a belt-and-
    suspenders truncation below the embedder's 8192-token input limit.
    """
    parts = [seg.get("text", "") for seg in segments if seg.get("kind") == SEGMENT_KIND_TEXT]
    joined = " ".join(p.strip() for p in parts if p and p.strip())
    if len(joined) > _EMBED_MAX_CHARS:
        log.info(
            "Narrative prose %d chars > %d cap — truncating before embed",
            len(joined), _EMBED_MAX_CHARS,
        )
        joined = joined[:_EMBED_MAX_CHARS]
    return joined
