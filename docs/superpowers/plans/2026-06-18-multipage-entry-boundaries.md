# Multi-page Entry Boundaries Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every image upload produce exactly one journal entry whose true text is bracketed by the OCR model, so a previous entry's tail (first page) or a next entry's start (last page) is kept verbatim in `raw_text` but greyed in the UI and excluded from search/embeddings/mood.

**Architecture:** The vision model brackets the target entry with `<<<ENTRY BEGINS>>>` / `<<<ENTRY ENDS>>>` tokens, guided by a per-page role (FIRST/MIDDLE/LAST/ONLY). A pure-Python function turns those tokens into a half-open **content window** `[content_start_char, content_end_char)` into `raw_text`. All derived artifacts (`final_text`, chunks, embeddings, mood) come from the in-bounds slice. Both image ingestion paths converge on one `_ingest_pages` method; the multi-entry-per-page fan-out is removed. The webapp greys out-of-bounds text and lets the user adjust/reset the window via PATCH.

**Tech Stack:** Python 3.13, uv, pytest, ruff, SQLite (migrations via `PRAGMA user_version`); Vue 3 + TypeScript, Vitest, Pinia.

## Global Constraints

- Python: type annotations everywhere; `uv run pytest`, `uv run ruff check src/ tests/` must pass.
- Webapp: `npm run test:coverage`, `npm run lint`, `npm run build` must pass; coverage ≥ 85% on all four metrics.
- Spec of record: `server/docs/superpowers/specs/2026-06-18-multipage-entry-boundaries-design.md`.
- Content window is half-open `[start, end)` in `raw_text` coordinates, same convention as `entry_uncertain_spans` (migration `0005`). `NULL` = whole text.
- Marker tokens: `ENTRY_BEGINS = "<<<ENTRY BEGINS>>>"`, `ENTRY_ENDS = "<<<ENTRY ENDS>>>"`. They are control tokens — never stored in `raw_text`.
- `page_role=None` on `OCRProvider.extract` must reproduce today's prompt verbatim (backward compatible).
- Commit after every task. Two repos → commit `server/` and `webapp/` separately. After pushing, watch CI (`gh run watch`) until green.
- Bug-fix/feature workflow: write the failing test first.

---

## Phase 1 — Server

### Task 1: Boundary marker constants + page roles

**Files:**
- Modify: `src/journal/providers/ocr.py` (add `PageRole`, marker constants, role-prompt map)
- Test: `tests/test_providers/test_ocr_roles.py` (create)

**Interfaces:**
- Produces: `class PageRole(StrEnum)` with members `FIRST, MIDDLE, LAST, ONLY`; module constants `ENTRY_BEGINS: str`, `ENTRY_ENDS: str`; function `role_prompt_clause(role: PageRole | None) -> str` returning `""` for `None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_providers/test_ocr_roles.py
from journal.providers.ocr import (
    ENTRY_BEGINS,
    ENTRY_ENDS,
    PageRole,
    role_prompt_clause,
)


def test_marker_tokens_are_distinct_triple_angle():
    assert ENTRY_BEGINS == "<<<ENTRY BEGINS>>>"
    assert ENTRY_ENDS == "<<<ENTRY ENDS>>>"
    assert ENTRY_BEGINS != ENTRY_ENDS


def test_none_role_yields_empty_clause():
    assert role_prompt_clause(None) == ""


def test_first_role_mentions_begins_not_ends():
    clause = role_prompt_clause(PageRole.FIRST)
    assert ENTRY_BEGINS in clause
    assert ENTRY_ENDS not in clause


def test_last_role_mentions_ends_not_begins():
    clause = role_prompt_clause(PageRole.LAST)
    assert ENTRY_ENDS in clause
    assert ENTRY_BEGINS not in clause


def test_middle_role_mentions_neither_marker():
    clause = role_prompt_clause(PageRole.MIDDLE)
    assert ENTRY_BEGINS not in clause
    assert ENTRY_ENDS not in clause


def test_only_role_mentions_both_markers():
    clause = role_prompt_clause(PageRole.ONLY)
    assert ENTRY_BEGINS in clause
    assert ENTRY_ENDS in clause
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_providers/test_ocr_roles.py -v`
Expected: FAIL with `ImportError: cannot import name 'PageRole'`.

- [ ] **Step 3: Add the enum, constants, and clause builder**

In `src/journal/providers/ocr.py`, near the top (after the existing imports add `from enum import StrEnum` if not present), and beside the existing `UNCERTAIN_OPEN`/`SYSTEM_PROMPT` block:

```python
class PageRole(StrEnum):
    """Where a page sits in a multi-page upload — drives the OCR prompt."""

    FIRST = "first"
    MIDDLE = "middle"
    LAST = "last"
    ONLY = "only"


ENTRY_BEGINS = "<<<ENTRY BEGINS>>>"
ENTRY_ENDS = "<<<ENTRY ENDS>>>"

_ROLE_CLAUSES: dict[PageRole, str] = {
    PageRole.FIRST: (
        "\n\nThis is the FIRST page of a journal entry that continues onto "
        "later pages. If text belonging to a PREVIOUS, already-finished entry "
        f"sits above this entry's first line, emit `{ENTRY_BEGINS}` on its own "
        "line immediately before this entry's first line. Never emit "
        f"`{ENTRY_ENDS}` — the entry continues past this page."
    ),
    PageRole.MIDDLE: (
        "\n\nThis is a MIDDLE page of a single ongoing entry — a pure "
        f"continuation. Do NOT emit `{ENTRY_BEGINS}` or `{ENTRY_ENDS}`."
    ),
    PageRole.LAST: (
        "\n\nThis is the LAST page of the entry; the entry ends on this page. "
        "If a DIFFERENT, new entry begins below where this entry ends (for "
        f"example a fresh date heading), emit `{ENTRY_ENDS}` on its own line "
        "immediately after this entry's last line. Never emit "
        f"`{ENTRY_BEGINS}`."
    ),
    PageRole.ONLY: (
        "\n\nThis image is a COMPLETE entry on a single page. If a previous "
        f"entry's tail sits above it, emit `{ENTRY_BEGINS}` on its own line "
        "immediately before this entry's first line. If a different, new entry "
        f"begins below it, emit `{ENTRY_ENDS}` on its own line immediately "
        "after this entry's last line. Emit each marker at most once."
    ),
}


def role_prompt_clause(role: PageRole | None) -> str:
    """Return the system-prompt addendum for a page role (``""`` if None)."""
    if role is None:
        return ""
    return _ROLE_CLAUSES[role]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_providers/test_ocr_roles.py -v`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add src/journal/providers/ocr.py tests/test_providers/test_ocr_roles.py
git commit -m "feat(ocr): add PageRole + entry-bracket marker prompt clauses"
```

---

### Task 2: Thread `page_role` through the OCR protocol and both adapters

**Files:**
- Modify: `src/journal/providers/ocr.py` (`OCRProvider` Protocol `extract`, `AnthropicOCRProvider.extract`, `GeminiOCRProvider.extract`)
- Test: `tests/test_providers/test_ocr_roles.py` (extend)

**Interfaces:**
- Consumes: `PageRole`, `role_prompt_clause` (Task 1).
- Produces: `OCRProvider.extract(self, image_data: bytes, media_type: str, page_role: PageRole | None = None) -> OCRResult` on the Protocol and both adapters. The role clause is appended to the request user-text (NOT the cached system block, so caching stays effective).

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_providers/test_ocr_roles.py
from journal.providers.ocr import OCRProvider, PageRole


class _FakeExtractor:
    """Minimal stand-in asserting the protocol's new default arg exists."""

    def extract(self, image_data, media_type, page_role=None):
        return (image_data, media_type, page_role)


def test_extract_accepts_optional_page_role_and_defaults_none():
    fake = _FakeExtractor()
    # default omitted → None
    assert fake.extract(b"x", "image/png")[2] is None
    # explicit role passes through
    assert fake.extract(b"x", "image/png", PageRole.FIRST)[2] is PageRole.FIRST
    # structural protocol check
    assert isinstance(fake, OCRProvider)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_providers/test_ocr_roles.py::test_extract_accepts_optional_page_role_and_defaults_none -v`
