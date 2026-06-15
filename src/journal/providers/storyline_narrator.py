"""Storyline narrative generation provider (Opus, Citations API).

Generates the third-person narrative panel for a storyline. The
input is the chronological corpus of journal entries that mention
the storyline's anchor entity; the output is a list of segment
dicts (per ``services/storylines/segments.py``) where each citation
segment carries an integer ``entry_id`` parsed from the Anthropic
Citations API response.

The provider does three things differently from `formatter.py` /
`mood_scorer.py`:

1. **Citations API with one ``source="text"`` document per entry.**
   Each entry's ``final_text`` becomes its own plain-text document;
   the entry id and date ride along in the document's ``title``
   field (which is passed to the model but is not citable, per the
   Citations docs). The Anthropic API auto-chunks the document at
   sentence boundaries and returns short ``cited_text`` excerpts
   alongside the per-request ``document_index``. The index maps
   back to ``entry_id`` via the order in which we pass documents.
   Pointers are *parsed*, not generated, so the model cannot
   fabricate an entry id that wasn't supplied.
2. **Two-breakpoint prompt caching.** 1h TTL on the system framing,
   5m TTL on the entry corpus (cache_control on the final document
   marks the breakpoint covering every preceding document).
3. **Quote-first prompt structure.** The system prompt instructs
   Opus to plan citations before composing prose. Combined with
   "external knowledge restriction" and "you may say I don't know"
   language, this is the layered defense against the "model puts
   words in your mouth" failure mode.

Design notes: docs/storylines-plan.md §"Decisions & tradeoffs".
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from journal.services.storylines.segments import (
    citation_segment,
    text_segment,
)

if TYPE_CHECKING:
    from journal.models import DatedEntryExcerpt

log = logging.getLogger(__name__)


SYSTEM_PROMPT = """\
You are a journal narrator. The user keeps a stream-of-consciousness journal in the
first person. You are given a chronologically ordered set of journal entries that
mention a specific subject (an entity — a person, place, activity, or topic). Your
job is to compose a faithful, third-person narrative about that subject using only
the material in the provided entries.

Rules — all are load-bearing:

* Third person. Never write as if you are the journal's author. Refer to the author
  as "he", "the author", "John" if a name appears in the entries — but do not
  invent a name if none is given.
* Use only information present in the provided entries. Do not draw on outside
  knowledge about parenting, running, relationships, software, or any other
  topic. If the entries do not say something, do not say it.
* Cite every factual claim. Every concrete event, quote, place, time, or feeling
  attributed to the subject must come from a specific entry. The Citations API
  will attach the source automatically when you ground a claim in the entries.
* Stay restrained. Do not invent dialogue, interior monologue, or emotional states
  that are not directly attested in the source text. "He felt frustrated" is OK
  only if an entry says he felt frustrated; "He must have been lonely" is not.
* You may say "the entries do not show". This is preferable to filling a gap
  with plausible-sounding generic content.
* Plan before composing. Before you write the narrative, internally list the
  specific entries you will draw on and the quotes that ground each claim. Use
  the Citations API to attach those quotes — do not fabricate quotes inline.

Output exactly one continuous third-person narrative. No headings, no preamble,
no meta-commentary. The reader will see the narrative side-by-side with a
chronological list of verbatim excerpts, so do not duplicate the excerpts
verbatim — synthesize them into prose, with citations carrying the link back.
"""


SECTIONING_SYSTEM_PROMPT = """\
You are a journal narrator. The user keeps a stream-of-consciousness journal in the
first person. You are given a chronologically ordered set of journal entries that
mention a specific subject (an entity — a person, place, activity, or topic). Your
job is to compose a faithful, third-person narrative about that subject using only
the material in the provided entries.

Rules — all are load-bearing:

* Third person. Never write as if you are the journal's author. Refer to the author
  as "he", "the author", "John" if a name appears in the entries — but do not
  invent a name if none is given.
