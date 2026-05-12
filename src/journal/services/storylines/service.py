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
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from journal.services.storylines.segments import (
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
    from journal.models import DatedEntryExcerpt, Storyline
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


@runtime_checkable
class StorylineGenerationServiceProtocol(Protocol):
    def regenerate(self, storyline_id: int) -> GenerationResult: ...


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

    def regenerate(self, storyline_id: int) -> GenerationResult:
        storyline = self._storyline_repository.get_storyline(storyline_id)
        if storyline is None:
            raise ValueError(f"Storyline {storyline_id} not found")

        start_date, end_date = self._resolve_date_window(storyline)
        excerpts, fts_count = self._fetch_excerpts(
            storyline, start_date=start_date, end_date=end_date,
        )
        result = GenerationResult(
            storyline_id=storyline_id,
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
                "Storyline %d: no excerpts in window %s..%s; persisting empty panels",
                storyline_id, start_date, end_date,
            )
            self._storyline_repository.upsert_panel(
                storyline_id=storyline_id,
                panel_kind="curation", segments=[], source_entry_ids=[],
                citation_count=0, model_used=self._glue.model,
            )
            self._storyline_repository.upsert_panel(
                storyline_id=storyline_id,
                panel_kind="narrative", segments=[], source_entry_ids=[],
                citation_count=0, model_used=self._narrator.model,
            )
            self._storyline_repository.record_generation_complete(storyline_id)
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
                "Storyline %d: narrator returned empty segments for "
                "non-empty corpus (%d excerpts) — preserving existing "
                "narrative panel rather than overwriting",
                storyline_id, len(excerpts),
            )
            result.warnings.append(
                "Narrative generation produced no segments; "
                "existing narrative was preserved."
            )
        else:
            self._storyline_repository.upsert_panel(
                storyline_id=storyline_id,
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
            storyline_id=storyline_id,
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
                        "Embedder failed for storyline %d; skipping summary embedding",
                        storyline_id,
                    )
                    result.warnings.append(
                        "Embedder failed; summary embedding not updated."
                    )
                else:
                    self._storyline_repository.update_summary_embedding(
                        storyline_id, embedding,
                    )

        self._storyline_repository.record_generation_complete(storyline_id)
        log.info(
            "Storyline %d regenerated: %d entries, %d narrative citations, %d curation citations",
            storyline_id, result.entry_count,
            result.narrative_citation_count, result.curation_citation_count,
        )
        return result

    # ── internal ───────────────────────────────────────────────

    def _resolve_date_window(
        self, storyline: Storyline,
    ) -> tuple[str | None, str | None]:
        """Return (start_date, end_date) ISO strings, applying the
        default 90-day window when neither bound is set on the
        storyline. If only one bound is set, the other stays None
        (open range)."""
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
        """Return (excerpts, fts_fallback_count).

        The combined list is sorted by entry_date ASC. FTS fallback
        rows are excluded from the entity-mention count returned in
        the result.
        """
        entity_excerpts = self._entity_store.get_dated_entity_excerpts(
            entity_id=storyline.entity_id,
            user_id=storyline.user_id,
            start_date=start_date,
            end_date=end_date,
        )
        entry_ids = {ex.entry_id for ex in entity_excerpts}

        if len(entity_excerpts) >= self._fts_fallback_threshold:
            return entity_excerpts, 0

        # FTS fallback: query for the entity's canonical_name as a
        # plain string match.
        entity = self._entity_store.get_entity(storyline.entity_id)
        if entity is None:
            return entity_excerpts, 0

        fts_excerpts = self._fts_fallback_excerpts(
            user_id=storyline.user_id,
            surface_form=entity.canonical_name,
            start_date=start_date,
            end_date=end_date,
            exclude_entry_ids=entry_ids,
        )

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