Expected: FAIL (`isinstance` against the runtime-checkable Protocol fails because the current signature has no `page_role`, or `OCRProvider` is not yet importable with the new shape).

- [ ] **Step 3: Update the Protocol and both adapters**

In `src/journal/providers/ocr.py`:

Protocol (find the `class OCRProvider(Protocol)` `extract` stub) — change to:

```python
    def extract(
        self, image_data: bytes, media_type: str, page_role: PageRole | None = None
    ) -> OCRResult: ...
```

`AnthropicOCRProvider.extract` (currently `def extract(self, image_data: bytes, media_type: str) -> OCRResult:` at ~line 343) — add the param and append the clause to the user text block:

```python
    def extract(
        self, image_data: bytes, media_type: str, page_role: PageRole | None = None
    ) -> OCRResult:
        ...
        user_text = "Extract all handwritten text from this image."
        user_text += role_prompt_clause(page_role)
        message = self._client.messages.create(
            ...
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {...}},
                        {"type": "text", "text": user_text},
                    ],
                }
            ],
        )
```

(Replace the inline `"text": "Extract all handwritten text from this image."` with `"text": user_text`.)

`GeminiOCRProvider.extract` — same change: add `page_role: PageRole | None = None`, and append `role_prompt_clause(page_role)` to whatever per-request prompt string the Gemini adapter sends (the equivalent user instruction text). Do NOT modify the cached `self._system_text`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_providers/ -v`
Expected: PASS. Then `uv run pytest -m "not integration" -q` to confirm no caller broke (all callers pass two positional args; the new third arg defaults to `None`).

- [ ] **Step 5: Commit**

```bash
git add src/journal/providers/ocr.py tests/test_providers/test_ocr_roles.py
git commit -m "feat(ocr): thread optional page_role through extract() adapters"
```

---

### Task 3: Pure content-window extraction (`boundaries.py`)

**Files:**
- Create: `src/journal/services/ingestion/boundaries.py`
- Test: `tests/test_services/test_boundaries.py` (create)

**Interfaces:**
- Consumes: `ENTRY_BEGINS`, `ENTRY_ENDS`, `PageRole` (Task 1).
- Produces:
  - `assign_roles(n: int) -> list[PageRole]` — `[ONLY]` for n==1; `[FIRST, MIDDLE*, LAST]` for n≥2.
  - `@dataclass(frozen=True) class ContentWindow: text: str; start: int; end: int; spans: list[tuple[int, int]]`.
  - `extract_content_window(text: str, spans: list[tuple[int, int]]) -> ContentWindow` — strips every `ENTRY_BEGINS`/`ENTRY_ENDS` token from `text`, returns the clean text, the half-open window `[start, end)` (first BEGINS → start, first ENDS at/after start → end; defaults `0`/`len`), and the input spans shifted to clean-text coordinates (spans fully inside a removed marker are dropped; spans straddling are clipped). Never raises; on a crossed/inverted window falls back to `(clean_text, 0, len(clean_text), spans)` and logs a warning.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_services/test_boundaries.py
from journal.providers.ocr import ENTRY_BEGINS, ENTRY_ENDS, PageRole
from journal.services.ingestion.boundaries import (
    ContentWindow,
    assign_roles,
    extract_content_window,
)


def test_assign_roles_single():
    assert assign_roles(1) == [PageRole.ONLY]


def test_assign_roles_two():
    assert assign_roles(2) == [PageRole.FIRST, PageRole.LAST]


def test_assign_roles_four():
    assert assign_roles(4) == [
        PageRole.FIRST,
        PageRole.MIDDLE,
        PageRole.MIDDLE,
        PageRole.LAST,
    ]


def test_no_markers_is_full_window():
    w = extract_content_window("Hello world", [])
    assert w == ContentWindow(text="Hello world", start=0, end=11, spans=[])


def test_begins_marker_sets_start_and_strips_token():
    text = f"old tail\n{ENTRY_BEGINS}\nMy entry"
    w = extract_content_window(text, [])
    assert ENTRY_BEGINS not in w.text
    # content is everything from "My entry"
    assert w.text[w.start:w.end] == "My entry"
    assert w.text[: w.start] == "old tail\n\n"


def test_ends_marker_sets_end_and_strips_token():
    text = f"My entry\n{ENTRY_ENDS}\nnext entry heading"
    w = extract_content_window(text, [])
    assert ENTRY_ENDS not in w.text
    assert w.text[w.start:w.end] == "My entry\n"
    assert w.text[w.end:] == "\nnext entry heading"


def test_both_markers_window_is_between():
    text = f"tail\n{ENTRY_BEGINS}\nbody\n{ENTRY_ENDS}\nnext"
    w = extract_content_window(text, [])
    assert ENTRY_BEGINS not in w.text and ENTRY_ENDS not in w.text
    assert w.text[w.start:w.end] == "body\n"


def test_spans_shift_after_removed_begins_marker():
    # span covers "body" which sits after the removed BEGINS marker
    prefix = f"{ENTRY_BEGINS}\n"
    text = prefix + "body"
    body_start = len(prefix)
    w = extract_content_window(text, [(body_start, body_start + 4)])
    # after removal the span addresses "body" at the new offset
    assert [w.text[s:e] for s, e in w.spans] == ["body"]


def test_inverted_window_falls_back_to_full_text():
    # ENDS before BEGINS → malformed → full text
    text = f"{ENTRY_ENDS}\nx\n{ENTRY_BEGINS}\ny"
    w = extract_content_window(text, [])
    assert (w.start, w.end) == (0, len(w.text))
    assert ENTRY_BEGINS not in w.text and ENTRY_ENDS not in w.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_services/test_boundaries.py -v`
Expected: FAIL with `ModuleNotFoundError: ... boundaries`.

- [ ] **Step 3: Implement `boundaries.py`**

