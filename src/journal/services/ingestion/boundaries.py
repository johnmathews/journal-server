"""Pure content-window extraction for image entries.

The OCR model brackets the target entry with ``ENTRY_BEGINS`` /
``ENTRY_ENDS`` tokens (see ``journal.providers.ocr``). This module turns
those tokens into a half-open ``[start, end)`` window into the
marker-stripped text and re-anchors uncertain spans. It is deliberately
pure (no I/O, no model calls) so the irreversible "what is in the entry"
decision is fully unit-testable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from journal.providers.ocr import ENTRY_BEGINS, ENTRY_ENDS, PageRole

log = logging.getLogger(__name__)


def assign_roles(n: int) -> list[PageRole]:
    """Role per page for an ``n``-page upload, in page order.

    - n == 0  → []
    - n == 1  → [ONLY]
    - n >= 2  → [FIRST, MIDDLE*, LAST]
    """
    if n <= 0:
        return []
    if n == 1:
        return [PageRole.ONLY]
    return [PageRole.FIRST] + [PageRole.MIDDLE] * (n - 2) + [PageRole.LAST]


@dataclass(frozen=True)
class ContentWindow:
    """Half-open ``[start, end)`` window into marker-stripped text.

    Attributes:
        text:  The original text with all ``ENTRY_BEGINS``/``ENTRY_ENDS``
               tokens removed.
        start: Index of the first character belonging to the target entry
               (0 when no ``ENTRY_BEGINS`` marker was present).
        end:   One past the last character of the target entry
               (``len(text)`` when no ``ENTRY_ENDS`` marker was present).
        spans: Input spans shifted to clean-text coordinates.  Spans that
               were entirely inside a removed marker are dropped; spans
               that straddled a marker boundary are clipped.
    """

    text: str
    start: int
    end: int
    spans: list[tuple[int, int]] = field(default_factory=list)


def extract_content_window(
    text: str, spans: list[tuple[int, int]]
) -> ContentWindow:
    """Strip entry-bracket markers and compute the content window.

    A single left-to-right scan over ``text`` simultaneously:
    - builds the clean text (markers removed),
    - records each marker's kind (B / E) and its clean-text offset,
    - shifts spans into clean-text coordinates.

    Rules:
    - ``start`` = clean offset of the first ``ENTRY_BEGINS`` (default 0).
    - ``end``   = clean offset of the first ``ENTRY_ENDS`` at or after
                  ``start`` (default ``len(clean)``).
    - If the window is crossed / inverted (``start > end`` or otherwise
      invalid), falls back to ``(clean, 0, len(clean), clean_spans)``
      and logs a warning.
    - Never raises.
    """
    # Single pass: collect (kind, clean_offset) for every marker found.
    markers: list[tuple[str, int]] = []   # ("B"|"E", clean_offset)
    removed: list[tuple[int, int]] = []   # (orig_start, orig_end) of removed tokens
    out: list[str] = []
    cursor = 0

    while cursor < len(text):
        nb = text.find(ENTRY_BEGINS, cursor)
        ne = text.find(ENTRY_ENDS, cursor)

        # Pick the nearest marker; prefer BEGINS on a tie.
        if nb == -1 and ne == -1:
            out.append(text[cursor:])
            break

        if nb == -1:
            idx, token, kind = ne, ENTRY_ENDS, "E"
        elif ne == -1 or nb <= ne:
            idx, token, kind = nb, ENTRY_BEGINS, "B"
        else:
            idx, token, kind = ne, ENTRY_ENDS, "E"

        out.append(text[cursor:idx])
        token_end = idx + len(token)
        removed.append((idx, token_end))
        # clean_offset: position in clean text right after the removed region,
        # i.e. where the next character will land once the suffix is appended.
        clean_offset = sum(len(s) for s in out)
        # For ENTRY_BEGINS, the marker is emitted "on its own line", so the
        # character immediately following in the original text is a newline
        # that separates the marker line from the entry body.  That newline
        # will appear in the clean text; advance clean_offset past it so that
        # ``start`` lands on the first content character.
        if kind == "B" and token_end < len(text) and text[token_end] == "\n":
            clean_offset += 1
        markers.append((kind, clean_offset))
        cursor = token_end

    clean = "".join(out)

    # Helper: shift an original-text position into clean-text coordinates.
    def shift(pos: int) -> int:
        return pos - sum(re - rs for rs, re in removed if re <= pos)

    # Shift input spans; drop any fully inside a removed marker region.
    clean_spans: list[tuple[int, int]] = []
    for s, e in spans:
        if any(s >= rs and e <= re for rs, re in removed):
            continue
        clean_spans.append((shift(s), shift(e)))

    # Derive start / end from the recorded markers.
    start = 0
    end = len(clean)
    start_set = False
    end_set = False

    for kind, clean_off in markers:
        if kind == "B" and not start_set:
            start = clean_off
            start_set = True
        elif kind == "E" and start_set and not end_set and clean_off >= start:
            end = clean_off
            end_set = True
        elif kind == "E" and not start_set and not end_set:
            # ENDS before any BEGINS — will likely produce an inverted window.
            end = clean_off
            end_set = True

    if not (0 <= start <= end <= len(clean)):
        log.warning(
            "content window markers crossed/inverted (start=%d end=%d len=%d) "
            "— falling back to full text",
            start,
            end,
            len(clean),
        )
        return ContentWindow(text=clean, start=0, end=len(clean), spans=clean_spans)

    return ContentWindow(text=clean, start=start, end=end, spans=clean_spans)
