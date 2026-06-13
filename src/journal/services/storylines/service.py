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
    warnings: list[str] = field(default_factory=list)


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
    ) -> GenerationResult: ...

    def regenerate_chapter(
        self,
        chapter_id: int,
        *,
        mode: GenerationMode = ...,
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
        window_days: int = DEFAULT_WINDOW_DAYS,
        fts_fallback_threshold: int = DEFAULT_FTS_FALLBACK_THRESHOLD,
    ) -> None:
        self._entity_store = entity_store
        self._entry_repository = entry_repository
        self._storyline_repository = storyline_repository
        self._narrator = narrator
        self._glue = glue
        self._embedder = embedder
        self._window_days = window_days
        self._fts_fallback_threshold = fts_fallback_threshold

    def regenerate(
        self,
        storyline_id: int,
        *,
        start_date: date | str | None = None,
        end_date: date | str | None = None,
        mode: GenerationMode = "replace",
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

        return self.regenerate_chapter(open_chapter.id, mode=mode)

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

        Only ``mode="replace"`` is supported here in Phase 1 — the open
        chapter is rebuilt over its window. Append is handled by
        :meth:`regenerate` / :meth:`_regenerate_append` for the open
        chapter.
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
        result.narrative_citation_count = narrative.citation_count
        result.narrative_model = narrative.model_used
        # Guard against silently wiping a previously good narrative.
        # The narrator catches API errors internally and returns an
        # empty NarrativeResult, so a single transient Anthropic
        # failure used to overwrite the persisted panel with zero
        # segments. When the corpus is non-empty but the narrator came
        # back empty, leave the existing panel alone and surface the
        # failure as a warning + log line.
        if not narrative.segments:
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
                segments=narrative.segments,
                source_entry_ids=narrative.source_entry_ids,
                citation_count=narrative.citation_count,
                model_used=narrative.model_used,
            )

        glue = self._glue.generate_transitions(excerpts)
        curation_segments = _build_curation_segments(excerpts, glue.transitions)
        result.curation_citation_count = count_citations(curation_segments)
        result.curation_model = glue.model_used
        self._storyline_repository.upsert_panel(
            chapter_id=chapter_id,
            panel_kind="curation",
            segments=curation_segments,
            source_entry_ids=collect_source_entry_ids(curation_segments),
            citation_count=result.curation_citation_count,
            model_used=glue.model_used,
        )

        if self._embedder is not None:
            narrative_text = _join_narrative_text(narrative.segments)
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
        log.info(
            "Chapter %d regenerated: %d entries, %d narrative citations, %d curation citations",
            chapter_id, result.entry_count,
            result.narrative_citation_count, result.curation_citation_count,
        )
        return result

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
                "and a previously-generated storyline."
            )
        if chapter.last_generated_at is None:
            raise ValueError(
                "Append mode requires explicit start_date "
                "and a previously-generated storyline."
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
                f"the storyline's last generation date "
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

    def _resolve_date_window(
        self,
        storyline: Storyline,
        *,
        start_override: str | None = None,
        end_override: str | None = None,
    ) -> tuple[str | None, str | None]:
        """Return (start_date, end_date) ISO strings.

        Resolution order:

        1. Explicit ``start_override``/``end_override`` win whenever
           supplied (caller already parsed them to ISO).
        2. Otherwise the storyline row's stored bounds, if either
           is set.
        3. Otherwise the default 90-day rolling window.

        Each bound is resolved independently — passing only
        ``start_override`` leaves the end falling through to the next
        rule, and vice versa. If only one storyline-row bound is set
        the other stays None (open range)."""
        # Either explicit param wins for that side. If both overrides
        # are None we fall back to the storyline row + default.
        if start_override is not None or end_override is not None:
            row_start = storyline.start_date if start_override is None else start_override
            row_end = storyline.end_date if end_override is None else end_override
            # If after the per-side merge we still have nothing, fill
            # in the default 90-day window so the downstream query has
            # a bounded range.
            if not row_start and not row_end:
                end = datetime.utcnow().date()
                start = end - timedelta(days=self._window_days)
                return start.isoformat(), end.isoformat()
            return row_start, row_end
        if storyline.start_date or storyline.end_date:
            return storyline.start_date, storyline.end_date
        end = datetime.utcnow().date()
        start = end - timedelta(days=self._window_days)
        return start.isoformat(), end.isoformat()

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