```python
# src/journal/services/ingestion/boundaries.py
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
    """Role per page for an ``n``-page upload, in page order."""
    if n <= 0:
        return []
    if n == 1:
        return [PageRole.ONLY]
    return (
        [PageRole.FIRST]
        + [PageRole.MIDDLE] * (n - 2)
        + [PageRole.LAST]
    )


@dataclass(frozen=True)
class ContentWindow:
    text: str
    start: int
    end: int
    spans: list[tuple[int, int]] = field(default_factory=list)


def _strip_markers(
    text: str, spans: list[tuple[int, int]]
) -> tuple[str, list[int], list[tuple[int, int]]]:
    """Remove every marker token; return (clean_text, marker_clean_offsets, spans).

    ``marker_clean_offsets`` records, in order of appearance, a tagged
    list is NOT returned here — instead callers re-locate markers via the
    returned mapping. To keep this simple we return the clean text plus
    spans shifted into clean coordinates; marker positions are recovered
    by the caller using the returned ``removed`` ranges.
    """
    # Build clean text by removing each marker occurrence; track removed
    # [orig_start, orig_end) ranges to shift spans.
    removed: list[tuple[int, int]] = []
    cursor = 0
    out: list[str] = []
    # Find marker occurrences left-to-right (either token).
    while cursor < len(text):
        nb = text.find(ENTRY_BEGINS, cursor)
        ne = text.find(ENTRY_ENDS, cursor)
        candidates = [i for i in (nb, ne) if i != -1]
        if not candidates:
            out.append(text[cursor:])
            break
        idx = min(candidates)
        token = ENTRY_BEGINS if idx == nb and (ne == -1 or nb <= ne) else ENTRY_ENDS
        out.append(text[cursor:idx])
        removed.append((idx, idx + len(token)))
        cursor = idx + len(token)
    clean = "".join(out)

    def shift(pos: int) -> int:
        return pos - sum(e - s for s, e in removed if e <= pos)

    clean_spans: list[tuple[int, int]] = []
    for s, e in spans:
        # drop spans fully inside a removed marker; clip otherwise
        if any(s >= rs and e <= re for rs, re in removed):
            continue
        clean_spans.append((shift(s), shift(e)))
    # marker clean-offsets, in original order
    marker_clean_offsets = [shift(s) for s, _ in removed]
    return clean, marker_clean_offsets, clean_spans


def extract_content_window(
    text: str, spans: list[tuple[int, int]]
) -> ContentWindow:
    """Compute the content window from bracket markers in ``text``."""
    begins_idx = text.find(ENTRY_BEGINS)
    ends_idx = text.find(ENTRY_ENDS, begins_idx if begins_idx != -1 else 0)

    clean, marker_offsets, clean_spans = _strip_markers(text, spans)

    # Map the located markers to clean-text offsets. Markers were recorded
    # in appearance order in marker_offsets; recompute start/end from them.
    start = 0
    end = len(clean)
    # Re-scan clean offsets by token type: recompute by stripping again with
    # explicit identity is overkill — instead derive from clean text search
    # is impossible (tokens removed). Use marker_offsets paired with order.
    order: list[str] = []
    cursor = 0
    while cursor < len(text):
        nb = text.find(ENTRY_BEGINS, cursor)
        ne = text.find(ENTRY_ENDS, cursor)
        candidates = [i for i in (nb, ne) if i != -1]
        if not candidates:
            break
        idx = min(candidates)
        is_begin = idx == nb and (ne == -1 or nb <= ne)
        order.append("B" if is_begin else "E")
        cursor = idx + (len(ENTRY_BEGINS) if is_begin else len(ENTRY_ENDS))

    for kind, clean_off in zip(order, marker_offsets, strict=True):
        if kind == "B" and begins_idx != -1 and start == 0:
            start = clean_off
        elif kind == "E" and ends_idx != -1 and end == len(clean):
            end = clean_off

    if not (0 <= start <= end <= len(clean)):
        log.warning(
            "content window markers crossed/inverted (start=%d end=%d len=%d) "
            "— falling back to full text",
            start, end, len(clean),
        )
        return ContentWindow(text=clean, start=0, end=len(clean), spans=clean_spans)

    return ContentWindow(text=clean, start=start, end=end, spans=clean_spans)
```

> Implementer note: the double-scan above is intentional for clarity over cleverness. If you prefer, collapse `_strip_markers` and the order-scan into one pass that records `(kind, clean_offset)` tuples directly — keep the public `ContentWindow` contract and all Step-1 tests green.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_services/test_boundaries.py -v`
Expected: PASS (all tests).

- [ ] **Step 5: Commit**

```bash
git add src/journal/services/ingestion/boundaries.py tests/test_services/test_boundaries.py
git commit -m "feat(ingestion): pure content-window extraction from entry markers"
```

---

### Task 4: Migration 0033 — content-window columns

**Files:**
- Create: `src/journal/db/migrations/0033_entry_content_window.sql`
- Test: `tests/test_db/test_migration_0033.py` (create)

**Interfaces:**
- Produces: `entries.content_start_char INTEGER NULL`, `entries.content_end_char INTEGER NULL`.

- [ ] **Step 1: Query prod for table shape (migration-testing rule)**

Run (read-only; confirms no surprise columns/constraints before writing the migration):

```bash
ssh media "docker exec journal-server sqlite3 /data/journal.db 'PRAGMA table_info(entries);'"
```

Expected: the documented columns; note anything unexpected in the commit message. If you cannot reach prod, proceed but flag it.

- [ ] **Step 2: Write the failing test**

```python
# tests/test_db/test_migration_0033.py
from journal.db.connection import get_connection
from journal.db.migrations import run_migrations


def test_entries_has_content_window_columns(tmp_path):
    db = tmp_path / "t.db"
    conn = get_connection(str(db))
    run_migrations(conn)
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(entries)")}
    assert "content_start_char" in cols
    assert "content_end_char" in cols
    # default NULL
    conn.execute(
        "INSERT INTO entries (user_id, entry_date, source_type, raw_text, "
        "final_text, word_count) VALUES (1,'2026-01-01','photo','hi','hi',1)"
    )
    row = conn.execute(
        "SELECT content_start_char, content_end_char FROM entries"
    ).fetchone()
    assert row["content_start_char"] is None
    assert row["content_end_char"] is None
```

(Match the existing migration-test helper style in `tests/test_db/` if it differs — e.g. a shared fixture that builds a migrated in-memory DB.)

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/test_db/test_migration_0033.py -v`
Expected: FAIL (`content_start_char` not in cols).

- [ ] **Step 4: Write the migration**

```sql
-- src/journal/db/migrations/0033_entry_content_window.sql
-- Content window: half-open [content_start_char, content_end_char) into
-- entries.raw_text. NULL = whole text. Same convention as
-- entry_uncertain_spans (0005). Marks neighbour-entry text on the first/
-- last photographed page so it is kept verbatim but excluded from derived
-- artifacts. Re-runnable: a partial failure leaves user_version unchanged,
-- and a half-applied ALTER is recovered by re-running (the migration
-- runner guards on PRAGMA user_version).
ALTER TABLE entries ADD COLUMN content_start_char INTEGER;
ALTER TABLE entries ADD COLUMN content_end_char INTEGER;
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_db/test_migration_0033.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/journal/db/migrations/0033_entry_content_window.sql tests/test_db/test_migration_0033.py
git commit -m "feat(db): migration 0033 — entry content-window columns"
```

---

### Task 5: Persist + read the content window (model + repository)

**Files:**
- Modify: `src/journal/models.py` (`Entry` dataclass)
- Modify: `src/journal/db/repository/protocol.py` (`_row_to_entry`)
- Modify: `src/journal/db/repository/core.py` (`create_entry`; add `set_content_window`)
- Modify: `src/journal/db/repository/protocol.py` (Protocol stub for `set_content_window`)
- Test: `tests/test_db/test_content_window_repo.py` (create)

**Interfaces:**
- Consumes: migration 0033 (Task 4).
- Produces:
  - `Entry.content_start_char: int | None = None`, `Entry.content_end_char: int | None = None`.
  - `create_entry(..., content_start_char: int | None = None, content_end_char: int | None = None)` persists the two columns.
  - `set_content_window(self, entry_id: int, start: int | None, end: int | None, user_id: int | None = None) -> Entry | None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_db/test_content_window_repo.py
def test_create_entry_persists_window(repo):  # `repo` = your migrated-repo fixture
    e = repo.create_entry(
        "2026-01-01", "photo", "tail\nbody\nnext", 3,
        final_text="body", content_start_char=5, content_end_char=9,
    )
    got = repo.get_entry(e.id)
    assert got.content_start_char == 5
    assert got.content_end_char == 9


def test_create_entry_window_defaults_null(repo):
    e = repo.create_entry("2026-01-01", "photo", "body", 1)
    got = repo.get_entry(e.id)
    assert got.content_start_char is None
    assert got.content_end_char is None


def test_set_content_window_updates_and_clears(repo):
    e = repo.create_entry("2026-01-01", "photo", "tail body next", 3)
    repo.set_content_window(e.id, 5, 9)
    assert repo.get_entry(e.id).content_start_char == 5
    repo.set_content_window(e.id, None, None)
    assert repo.get_entry(e.id).content_start_char is None
    assert repo.get_entry(e.id).content_end_char is None
```

