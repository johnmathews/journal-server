"""Storyline generation orchestrator.

Wires together the dated-mentions query (W3), the narrator provider
(W4), the glue provider (W4), and the storyline repository (W3). The
service is the single entry point used by:

* W5's job worker (`run_storyline_generation`)
* W9's MCP tool `journal_regenerate_storyline`
* Anywhere else that needs a synchronous regenerate

Method ``regenerate(storyline_id)`` is the load-bearing call. It is
idempotent: calling it repeatedly produces equivalent panels modulo
LLM variance. State changes are confined to ``storylines.summary_
embedding`` + ``storyline_panels.segments`` rows + the
``last_generated_at`` timestamp; no other tables are touched.

FTS fallback: when the entity has fewer than ``fts_fallback_threshold``
dated mentions in the storyline's date window, the service queries
FTS5 for the entity's canonical name as a string match and merges in
the matching entries (deduplicated against the entity-mention set).
This catches pronominal mentions ("my son", "he") that the entity
extractor missed, at the cost of slightly noisier source material.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from journal.db.storyline_repository import ChapterSpec
from journal.services.storylines.segments import (
    SEGMENT_KIND_CITATION,
    SEGMENT_KIND_TEXT,
    citation_segment,
    collect_source_entry_ids,
    count_citations,
    text_segment,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from journal.db.repository.protocol import EntryRepository
    from journal.db.storyline_repository import SQLiteStorylineRepository
    from journal.entitystore.protocol import EntityStore
    from journal.models import DatedEntryExcerpt, Storyline, StorylineChapter
    from journal.providers.storyline_glue import StorylineGlueProtocol
    from journal.providers.storyline_narrator import StorylineNarratorProtocol

log = logging.getLogger(__name__)


# Default sliding window when the storyline has no explicit
# start_date/end_date. 90 days = "last 3 months".
DEFAULT_WINDOW_DAYS = 90

# Below this many entity-mention rows in the window, FTS fallback
# fires. Three is the spike's pragmatic threshold — enough excerpts
# to compose a non-degenerate narrative, low enough to actually
# trigger on sparse threads.
DEFAULT_FTS_FALLBACK_THRESHOLD = 3

# Ingest-time auto-split ceiling (W5). When the ingest path regenerates a
# storyline's open chapter and its narrative now exceeds this many words,
# the storyline is automatically re-segmented. Mirrors
# ``config.storyline_chapter_max_words`` (the bootstrap passes that in);
# this module-level default lets tests construct the service without
# config.
DEFAULT_MAX_CHAPTER_WORDS = 240

# Soft cap on anchors per storyline. Enforced at the service boundary
# (``create_storyline`` / ``set_anchors`` go through validation that
# raises ``ValueError`` when this is exceeded). Routes / MCP tools
# surface the error as 422 / ToolError. Bump when prompt budgets allow.
MAX_ANCHORS = 15

# How many characters of context to grab around the FTS-matched
# surface form when building a synthetic excerpt for the curation
# panel. The full entry text still goes to the narrator (via
# Citations); this is just the verbatim quote shown in panel 1.
_FTS_SNIPPET_RADIUS_CHARS = 240


@dataclass
class GenerationResult:
    """Aggregate stats from one ``regenerate`` call."""

    storyline_id: int
    entry_count: int = 0
    entity_mention_count: int = 0
    fts_fallback_count: int = 0
    narrative_citation_count: int = 0
    curation_citation_count: int = 0
    narrative_model: str = ""
    curation_model: str = ""
    chapter_count: int = 0
    warnings: list[str] = field(default_factory=list)


@dataclass
class _Span:
    """An unlocked run of consecutive chapters to be re-carved.

    ``first_idx``/``last_idx`` index into the storyline's seq-ordered
    chapter list. ``span_start``/``span_end`` are the resolved date
    window; ``span_end`` is None when the span includes the open chapter.
    """

    first_idx: int
    last_idx: int
    span_start: str | None
    span_end: str | None


@dataclass
class _SectionPlan:
    """A planned replacement chapter derived from one narrator section."""

    title: str
    start_date: str | None
    end_date: str | None
    state: str
    excerpts: list[DatedEntryExcerpt]
    segments: list[dict[str, Any]]
    source_entry_ids: list[int]
    citation_count: int
    model_used: str
    title_locked: bool


GenerationMode = Literal["replace", "append"]


@runtime_checkable
class StorylineGenerationServiceProtocol(Protocol):
    def regenerate(
        self,
        storyline_id: int,
        *,
        start_date: date | str | None = ...,
        end_date: date | str | None = ...,
        mode: GenerationMode = ...,
        auto_split: bool = ...,
    ) -> GenerationResult: ...

    def regenerate_chapter(
        self,
        chapter_id: int,
        *,
        mode: GenerationMode = ...,
    ) -> GenerationResult: ...

    def resegment_storyline(
        self,
        storyline_id: int,
        *,
        override_locked: bool = ...,
    ) -> GenerationResult: ...


class StorylineGenerationService:
    """Orchestrates one storyline regeneration end-to-end.

    Constructor takes Protocol-typed collaborators so tests can
    inject fakes. ``embedder`` is optional — when None, the summary
    embedding is not updated (the extension classifier will fall
    back to its other prefilters).
    """

    def __init__(
        self,
        *,
        entity_store: EntityStore,
        entry_repository: EntryRepository,
        storyline_repository: SQLiteStorylineRepository,
        narrator: StorylineNarratorProtocol,
        glue: StorylineGlueProtocol,
        embedder: Callable[[str], list[float]] | None = None,
        # Currently unused by per-chapter generation (the chapter's own
        # window is authoritative). Reserved and kept for constructor /
        # bootstrap compatibility — production bootstrap passes
        # window_days=...
        window_days: int = DEFAULT_WINDOW_DAYS,
        fts_fallback_threshold: int = DEFAULT_FTS_FALLBACK_THRESHOLD,
        max_chapter_words: int = DEFAULT_MAX_CHAPTER_WORDS,
    ) -> None:
        self._entity_store = entity_store
        self._entry_repository = entry_repository
        self._storyline_repository = storyline_repository
        self._narrator = narrator
        self._glue = glue
        self._embedder = embedder
        self._window_days = window_days
        self._fts_fallback_threshold = fts_fallback_threshold
        self._max_chapter_words = max_chapter_words

    def regenerate(
        self,
        storyline_id: int,
        *,
        start_date: date | str | None = None,
        end_date: date | str | None = None,
        mode: GenerationMode = "replace",
        auto_split: bool = False,
    ) -> GenerationResult:
        """Back-compat entry point: regenerate the storyline's open chapter.

        Chapters are the unit of generation; the storyline-level
        ``regenerate`` resolves the storyline's single **open** chapter
        and delegates to :meth:`regenerate_chapter`. This preserves the
        existing job/button behavior for callers that still think in
        terms of storylines.

        ``mode="replace"`` (the default) rebuilds both panels from
        scratch over the chapter's date window. ``mode="append"``
        requires both ``start_date`` and a previously-generated open
        chapter; the new excerpts in [start_date, end_date] are appended
        to the open chapter's existing panels rather than replacing them.

        ``start_date`` and ``end_date`` may be ``datetime.date`` or ISO
        ``YYYY-MM-DD`` strings. In ``replace`` mode the chapter's own
        window is authoritative; the overrides are honoured only on the
        append path (which mirrors the previous behavior and is exercised
        by the append-mode tests).

        ``auto_split`` is the ingest-time auto-split gate (W5). It is
        honoured ONLY on the default ``mode="replace"`` open-chapter path
        — the path the ingest extension-check hook uses. After the normal
        regenerate, if the (now-refreshed) open chapter's
        ``narrative_word_count`` strictly exceeds ``max_chapter_words``,
        the storyline is re-segmented via :meth:`resegment_storyline` and
        THAT result is returned. Manual "refresh" callers (REST/MCP) leave
        ``auto_split`` False, so re-segmentation stays opt-in for them.
        ``resegment_storyline`` does not call ``regenerate``, so the split
        fires at most once — no recursion. ``auto_split`` is ignored in
        ``mode="append"`` (append never crosses the open chapter boundary).
        """
        if mode not in ("replace", "append"):
            raise ValueError(
                f"Invalid mode {mode!r}; expected 'replace' or 'append'"
            )
        open_chapter = self._storyline_repository.get_open_chapter(storyline_id)
        if open_chapter is None:
            # Distinguish "no storyline" from "storyline has no open
            # chapter" so callers (and tests) get the right error.
            storyline = self._storyline_repository.get_storyline(storyline_id)
            if storyline is None:
                raise ValueError(f"Storyline {storyline_id} not found")
            raise ValueError(f"Storyline {storyline_id} has no open chapter")

        if mode == "append":
            storyline = self._storyline_repository.get_storyline(storyline_id)
            if storyline is None:
                raise ValueError(f"Storyline {storyline_id} not found")
            start_iso = _to_iso(start_date)
            end_iso = _to_iso(end_date)
            return self._regenerate_append(
                storyline, open_chapter,
                start_iso=start_iso, end_iso=end_iso,
            )

        result = self.regenerate_chapter(open_chapter.id, mode=mode)

        # Ingest-time auto-split (W5). Only the ingest path passes
        # auto_split=True. After refreshing the open chapter, read its
        # cached narrative word count back; if it now exceeds the chapter
        # ceiling, re-segment the storyline and return that result.
        # resegment_storyline never calls regenerate, so this fires at
        # most once (no recursion).
        if auto_split:
            refreshed = self._storyline_repository.get_open_chapter(storyline_id)
            if (
                refreshed is not None
                and refreshed.narrative_word_count > self._max_chapter_words
            ):
                log.info(
                    "Storyline %d open chapter narrative %d words > %d ceiling; "
                    "auto-splitting via resegment",
                    storyline_id, refreshed.narrative_word_count,
                    self._max_chapter_words,
                )
                return self.resegment_storyline(storyline_id)
        return result

    def regenerate_chapter(
        self,
        chapter_id: int,
        *,
        mode: GenerationMode = "replace",
    ) -> GenerationResult:
        """Regenerate one chapter's panels end-to-end (the core).

        Resolves the chapter and its parent storyline, uses the
        **chapter's** ``start_date``/``end_date`` as the generation
        window (anchors are still storyline-level and resolved via the
        storyline), and writes both panels keyed on ``chapter_id``.
        ``last_generated_at`` and the summary embedding are stamped on
        the chapter row, not the storyline.

        Only ``mode="replace"`` is supported here in Phase 1 — the
        chapter is rebuilt over its window. Append is available only for
        the open chapter via ``regenerate(storyline_id, mode="append")``,
        not per-chapter.
        """
        if mode != "replace":
            raise ValueError(
                f"regenerate_chapter only supports mode='replace'; got {mode!r}"
            )
        chapter = self._storyline_repository.get_chapter(chapter_id)
        if chapter is None:
            raise ValueError(f"Chapter {chapter_id} not found")
        storyline = self._storyline_repository.get_storyline(chapter.storyline_id)
        if storyline is None:
            raise ValueError(f"Storyline {chapter.storyline_id} not found")

        excerpts, fts_count = self._fetch_excerpts(
            storyline, start_date=chapter.start_date, end_date=chapter.end_date,
        )
        result = GenerationResult(
            storyline_id=storyline.id,
            entry_count=len(excerpts),
            entity_mention_count=len(excerpts) - fts_count,
            fts_fallback_count=fts_count,
        )

        if not excerpts:
            result.warnings.append(
                "No entries found for entity in date window. "
                "Skipping generation."
            )
            log.info(
                "Chapter %d: no excerpts in window %s..%s; persisting empty panels",
                chapter_id, chapter.start_date, chapter.end_date,
            )
            self._storyline_repository.upsert_panel(
                chapter_id=chapter_id,
                panel_kind="curation", segments=[], source_entry_ids=[],
                citation_count=0, model_used=self._glue.model,
            )
            self._storyline_repository.upsert_panel(
                chapter_id=chapter_id,
                panel_kind="narrative", segments=[], source_entry_ids=[],
                citation_count=0, model_used=self._narrator.model,
            )
            self._storyline_repository.record_chapter_generation_complete(
                chapter_id,
            )
            return result

        narrative = self._narrator.generate_narrative(
            excerpts=excerpts,
            storyline_name=storyline.name,
            storyline_description=storyline.description,
        )
        self._write_chapter_panels(
            chapter_id,
            excerpts,
            narrative_segments=narrative.segments,
            narrative_source_ids=narrative.source_entry_ids,
            narrative_citation_count=narrative.citation_count,
            narrative_model=narrative.model_used,
            result=result,
        )
        log.info(
            "Chapter %d regenerated: %d entries, %d narrative citations, %d curation citations",
            chapter_id, result.entry_count,
            result.narrative_citation_count, result.curation_citation_count,
        )
        return result

    def _write_chapter_panels(
        self,
        chapter_id: int,
        excerpts: list[DatedEntryExcerpt],
        narrative_segments: list[dict[str, Any]],
        narrative_source_ids: list[int],
        narrative_citation_count: int,
        narrative_model: str,
        *,
        result: GenerationResult,
    ) -> None:
        """Write both panels for one chapter from a narrative result.

        Shared by ``regenerate_chapter`` (one flat narrative) and
        ``resegment_storyline`` (one section per chapter). Given a
        chapter id, its excerpts, and the narrative segments to persist,
        this:

        * upserts the narrative panel, guarding against silently wiping a
          previously-good narrative when ``narrative_segments`` is empty
          (the narrator catches its own API errors and returns empty);
        * builds the curation panel via the glue provider's transitions
          and upserts it;
        * caches ``narrative_word_count`` from the narrative text
          segments (same whitespace-split count W2 uses);
        * refreshes the chapter's summary embedding (best-effort); and
        * stamps ``last_generated_at``.

        Stats are accumulated onto ``result`` (so a multi-chapter caller
        aggregates across chapters).
        """
        result.narrative_citation_count += narrative_citation_count
        result.narrative_model = narrative_model
        if not narrative_segments:
            log.warning(
                "Chapter %d: narrator returned empty segments for "
                "non-empty corpus (%d excerpts) — preserving existing "
                "narrative panel rather than overwriting",
                chapter_id, len(excerpts),
            )
            result.warnings.append(
                "Narrative generation produced no segments; "
                "existing narrative was preserved."
            )
        else:
            self._storyline_repository.upsert_panel(
                chapter_id=chapter_id,
                panel_kind="narrative",
                segments=narrative_segments,
                source_entry_ids=narrative_source_ids,
                citation_count=narrative_citation_count,
                model_used=narrative_model,
            )

        glue = self._glue.generate_transitions(excerpts)
        curation_segments = _build_curation_segments(excerpts, glue.transitions)
        result.curation_citation_count += count_citations(curation_segments)
        result.curation_model = glue.model_used
        self._storyline_repository.upsert_panel(
            chapter_id=chapter_id,
            panel_kind="curation",
            segments=curation_segments,
            source_entry_ids=collect_source_entry_ids(curation_segments),
            citation_count=count_citations(curation_segments),
            model_used=glue.model_used,
        )

        # Cache the narrative word count (text segments only — same
        # whitespace-split count W2's _finalize_section uses).
        self._storyline_repository.set_chapter_word_count(
            chapter_id, _count_narrative_words(narrative_segments),
        )

        if self._embedder is not None:
            narrative_text = _join_narrative_text(narrative_segments)
            if narrative_text.strip():
                try:
                    embedding = self._embedder(narrative_text)
                except Exception:  # noqa: BLE001 — embedding is best-effort
                    log.exception(
                        "Embedder failed for chapter %d; skipping summary embedding",
                        chapter_id,
                    )
                    result.warnings.append(
                        "Embedder failed; summary embedding not updated."
                    )
                else:
                    self._storyline_repository.update_chapter_summary_embedding(
                        chapter_id, embedding,
                    )

        self._storyline_repository.record_chapter_generation_complete(chapter_id)

    def _write_empty_chapter_panels(self, chapter_id: int) -> None:
        """Persist empty curation + narrative panels for an empty chapter.

        Used when a span has no excerpts so the rebuilt chapter still has
        well-formed (empty) panels rather than none. Mirrors the
        empty-corpus branch of :meth:`regenerate_chapter`.
        """
        self._storyline_repository.upsert_panel(
            chapter_id=chapter_id,
            panel_kind="curation", segments=[], source_entry_ids=[],
            citation_count=0, model_used=self._glue.model,
        )
        self._storyline_repository.upsert_panel(
            chapter_id=chapter_id,
            panel_kind="narrative", segments=[], source_entry_ids=[],
            citation_count=0, model_used=self._narrator.model,
        )
        self._storyline_repository.set_chapter_word_count(chapter_id, 0)
        self._storyline_repository.record_chapter_generation_complete(chapter_id)

    # ── re-segmentation ─────────────────────────────────────────

    def resegment_storyline(
        self,
        storyline_id: int,
        *,
        override_locked: bool = False,
    ) -> GenerationResult:
        """Re-carve a storyline into titled, word-sized chapters.

        The storyline's chapters tile its timeline contiguously, with
        exactly one ``open`` chapter (``end_date=NULL``). This method
        re-derives chapter boundaries from a single sectioning-narrator
        call per *unlocked span*, then atomically rebuilds the chapter
        rows and writes panels for each new chapter.

        ``boundary_locked`` chapters are fixed anchors: their id, window,
        title, and panels survive untouched, and they split the timeline
        into the maximal runs of consecutive non-locked chapters that
        form the unlocked spans. With ``override_locked=True`` the entire
        timeline is treated as ONE unlocked span (locks ignored), so a
        re-carve can cross hand-painted boundaries.

        On a transient narrator failure (zero sections) for a span, that
        span's existing chapters + panels are left intact and a warning
        is recorded — we never wipe good data on a flaky API call.
        """
        storyline = self._storyline_repository.get_storyline(storyline_id)
        if storyline is None:
            raise ValueError(f"Storyline {storyline_id} not found")

        chapters = self._storyline_repository.list_chapters(storyline_id)
        result = GenerationResult(storyline_id=storyline_id)
        if not chapters:
            result.warnings.append("Storyline has no chapters; nothing to do.")
            return result

        spans = self._compute_unlocked_spans(chapters, override_locked)

        # Build the full desired chapter list in date order. ``plans`` is a
        # list of either ("preserve", StorylineChapter) for boundary_locked
        # anchors, or ("new", _SectionPlan) for a freshly-derived chapter.
        plans: list[tuple[str, Any]] = []
        # Map chapter index → span (so we can interleave preserved anchors).
        span_by_first_idx = {s.first_idx: s for s in spans}
        idx = 0
        while idx < len(chapters):
            if idx in span_by_first_idx:
                span = span_by_first_idx[idx]
                section_plans = self._plan_span(storyline, span, chapters, result)
                if section_plans is None:
                    # Narrator failure: preserve the span's existing
                    # chapters untouched (treat each as a preserve plan).
                    for ci in range(span.first_idx, span.last_idx + 1):
                        plans.append(("preserve", chapters[ci]))
                else:
                    for sp in section_plans:
                        plans.append(("new", sp))
                idx = span.last_idx + 1
            else:
                # A boundary_locked anchor (not part of any unlocked span).
                plans.append(("preserve", chapters[idx]))
                idx += 1

        specs = _plans_to_specs(plans)
        rebuilt = self._storyline_repository.rebuild_chapters(storyline_id, specs)
        result.chapter_count = len(rebuilt)

        # Write panels for the NEW chapters (preserved ones keep theirs).
        # rebuilt is in seq order, which matches the plans order.
        for chapter, (kind, payload) in zip(rebuilt, plans, strict=True):
            if kind != "new":
                continue
            section_plan: _SectionPlan = payload
            if not section_plan.excerpts:
                # Empty span chapter: persist empty panels (mirror the
                # empty-corpus branch of regenerate_chapter). The
                # narrative guard in _write_chapter_panels would otherwise
                # skip writing — but here there is nothing to preserve.
                self._write_empty_chapter_panels(chapter.id)
                continue
            self._write_chapter_panels(
                chapter.id,
                section_plan.excerpts,
                narrative_segments=section_plan.segments,
                narrative_source_ids=section_plan.source_entry_ids,
                narrative_citation_count=section_plan.citation_count,
                narrative_model=section_plan.model_used,
                result=result,
            )

        log.info(
            "Storyline %d resegmented: %d chapters (override_locked=%s)",
            storyline_id, result.chapter_count, override_locked,
        )
        return result

    def _compute_unlocked_spans(
        self,
        chapters: list[StorylineChapter],
        override_locked: bool,
    ) -> list[_Span]:
        """Return the maximal runs of consecutive non-boundary_locked
        chapters. With ``override_locked`` the whole list is one span.

        Each span carries the chapter index range plus the resolved
        ``[span_start, span_end]`` date window (``span_end`` is None when
        the span includes the open chapter)."""
        if override_locked:
            first, last = chapters[0], chapters[-1]
            return [
                _Span(
                    first_idx=0,
                    last_idx=len(chapters) - 1,
                    span_start=first.start_date,
                    span_end=last.end_date,
                )
            ]
        spans: list[_Span] = []
        run_start: int | None = None
        for i, ch in enumerate(chapters):
            if not ch.boundary_locked:
                if run_start is None:
                    run_start = i
            else:
                if run_start is not None:
                    spans.append(
                        _Span(
                            first_idx=run_start,
                            last_idx=i - 1,
                            span_start=chapters[run_start].start_date,
                            span_end=chapters[i - 1].end_date,
                        )
                    )
                    run_start = None
        if run_start is not None:
            spans.append(
                _Span(
                    first_idx=run_start,
                    last_idx=len(chapters) - 1,
                    span_start=chapters[run_start].start_date,
                    span_end=chapters[-1].end_date,
                )
            )
        return spans

    def _plan_span(
        self,
        storyline: Storyline,
        span: _Span,
        chapters: list[StorylineChapter],
        result: GenerationResult,
    ) -> list[_SectionPlan] | None:
        """Build the replacement chapter plans for one unlocked span.

        Returns ``None`` to signal "preserve the span untouched" (narrator
        returned zero sections — a transient failure we must not let wipe
        good data). Returns a list of one-or-more :class:`_SectionPlan` to
        replace the span otherwise; an empty corpus yields a single empty
        chapter covering the whole span.
        """
        excerpts, fts_count = self._fetch_excerpts(
            storyline, start_date=span.span_start, end_date=span.span_end,
        )
        result.entry_count += len(excerpts)
        result.entity_mention_count += len(excerpts) - fts_count
        result.fts_fallback_count += fts_count

        if not excerpts:
            # Empty span → one empty chapter over the whole span. Mirrors
            # the empty-corpus path in regenerate_chapter (empty panels).
            return [
                _SectionPlan(
                    title="",
                    start_date=span.span_start,
                    end_date=span.span_end,
                    state="open" if span.span_end is None else "closed",
                    excerpts=[],
                    segments=[],
                    source_entry_ids=[],
                    citation_count=0,
                    model_used=self._narrator.model,
                    title_locked=False,
                )
            ]

        sectioned = self._narrator.generate_sectioned_narrative(
            excerpts, storyline.name, storyline.description,
        )
        if not sectioned.sections:
            log.warning(
                "Storyline %d span [%s..%s]: sectioned narrator returned zero "
                "sections for %d excerpts — preserving existing chapters",
                storyline.id, span.span_start, span.span_end, len(excerpts),
            )
            result.warnings.append(
                "Sectioning produced no sections; existing chapters in this "
                "span were preserved."
            )
            return None

        # Map entry_id → entry_date for window derivation off citations.
        date_by_entry: dict[int, str] = {
            ex.entry_id: str(ex.entry_date) for ex in excerpts
        }
        windows = _derive_section_windows(
            sectioned.sections,
            date_by_entry,
            span_start=span.span_start,
            span_end=span.span_end,
        )

        # Carry forward any title_locked chapters that previously lived in
        # this span, so a new section that majority-overlaps one inherits
        # its locked title.
        locked_titles = [
            chapters[ci]
            for ci in range(span.first_idx, span.last_idx + 1)
            if chapters[ci].title_locked
        ]

        plans: list[_SectionPlan] = []
        last_index = len(sectioned.sections) - 1
        for i, (section, (win_start, win_end)) in enumerate(
            zip(sectioned.sections, windows, strict=True)
        ):
            title = section.title
            title_locked = False
            inherited = _find_locked_title(locked_titles, win_start, win_end)
            if inherited is not None:
                title = inherited
                title_locked = True
            # A None win_start means "no lower bound" (the span itself
            # had no start_date) — include from the beginning rather than
            # silently dropping every excerpt for this section.
            section_excerpts = [
                ex
                for ex in excerpts
                if (win_start is None or str(ex.entry_date) >= win_start)
                and (win_end is None or str(ex.entry_date) <= win_end)
            ]
            plans.append(
                _SectionPlan(
                    title=title,
                    start_date=win_start,
                    end_date=win_end,
                    state="open" if (i == last_index and span.span_end is None)
                    else "closed",
                    excerpts=section_excerpts,
                    segments=section.segments,
                    source_entry_ids=section.source_entry_ids,
                    citation_count=section.citation_count,
                    model_used=sectioned.model_used,
                    title_locked=title_locked,
                )
            )
        return plans

    # ── append mode ────────────────────────────────────────────

    def _regenerate_append(
        self,
        storyline: Storyline,
        chapter: StorylineChapter,
        *,
        start_iso: str | None,
        end_iso: str | None,
    ) -> GenerationResult:
        """Append-only-at-end regeneration (decision D2 in the plan).

        Operates on the storyline's **open** chapter: panels, the
        generation timestamp, and the summary embedding are all keyed
        on ``chapter.id``.

        Validates that we have an explicit ``start_iso`` AND the open
        chapter has been generated at least once AND
        ``start_iso >= last_generated_at.date()``. Fetches the
        new-range excerpts only, then:

        * Narrative panel: invokes the narrator with the new
          excerpts plus the existing narrative prose as
          ``prior_narrative`` context. Appends the new segments to
          the existing ones.
        * Curation panel: appends new citation segments after the
          existing ones. Regenerates transitions for the seam (last
          existing → first new) plus the new internal transitions.
          Existing transitions stay untouched.
        * Summary embedding: re-runs the embedder on the merged
          narrative text (text-only segments, capped at 32k chars).

        Returns a ``GenerationResult`` whose counts reflect only the
        excerpts pulled in this run (not the totals across the merged
        panels) — call sites use ``entry_count`` to mean "what did
        this run process".
        """
        storyline_id = storyline.id
        chapter_id = chapter.id
        if start_iso is None:
            raise ValueError(
                "Append mode requires explicit start_date "
                "and a previously-generated chapter."
            )
        if chapter.last_generated_at is None:
            raise ValueError(
                "Append mode requires explicit start_date "
                "and a previously-generated chapter."
            )
        # Compare dates only — last_generated_at is an ISO timestamp.
        try:
            start_date_obj = datetime.strptime(start_iso, "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError(
                f"Invalid start_date {start_iso!r}; expected YYYY-MM-DD"
            ) from exc
        last_gen_date = _last_generated_as_date(chapter.last_generated_at)
        if last_gen_date is not None and start_date_obj < last_gen_date:
            raise ValueError(
                "Append mode requires start_date to be on or after "
                f"the chapter's last generation date "
                f"({last_gen_date.isoformat()}); got {start_iso}."
            )

        new_excerpts, fts_count = self._fetch_excerpts(
            storyline, start_date=start_iso, end_date=end_iso,
        )
        result = GenerationResult(
            storyline_id=storyline_id,
            entry_count=len(new_excerpts),
            entity_mention_count=len(new_excerpts) - fts_count,
            fts_fallback_count=fts_count,
        )
        if not new_excerpts:
            result.warnings.append(
                "No new entries found in append window. Existing panels "
                "preserved."
            )
            log.info(
                "Chapter %d append-mode: no new excerpts in window %s..%s",
                chapter_id, start_iso, end_iso,
            )
            # Still record that we attempted a generation — bumps
            # last_generated_at so the next append can pick up where
            # this one left off.
            self._storyline_repository.record_chapter_generation_complete(
                chapter_id,
            )
            return result

        existing_narrative = self._storyline_repository.get_panel(
            chapter_id, "narrative",
        )
        existing_curation = self._storyline_repository.get_panel(
            chapter_id, "curation",
        )
        prior_narrative_text = _join_narrative_text(
            existing_narrative.segments if existing_narrative else []
        )

        # ── narrative continuation ─────────────────────────────
        narrative = self._narrator.generate_narrative(
            excerpts=new_excerpts,
            storyline_name=storyline.name,
            storyline_description=storyline.description,
            prior_narrative=prior_narrative_text or None,
        )
        result.narrative_citation_count = narrative.citation_count
        result.narrative_model = narrative.model_used

        if not narrative.segments:
            log.warning(
                "Chapter %d append: narrator returned empty segments "
                "for %d new excerpts — preserving existing narrative panel",
                chapter_id, len(new_excerpts),
            )
            result.warnings.append(
                "Narrative continuation produced no segments; "
                "existing narrative was preserved."
            )
        else:
            existing_segs = list(
                existing_narrative.segments if existing_narrative else []
            )
            merged_narrative_segments = existing_segs + list(narrative.segments)
            existing_source_ids = (
                list(existing_narrative.source_entry_ids)
                if existing_narrative
                else []
            )
            seen_eids: set[int] = set(existing_source_ids)
            merged_source_ids = list(existing_source_ids)
            for eid in narrative.source_entry_ids:
                if eid in seen_eids:
                    continue
                seen_eids.add(eid)
                merged_source_ids.append(eid)
            merged_citation_count = (
                (existing_narrative.citation_count if existing_narrative else 0)
                + narrative.citation_count
            )
            self._storyline_repository.upsert_panel(
                chapter_id=chapter_id,
                panel_kind="narrative",
                segments=merged_narrative_segments,
                source_entry_ids=merged_source_ids,
                citation_count=merged_citation_count,
                model_used=narrative.model_used,
            )

        # ── curation append ────────────────────────────────────
        existing_segments = list(
            existing_curation.segments if existing_curation else []
        )
        seam_excerpt = _seam_excerpt_from_curation(existing_segments)
        # Build glue input: the last existing citation excerpt
        # followed by the new excerpts. generate_transitions(N) ->
        # N-1 transitions, which is exactly what we need: 1 seam
        # transition + (len(new_excerpts) - 1) new internal ones.
        if seam_excerpt is not None:
            glue_input = [seam_excerpt, *new_excerpts]
        else:
            glue_input = list(new_excerpts)
        glue = self._glue.generate_transitions(glue_input)
        # Build new-only citation segments. If we have a seam, the
        # first glue transition is the seam itself (text segment
        # inserted between existing segments and the first new
        # citation). If no seam, the new run becomes a fresh sub-
        # panel starting with a lede.
        if seam_excerpt is not None:
            new_segments = _build_curation_append_segments(
                new_excerpts=new_excerpts,
                transitions=glue.transitions,
            )
        else:
            new_segments = _build_curation_segments(
                new_excerpts, glue.transitions,
            )
        merged_curation_segments = existing_segments + new_segments
        result.curation_citation_count = count_citations(new_segments)
        result.curation_model = glue.model_used
        self._storyline_repository.upsert_panel(
            chapter_id=chapter_id,
            panel_kind="curation",
            segments=merged_curation_segments,
            source_entry_ids=collect_source_entry_ids(merged_curation_segments),
            citation_count=count_citations(merged_curation_segments),
            model_used=glue.model_used,
        )

        # ── summary embedding refresh ──────────────────────────
        if self._embedder is not None:
            # Re-embed the merged narrative text (text-only) so the
            # extension classifier's nearest-neighbor query sees the
            # continued storyline. _join_narrative_text already
            # caps at 32k chars.
            merged_for_embed = self._storyline_repository.get_panel(
                chapter_id, "narrative",
            )
            narrative_text = _join_narrative_text(
                merged_for_embed.segments if merged_for_embed else []
            )
            if narrative_text.strip():
                try:
                    embedding = self._embedder(narrative_text)
                except Exception:  # noqa: BLE001 — embedding is best-effort
                    log.exception(
                        "Embedder failed for chapter %d (append); skipping",
                        chapter_id,
                    )
                    result.warnings.append(
                        "Embedder failed; summary embedding not updated."
                    )
                else:
                    self._storyline_repository.update_chapter_summary_embedding(
                        chapter_id, embedding,
                    )

        self._storyline_repository.record_chapter_generation_complete(chapter_id)
        log.info(
            "Chapter %d appended: %d new entries, %d new narrative citations, "
            "%d new curation citations",
            chapter_id, result.entry_count,
            result.narrative_citation_count, result.curation_citation_count,
        )
        return result

    # ── internal ───────────────────────────────────────────────

    def _fetch_excerpts(
        self,
        storyline: Storyline,
        *,
        start_date: str | None,
        end_date: str | None,
    ) -> tuple[list[DatedEntryExcerpt], int]:
        """Return (excerpts, fts_fallback_count) across all anchors.

        Excerpts are unioned across anchor entities and deduplicated
        on ``entry_id`` (an entry that mentions multiple anchors
        contributes one excerpt, not N). The combined list is sorted
        by ``entry_date`` ASC. FTS fallback rows are counted
        separately and only fire when the anchor entity-mention set
        is below ``fts_fallback_threshold``; the fallback runs
        per-anchor and the results are unioned.
        """
        anchor_ids = self._storyline_repository.list_anchors(storyline.id)

        # Mention-driven excerpts, dedup'd across anchors by entry_id.
        entry_id_to_excerpt: dict[int, DatedEntryExcerpt] = {}
        for entity_id in anchor_ids:
            for ex in self._entity_store.get_dated_entity_excerpts(
                entity_id=entity_id,
                user_id=storyline.user_id,
                start_date=start_date,
                end_date=end_date,
            ):
                # First excerpt for each entry_id wins. Anchor order is
                # stable (ASC), so this is deterministic.
                entry_id_to_excerpt.setdefault(ex.entry_id, ex)

        entity_excerpts = list(entry_id_to_excerpt.values())

        if len(entity_excerpts) >= self._fts_fallback_threshold:
            entity_excerpts.sort(key=lambda ex: (ex.entry_date, ex.entry_id))
            return entity_excerpts, 0

        # FTS fallback: per-anchor canonical-name match, unioned in,
        # dedup'd against already-known entry ids and across anchors.
        excluded = set(entry_id_to_excerpt.keys())
        fts_excerpts: list[DatedEntryExcerpt] = []
        seen_fts_eids: set[int] = set()
        for entity_id in anchor_ids:
            entity = self._entity_store.get_entity(entity_id)
            if entity is None:
                continue
            for ex in self._fts_fallback_excerpts(
                user_id=storyline.user_id,
                surface_form=entity.canonical_name,
                start_date=start_date,
                end_date=end_date,
                exclude_entry_ids=excluded,
            ):
                if ex.entry_id in seen_fts_eids:
                    continue
                seen_fts_eids.add(ex.entry_id)
                fts_excerpts.append(ex)

        combined = sorted(
            entity_excerpts + fts_excerpts,
            key=lambda ex: (ex.entry_date, ex.entry_id),
        )
        return combined, len(fts_excerpts)

    def _fts_fallback_excerpts(
        self,
        *,
        user_id: int,
        surface_form: str,
        start_date: str | None,
        end_date: str | None,
        exclude_entry_ids: set[int],
    ) -> list[DatedEntryExcerpt]:
        from journal.models import DatedEntryExcerpt

        # `_SearchMixin.search_text` returns `list[Entry]` directly,
        # ordered by FTS rank, with no pagination args. It does the
        # date filtering for us.
        entries = self._entry_repository.search_text(
            query=surface_form,
            user_id=user_id,
            start_date=start_date,
            end_date=end_date,
        )
        fallback: list[DatedEntryExcerpt] = []
        for entry in entries:
            if entry.id in exclude_entry_ids:
                continue
            body = entry.final_text or entry.raw_text
            if not body:
                continue
            quote = _extract_snippet(body, surface_form)
            fallback.append(
                DatedEntryExcerpt(
                    entry_id=entry.id,
                    entry_date=entry.entry_date,
                    final_text=body,
                    quotes=[quote] if quote else [],
                )
            )
        return fallback


def _count_narrative_words(segments: list[dict[str, Any]]) -> int:
    """Whitespace-split word count of a narrative's text segments.

    Citation ``quote`` text is excluded — this mirrors W2's per-section
    ``_finalize_section`` word count so the cached
    ``narrative_word_count`` is comparable to the narrator's band logic.
    """
    return sum(
        len((seg.get("text") or "").split())
        for seg in segments
        if seg.get("kind") == SEGMENT_KIND_TEXT
    )


def _add_days(iso: str, days: int) -> str:
    """ISO day ``days`` after ``iso`` (mirrors the repository's day math)."""
    return (date.fromisoformat(iso) + timedelta(days=days)).isoformat()


def _derive_section_windows(
    sections: list[Any],
    date_by_entry: dict[int, str],
    *,
    span_start: str | None,
    span_end: str | None,
) -> list[tuple[str | None, str | None]]:
    """Derive each section's [start, end] window, clamped to tile the span.

    Step 1 — raw windows from citations: a section's raw start/end is the
    min/max ``entry_date`` over its citation segments. A section with NO
    citations inherits a degenerate single-day window at the previous
    section's end + 1 day (rare — the prompt asks the model to cite every
    section; this just keeps the tiling monotonic when it doesn't).

    Step 2 — clamp to tile the span contiguously and non-overlapping:
    the first section's start is forced to ``span_start``; each subsequent
    section's start is the day after the previous section's end; the last
    section's end is ``span_end`` (None when the span is the open one).
    Section ends are bumped forward when needed so windows stay monotonic
    and never collapse below their start.
    """
    # Step 1: raw per-section windows from citation dates.
    raw: list[tuple[str | None, str | None]] = []
    prev_end: str | None = span_start
    for section in sections:
        cited_dates = [
            date_by_entry[seg["entry_id"]]
            for seg in section.segments
            if seg.get("kind") == SEGMENT_KIND_CITATION
            and seg.get("entry_id") in date_by_entry
        ]
        if cited_dates:
            raw_start = min(cited_dates)
            raw_end = max(cited_dates)
        else:
            # Citation-less section: degenerate single day at prev_end + 1.
            anchor = _add_days(prev_end, 1) if prev_end else span_start
            raw_start = anchor
            raw_end = anchor
        raw.append((raw_start, raw_end))
        prev_end = raw_end

    # Step 2: clamp to tile the span.
    n = len(sections)
    out: list[tuple[str | None, str | None]] = []
    cursor: str | None = span_start
    for i in range(n):
        start = cursor if cursor is not None else raw[i][0]
        if i == n - 1:
            end = span_end
        else:
            end = raw[i][1]
            # Keep monotonic: end must be >= start.
            if end is not None and start is not None and end < start:
                end = start
            # When the span is bounded, a non-last section must not
            # consume the days the remaining sections (and the final
            # section's span_end) need. Cap this end at span_end minus
            # one day per still-to-come section, so the windows tile
            # without inverting (start > end). The previous code clamped
            # to span_end itself, which left the next section starting
            # the day AFTER span_end — producing inverted windows.
            if span_end is not None:
                max_end = _add_days(span_end, -(n - 1 - i))
                if end is None or end > max_end:
                    end = max_end
                if start is not None and end < start:
                    # Pathological: more sections than days in the span.
                    # rebuild_chapters rejects any inverted spec, turning
                    # this into a loud failure rather than silent corruption.
                    end = start
        out.append((start, end))
        cursor = _add_days(end, 1) if end is not None else None
    return out


def _overlap_days(
    a_start: str | None,
    a_end: str | None,
    b_start: str | None,
    b_end: str | None,
) -> int:
    """Inclusive overlap in days between [a_start, a_end] and [b_start,
    b_end]. None bounds are treated as ±infinity for the purpose of the
    overlap (an open end extends far into the future)."""
    lo = max(a_start or "0000-01-01", b_start or "0000-01-01")
    hi = min(a_end or "9999-12-31", b_end or "9999-12-31")
    if hi < lo:
        return 0
    return (date.fromisoformat(hi) - date.fromisoformat(lo)).days + 1


def _span_days(start: str | None, end: str | None) -> int:
    """Inclusive day length of a window; large finite number for open."""
    s = start or "0000-01-01"
    e = end or "9999-12-31"
    return (date.fromisoformat(e) - date.fromisoformat(s)).days + 1


def _find_locked_title(
    locked_chapters: list[Any],
    win_start: str | None,
    win_end: str | None,
) -> str | None:
    """Return a locked title to inherit if a previously title_locked
    chapter's window overlaps ``[win_start, win_end]`` by a MAJORITY of
    the new window's days. Returns the best match's title, else None."""
    best_title: str | None = None
    best_overlap = 0
    new_len = _span_days(win_start, win_end)
    for ch in locked_chapters:
        overlap = _overlap_days(
            win_start, win_end, ch.start_date, ch.end_date,
        )
        if overlap * 2 > new_len and overlap > best_overlap:
            best_overlap = overlap
            best_title = ch.title
    return best_title


def _plans_to_specs(plans: list[tuple[str, Any]]) -> list[ChapterSpec]:
    """Translate the service's plan list into repository ``ChapterSpec``s.

    Preserve plans carry the existing chapter id; new plans carry the
    INSERT column values. The state on each spec is its desired final
    state — the repository defers the single ``open`` promotion to the
    last statement, so only the final-in-time plan should be 'open'.
    """
    specs: list[ChapterSpec] = []
    for kind, payload in plans:
        if kind == "preserve":
            chapter = payload
            specs.append(
                ChapterSpec(
                    preserve_id=chapter.id,
                    state=chapter.state,
                )
            )
        else:
            plan: _SectionPlan = payload
            specs.append(
                ChapterSpec(
                    state=plan.state,
                    title=plan.title,
                    start_date=plan.start_date,
                    end_date=plan.end_date,
                    title_locked=plan.title_locked,
                    boundary_locked=False,
                    narrative_word_count=0,
                )
            )
    return specs


def _build_curation_segments(
    excerpts: list[DatedEntryExcerpt],
    transitions: list[str],
) -> list[dict[str, Any]]:
    """Interleave verbatim citation segments and transition prose.

    Layout:

        [intro text]              ← excerpt 0 date
        [citation: excerpt 0]
        [transition 0]            ← e.g. "Three days later:"
        [citation: excerpt 1]
        [transition 1]
        [citation: excerpt 2]
        ...
    """
    segments: list[dict[str, Any]] = []
    if not excerpts:
        return segments

    segments.append(text_segment(_first_excerpt_lede(excerpts[0])))
    first_date = str(excerpts[0].entry_date)
    for quote in excerpts[0].quotes or [excerpts[0].final_text[:240]]:
        segments.append(
            citation_segment(excerpts[0].entry_id, quote, entry_date=first_date)
        )

    for idx, excerpt in enumerate(excerpts[1:]):
        transition = (
            transitions[idx]
            if idx < len(transitions) and transitions[idx]
            else "Some time later:"
        )
        segments.append(text_segment(transition))
        ex_date = str(excerpt.entry_date)
        for quote in excerpt.quotes or [excerpt.final_text[:240]]:
            segments.append(
                citation_segment(excerpt.entry_id, quote, entry_date=ex_date)
            )
    return segments


def _first_excerpt_lede(excerpt: DatedEntryExcerpt) -> str:
    """Lede sentence at the top of the curation panel — locates the
    first excerpt in time so the reader knows where the storyline
    starts."""
    return f"It begins on {excerpt.entry_date}:"


def _join_narrative_text(segments: list[dict[str, Any]]) -> str:
    """Flatten the narrative *prose* segments into one plain string for
    embedding.

    Citation segments are intentionally excluded — they carry the
    cited block from the Citations API, which for source=content
    documents is the entire wrapped journal entry. Including those
    would routinely push the join past ``text-embedding-3-large``'s
    8192-token input limit (caught in production on the first real
    regen). The synthesized prose is the right basis for the
    storyline's summary embedding anyway: it captures the model's
    third-person view of the thread, which is what the extension
    classifier will compare future entries against.

    A character cap is applied as belt-and-suspenders: ~32k chars
    is a conservative ceiling well below 8192 tokens for English
    prose (~4 chars/token). If a future narrator emits prose longer
    than the cap we truncate rather than fail — the embedding is
    best-effort and a truncated summary is still useful.
    """
    parts: list[str] = []
    for seg in segments:
        if seg.get("kind") == "text":
            parts.append(seg.get("text", ""))
    joined = " ".join(p.strip() for p in parts if p and p.strip())
    if len(joined) > _EMBED_MAX_CHARS:
        log.info(
            "Narrative prose %d chars > %d cap — truncating before embed",
            len(joined), _EMBED_MAX_CHARS,
        )
        joined = joined[:_EMBED_MAX_CHARS]
    return joined


# Conservative ceiling for the embedder input. text-embedding-3-large
# accepts 8192 tokens; English prose averages ~4 chars/token, so 32k
# chars sits comfortably below the limit with headroom for token-density
# variation (code snippets, citation markers, etc.).
_EMBED_MAX_CHARS = 32_000


def _to_iso(value: date | str | None) -> str | None:
    """Normalise a ``datetime.date`` or ISO string into an ISO string.

    Returns ``None`` for ``None``; raises ``ValueError`` on a bare-
    string that doesn't parse as YYYY-MM-DD so callers fail loudly
    rather than silently passing garbage to the SQL query."""
    if value is None:
        return None
    if isinstance(value, date):
        return value.isoformat()
    # Validate the string shape; we want a hard failure now, not a
    # silent empty result later when SQLite compares lexically.
    datetime.strptime(value, "%Y-%m-%d")
    return value


def _last_generated_as_date(value: str | None) -> date | None:
    """Parse the ``last_generated_at`` ISO timestamp to a ``date``.

    The repository stores ``YYYY-MM-DDTHH:MM:SSZ``; we just need the
    date portion for the append-mode boundary check. Returns ``None``
    if the value can't be parsed (defensive — should never happen on
    server-generated timestamps)."""
    if not value:
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except ValueError:
        log.warning(
            "last_generated_at %r did not parse as a date; "
            "skipping append-mode boundary check",
            value,
        )
        return None


def _seam_excerpt_from_curation(
    segments: list[dict[str, Any]],
) -> DatedEntryExcerpt | None:
    """Build a synthetic ``DatedEntryExcerpt`` from the last citation
    segment in an existing curation panel.

    The glue provider's ``generate_transitions`` only reads
    ``entry_date`` off each excerpt; ``final_text`` and ``quotes``
    are unused for the transition pairs. We fabricate just enough to
    feed the seam transition (last existing → first new) into the
    same code path."""
    from journal.models import DatedEntryExcerpt as _Excerpt

    last_citation = None
    for seg in reversed(segments):
        if seg.get("kind") == SEGMENT_KIND_CITATION:
            last_citation = seg
            break
    if last_citation is None:
        return None
    entry_date = last_citation.get("entry_date")
    if not entry_date:
        return None
    return _Excerpt(
        entry_id=int(last_citation.get("entry_id", 0)),
        entry_date=str(entry_date),
        final_text=str(last_citation.get("quote", "")),
        quotes=[],
    )


def _build_curation_append_segments(
    new_excerpts: list[DatedEntryExcerpt],
    transitions: list[str],
) -> list[dict[str, Any]]:
    """Build segments to append after an existing curation panel.

    Layout (note: NO lede — the existing panel already has one):

        [transition 0]            ← seam (last existing → first new)
        [citation: new 0]
        [transition 1]            ← new internal transition
        [citation: new 1]
        ...

    ``transitions`` has length ``len(new_excerpts)`` (one seam + one
    per internal new pair), produced by feeding the glue provider a
    list of ``[seam_anchor, *new_excerpts]``."""
    segments: list[dict[str, Any]] = []
    if not new_excerpts:
        return segments

    seam = transitions[0] if transitions and transitions[0] else "Some time later:"
    segments.append(text_segment(seam))
    first_date = str(new_excerpts[0].entry_date)
    for quote in new_excerpts[0].quotes or [new_excerpts[0].final_text[:240]]:
        segments.append(
            citation_segment(
                new_excerpts[0].entry_id, quote, entry_date=first_date,
            )
        )

    for idx, excerpt in enumerate(new_excerpts[1:]):
        # transitions[0] is the seam; internal transitions start at 1.
        transition_idx = idx + 1
        transition = (
            transitions[transition_idx]
            if transition_idx < len(transitions) and transitions[transition_idx]
            else "Some time later:"
        )
        segments.append(text_segment(transition))
        ex_date = str(excerpt.entry_date)
        for quote in excerpt.quotes or [excerpt.final_text[:240]]:
            segments.append(
                citation_segment(excerpt.entry_id, quote, entry_date=ex_date)
            )
    return segments


# Defensive re-export so ruff doesn't strip the SEGMENT_KIND_TEXT
# import (used in this module's docstring discussion of seam segments).
_ = SEGMENT_KIND_TEXT


def _extract_snippet(body: str, surface_form: str) -> str:
    """Return a `_FTS_SNIPPET_RADIUS_CHARS`-wide window around the
    first occurrence of ``surface_form`` (case-insensitive). If the
    surface form is not found, returns the leading
    ``_FTS_SNIPPET_RADIUS_CHARS`` of the body."""
    if not body:
        return ""
    lower_body = body.lower()
    lower_form = surface_form.lower()
    idx = lower_body.find(lower_form)
    if idx < 0:
        return body[: _FTS_SNIPPET_RADIUS_CHARS].strip()
    start = max(0, idx - _FTS_SNIPPET_RADIUS_CHARS // 2)
    end = min(len(body), idx + len(surface_form) + _FTS_SNIPPET_RADIUS_CHARS // 2)
    snippet = body[start:end].strip()
    if start > 0:
        snippet = "…" + snippet
    if end < len(body):
        snippet = snippet + "…"
    return snippet
