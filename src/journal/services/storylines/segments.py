"""Segment helpers for storyline panels.

A panel is a list of segment dicts. Each dict is either:

    {"kind": "text", "text": "..."}                 — plain prose
    {"kind": "citation", "entry_id": 42, "quote": "..."}  — cited excerpt

We keep segments as plain dicts (not dataclasses) because they're
serialised straight to JSON on the wire and into the
``storyline_chapters.segments_json`` column. Helpers here exist so
producers (the narrator, via ``StorylineEngine``) build well-shaped
dicts without re-inventing the keys each time.
"""

from __future__ import annotations

from typing import Any

SEGMENT_KIND_TEXT = "text"
SEGMENT_KIND_CITATION = "citation"


def text_segment(text: str) -> dict[str, Any]:
    """Build a plain-text segment. Empty/whitespace text is allowed but
    callers should avoid it — empty segments still render as a no-op
    on the webapp and just bloat the JSON."""
    return {"kind": SEGMENT_KIND_TEXT, "text": text}


def citation_segment(
    entry_id: int, quote: str, entry_date: str | None = None
) -> dict[str, Any]:
    """Build a citation segment. ``entry_id`` is the integer id of the
    source journal entry; the webapp renders this as a router link to
    ``/entries/{entry_id}``. ``quote`` is the verbatim excerpt from
    that entry that the citation refers to. ``entry_date`` is the
    ISO ``YYYY-MM-DD`` date of the cited entry — when present the
    webapp uses it for the absolute-date toggle on the curation
    panel and for the date eyebrows in the narrative panel. Optional
    so historical panels stored before the field existed still
    deserialise."""
    seg: dict[str, Any] = {
        "kind": SEGMENT_KIND_CITATION,
        "entry_id": int(entry_id),
        "quote": quote,
    }
    if entry_date is not None:
        seg["entry_date"] = str(entry_date)
    return seg


def collect_source_entry_ids(segments: list[dict[str, Any]]) -> list[int]:
    """Return the deduplicated, order-preserving list of entry IDs
    referenced by citation segments. Useful for the
    ``source_entry_ids`` field on a panel — lets the UI show "N
    entries cited" without scanning the segments list.
    """
    seen: set[int] = set()
    out: list[int] = []
    for seg in segments:
        if seg.get("kind") != SEGMENT_KIND_CITATION:
            continue
        eid_raw = seg.get("entry_id")
        if eid_raw is None:
            continue
        eid = int(eid_raw)
        if eid in seen:
            continue
        seen.add(eid)
        out.append(eid)
    return out


def count_citations(segments: list[dict[str, Any]]) -> int:
    """Return the number of citation segments (not deduplicated — this
    is the raw count, useful for the ``citation_count`` column)."""
    return sum(1 for s in segments if s.get("kind") == SEGMENT_KIND_CITATION)


def is_valid_segment(value: Any) -> bool:
    """Sanity check used by parsers: ``value`` is a dict shaped like
    one of the recognised kinds."""
    if not isinstance(value, dict):
        return False
    kind = value.get("kind")
    if kind == SEGMENT_KIND_TEXT:
        return isinstance(value.get("text"), str)
    if kind == SEGMENT_KIND_CITATION:
        if not (
            isinstance(value.get("entry_id"), int)
            and isinstance(value.get("quote"), str)
        ):
            return False
        entry_date = value.get("entry_date")
        return entry_date is None or isinstance(entry_date, str)
    return False