Use the same repository fixture the other `tests/test_db/` files use; if none, build one that runs migrations against an in-memory DB and wraps it in `SQLiteEntryRepository`.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_db/test_content_window_repo.py -v`
Expected: FAIL (`create_entry` has no `content_start_char` kwarg).

- [ ] **Step 3: Implement**

`src/journal/models.py` — add fields to `Entry` (after `doubts_verified`):

```python
    content_start_char: int | None = None
    content_end_char: int | None = None
```

`src/journal/db/repository/protocol.py::_row_to_entry` — add (sqlite3.Row returns `None` for NULL):

```python
        content_start_char=row["content_start_char"],
        content_end_char=row["content_end_char"],
```

Add to the Protocol class:

```python
    def set_content_window(
        self, entry_id: int, start: int | None, end: int | None,
        user_id: int | None = None,
    ) -> Entry | None: ...
```

`src/journal/db/repository/core.py::create_entry` — extend signature and INSERT:

```python
    def create_entry(
        self, entry_date: str, source_type: str, raw_text: str, word_count: int,
        final_text: str | None = None,
        user_id: int = 1,
        content_start_char: int | None = None,
        content_end_char: int | None = None,
    ) -> Entry:
        actual_final = final_text if final_text is not None else raw_text
        sql = (
            "INSERT INTO entries"
            " (user_id, entry_date, source_type, raw_text, final_text, word_count,"
            "  content_start_char, content_end_char)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
        )
        params = (
            user_id, entry_date, source_type, raw_text, actual_final, word_count,
            content_start_char, content_end_char,
        )
        conn = self._conn()
        with conn:
            cursor = conn.execute(sql, params)
        entry_id = cursor.lastrowid
        log.info("Created entry %d for date %s", entry_id, entry_date)
        return self.get_entry(entry_id)  # type: ignore[return-value]
```

Add `set_content_window` to `_CoreMixin`:

```python
    def set_content_window(
        self, entry_id: int, start: int | None, end: int | None,
        user_id: int | None = None,
    ) -> "Entry | None":
        conn = self._conn()
        sql = "UPDATE entries SET content_start_char = ?, content_end_char = ?"
        params: list[object] = [start, end]
        sql += " WHERE id = ?"
        params.append(entry_id)
        if user_id is not None:
            sql += " AND user_id = ?"
            params.append(user_id)
        with conn:
            conn.execute(sql, params)
        return self.get_entry(entry_id, user_id=user_id)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_db/test_content_window_repo.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/journal/models.py src/journal/db/repository/ tests/test_db/test_content_window_repo.py
git commit -m "feat(db): persist + read + update entry content window"
```

---

### Task 6: Unify image ingestion on `_ingest_pages`; remove fan-out

**Files:**
- Modify: `src/journal/services/ingestion/image.py` (add `_ingest_pages`; rewrite `ingest_image` and `ingest_multi_page_entry` as thin wrappers; delete `ingest_image_entries`, `_create_entry_from_image_segment`, `split_text_into_entries`, `ENTRY_DELIMITER`)
- Test: `tests/test_services/test_ingestion_boundaries.py` (create)
- Test: `tests/test_services/test_ingestion.py` (update/remove now-invalid split tests)

**Interfaces:**
- Consumes: `assign_roles`, `extract_content_window` (Task 3); `set_content_window`, `create_entry(content_*)` (Task 5); `OCRProvider.extract(..., page_role=)` (Task 2).
- Produces:
  - `_ingest_pages(self, images: list[tuple[bytes, str]], date: str, *, skip_mood: bool = False, on_progress: Callable[[int, int], None] | None = None, user_id: int = 1) -> Entry`.
  - `ingest_image(self, image_data, media_type, date, *, skip_mood=False, user_id=1) -> Entry` (unchanged signature; now one entry).
  - `ingest_multi_page_entry(...)` (unchanged signature).
  - `ingest_image_entries`, `_create_entry_from_image_segment`, `split_text_into_entries`, `ENTRY_DELIMITER` no longer exist.

- [ ] **Step 1: Write the failing test (fake role-aware OCR provider)**

```python
# tests/test_services/test_ingestion_boundaries.py
from journal.providers.ocr import ENTRY_BEGINS, ENTRY_ENDS, OCRResult, PageRole


class _RoleOCR:
    """Returns canned text per page; records the roles it was given."""

    def __init__(self, pages: list[str]):
        self._pages = pages
        self._i = 0
        self.roles: list[PageRole | None] = []

    def extract(self, image_data, media_type, page_role=None):
        self.roles.append(page_role)
        text = self._pages[self._i]
        self._i += 1
        return OCRResult(text=text, uncertain_spans=[])


def test_single_image_only_role_trims_tail_and_next(make_ingestion):
    # make_ingestion = fixture wiring IngestionService with a given OCR provider
    ocr = _RoleOCR([f"prev tail\n{ENTRY_BEGINS}\nMy entry body\n{ENTRY_ENDS}\nnext"])
    svc = make_ingestion(ocr)
    entry = svc.ingest_image(b"img", "image/png", "2026-01-01")
    assert ocr.roles == [PageRole.ONLY]
    # raw_text keeps everything (markers stripped), window isolates the body
    assert "prev tail" in entry.raw_text and "next" in entry.raw_text
    assert ENTRY_BEGINS not in entry.raw_text
    assert entry.raw_text[entry.content_start_char:entry.content_end_char] == "My entry body\n"
    # final_text (reading view) is the in-bounds slice only
    assert "prev tail" not in entry.final_text and "next" not in entry.final_text


def test_multi_page_roles_and_first_last_trim(make_ingestion):
    ocr = _RoleOCR([
        f"yesterday tail\n{ENTRY_BEGINS}\nPage one body",   # FIRST
        "page two body",                                     # MIDDLE
        f"page three end\n{ENTRY_ENDS}\ntomorrow heading",   # LAST
    ])
    svc = make_ingestion(ocr)
    entry = svc.ingest_multi_page_entry(
        [(b"a", "image/png"), (b"b", "image/png"), (b"c", "image/png")],
        "2026-01-01",
    )
    assert ocr.roles == [PageRole.FIRST, PageRole.MIDDLE, PageRole.LAST]
    content = entry.raw_text[entry.content_start_char:entry.content_end_char]
    assert "yesterday tail" not in content
    assert "tomorrow heading" not in content
    assert "Page one body" in content and "page three end" in content


def test_no_markers_leaves_window_null(make_ingestion):
    ocr = _RoleOCR(["just one clean entry"])
    svc = make_ingestion(ocr)
    entry = svc.ingest_image(b"img", "image/png", "2026-01-01")
    assert entry.content_start_char is None
    assert entry.content_end_char is None
```

Add a `make_ingestion` fixture (in this file or `conftest.py`) that builds `IngestionService` with an in-memory repo + the given OCR provider + a stub vector store + a heading detector, mirroring the existing ingestion-test fixtures in `tests/test_services/test_ingestion.py`. Reuse those fixtures if they already expose a way to inject the OCR provider.

> Window-NULL rule: when `extract_content_window` returns `start == 0 and end == len(clean_text)` (no markers), store `NULL`/`NULL` rather than `0`/`len`, so "no trimming" is distinguishable from "trimmed to the whole text" and matches existing entries.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_services/test_ingestion_boundaries.py -v`
Expected: FAIL (roles not passed / window not set / `final_text` still includes neighbor text).

- [ ] **Step 3: Implement `_ingest_pages` and rewire**

Add to `_ImageIngestMixin` (replacing the bodies of `ingest_image`, `ingest_multi_page_entry`, and deleting the fan-out helpers + `split_text_into_entries` + `ENTRY_DELIMITER`):

