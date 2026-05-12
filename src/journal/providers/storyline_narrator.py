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


@runtime_checkable
class StorylineNarratorProtocol(Protocol):
    """Narrative generation protocol (one method)."""

    def generate_narrative(
        self,
        excerpts: list[DatedEntryExcerpt],
        storyline_name: str,
        storyline_description: str = "",
    ) -> NarrativeResult: ...


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
    ) -> NarrativeResult:
        """Generate a third-person narrative grounded in ``excerpts``.

        Returns a populated ``NarrativeResult``; the segments list is
        in render order (interleaved text + citation). The caller
        persists segments via ``SQLiteStorylineRepository.upsert_panel``.
        On empty input or hard API failure, returns an empty result —
        callers decide whether to retry or surface as "no narrative
        available".
        """
        if not excerpts:
            log.info("Narrator called with empty corpus — returning empty result")
            return NarrativeResult(model_used=self._model)

        document_blocks = _build_documents(excerpts)
        # document_index → entry_id map so we can resolve the
        # per-request document_index on each citation back to an
        # entry id. Documents are passed in the same order as
        # ``excerpts``, so index i ↔ excerpts[i].entry_id.
        document_to_entry: dict[int, int] = {
            i: ex.entry_id for i, ex in enumerate(excerpts)
        }
        user_query = _build_user_query(
            storyline_name=storyline_name,
            storyline_description=storyline_description,
            entry_count=len(excerpts),
        )

        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
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
        except Exception:  # noqa: BLE001 — provider failures surface as empty
            log.exception("Storyline narrator API call failed")
            return NarrativeResult(model_used=self._model)

        segments = _parse_narrative_response(response, document_to_entry)
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
) -> str:
    desc = storyline_description.strip() or storyline_name
    return (
        f"Compose a third-person narrative about: {storyline_name}.\n\n"
        f"Description: {desc}\n\n"
        f"You have {entry_count} journal entries to draw on, attached as a "
        f"document with one block per entry, in chronological order. Cite "
        f"every claim. Write naturally and concisely — quality of grounding "
        f"matters more than length."
    )


def _parse_narrative_response(
    response: Any,  # noqa: ANN401
    document_to_entry: dict[int, int],
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
            segments.append(citation_segment(entry_id, cited_text))
    return segments


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