* Use only information present in the provided entries. Do not draw on outside
  knowledge about parenting, running, relationships, software, or any other
  topic. If the entries do not say something, do not say it.
* Cite every factual claim. Every concrete event, quote, place, time, or feeling
  attributed to the subject must come from a specific entry. The Citations API
  will attach the source automatically when you ground a claim in the entries.
* Stay restrained. Do not invent dialogue, interior monologue, or emotional states
  that are not directly attested in the source text. "He felt frustrated" is OK
  only if an entry says he felt frustrated; "He must have been lonely" is not.
* You may say "the entries do not show". This is preferable to filling a gap
  with plausible-sounding generic content.
* Plan before composing. Before you write the narrative, internally list the
  specific entries you will draw on and the quotes that ground each claim. Use
  the Citations API to attach those quotes — do not fabricate quotes inline.

Divide the narrative into chronological sections. Begin each section with a line
`## <short title>` (a few words) on its own line. Break at natural topic shifts.
Aim for about 200 words per section (it's fine to range roughly 180–240) but NEVER
split or pad a coherent topic just to hit a word count — semantic coherence beats
word count. Sections must be in chronological order. Do not add any preamble before
the first `## ` heading, and do not duplicate the verbatim excerpts — synthesize
them into prose, with citations carrying the link back.
"""


# Section-marker regex. A text segment whose first line is heading-shaped opens
# a new section. ``^\s*##\s+(.+)$`` with MULTILINE so the heading can be the
# first line of a multi-line block; the remainder of the block (after the first
# newline) becomes the new section's opening prose.
_HEADING_RE = re.compile(r"^\s*##\s+(.+?)\s*$", re.MULTILINE)

# Soft word band per section. Sections outside [WORD_BAND_MIN, WORD_BAND_MAX]
# are logged as warnings but still returned — semantics win over word count.
WORD_BAND_MIN = 180
WORD_BAND_MAX = 240


@dataclass
class NarrativeResult:
    """The output of a narrative generation call."""

    segments: list[dict[str, Any]] = field(default_factory=list)
    source_entry_ids: list[int] = field(default_factory=list)
    citation_count: int = 0
    model_used: str = ""
    raw_usage: dict[str, Any] | None = None
    """Anthropic usage block (input_tokens, output_tokens,
    cache_creation_input_tokens, cache_read_input_tokens). Used by
    callers to log cache-hit performance after generation."""


@dataclass
class NarrativeSection:
    """One titled section of a sectioned narrative.

    ``segments`` are the section's interleaved text + citation segments
    (per ``services/storylines/segments.py``), EXCLUDING the ``## title``
    heading marker itself — the heading is captured into ``title``.
    ``word_count`` is computed from the section's text segments only
    (whitespace-split), excluding citation ``quote`` text.
    """

    title: str
    segments: list[dict[str, Any]] = field(default_factory=list)
    source_entry_ids: list[int] = field(default_factory=list)
    citation_count: int = 0
    word_count: int = 0


@dataclass
class SectionedNarrativeResult:
    """The output of a sectioned narrative generation call."""

    sections: list[NarrativeSection] = field(default_factory=list)
    model_used: str = ""
    raw_usage: dict[str, Any] | None = None

    @property
    def citation_count(self) -> int:
        """Total citation segments across all sections."""
        return sum(s.citation_count for s in self.sections)

    @property
    def source_entry_ids(self) -> list[int]:
        """Deduplicated, first-seen-order entry ids across all sections."""
        seen: set[int] = set()
        out: list[int] = []
        for section in self.sections:
            for eid in section.source_entry_ids:
                if eid not in seen:
                    seen.add(eid)
                    out.append(eid)
        return out


@runtime_checkable
class StorylineNarratorProtocol(Protocol):
    """Narrative generation protocol (one method).

    ``prior_narrative`` is an optional string of previously-written
    narrative prose. When non-empty, the narrator is asked to produce
    a *continuation* (new segments to append after the prior text),
    not a from-scratch retelling. Used by append-mode regeneration.
    """

    def generate_narrative(
        self,
        excerpts: list[DatedEntryExcerpt],
        storyline_name: str,
        storyline_description: str = "",
        prior_narrative: str | None = None,
    ) -> NarrativeResult: ...

    def generate_sectioned_narrative(
        self,
        excerpts: list[DatedEntryExcerpt],
        storyline_name: str,
        storyline_description: str = "",
    ) -> SectionedNarrativeResult: ...


class AnthropicStorylineNarrator:
    """Citations-grounded narrative generator using the Anthropic API."""

    def __init__(
        self,
        api_key: str,
        model: str = "claude-opus-4-7",
        max_tokens: int = 4096,
        client: Any | None = None,
    ) -> None:
        if client is not None:
            self._client = client
        else:
            import anthropic
            self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model
        self._max_tokens = max_tokens

    @property
    def model(self) -> str:
        return self._model

    def generate_narrative(
        self,
        excerpts: list[DatedEntryExcerpt],
        storyline_name: str,
        storyline_description: str = "",
        prior_narrative: str | None = None,
    ) -> NarrativeResult:
        """Generate a third-person narrative grounded in ``excerpts``.

        Returns a populated ``NarrativeResult``; the segments list is
        in render order (interleaved text + citation). The caller
        persists segments via ``SQLiteStorylineRepository.upsert_panel``.
        On empty input or hard API failure, returns an empty result —
        callers decide whether to retry or surface as "no narrative
        available".

        When ``prior_narrative`` is non-empty, the user-query block is
        prefixed with the prior text and the model is instructed to
        produce a *continuation* — used by append-mode regeneration so
        the new segments narrate only the newly-arrived excerpts
        without re-summarising what has already been written. We chose
        an extra kwarg (rather than a separate method) so the cache-
        breakpointed system prompt + document layout stays identical
        between modes; only the user-query text changes.
        """
        if not excerpts:
            log.info("Narrator called with empty corpus — returning empty result")
            return NarrativeResult(model_used=self._model)

        document_to_entry, document_to_date = _build_index_maps(excerpts)
        user_query = _build_user_query(
            storyline_name=storyline_name,
            storyline_description=storyline_description,
            entry_count=len(excerpts),
            prior_narrative=prior_narrative,
        )

        try:
            response = self._call_api(excerpts, user_query, SYSTEM_PROMPT)
        except Exception:  # noqa: BLE001 — provider failures surface as empty
            log.exception("Storyline narrator API call failed")
            return NarrativeResult(model_used=self._model)

        segments = _parse_narrative_response(
            response, document_to_entry, document_to_date
        )
        source_entry_ids: list[int] = []
        seen: set[int] = set()
        citation_count = 0
        for seg in segments:
            if seg.get("kind") != "citation":
                continue
            citation_count += 1
            eid = int(seg.get("entry_id", 0))
            if eid and eid not in seen:
                seen.add(eid)
                source_entry_ids.append(eid)

        usage = _extract_usage(response)
        return NarrativeResult(
            segments=segments,
            source_entry_ids=source_entry_ids,
            citation_count=citation_count,
            model_used=self._model,
            raw_usage=usage,
        )

    def generate_sectioned_narrative(
        self,
        excerpts: list[DatedEntryExcerpt],
        storyline_name: str,
        storyline_description: str = "",
    ) -> SectionedNarrativeResult:
        """Generate a third-person narrative split into titled sections.

        Builds the same Citations-API documents + index maps as
        ``generate_narrative`` (via the shared ``_build_index_maps`` /
        ``_call_api`` helpers), but sends the SECTIONING system prompt
        and parses the response into ordered ``NarrativeSection``s. The
        model begins each section with a ``## <title>`` heading line; the
        parser opens a new section on each heading-shaped text segment.

        On empty input or hard API failure, returns an empty result —
        callers decide whether to retry or surface as "no narrative
        available".
        """
        if not excerpts:
            log.info(
                "Sectioned narrator called with empty corpus — empty result"
            )
            return SectionedNarrativeResult(model_used=self._model)

        document_to_entry, document_to_date = _build_index_maps(excerpts)
        user_query = _build_user_query(
            storyline_name=storyline_name,
            storyline_description=storyline_description,
            entry_count=len(excerpts),
        )

        try:
            response = self._call_api(
                excerpts, user_query, SECTIONING_SYSTEM_PROMPT
            )
        except Exception:  # noqa: BLE001 — provider failures surface as empty
            log.exception("Sectioned storyline narrator API call failed")
            return SectionedNarrativeResult(model_used=self._model)

        sections = _parse_sectioned_response(
            response, document_to_entry, document_to_date
        )
        usage = _extract_usage(response)
        return SectionedNarrativeResult(
            sections=sections,
            model_used=self._model,
            raw_usage=usage,
        )

    def _call_api(
        self,
        excerpts: list[DatedEntryExcerpt],
        user_query: str,
        system_prompt: str,
    ) -> Any:  # noqa: ANN401
        """Shared Anthropic Citations-API call.

        Identical request shape for both the flat and sectioned paths —
        only the system prompt differs. The corpus documents and the
        user-query text block ride in a single user message; the system
        prompt is cache-breakpointed.
        """
        document_blocks = _build_documents(excerpts)
        return self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=[
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[
                {
                    "role": "user",
                    "content": [
                        *document_blocks,
                        {"type": "text", "text": user_query},
                    ],
                }
            ],
        )


def _build_index_maps(
    excerpts: list[DatedEntryExcerpt],
) -> tuple[dict[int, int], dict[int, str]]:
    """Build the parallel document_index → entry_id and → date maps.

    Documents are passed in the same order as ``excerpts``, so index i
    ↔ ``excerpts[i]``. ``document_to_entry`` resolves a citation's
    per-request ``document_index`` back to an entry id; the parallel
    ``document_to_date`` stamps each citation segment with the source
    entry's ISO date (used by the webapp's absolute-date eyebrows).
    """
    document_to_entry: dict[int, int] = {
        i: ex.entry_id for i, ex in enumerate(excerpts)
    }
    document_to_date: dict[int, str] = {
        i: str(ex.entry_date) for i, ex in enumerate(excerpts)
    }
    return document_to_entry, document_to_date


def _build_documents(
    excerpts: list[DatedEntryExcerpt],
) -> list[dict[str, Any]]:
    """Render one ``source="text"`` document per excerpt.

    The entry id and date live in the document's ``title`` field so
    the model can reason about which entry it's reading without that
    metadata showing up inside ``cited_text`` (titles are passed to
    the model but not citable, per the Citations API docs).

    ``cache_control`` attaches only to the LAST document — a single
    breakpoint that covers the whole corpus up to and including the
    newest entry. With N documents and one breakpoint at the end we
    stay well under the four-breakpoint request limit.
    """
    docs: list[dict[str, Any]] = []
    last_index = len(excerpts) - 1
    for i, ex in enumerate(excerpts):
        doc: dict[str, Any] = {
            "type": "document",
            "source": {
                "type": "text",
                "media_type": "text/plain",
                "data": ex.final_text,
            },
            "title": f"Entry {ex.entry_id} ({ex.entry_date})",
            "citations": {"enabled": True},
        }
        if i == last_index:
            doc["cache_control"] = {"type": "ephemeral"}
        docs.append(doc)
    return docs


def _build_user_query(
    storyline_name: str,
    storyline_description: str,
    entry_count: int,
    prior_narrative: str | None = None,
) -> str:
    desc = storyline_description.strip() or storyline_name
    base = (
        f"Compose a third-person narrative about: {storyline_name}.\n\n"
        f"Description: {desc}\n\n"
        f"You have {entry_count} journal entries to draw on, attached as a "
        f"document with one block per entry, in chronological order. Cite "
        f"every claim. Write naturally and concisely — quality of grounding "
        f"matters more than length."
    )
    if prior_narrative and prior_narrative.strip():
        # Append-mode addendum: the model sees the existing narrative
        # as "previous chapters" context. It should continue the
        # storyline from that point — narrating only the new entries
        # — rather than re-stating ground already covered.
        return (
            f"{base}\n\n"
            "Here is what has been written about this storyline so far. "
            "Continue the narrative from where it leaves off — narrate "
            "ONLY the newly-attached entries and do not repeat content "
            "already covered. The continuation will be concatenated "
            "directly onto the prior text, so begin in the same "
            "third-person voice and tense.\n\n"
            "PREVIOUS CHAPTERS:\n"
            f"{prior_narrative.strip()}"
        )
    return base


def _parse_narrative_response(
    response: Any,  # noqa: ANN401
    document_to_entry: dict[int, int],
    document_to_date: dict[int, str] | None = None,
) -> list[dict[str, Any]]:
    """Walk the Anthropic response content, emitting text + citation segments.

    Anthropic returns ``response.content`` as a list of blocks. Each
    block is either a plain ``TextBlock`` or one with a ``citations``
    field. With ``source="text"`` documents, each citation carries a
    ``char_location`` shape: ``document_index`` identifies which of
    the documents we sent, and ``cited_text`` is a short sentence-
    level excerpt. We map ``document_index`` back to ``entry_id``
    via the index → entry id map built from the order documents
    were sent. (``document_index`` is also present on the other
    citation shapes — page_location, content_block_location — so
    this parser is robust to a future provider swap.)

    When a text block has citations, we emit one ``citation`` segment
    per citation plus a ``text`` segment for the block's narrative
    prose. The order in the list mirrors the order in which the model
    produced them — which is the read order.
    """
    segments: list[dict[str, Any]] = []
    content = getattr(response, "content", None) or []
    for block in content:
        block_type = _attr_or_key(block, "type")
        if block_type != "text":
            continue
        text = _attr_or_key(block, "text") or ""
        citations = _attr_or_key(block, "citations") or []

        if not citations:
            if text:
                segments.append(text_segment(text))
            continue

        # When a block has citations, emit narrative text + one citation
        # per cited source. Anthropic's design treats citations as
        # attachments on the prose block; we surface both because the
        # webapp's segment renderer expects discrete items.
        if text:
            segments.append(text_segment(text))
        for citation in citations:
            doc_idx_raw = _attr_or_key(citation, "document_index")
            cited_text = _attr_or_key(citation, "cited_text") or ""
            if doc_idx_raw is None:
                continue
            doc_idx = int(doc_idx_raw)
            entry_id = document_to_entry.get(doc_idx)
            if entry_id is None:
                log.warning(
                    "Citation document_index %d not in document_to_entry map "
                    "(known keys: %s) — skipping",
                    doc_idx, sorted(document_to_entry.keys()),
                )
                continue
            entry_date = (
                document_to_date.get(doc_idx)
                if document_to_date is not None
                else None
            )
            segments.append(
                citation_segment(entry_id, cited_text, entry_date=entry_date)
            )
    return segments


def _split_heading(text: str) -> tuple[str | None, str]:
    """If ``text`` opens with a ``## <title>`` heading line, return
    ``(title, remainder)`` where remainder is the prose after the first
    line (with the heading line stripped). Otherwise return
    ``(None, text)``.

    Only a heading on the FIRST line opens a section — a ``##`` later in
    the block is treated as ordinary prose (it would be a within-section
    artefact, not a section boundary).
    """
    first_line, sep, rest = text.partition("\n")
    match = _HEADING_RE.match(first_line)
    if match is None:
        return None, text
    return match.group(1).strip(), rest


def _parse_sectioned_response(
    response: Any,  # noqa: ANN401
    document_to_entry: dict[int, int],
    document_to_date: dict[int, str] | None = None,
) -> list[NarrativeSection]:
    """Parse the Anthropic response into ordered titled sections.

    Reuses ``_parse_narrative_response`` to get the flat, render-order
    list of text + citation segments (so citation→entry_id mapping and
    date stamping stay in one place), then groups that flat list into
    sections. A text segment whose first line matches the ``## <title>``
    heading pattern opens a NEW section with that title; the remainder
    of that block (after the heading line) becomes the section's first
    prose segment, and subsequent segments accrue to the current
    section.

    Pre-first-heading text (model preamble — ideally none, the prompt
    forbids it) is NOT dropped: it goes into an implicit leading section
    with an empty title. We chose a leading section over attaching it to
    the first real section so a stray preamble never silently merges
    into — and corrupts the word count / title of — the first authored
    section. If there are no headings at all, the whole narrative
    becomes one untitled (``title == ""``) section.

    Per-section ``word_count`` is computed from text segments only
    (whitespace-split), excluding citation ``quote`` text. Sections
    outside the soft ``[WORD_BAND_MIN, WORD_BAND_MAX]`` band are logged
    as warnings but still returned.
    """
    flat = _parse_narrative_response(
        response, document_to_entry, document_to_date
    )

    sections: list[NarrativeSection] = []
    current: NarrativeSection | None = None

    def _open(title: str) -> NarrativeSection:
        section = NarrativeSection(title=title)
        sections.append(section)
        return section

    for seg in flat:
        if seg.get("kind") == "text":
            title, remainder = _split_heading(seg.get("text", ""))
            if title is not None:
                # Heading line opens a new section. Remainder (if any)
                # becomes the new section's first prose segment.
                current = _open(title)
                if remainder.strip():
                    current.segments.append(text_segment(remainder))
                continue
        # Non-heading segment (prose or citation). If we haven't opened a
        # section yet, open an implicit leading section for the preamble.
        if current is None:
            current = _open("")
        current.segments.append(seg)

    for section in sections:
        _finalize_section(section)
    return sections


def _finalize_section(section: NarrativeSection) -> None:
    """Compute ``word_count``, ``source_entry_ids``, and
    ``citation_count`` for a section, then warn if it's out of band."""
    word_count = 0
    citation_count = 0
    seen: set[int] = set()
    source_entry_ids: list[int] = []
    for seg in section.segments:
        if seg.get("kind") == "text":
            word_count += len((seg.get("text") or "").split())
        elif seg.get("kind") == "citation":
            citation_count += 1
            eid = int(seg.get("entry_id", 0))
            if eid and eid not in seen:
                seen.add(eid)
                source_entry_ids.append(eid)
    section.word_count = word_count
    section.citation_count = citation_count
    section.source_entry_ids = source_entry_ids
    # Empty sections (heading with no prose) are tolerated silently —
    # only flag sections that have content but miss the word band.
    if word_count and not (WORD_BAND_MIN <= word_count <= WORD_BAND_MAX):
        log.warning(
            "Narrative section %r has %d words, outside band [%d, %d] "
            "— returning anyway (semantics win over word count)",
            section.title, word_count, WORD_BAND_MIN, WORD_BAND_MAX,
        )


def _attr_or_key(obj: Any, key: str) -> Any:  # noqa: ANN401
    """Read ``key`` from either an attribute (SDK objects) or a dict
    (tests' canned responses). Anthropic's Python SDK returns Pydantic
    models for content blocks; our tests inject plain dicts. This
    helper bridges both."""
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _extract_usage(response: Any) -> dict[str, Any] | None:  # noqa: ANN401
    usage = _attr_or_key(response, "usage")
    if usage is None:
        return None
    out: dict[str, Any] = {}
    for key in (
        "input_tokens", "output_tokens",
        "cache_creation_input_tokens", "cache_read_input_tokens",
    ):
        value = _attr_or_key(usage, key)
        if value is not None:
            out[key] = value
    return out or None