```python
    def ingest_image(
        self, image_data: bytes, media_type: str, date: str, *,
        skip_mood: bool = False, user_id: int = 1,
    ) -> "Entry":
        """OCR a single page image into one entry (see _ingest_pages)."""
        return self._ingest_pages(
            [(image_data, media_type)], date,
            skip_mood=skip_mood, user_id=user_id,
        )

    def ingest_multi_page_entry(
        self, images: list[tuple[bytes, str]], date: str, *,
        skip_mood: bool = False,
        on_progress: "Callable[[int, int], None] | None" = None,
        user_id: int = 1,
    ) -> "Entry":
        """OCR multiple page images into one entry (see _ingest_pages)."""
        return self._ingest_pages(
            images, date, skip_mood=skip_mood,
            on_progress=on_progress, user_id=user_id,
        )

    def _ingest_pages(
        self, images: list[tuple[bytes, str]], date: str, *,
        skip_mood: bool = False,
        on_progress: "Callable[[int, int], None] | None" = None,
        user_id: int = 1,
    ) -> "Entry":
        from journal.services.date_extraction import extract_date_from_text
        from journal.services.ingestion.boundaries import (
            assign_roles,
            extract_content_window,
        )

        if not images:
            raise ValueError("At least one image is required")
        roles = assign_roles(len(images))

        page_results: list[OCRResult] = []
        page_hashes: list[str] = []
        page_media_types: list[str] = []
        for i, (image_data, media_type) in enumerate(images):
            file_hash = hashlib.sha256(image_data).hexdigest()
            if self._is_duplicate(file_hash):  # type: ignore[attr-defined]
                raise ValueError(
                    f"Page {i + 1} has already been uploaded in another entry. "
                    f"Delete the existing entry first if you want to re-upload."
                )
            image_data, media_type = self._maybe_preprocess(  # type: ignore[attr-defined]
                image_data, media_type,
            )
            ocr_result = self._ocr.extract(  # type: ignore[attr-defined]
                image_data, media_type, roles[i],
            )
            if not ocr_result.text.strip():
                raise ValueError(f"OCR extracted no text from page {i + 1}")
            page_results.append(ocr_result)
            page_hashes.append(file_hash)
            page_media_types.append(media_type)
            if on_progress is not None:
                on_progress(i + 1, len(images))

        # Combine pages (single-\n join; see chunking rationale) carrying
        # markers, then resolve the content window.
        combined_with_markers, combined_spans = self._combine_pages(page_results)
        window = extract_content_window(combined_with_markers, combined_spans)
        raw_text = window.text
        content = raw_text[window.start:window.end]
        word_count = len(content.split())

        extracted = extract_date_from_text(content)
        if extracted:
            date = extracted
        det = self._detect_heading(content, date)  # type: ignore[attr-defined]
        if det.date_iso:
            date = det.date_iso
        final_text = det.body if det.has_heading else content

        trimmed = window.start != 0 or window.end != len(raw_text)
        entry = self._repo.create_entry(  # type: ignore[attr-defined]
            date, "photo", raw_text, word_count, user_id=user_id,
            final_text=final_text,
            content_start_char=window.start if trimmed else None,
            content_end_char=window.end if trimmed else None,
        )

        for i, (_image_data, _) in enumerate(images):
            source_file_id = self.store_source_file(  # type: ignore[attr-defined]
                entry.id, f"image_{date}_p{i + 1}",
                page_media_types[i], page_hashes[i],
            )
            self._repo.add_entry_page(  # type: ignore[attr-defined]
                entry.id, i + 1, page_results[i].text, source_file_id,
            )

        self._repo.add_uncertain_spans(entry.id, window.spans)  # type: ignore[attr-defined]
        chunk_count = self._process_text(  # type: ignore[attr-defined]
            entry.id, entry.final_text, date,
            skip_mood=skip_mood, user_id=user_id,
        )
        self._repo.update_chunk_count(entry.id, chunk_count)  # type: ignore[attr-defined]
        log.info(
            "Ingested entry %d: %d page(s), %d words, date %s, window=%s",
            entry.id, len(images), word_count, date,
            (entry.content_start_char, entry.content_end_char),
        )
        return self._repo.get_entry(entry.id)  # type: ignore[attr-defined,return-value]

    def _combine_pages(
        self, page_results: list[OCRResult],
    ) -> tuple[str, list[tuple[int, int]]]:
        """Strip + single-\\n join pages; shift uncertain spans to combined coords."""
        stripped_parts: list[str] = []
        combined_spans: list[tuple[int, int]] = []
        cumulative_offset = 0
        for i, r in enumerate(page_results):
            stripped, shifted = _strip_and_shift_page_spans(
                r.text, r.uncertain_spans, cumulative_offset,
            )
            stripped_parts.append(stripped)
            combined_spans.extend(shifted)
            cumulative_offset += len(stripped)
            if i < len(page_results) - 1:
                cumulative_offset += 1  # the "\n" separator
        return "\n".join(stripped_parts), combined_spans
```

Keep `_strip_and_shift_page_spans` (still used). Delete `split_text_into_entries`, `ENTRY_DELIMITER`, `ingest_image_entries`, and `_create_entry_from_image_segment`.

- [ ] **Step 4: Update/remove obsolete split tests**

In `tests/test_services/test_ingestion.py`, delete or rewrite tests asserting multi-entry splitting / orphan-tail discard (the `split_text_into_entries` tests and the "page splits into N entries" ingestion tests). Replace their intent with the Task-6 boundary tests.

Run: `uv run pytest tests/test_services/test_ingestion.py tests/test_services/test_ingestion_boundaries.py -v`
Expected: PASS (obsolete tests removed; new ones green).

- [ ] **Step 5: Commit**

```bash
git add src/journal/services/ingestion/image.py tests/test_services/test_ingestion.py tests/test_services/test_ingestion_boundaries.py
git commit -m "feat(ingestion): unify image paths on _ingest_pages; window not fan-out"
```

---

### Task 7: Collapse the image-ingestion worker to a single entry

**Files:**
- Modify: `src/journal/services/jobs/workers/image_ingestion.py`
- Test: `tests/test_services/test_jobs/test_image_ingestion_worker.py` (update/extend)

**Interfaces:**
- Consumes: `ingest_image`, `ingest_multi_page_entry` (Task 6) — both now return one `Entry`.
- Produces: worker creates exactly one entry; follow-up jobs use unsuffixed keys; result has no `entry_ids` fan-out.

- [ ] **Step 1: Write/locate the failing test**

```python
# tests/test_services/test_jobs/test_image_ingestion_worker.py (add)
def test_single_image_with_trailing_neighbor_creates_one_entry(worker_ctx):
    # OCR provider returns: body + ENTRY_ENDS + next entry on one image.
    # The worker must create exactly one entry (regression: no fan-out).
    ...
    assert len(created_entry_ids) == 1
    assert "entry_ids" not in result  # no multi-entry key
```

Build on the existing worker test fixtures; assert via the repo that exactly one entry exists for the upload and that `result["follow_up_jobs"]` keys are unsuffixed.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_services/test_jobs/test_image_ingestion_worker.py -v`
Expected: FAIL (current code path still branches on `len(images) == 1` calling `ingest_image_entries`, which no longer exists → Import/AttributeError, or produces fan-out keys).

- [ ] **Step 3: Implement the collapse**

In `image_ingestion.py`, replace the `operation()` branch and the fan-out loop:

```python
        def operation():  # noqa: ANN202 — local helper
            assert ctx.ingestion is not None  # noqa: S101 — guarded above
            ctx.jobs.update_progress(job_id, 0, total)
            if len(images) == 1:
                entry = ctx.ingestion.ingest_image(
                    images[0][0], images[0][1], entry_date,
                    skip_mood=True, user_id=job_user_id or 1,
                )
                ctx.jobs.update_progress(job_id, 1, total)
            else:
                entry = ctx.ingestion.ingest_multi_page_entry(
                    images, entry_date, skip_mood=True,
                    on_progress=progress_callback,
                    user_id=job_user_id or 1,
                )
            return entry

        entry = run_with_retry(
            jobs=ctx.jobs,
            notifier=ctx.notifier,
            job_id=job_id,
            job_type="ingest_images",
            user_id=job_user_id,
            operation=operation,
            log_prefix="Image ingestion",
        )

        ctx.jobs.update_progress(job_id, total, total)

        # Single entry → follow-up jobs keep the unsuffixed pipeline keys.
        follow_up_ids = ctx.queue_post_ingestion_jobs(
            job_id, "Image", entry.id, job_user_id,
        )

        result: dict[str, Any] = {
            "entry_id": entry.id,
            "entry_date": entry.entry_date,
            "source_type": entry.source_type,
            "word_count": entry.word_count,
            "chunk_count": entry.chunk_count,
            "page_count": total,
            "follow_up_jobs": follow_up_ids,
        }
        ctx.jobs.mark_succeeded(job_id, result)
