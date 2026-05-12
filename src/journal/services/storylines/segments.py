"""Segment helpers for storyline panels.

A panel is a list of segment dicts. Each dict is either:

    {"kind": "text", "text": "..."}                 — plain prose
    {"kind": "citation", "entry_id": 42, "quote": "..."}  — cited excerpt

We keep segments as plain dicts (not dataclasses) because they're
serialised straight to JSON on the wire and into the SQLite
``storyline_panels.segments_json`` column. Helpers here exist so
producers (narrator, glue, curation builder) build well-shaped
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


def citation_segment(entry_id: int, quote: str) -> dict[str, Any]:
    """Build a citation segment. ``entry_id`` is the integer id of the
    source journal entry; the webapp renders this as a router link to
    ``/entries/{entry_id}``. ``quote`` is the verbatim excerpt from
    that entry that the citation refers to."""
    return {
        "kind": SEGMENT_KIND_CITATION,
        "entry_id": int(entry_id),
        "quote": quote,
    }


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
        return (
            isinstance(value.get("entry_id"), int)
            and isinstance(value.get("quote"), str)
        )
    return False
