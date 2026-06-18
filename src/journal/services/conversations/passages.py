"""Passage selection, truncation, and citation helpers for replies.

Pure functions — no I/O. `window_passage` centers an entry's truncation
window on the span that actually matched the query (matched-chunk
truncation) instead of taking the first N chars. `select_passages`
adapts how many passages to keep to the rerank-score distribution.
`build_citations` resolves cited entry ids back to preview snippets.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from journal.providers.answerer import AnswerPassage

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


def select_passages(
    results: list[SearchResult],
    *,
    max_chars: int,
    floor: int = 3,
    ceiling: int = 15,
    band: float = 0.5,
) -> list[AnswerPassage]:
    """Pick an adaptive number of passages from ranked `results`.

    `results` must be ordered by rerank score descending. Keeps every
    result whose score is within `band` (relative to the top score) of
    the top, then clamps the count to `[floor, ceiling]`. Each kept
    result is truncated with `window_passage`.
    """
    if not results:
        return []
    top = results[0].score
    cutoff = top * (1.0 - band) if top > 0 else 0.0
    kept = [r for r in results if r.score >= cutoff]
    n = max(floor, min(len(kept), ceiling))
    n = min(n, len(results))
    chosen = results[:n]
    return [
        AnswerPassage(
            entry_id=r.entry_id,
            entry_date=r.entry_date,
            text=window_passage(r, max_chars),
        )
        for r in chosen
    ]


def build_citations(
    cited_entry_ids: list[int],
    by_id: dict[int, tuple[str, str]],
    *,
    snippet_chars: int = 160,
) -> list[dict]:
    """Resolve cited entry ids to citation dicts, dropping unknown ids.

    `by_id` maps entry_id -> (entry_date, text). Preserves the order of
    `cited_entry_ids`. Mirrors the citation shape the conversation repo
    persists.
    """
    out: list[dict] = []
    for eid in cited_entry_ids:
        if eid not in by_id:
            continue
        date, text = by_id[eid]
        out.append(
            {
                "entry_id": eid,
                "entry_date": date,
                "snippet": text[:snippet_chars].strip(),
            }
        )
    return out