```

(Remove the `entries[-1]` "primary entry" comment block, the `for created_entry in entries` loop, and the `if len(entries) > 1: result["entry_ids"] = ...` block.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_services/test_jobs/ -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/journal/services/jobs/workers/image_ingestion.py tests/test_services/test_jobs/test_image_ingestion_worker.py
git commit -m "feat(jobs): image worker creates exactly one entry (no fan-out)"
```

---

### Task 8: Serialize `content_boundary` in the API

**Files:**
- Modify: `src/journal/api/_shared.py` (`_entry_to_dict`)
- Test: `tests/test_api/test_entry_serialization.py` (add)

**Interfaces:**
- Consumes: `Entry.content_start_char/_end_char` (Task 5).
- Produces: every entry dict carries `content_boundary: {"char_start": int, "char_end": int} | None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_api/test_entry_serialization.py (add)
from journal.api._shared import _entry_to_dict
from journal.models import Entry


def _entry(**kw):
    base = dict(
        id=1, entry_date="2026-01-01", source_type="photo",
        raw_text="tail body next", final_text="body", word_count=1,
    )
    base.update(kw)
    return Entry(**base)


def test_content_boundary_present_when_set():
    d = _entry_to_dict(_entry(content_start_char=5, content_end_char=9))
    assert d["content_boundary"] == {"char_start": 5, "char_end": 9}


def test_content_boundary_null_when_unset():
    d = _entry_to_dict(_entry())
    assert d["content_boundary"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_api/test_entry_serialization.py -v`
Expected: FAIL (`KeyError: 'content_boundary'`).

- [ ] **Step 3: Implement**

In `src/journal/api/_shared.py::_entry_to_dict`, add to the returned dict (after `uncertain_spans`):

```python
        "content_boundary": (
            {
                "char_start": entry.content_start_char,
                "char_end": entry.content_end_char,
            }
            if entry.content_start_char is not None
            and entry.content_end_char is not None
            else None
        ),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_api/test_entry_serialization.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/journal/api/_shared.py tests/test_api/test_entry_serialization.py
git commit -m "feat(api): expose content_boundary on entry responses"
```

---

### Task 9: PATCH accepts + applies a content-window change

**Files:**
- Modify: `src/journal/api/entries.py` (`_patch_entry`)
- Modify: `src/journal/services/ingestion/service.py` (add `update_content_window`)
- Test: `tests/test_api/test_entries_patch_boundary.py` (create)

**Interfaces:**
- Consumes: `set_content_window` (Task 5); `submit_save_entry_pipeline` (existing); `extract_date_from_text` + heading detection (existing) for re-deriving `final_text`.
- Produces:
  - `IngestionService.update_content_window(self, entry_id: int, start: int | None, end: int | None, user_id: int = 1) -> Entry` — sets the window, recomputes `final_text` from `raw_text[start:end]` (heading detection applied; full text when cleared), persists via the existing final-text update path so `word_count` stays correct.
  - `_patch_entry` accepts `content_start_char` / `content_end_char` (both-or-neither; `null`/`null` clears), validates `0 <= start < end <= len(raw_text)`, then reruns the existing save pipeline exactly as the `final_text` branch does.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_api/test_entries_patch_boundary.py
