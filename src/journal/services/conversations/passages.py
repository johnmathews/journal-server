"""Passage selection, truncation, and citation helpers for replies.

Pure functions — no I/O. `window_passage` centers an entry's truncation
window on the span that actually matched the query (matched-chunk
truncation) instead of taking the first N chars. `select_passages`
adapts how many passages to keep to the rerank-score distribution.
`build_citations` resolves cited entry ids back to preview snippets.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from journal.models import SearchResult

#: FTS5 snippet() wraps matched terms with these control characters.
_FTS_MARK_START = "\x02"
_FTS_MARK_END = "\x03"


def window_passage(result: SearchResult, max_chars: int) -> str:
    """Return up to `max_chars` of `result.text`, centered on the match.

    Locates the matched span via (1) the top dense `matching_chunks`
    offset, else (2) the FTS5 `snippet` control-char position, else (3)
    falls back to head truncation. The window is clamped to the text
    bounds. Always returns at most `max_chars` characters.
    """
    text = result.text
    if len(text) <= max_chars:
        return text

    center = _match_center(result, text)
    if center is None:
        return text[:max_chars]

    half = max_chars // 2
    start = max(0, min(center - half, len(text) - max_chars))
    return text[start : start + max_chars]


def _match_center(result: SearchResult, text: str) -> int | None:
    """Character index to center the window on, or None for head-truncate."""
    for chunk in result.matching_chunks:
        if chunk.char_start is not None and chunk.char_end is not None:
            return (chunk.char_start + chunk.char_end) // 2
    if result.snippet:
        marked = result.snippet.find(_FTS_MARK_START)
        if marked >= 0:
            term = result.snippet[marked + 1 :].split(_FTS_MARK_END, 1)[0]
            pos = text.find(term) if term else -1
            if pos >= 0:
                return pos + len(term) // 2
    return None