def test_patch_sets_window_and_rederives_final_text(client, seeded_entry):
    # seeded_entry.raw_text == "tail body next" (len 14), no window
    resp = client.patch(
        f"/api/entries/{seeded_entry.id}",
        json={"content_start_char": 5, "content_end_char": 9},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["content_boundary"] == {"char_start": 5, "char_end": 9}
    assert body["final_text"] == "body"
    # pipeline queued
    assert "pipeline_job_id" in body or "reprocess_job_id" in body


def test_patch_clears_window_with_nulls(client, seeded_windowed_entry):
    resp = client.patch(
        f"/api/entries/{seeded_windowed_entry.id}",
        json={"content_start_char": None, "content_end_char": None},
    )
    assert resp.status_code == 200
    assert resp.json()["content_boundary"] is None


def test_patch_rejects_out_of_range_window(client, seeded_entry):
    resp = client.patch(
        f"/api/entries/{seeded_entry.id}",
        json={"content_start_char": 5, "content_end_char": 999},
    )
    assert resp.status_code == 400


def test_patch_rejects_partial_window(client, seeded_entry):
    resp = client.patch(
        f"/api/entries/{seeded_entry.id}",
        json={"content_start_char": 5},
    )
    assert resp.status_code == 400
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_api/test_entries_patch_boundary.py -v`
Expected: FAIL (boundary keys ignored; no re-derive).

- [ ] **Step 3: Implement the service method**

In `src/journal/services/ingestion/service.py`, beside `save_final_text` (~line 337):

```python
    def update_content_window(
        self, entry_id: int, start: int | None, end: int | None,
        user_id: int = 1,
    ) -> "Entry":
        """Set the content window and re-derive final_text from the slice."""
        from journal.services.date_extraction import extract_date_from_text

        entry = self._repo.get_entry(entry_id, user_id=user_id)
        if entry is None:
            raise ValueError(f"Entry {entry_id} not found")
        self._repo.set_content_window(entry_id, start, end, user_id=user_id)
        lo = start if start is not None else 0
        hi = end if end is not None else len(entry.raw_text)
        content = entry.raw_text[lo:hi]
        date = entry.entry_date
        extracted = extract_date_from_text(content)
        if extracted:
            date = extracted
        det = self._detect_heading(content, date)
        final_text = det.body if det.has_heading else content
        # Reuse the existing final-text persistence (keeps word_count etc.).
        return self.save_final_text(entry_id, final_text, user_id=user_id)
```

(If `save_final_text` rejects empty text, guard `content` non-empty before calling and raise a `ValueError` the route maps to 400.)

- [ ] **Step 4: Implement the route changes**

In `_patch_entry` (`src/journal/api/entries.py`), after reading `final_text`/`new_date`, add window parsing and make the "at least one field" check include the window:

```python
        has_start = "content_start_char" in body
        has_end = "content_end_char" in body
        start = body.get("content_start_char")
        end = body.get("content_end_char")

        if final_text is None and new_date is None and not (has_start or has_end):
            return JSONResponse(
                {"error": "At least one of 'final_text', 'entry_date', or "
                          "'content_start_char'/'content_end_char' is required"},
                status_code=400,
            )

        if has_start or has_end:
            if not (has_start and has_end):
                return JSONResponse(
                    {"error": "content_start_char and content_end_char must be "
                              "provided together"},
                    status_code=400,
                )
            if start is None and end is None:
                updated = ingestion_svc.update_content_window(
                    entry_id, None, None, user_id=user_id,
                )
            else:
                if (
                    not isinstance(start, int) or not isinstance(end, int)
                    or not (0 <= start < end <= len(entry.raw_text))
                ):
                    return JSONResponse(
                        {"error": "content window must satisfy "
                                  "0 <= start < end <= len(raw_text)"},
                        status_code=400,
                    )
                updated = ingestion_svc.update_content_window(
                    entry_id, start, end, user_id=user_id,
                )
            # Re-derive triggers the same save pipeline as a text edit.
            _queue_save_pipeline = True
```

Then refactor the existing pipeline-queue block (lines ~214-239) so it runs when `final_text is not None` **or** a window change occurred (`_queue_save_pipeline`). Keep the response assembly identical (it already calls `_entry_to_dict(updated, ...)`, which now includes `content_boundary`).

> Implementer note: the cleanest refactor is to set a `should_queue_pipeline` flag in both the `final_text` branch and the window branch, then run the single pipeline-submit block once afterward. Avoid double-submitting if both `final_text` and a window are sent in one request.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_api/test_entries_patch_boundary.py tests/test_api/ -v`
Expected: PASS.

- [ ] **Step 6: Full server gate + commit**

Run: `uv run pytest -m "not integration" -q && uv run ruff check src/ tests/`
Expected: all pass, no lint errors.

```bash
git add src/journal/api/entries.py src/journal/services/ingestion/service.py tests/test_api/test_entries_patch_boundary.py
git commit -m "feat(api): PATCH content window re-derives final_text + reruns pipeline"
```

---

### Task 10: Server docs + journal

**Files:**
- Modify: `src/journal/services/ingestion/` docs or `docs/` ingestion page (document the content window + one-entry-per-upload rule)
- Modify: `journal/260603-ocr-multi-entry-segmentation.md` (add superseded header)
- Create: `journal/2606XX-entry-content-window.md` (dev journal entry; use today's date `260618`)

- [ ] **Step 1: Add the superseded header to the old journal entry**

At the top of `journal/260603-ocr-multi-entry-segmentation.md`:

```markdown
**Status:** superseded by [entry content window](260618-entry-content-window.md) (2026-06-18). Multi-entry-per-page fan-out was removed; every upload now produces exactly one entry and neighbour text is kept + greyed via the content window.
```

- [ ] **Step 2: Write the new journal entry**

Create `journal/260618-entry-content-window.md` summarizing: the problem (neighbor text on first/last page), the begin/end marker scheme, `PageRole`, the content window model, one-entry-per-upload, and the PATCH adjust/reset contract. Link the spec.

- [ ] **Step 3: Update the relevant `docs/` page**

Find the ingestion doc under `docs/` (e.g. an ingestion or OCR page); add a short "Content window" section describing the half-open window, that derived artifacts use the in-bounds slice, and the API field. Keep it concise.

- [ ] **Step 4: Commit**

```bash
git add docs/ journal/260603-ocr-multi-entry-segmentation.md journal/260618-entry-content-window.md
git commit -m "docs: content window + supersede multi-entry segmentation note"
```

- [ ] **Step 5: Push + watch CI**

```bash
git push -u origin feature/multipage-entry-boundaries
gh run watch
```
Fix failures (full local suite first), recommit, repeat until green. Max 3 attempts, then flag.

---

## Phase 2 — Webapp

### Task 11: Types + API client for `content_boundary`

**Files:**
- Modify: `src/types/entry.ts`
- Modify: `src/api/entries.ts`
- Test: `src/api/__tests__/entries.spec.ts` (add) — match the existing test file location/naming

**Interfaces:**
- Produces:
  - `EntryDetail.content_boundary: { char_start: number; char_end: number } | null`.
  - `updateEntryBoundary(id: number, start: number | null, end: number | null): Promise<UpdateEntryTextResponse>` issuing `PATCH /api/entries/:id` with `content_start_char`/`content_end_char`.

- [ ] **Step 1: Write the failing test**

```typescript
// src/api/__tests__/entries.spec.ts (add)
import { updateEntryBoundary } from '@/api/entries'

it('PATCHes content window offsets', async () => {
  const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
    new Response(JSON.stringify({ id: 1, content_boundary: { char_start: 5, char_end: 9 } }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    }),
  )
  await updateEntryBoundary(1, 5, 9)
  const [, init] = fetchSpy.mock.calls[0]
  expect(init?.method).toBe('PATCH')
  expect(JSON.parse(init?.body as string)).toEqual({
    content_start_char: 5,
    content_end_char: 9,
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm run test:unit -- entries`
Expected: FAIL (`updateEntryBoundary` not exported).

- [ ] **Step 3: Implement**

`src/types/entry.ts` — add to `EntryDetail`:

```typescript
  /** Half-open [char_start, char_end) into raw_text marking the entry's
   *  in-bounds content. null = whole text. Out-of-bounds text is greyed
   *  and excluded from search/embeddings/mood. */
  content_boundary: { char_start: number; char_end: number } | null
```

`src/api/entries.ts` — add:

```typescript
export function updateEntryBoundary(
  id: number,
  start: number | null,
  end: number | null,
): Promise<UpdateEntryTextResponse> {
  return apiFetch<UpdateEntryTextResponse>(`/api/entries/${id}`, {
    method: 'PATCH',
    body: JSON.stringify({ content_start_char: start, content_end_char: end }),
  })
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `npm run test:unit -- entries`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/types/entry.ts src/api/entries.ts src/api/__tests__/entries.spec.ts
git commit -m "feat(webapp): content_boundary type + updateEntryBoundary client"
```

---

### Task 12: Out-of-bounds overlay in `useDiffHighlight`

**Files:**
- Modify: `src/composables/useDiffHighlight.ts`
- Test: `src/composables/__tests__/useDiffHighlight.spec.ts` (add)

**Interfaces:**
- Consumes: `content_boundary` (Task 11).
- Produces: an `applyOutOfBoundsOverlay(segments, boundary, textLength)` step (mirroring `applyUncertainOverlay`) that promotes segments outside `[char_start, char_end)` to an `out-of-bounds` kind; `CLASS_FOR_KIND['out-of-bounds']` renders greyed/struck. `useDiffHighlight` options gain `contentBoundary?: { char_start: number; char_end: number } | null`.

- [ ] **Step 1: Write the failing test**

```typescript
// src/composables/__tests__/useDiffHighlight.spec.ts (add)
import { applyOutOfBoundsOverlay, segmentsToHtml } from '@/composables/useDiffHighlight'

it('greys segments outside the content boundary', () => {
  // raw_text = "tail body next" → window [5,9) == "body"
  const segs = [{ text: 'tail body next', kind: 'equal' as const }]
  const out = applyOutOfBoundsOverlay(segs, { char_start: 5, char_end: 9 }, 14)
  const html = segmentsToHtml(out)
  // "tail " and " next" greyed, "body" plain
  expect(html).toContain('opacity-40')
  expect(html).toContain('body')
  // the in-bounds slice is not inside an out-of-bounds mark
  const bodyIdx = html.indexOf('body')
  const greyBeforeBody = html.lastIndexOf('opacity-40', bodyIdx)
  const closeBeforeBody = html.lastIndexOf('</mark>', bodyIdx)
  expect(closeBeforeBody).toBeGreaterThan(greyBeforeBody)
})

it('is a no-op when boundary is null', () => {
  const segs = [{ text: 'hello', kind: 'equal' as const }]
  expect(applyOutOfBoundsOverlay(segs, null, 5)).toEqual(segs)
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm run test:unit -- useDiffHighlight`
Expected: FAIL (`applyOutOfBoundsOverlay` not exported).

- [ ] **Step 3: Implement**

In `src/composables/useDiffHighlight.ts`:

Add the kind + class:

```typescript
// extend HighlightKind union with 'out-of-bounds'
const CLASS_FOR_KIND: Record<HighlightKind, string> = {
  // ...existing kinds...
  'out-of-bounds':
    'opacity-40 line-through decoration-gray-400 text-gray-500 dark:text-gray-500',
}
```

Add the overlay (model it on `applyUncertainOverlay` — split segments at `char_start` and `char_end`, promote the outside pieces):

```typescript
export function applyOutOfBoundsOverlay(
  segments: HighlightSegment[],
  boundary: { char_start: number; char_end: number } | null,
  textLength: number,
): HighlightSegment[] {
  if (!boundary) return segments
  const { char_start, char_end } = boundary
  if (char_start <= 0 && char_end >= textLength) return segments
  const out: HighlightSegment[] = []
  let pos = 0
  for (const seg of segments) {
    const segStart = pos
    const segEnd = pos + seg.text.length
    // walk the segment, splitting at the two boundary offsets
    let cur = segStart
    while (cur < segEnd) {
      const inBounds = cur >= char_start && cur < char_end
      const nextEdge = inBounds
        ? Math.min(char_end, segEnd)
        : Math.min(cur < char_start ? char_start : segEnd, segEnd)
      const piece = seg.text.slice(cur - segStart, nextEdge - segStart)
      if (piece.length > 0) {
        out.push({ text: piece, kind: inBounds ? seg.kind : 'out-of-bounds' })
      }
      cur = nextEdge
    }
    pos = segEnd
  }
  return out
}
```

Wire it into the composable: accept `contentBoundary` in the options object and apply `applyOutOfBoundsOverlay` to the original (raw_text) segment list before `segmentsToHtml`, after the uncertain overlay. Out-of-bounds applies to the **original** view only.

- [ ] **Step 4: Run test to verify it passes**

Run: `npm run test:unit -- useDiffHighlight`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/composables/useDiffHighlight.ts src/composables/__tests__/useDiffHighlight.spec.ts
git commit -m "feat(webapp): grey out-of-bounds text via content boundary overlay"
```

---

### Task 13: Boundary adjust/reset UI in `EntryDetailView`

**Files:**
- Modify: `src/views/EntryDetailView.vue`
- Modify: `src/stores/entries.ts` (add `saveEntryBoundary` action)
- Test: `src/stores/__tests__/entries.spec.ts` (add); `src/views/__tests__/EntryDetailView.spec.ts` (add)

**Interfaces:**
- Consumes: `updateEntryBoundary` (Task 11); `content_boundary` (Task 11); the overlay (Task 12).
- Produces:
  - `entriesStore.saveEntryBoundary(id, start, end)` → calls `updateEntryBoundary`, updates `currentEntry`, returns job ids (same shape as `saveEntryText`).
  - `EntryDetailView`: when `content_boundary` is set, the original/Review view greys out-of-bounds text and shows "entry starts here ▲ / ends here ▼" handles at paragraph breaks plus a "Use full page" button; actions call `saveEntryBoundary` and track jobs via `jobsStore`.

- [ ] **Step 1: Write the failing store test**

```typescript
// src/stores/__tests__/entries.spec.ts (add)
it('saveEntryBoundary PATCHes and updates currentEntry', async () => {
  const store = useEntriesStore()
  vi.spyOn(api, 'updateEntryBoundary').mockResolvedValue({
    id: 1, content_boundary: { char_start: 5, char_end: 9 },
    reprocess_job_id: 'r1',
  } as never)
  const res = await store.saveEntryBoundary(1, 5, 9)
  expect(store.currentEntry?.content_boundary).toEqual({ char_start: 5, char_end: 9 })
  expect(res.reprocessJobId).toBe('r1')
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm run test:unit -- stores/entries`
Expected: FAIL (`saveEntryBoundary` undefined).

- [ ] **Step 3: Implement the store action**

In `src/stores/entries.ts`, mirror `saveEntryText`:

```typescript
  async function saveEntryBoundary(
    id: number, start: number | null, end: number | null,
  ): Promise<{ reprocessJobId?: string; extractionJobId?: string; moodJobId?: string }> {
    loading.value = true
    error.value = null
    try {
      const resp = await updateEntryBoundary(id, start, end)
      currentEntry.value = resp
      return {
        extractionJobId: resp.entity_extraction_job_id,
        reprocessJobId: resp.reprocess_job_id,
        moodJobId: resp.mood_job_id,
      }
    } catch (e) {
      error.value = e instanceof Error ? e.message : 'Failed to save boundary'
      throw e
    } finally {
      loading.value = false
    }
  }
```

Export it from the store return object and import `updateEntryBoundary`.

- [ ] **Step 4: Write the failing view test + implement the UI**

```typescript
// src/views/__tests__/EntryDetailView.spec.ts (add)
it('shows boundary controls when content_boundary is set and resets to full page', async () => {
  // mount with an entry whose content_boundary = {char_start:5,char_end:9}
  // assert greyed text present and a "Use full page" control exists
  // click it → expect saveEntryBoundary(id, null, null)
})
```

Implement in `EntryDetailView.vue`:
- Pass `contentBoundary: entry.content_boundary` into the `useDiffHighlight` options for the original view.
- When `entry.content_boundary` is non-null, render a control bar with: "Use full page" (calls `entriesStore.saveEntryBoundary(id, null, null)`) and start/end adjust handles. For v1, adjust handles set the window to paragraph offsets: compute paragraph break character offsets in `raw_text` (split on `/\n\n+/`), and clicking a handle at a break sets `content_start_char`/`content_end_char` accordingly, then calls `saveEntryBoundary`.
- After a successful save, track the returned job ids via `jobsStore.trackJob(...)` exactly as the text-save flow does.

- [ ] **Step 5: Run tests to verify they pass**

Run: `npm run test:unit -- stores/entries EntryDetailView`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/stores/entries.ts src/views/EntryDetailView.vue src/stores/__tests__/entries.spec.ts src/views/__tests__/EntryDetailView.spec.ts
git commit -m "feat(webapp): boundary adjust/reset UI in entry detail"
```

---

### Task 14: Webapp docs + full gate + push

**Files:**
- Modify: `webapp/docs/` (document the greyed out-of-bounds rendering + adjust/reset)
- (Journal entry under `webapp/journal/260618-*.md`)

- [ ] **Step 1: Update docs + journal**

Add a short section to the relevant webapp doc (entry detail / review UI) describing the greyed neighbor text and the adjust/reset control; create `webapp/journal/260618-entry-content-window-ui.md`.

- [ ] **Step 2: Full webapp gate**

Run: `npm run format:check && npm run lint && npm run test:coverage && npm run build`
Expected: all pass; coverage ≥ 85% on all four metrics. Add tests if any metric dropped.

- [ ] **Step 3: Commit + push + watch CI**

```bash
git add webapp/docs webapp/journal
git commit -m "docs(webapp): content window rendering + adjust/reset"
git push -u origin feature/multipage-entry-boundaries
gh run watch
```
Fix failures (full local suite first), recommit, repeat until green. Max 3 attempts, then flag.

---

## Self-review notes (author)

- **Spec coverage:** decisions 1–5 map to Tasks 3/6 (keep+window), 6/9 (in-bounds derivation), 1–3/6 (role-aware + deterministic), 6/7 (one entry per upload), 11–13 (adjust/reset). Storage → 4/5. API → 8/9. Webapp → 11–13.
- **Marker scheme:** begin/end brackets implemented in Task 1, consumed in Tasks 3 & 6. No `<<<NEW ENTRY>>>` remains after Task 6.
- **Type consistency:** `content_start_char`/`content_end_char` (DB/model/API request), `content_boundary` (API response/TS), `ContentWindow` (Python boundary module). `assign_roles`, `extract_content_window`, `update_content_window`, `set_content_window`, `updateEntryBoundary`, `saveEntryBoundary` are each defined once and reused.
- **Open implementer choices flagged inline:** the `boundaries.py` two-pass simplification (Task 3), the single pipeline-submit refactor in `_patch_entry` (Task 9), and the paragraph-offset adjust granularity (Task 13).
