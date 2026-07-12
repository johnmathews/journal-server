# 1. Storyline Chapter Editing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add manual chapter editing — move boundary, split, merge, add, delete — to storylines, with automatic per-chapter regeneration, exposed over discrete REST + MCP endpoints and an inline chapter-rail UI.

**Architecture:** Phase A of [the design spec](../../archive/2026-06-15-storyline-chapter-editing-design.md) (superseded 2026-07-12 — this whole plan describes deleted functionality; see [`docs/storylines.md`](../../storylines.md)). Purely additive on migration 0030 — no schema change. New transactional repository methods enforce the invariants (one open chapter, book-like contiguity by default with an `allow_gap` override, no overlaps); new write routes in `api/storylines_write.py` mutate then enqueue the existing `storyline_generation` job per affected chapter; MCP tools mirror the routes; the webapp gains store actions and an inline `⋯`-menu + `+ Add` rail UI.

**Tech Stack:** Python 3.13 / SQLite / Starlette + FastMCP (server, `uv`, pytest); Vue 3 + TypeScript / Pinia / Vitest (webapp).

**Two repos, two commit streams.** Server work lands in `server/` (`git -C server`), webapp work in `webapp/`. Run each repo's tests from inside that repo.

---

# 2. File structure

## 2.1 Server (`server/`)

- Modify `src/journal/db/storyline_repository.py` — date helpers, a `_shift_seqs` resequencer, an invariant validator, and five operation methods.
- Modify `src/journal/api/storylines_write.py` — extend the chapter `PATCH` route to accept dates; add `POST …/chapters`, `POST …/chapters/{cid}/split`, `POST …/chapters/merge`, `DELETE …/chapters/{cid}`.
- Modify `src/journal/mcp_server/tools/storylines.py` — five mirroring tools.
- Test: `tests/db/test_storyline_repository_editing.py` (new), `tests/api/test_storylines_chapter_editing.py` (new), `tests/mcp_server/test_storyline_chapter_editing_tools.py` (new).
- Docs: `docs/storylines*.md` (whichever is the active storyline doc) + the API contract doc; `journal/260615-storyline-chapter-editing.md`.

## 2.2 Webapp (`webapp/`)

- Modify `src/types/storyline.ts` — request/response types for the five operations.
- Modify `src/api/storylines.ts` — client functions.
- Modify `src/stores/storylines.ts` — `addChapter`, `splitChapter`, `mergeChapters`, `updateChapterDates`, `deleteChapter`.
- Modify `src/views/StorylineDetailView.vue` — wire the rail `⋯` menu + `+ Add chapter`.
- Create `src/components/storylines/ChapterEditMenu.vue` — per-rail-item action menu.
- Create `src/components/storylines/ChapterDateModal.vue` — date-picker modal (edit / add / split).
- Create `src/components/storylines/ChapterConfirmModal.vue` — confirm (merge / delete) with `allow_gap` toggle on delete.
- Test: `src/stores/__tests__/storylines.editing.spec.ts` (new), `src/components/storylines/__tests__/ChapterEditMenu.spec.ts`, `…/ChapterDateModal.spec.ts`, `…/ChapterConfirmModal.spec.ts` (new).
- Docs: `docs/` storyline page + API-contract note; `journal/260615-chapter-editing-ui.md`.

---

# 3. Conventions used in every server task

- The repository uses per-thread connections via `self._conn()`. Multi-statement operations run in one transaction: execute statements, then `conn.commit()`; on any raised exception call `conn.rollback()` and re-raise. Use a helper `_txn` context manager (Task 1) so each method stays uniform.
- Dates are inclusive ISO `YYYY-MM-DD` days. Operations use `_day_before` / `_day_after` (Task 1) for boundary math.
- Validation failures raise `ValueError` with an actionable message; the API layer maps `ValueError` → `400`.
- Run server tests from inside `server/`: `uv run pytest <path> -v`. Keep the unit tier green (in-memory SQLite, no Chroma).

The repository tests build a real on-disk-less DB via the existing test fixture. Find it first:

- [ ] **Step 0 (once): locate the repository test fixture**

Run: `grep -rn "SQLiteStorylineRepository\|storyline_repo" server/tests/db | head`
Expected: an existing `tests/db/test_storyline_repository*.py` showing how a `SQLiteStorylineRepository` is constructed from a `ConnectionFactory` with migrations applied. Reuse that fixture (import it or copy its construction) in the new test files. Every repository test below assumes a `repo` fixture yielding a `SQLiteStorylineRepository` on a migrated in-memory DB and a helper to create a storyline + entity.

---

# 4. Part 1 — Server repository

### Task 1: Date helpers, transaction helper, and resequencer

**Files:**
- Modify: `src/journal/db/storyline_repository.py`
- Test: `tests/db/test_storyline_repository_editing.py`

- [ ] **Step 1: Write the failing test**

Create `tests/db/test_storyline_repository_editing.py` (reuse the fixture from Step 0; the import path below matches the existing repository test module — adjust if Step 0 shows a different one):

```python
from journal.db.storyline_repository import _day_after, _day_before


def test_day_before_and_after():
    assert _day_before("2026-03-01") == "2026-02-28"
    assert _day_after("2026-02-28") == "2026-03-01"
    # leap year
    assert _day_after("2024-02-28") == "2024-02-29"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/db/test_storyline_repository_editing.py -v`
Expected: FAIL with `ImportError: cannot import name '_day_after'`.

- [ ] **Step 3: Add the helpers to `storyline_repository.py`**

Add near the top of the module (after the existing imports — add `from datetime import date as _date, timedelta as _timedelta`):

```python
def _day_before(iso: str) -> str:
    """ISO day immediately before ``iso`` (inclusive-window math)."""
    return (_date.fromisoformat(iso) - _timedelta(days=1)).isoformat()


def _day_after(iso: str) -> str:
    """ISO day immediately after ``iso`` (inclusive-window math)."""
    return (_date.fromisoformat(iso) + _timedelta(days=1)).isoformat()
```

Add these methods to `SQLiteStorylineRepository` (after `_conn`):

```python
def _shift_seqs(
    self, conn: sqlite3.Connection, storyline_id: int,
    from_seq: int, delta: int,
) -> None:
    """Shift seq by ``delta`` for chapters with seq >= ``from_seq``.

    Two-pass via a negative offset so we never collide with the
    UNIQUE(storyline_id, seq) index mid-update.
    """
    conn.execute(
        "UPDATE storyline_chapters SET seq = -(seq + ?)"
        " WHERE storyline_id = ? AND seq >= ?",
        (delta, storyline_id, from_seq),
    )
    conn.execute(
        "UPDATE storyline_chapters SET seq = -seq"
        " WHERE storyline_id = ? AND seq < 0",
        (storyline_id,),
    )
```

(The `import sqlite3` already exists under `TYPE_CHECKING`; add a runtime `import sqlite3` at module top since `_shift_seqs` references the type only in an annotation — keep it in the `TYPE_CHECKING` block and use `"sqlite3.Connection"` as a string annotation, matching the file's existing style.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/db/test_storyline_repository_editing.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git -C server add src/journal/db/storyline_repository.py tests/db/test_storyline_repository_editing.py
git -C server commit -m "feat(storylines): chapter-editing date + resequence helpers"
```

---

### Task 2: `split_chapter`

**Files:**
- Modify: `src/journal/db/storyline_repository.py`
- Test: `tests/db/test_storyline_repository_editing.py`

Semantics: split chapter `cid` at ISO `date`. Left keeps the row (`seq`, start) with `end_date = _day_before(date)`; right is a new row at `seq+1` with `start_date = date` and the original `end_date`. If the source was `open`, left becomes `closed` and right stays `open`; otherwise both `closed`. `date` must satisfy `start_date < date` and (when the source has an end) `date <= end_date`.

- [ ] **Step 1: Write the failing tests**

Append:

```python
def test_split_closed_chapter_yields_two_contiguous_closed(repo, storyline):
    ch = repo.create_chapter(
        storyline.id, seq=1, title="All",
        start_date="2026-01-01", end_date="2026-06-30", state="closed",
    )
    left, right = repo.split_chapter(ch.id, "2026-04-01")
    assert (left.start_date, left.end_date) == ("2026-01-01", "2026-03-31")
    assert (right.start_date, right.end_date) == ("2026-04-01", "2026-06-30")
    assert left.seq == 1 and right.seq == 2
    assert left.state == "closed" and right.state == "closed"


def test_split_open_chapter_keeps_right_half_open(repo, storyline):
    ch = repo.create_chapter(
        storyline.id, seq=1, title="Live",
        start_date="2026-01-01", end_date=None, state="open",
    )
    left, right = repo.split_chapter(ch.id, "2026-04-01")
    assert left.state == "closed" and left.end_date == "2026-03-31"
    assert right.state == "open" and right.end_date is None
    # exactly one open chapter survives
    opens = [c for c in repo.list_chapters(storyline.id) if c.state == "open"]
    assert len(opens) == 1 and opens[0].id == right.id


def test_split_shifts_later_chapters_up(repo, storyline):
    a = repo.create_chapter(storyline.id, seq=1, title="A",
                            start_date="2026-01-01", end_date="2026-06-30",
                            state="closed")
    b = repo.create_chapter(storyline.id, seq=2, title="B",
                            start_date="2026-07-01", end_date=None,
                            state="open")
    repo.split_chapter(a.id, "2026-04-01")
    seqs = {c.title: c.seq for c in repo.list_chapters(storyline.id)}
    assert seqs["A"] == 1
    assert seqs["B"] == 3  # pushed from 2 to 3 by the inserted right half


def test_split_rejects_date_outside_window(repo, storyline):
    ch = repo.create_chapter(storyline.id, seq=1, title="X",
                             start_date="2026-01-01", end_date="2026-06-30",
                             state="closed")
    import pytest
    with pytest.raises(ValueError):
        repo.split_chapter(ch.id, "2026-01-01")   # == start, not strictly inside
    with pytest.raises(ValueError):
        repo.split_chapter(ch.id, "2026-07-01")   # > end
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/db/test_storyline_repository_editing.py -k split -v`
Expected: FAIL (`AttributeError: 'SQLiteStorylineRepository' object has no attribute 'split_chapter'`).

- [ ] **Step 3: Implement `split_chapter`**

```python
def split_chapter(
    self, chapter_id: int, date: str,
) -> tuple[StorylineChapter, StorylineChapter]:
    """Split a chapter at ``date`` into a left + right pair."""
    conn = self._conn()
    src = self.get_chapter(chapter_id)
    if src is None:
        raise ValueError(f"Chapter {chapter_id} not found")
    if src.start_date is not None and date <= src.start_date:
        raise ValueError("split date must be after the chapter start")
    if src.end_date is not None and date > src.end_date:
        raise ValueError("split date must be on or before the chapter end")
    right_state = "open" if src.state == "open" else "closed"
    try:
        # Make room for the right half at src.seq + 1.
        self._shift_seqs(conn, src.storyline_id, src.seq + 1, 1)
        conn.execute(
            "UPDATE storyline_chapters"
            " SET end_date = ?, state = 'closed',"
            "     updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')"
            " WHERE id = ?",
            (_day_before(date), chapter_id),
        )
        cursor = conn.execute(
            "INSERT INTO storyline_chapters"
            " (storyline_id, seq, title, start_date, end_date, state)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (src.storyline_id, src.seq + 1, src.title,
             date, src.end_date, right_state),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    left = self.get_chapter(chapter_id)
    right = self.get_chapter(cursor.lastrowid)
    assert left is not None and right is not None
    return left, right
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/db/test_storyline_repository_editing.py -k split -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git -C server add src/journal/db/storyline_repository.py tests/db/test_storyline_repository_editing.py
git -C server commit -m "feat(storylines): repository split_chapter"
```

---

### Task 3: `merge_chapters`

**Files:**
- Modify: `src/journal/db/storyline_repository.py`
- Test: `tests/db/test_storyline_repository_editing.py`

Semantics: merge 2+ chapters whose `seq`s form a contiguous run in one storyline. Survivor = lowest `seq` row; its window becomes `start = min(start)`, `end = max(end)` (NULL if any input was open); state `open` if any input open else `closed`; title kept from the survivor. Other rows deleted; tail shifted down by `len(ids)-1`.

- [ ] **Step 1: Write the failing tests**

```python
def test_merge_adjacent_unions_window(repo, storyline):
    a = repo.create_chapter(storyline.id, seq=1, title="A",
                            start_date="2026-01-01", end_date="2026-03-31",
                            state="closed")
    b = repo.create_chapter(storyline.id, seq=2, title="B",
                            start_date="2026-04-01", end_date="2026-06-30",
                            state="closed")
    merged = repo.merge_chapters([a.id, b.id])
    assert merged.id == a.id  # survivor = lowest seq
    assert (merged.start_date, merged.end_date) == ("2026-01-01", "2026-06-30")
    assert merged.state == "closed"
    assert len(repo.list_chapters(storyline.id)) == 1


def test_merge_with_open_stays_open(repo, storyline):
    a = repo.create_chapter(storyline.id, seq=1, title="A",
                            start_date="2026-01-01", end_date="2026-03-31",
                            state="closed")
    b = repo.create_chapter(storyline.id, seq=2, title="B",
                            start_date="2026-04-01", end_date=None,
                            state="open")
    merged = repo.merge_chapters([a.id, b.id])
    assert merged.state == "open" and merged.end_date is None


def test_merge_shifts_tail_down(repo, storyline):
    a = repo.create_chapter(storyline.id, seq=1, title="A",
                            start_date="2026-01-01", end_date="2026-03-31",
                            state="closed")
    b = repo.create_chapter(storyline.id, seq=2, title="B",
                            start_date="2026-04-01", end_date="2026-06-30",
                            state="closed")
    c = repo.create_chapter(storyline.id, seq=3, title="C",
                            start_date="2026-07-01", end_date=None, state="open")
    repo.merge_chapters([a.id, b.id])
    seqs = {ch.title: ch.seq for ch in repo.list_chapters(storyline.id)}
    assert seqs["A"] == 1 and seqs["C"] == 2


def test_merge_rejects_non_adjacent(repo, storyline):
    a = repo.create_chapter(storyline.id, seq=1, title="A",
                            start_date="2026-01-01", end_date="2026-03-31",
                            state="closed")
    repo.create_chapter(storyline.id, seq=2, title="B",
                        start_date="2026-04-01", end_date="2026-06-30",
                        state="closed")
    c = repo.create_chapter(storyline.id, seq=3, title="C",
                            start_date="2026-07-01", end_date=None, state="open")
    import pytest
    with pytest.raises(ValueError):
        repo.merge_chapters([a.id, c.id])  # seq 1 and 3 — not contiguous
    with pytest.raises(ValueError):
        repo.merge_chapters([a.id])        # need 2+
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/db/test_storyline_repository_editing.py -k merge -v`
Expected: FAIL (no `merge_chapters`).

- [ ] **Step 3: Implement `merge_chapters`**

```python
def merge_chapters(self, chapter_ids: list[int]) -> StorylineChapter:
    """Merge a contiguous run of chapters into the lowest-seq one."""
    if len(chapter_ids) < 2:
        raise ValueError("merge requires at least two chapters")
    chapters = [self.get_chapter(cid) for cid in chapter_ids]
    if any(c is None for c in chapters):
        raise ValueError("one or more chapters not found")
    chapters = sorted(chapters, key=lambda c: c.seq)  # type: ignore[union-attr]
    sid = chapters[0].storyline_id
    if any(c.storyline_id != sid for c in chapters):
        raise ValueError("chapters belong to different storylines")
    seqs = [c.seq for c in chapters]
    if seqs != list(range(seqs[0], seqs[0] + len(seqs))):
        raise ValueError("chapters to merge must be adjacent (contiguous seq)")
    survivor = chapters[0]
    starts = [c.start_date for c in chapters if c.start_date is not None]
    is_open = any(c.state == "open" for c in chapters)
    new_start = min(starts) if starts else None
    new_end = None if is_open else max(
        c.end_date for c in chapters if c.end_date is not None
    )
    new_state = "open" if is_open else "closed"
    conn = self._conn()
    try:
        conn.execute(
            "UPDATE storyline_chapters"
            " SET start_date = ?, end_date = ?, state = ?,"
            "     updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')"
            " WHERE id = ?",
            (new_start, new_end, new_state, survivor.id),
        )
        for c in chapters[1:]:
            conn.execute(
                "DELETE FROM storyline_chapters WHERE id = ?", (c.id,),
            )
        # Pull the tail down into the gap the deletions left.
        self._shift_seqs(
            conn, sid, chapters[-1].seq + 1, -(len(chapters) - 1),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    merged = self.get_chapter(survivor.id)
    assert merged is not None
    return merged
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/db/test_storyline_repository_editing.py -k merge -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git -C server add src/journal/db/storyline_repository.py tests/db/test_storyline_repository_editing.py
git -C server commit -m "feat(storylines): repository merge_chapters"
```

---

### Task 4: `add_chapter`

**Files:**
- Modify: `src/journal/db/storyline_repository.py`
- Test: `tests/db/test_storyline_repository_editing.py`

Semantics, two flavors keyed on `end_date`:
- **New-latest** (`end_date is None`): close the current open chapter at `_day_before(start_date)`, append a new `open` chapter `[start_date, NULL)` at `max(seq)+1`. Require `start_date` strictly after the open chapter's start.
- **Ranged** (`end_date` set): insert a `closed` chapter `[start_date, end_date]` into a free range. Reject if it overlaps any existing chapter. `seq` assigned by date order; tail shifted up.

- [ ] **Step 1: Write the failing tests**

```python
def test_add_new_latest_closes_open_and_opens_fresh(repo, storyline):
    old = repo.create_chapter(storyline.id, seq=1, title="Old",
                              start_date="2026-01-01", end_date=None,
                              state="open")
    new = repo.add_chapter(storyline.id, start_date="2026-04-01")
    closed = repo.get_chapter(old.id)
    assert closed.state == "closed" and closed.end_date == "2026-03-31"
    assert new.state == "open" and new.start_date == "2026-04-01"
    assert new.end_date is None and new.seq == 2
    opens = [c for c in repo.list_chapters(storyline.id) if c.state == "open"]
    assert len(opens) == 1 and opens[0].id == new.id


def test_add_ranged_into_gap_is_closed_and_ordered(repo, storyline):
    # Ch1 Jan–Mar, then a gap, then open Ch2 Jul–
    repo.create_chapter(storyline.id, seq=1, title="A",
                        start_date="2026-01-01", end_date="2026-03-31",
                        state="closed")
    repo.create_chapter(storyline.id, seq=2, title="B",
                        start_date="2026-07-01", end_date=None, state="open")
    added = repo.add_chapter(storyline.id, start_date="2026-04-01",
                             end_date="2026-05-31")
    assert added.state == "closed"
    assert added.seq == 2          # slots between A(1) and B(now 3) by date
    titles = [c.title for c in repo.list_chapters(storyline.id)]
    assert titles == ["A", "", "B"]  # new chapter has empty title by default


def test_add_ranged_rejects_overlap(repo, storyline):
    repo.create_chapter(storyline.id, seq=1, title="A",
                        start_date="2026-01-01", end_date="2026-06-30",
                        state="closed")
    import pytest
    with pytest.raises(ValueError):
        repo.add_chapter(storyline.id, start_date="2026-03-01",
                         end_date="2026-09-30")
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/db/test_storyline_repository_editing.py -k add_ -v`
Expected: FAIL (no `add_chapter`).

- [ ] **Step 3: Implement `add_chapter`**

```python
def add_chapter(
    self, storyline_id: int, start_date: str, end_date: str | None = None,
) -> StorylineChapter:
    """Add a chapter: new-latest (end None) or ranged (end set)."""
    conn = self._conn()
    existing = self.list_chapters(storyline_id)
    if end_date is None:
        open_ch = self.get_open_chapter(storyline_id)
        if open_ch is not None and open_ch.start_date is not None \
                and start_date <= open_ch.start_date:
            raise ValueError(
                "new chapter must start after the current open chapter",
            )
        new_seq = (max((c.seq for c in existing), default=0)) + 1
        try:
            if open_ch is not None:
                conn.execute(
                    "UPDATE storyline_chapters"
                    " SET end_date = ?, state = 'closed',"
                    "     updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')"
                    " WHERE id = ?",
                    (_day_before(start_date), open_ch.id),
                )
            cursor = conn.execute(
                "INSERT INTO storyline_chapters"
                " (storyline_id, seq, title, start_date, end_date, state)"
                " VALUES (?, ?, '', ?, NULL, 'open')",
                (storyline_id, new_seq, start_date),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        ch = self.get_chapter(cursor.lastrowid)
        assert ch is not None
        return ch
    # Ranged insert into a free range.
    if end_date < start_date:
        raise ValueError("end_date must be on or after start_date")
    for c in existing:
        c_end = c.end_date if c.end_date is not None else "9999-12-31"
        c_start = c.start_date if c.start_date is not None else "0000-01-01"
        if start_date <= c_end and end_date >= c_start:
            raise ValueError("new chapter overlaps an existing chapter")
    later = [c for c in existing
             if (c.start_date or "9999-12-31") > end_date]
    insert_seq = min((c.seq for c in later), default=len(existing) + 1)
    try:
        self._shift_seqs(conn, storyline_id, insert_seq, 1)
        cursor = conn.execute(
            "INSERT INTO storyline_chapters"
            " (storyline_id, seq, title, start_date, end_date, state)"
            " VALUES (?, ?, '', ?, ?, 'closed')",
            (storyline_id, insert_seq, start_date, end_date),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    ch = self.get_chapter(cursor.lastrowid)
    assert ch is not None
    return ch
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/db/test_storyline_repository_editing.py -k add_ -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git -C server add src/journal/db/storyline_repository.py tests/db/test_storyline_repository_editing.py
git -C server commit -m "feat(storylines): repository add_chapter"
```

---

### Task 5: `update_chapter_window`

**Files:**
- Modify: `src/journal/db/storyline_repository.py`
- Test: `tests/db/test_storyline_repository_editing.py`

Semantics: change a chapter's `start_date`/`end_date`. By default the shared edge of the touching neighbor ripples to stay contiguous: changing a chapter's `start` sets the previous chapter's `end` to `_day_before(new_start)`; changing a chapter's `end` sets the next chapter's `start` to `_day_after(new_end)`. With `allow_gap=True`, neighbors are left alone (a gap is permitted). Overlaps after the change are rejected. The open chapter's `end_date` cannot be set (stays NULL). Returns the list of changed chapters (the edited one + any rippled neighbor).

- [ ] **Step 1: Write the failing tests**

```python
def test_update_window_ripples_previous_neighbor_end(repo, storyline):
    a = repo.create_chapter(storyline.id, seq=1, title="A",
                            start_date="2026-01-01", end_date="2026-03-31",
                            state="closed")
    b = repo.create_chapter(storyline.id, seq=2, title="B",
                            start_date="2026-04-01", end_date=None,
                            state="open")
    changed = repo.update_chapter_window(b.id, start_date="2026-05-01",
                                         end_date=None)
    ids = {c.id: c for c in changed}
    assert ids[b.id].start_date == "2026-05-01"
    assert ids[a.id].end_date == "2026-04-30"   # rippled to stay contiguous


def test_update_window_allow_gap_leaves_neighbor(repo, storyline):
    a = repo.create_chapter(storyline.id, seq=1, title="A",
                            start_date="2026-01-01", end_date="2026-03-31",
                            state="closed")
    b = repo.create_chapter(storyline.id, seq=2, title="B",
                            start_date="2026-04-01", end_date=None,
                            state="open")
    changed = repo.update_chapter_window(b.id, start_date="2026-05-01",
                                         end_date=None, allow_gap=True)
    assert repo.get_chapter(a.id).end_date == "2026-03-31"  # untouched
    assert {c.id for c in changed} == {b.id}


def test_update_window_rejects_end_on_open_chapter(repo, storyline):
    b = repo.create_chapter(storyline.id, seq=1, title="B",
                            start_date="2026-04-01", end_date=None,
                            state="open")
    import pytest
    with pytest.raises(ValueError):
        repo.update_chapter_window(b.id, start_date="2026-04-01",
                                   end_date="2026-06-30")
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/db/test_storyline_repository_editing.py -k update_window -v`
Expected: FAIL (no `update_chapter_window`).

- [ ] **Step 3: Implement `update_chapter_window`**

```python
def update_chapter_window(
    self, chapter_id: int, start_date: str | None,
    end_date: str | None, allow_gap: bool = False,
) -> list[StorylineChapter]:
    """Move a chapter's boundaries, rippling neighbors by default."""
    target = self.get_chapter(chapter_id)
    if target is None:
        raise ValueError(f"Chapter {chapter_id} not found")
    if target.state == "open" and end_date is not None:
        raise ValueError("the open chapter's end cannot be set")
    if start_date is not None and end_date is not None and end_date < start_date:
        raise ValueError("end_date must be on or after start_date")
    chapters = self.list_chapters(target.storyline_id)
    idx = next(i for i, c in enumerate(chapters) if c.id == chapter_id)
    changed_ids: set[int] = {chapter_id}
    conn = self._conn()
    try:
        conn.execute(
            "UPDATE storyline_chapters"
            " SET start_date = ?, end_date = ?,"
            "     updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')"
            " WHERE id = ?",
            (start_date, end_date, chapter_id),
        )
        if not allow_gap:
            if start_date is not None and idx > 0:
                prev = chapters[idx - 1]
                conn.execute(
                    "UPDATE storyline_chapters SET end_date = ?,"
                    " updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')"
                    " WHERE id = ?",
                    (_day_before(start_date), prev.id),
                )
                changed_ids.add(prev.id)
            if end_date is not None and idx < len(chapters) - 1:
                nxt = chapters[idx + 1]
                conn.execute(
                    "UPDATE storyline_chapters SET start_date = ?,"
                    " updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')"
                    " WHERE id = ?",
                    (_day_after(end_date), nxt.id),
                )
                changed_ids.add(nxt.id)
        self._assert_no_overlap(target.storyline_id, conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return [c for c in self.list_chapters(target.storyline_id)
            if c.id in changed_ids]


def _assert_no_overlap(
    self, storyline_id: int, conn: "sqlite3.Connection",
) -> None:
    """Raise ValueError if any two chapters' windows overlap."""
    rows = conn.execute(
        "SELECT start_date, end_date FROM storyline_chapters"
        " WHERE storyline_id = ? ORDER BY seq ASC",
        (storyline_id,),
    ).fetchall()
    prev_end: str | None = None
    for r in rows:
        start = r["start_date"] or "0000-01-01"
        if prev_end is not None and start <= prev_end:
            raise ValueError("chapter windows overlap")
        prev_end = r["end_date"] or "9999-12-31"
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/db/test_storyline_repository_editing.py -k update_window -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git -C server add src/journal/db/storyline_repository.py tests/db/test_storyline_repository_editing.py
git -C server commit -m "feat(storylines): repository update_chapter_window"
```

---

### Task 6: `delete_chapter`

**Files:**
- Modify: `src/journal/db/storyline_repository.py`
- Test: `tests/db/test_storyline_repository_editing.py`

Semantics: delete a chapter. Reject if it's the only chapter. By default the previous neighbor absorbs the deleted range (its `end_date` extends to the deleted `end_date`); deleting the first chapter, the next neighbor's `start_date` extends back. Deleting the open chapter promotes the previous chapter to `open` (`end_date → NULL`). With `allow_gap=True`, no neighbor is changed. Tail `seq` shifts down by 1. Returns the chapter ids whose windows changed (so the API regenerates them) — empty when a gap was left.

- [ ] **Step 1: Write the failing tests**

```python
def test_delete_absorbs_into_previous(repo, storyline):
    a = repo.create_chapter(storyline.id, seq=1, title="A",
                            start_date="2026-01-01", end_date="2026-03-31",
                            state="closed")
    b = repo.create_chapter(storyline.id, seq=2, title="B",
                            start_date="2026-04-01", end_date="2026-06-30",
                            state="closed")
    c = repo.create_chapter(storyline.id, seq=3, title="C",
                            start_date="2026-07-01", end_date=None, state="open")
    affected = repo.delete_chapter(b.id)
    assert repo.get_chapter(b.id) is None
    assert repo.get_chapter(a.id).end_date == "2026-06-30"  # absorbed B
    assert affected == [a.id]
    seqs = {ch.title: ch.seq for ch in repo.list_chapters(storyline.id)}
    assert seqs == {"A": 1, "C": 2}


def test_delete_open_promotes_previous(repo, storyline):
    a = repo.create_chapter(storyline.id, seq=1, title="A",
                            start_date="2026-01-01", end_date="2026-03-31",
                            state="closed")
    b = repo.create_chapter(storyline.id, seq=2, title="B",
                            start_date="2026-04-01", end_date=None, state="open")
    repo.delete_chapter(b.id)
    promoted = repo.get_chapter(a.id)
    assert promoted.state == "open" and promoted.end_date is None


def test_delete_allow_gap_leaves_neighbors(repo, storyline):
    a = repo.create_chapter(storyline.id, seq=1, title="A",
                            start_date="2026-01-01", end_date="2026-03-31",
                            state="closed")
    b = repo.create_chapter(storyline.id, seq=2, title="B",
                            start_date="2026-04-01", end_date="2026-06-30",
                            state="closed")
    c = repo.create_chapter(storyline.id, seq=3, title="C",
                            start_date="2026-07-01", end_date=None, state="open")
    affected = repo.delete_chapter(b.id, allow_gap=True)
    assert repo.get_chapter(a.id).end_date == "2026-03-31"  # untouched
    assert affected == []


def test_delete_last_chapter_rejected(repo, storyline):
    only = repo.create_chapter(storyline.id, seq=1, title="Only",
                               start_date="2026-01-01", end_date=None,
                               state="open")
    import pytest
    with pytest.raises(ValueError):
        repo.delete_chapter(only.id)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/db/test_storyline_repository_editing.py -k delete -v`
Expected: FAIL (no `delete_chapter`).

- [ ] **Step 3: Implement `delete_chapter`**

```python
def delete_chapter(
    self, chapter_id: int, allow_gap: bool = False,
) -> list[int]:
    """Delete a chapter; absorb its range into a neighbor by default."""
    target = self.get_chapter(chapter_id)
    if target is None:
        raise ValueError(f"Chapter {chapter_id} not found")
    chapters = self.list_chapters(target.storyline_id)
    if len(chapters) == 1:
        raise ValueError("cannot delete a storyline's only chapter")
    idx = next(i for i, c in enumerate(chapters) if c.id == chapter_id)
    affected: list[int] = []
    conn = self._conn()
    try:
        conn.execute(
            "DELETE FROM storyline_chapters WHERE id = ?", (chapter_id,),
        )
        if not allow_gap:
            if idx > 0:
                prev = chapters[idx - 1]
                # Absorb the deleted range; if we deleted the open
                # chapter, the previous one becomes open.
                if target.state == "open":
                    conn.execute(
                        "UPDATE storyline_chapters SET end_date = NULL,"
                        " state = 'open',"
                        " updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')"
                        " WHERE id = ?",
                        (prev.id,),
                    )
                else:
                    conn.execute(
                        "UPDATE storyline_chapters SET end_date = ?,"
                        " updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')"
                        " WHERE id = ?",
                        (target.end_date, prev.id),
                    )
                affected.append(prev.id)
            else:
                nxt = chapters[idx + 1]
                conn.execute(
                    "UPDATE storyline_chapters SET start_date = ?,"
                    " updated_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')"
                    " WHERE id = ?",
                    (target.start_date, nxt.id),
                )
                affected.append(nxt.id)
        self._shift_seqs(conn, target.storyline_id, target.seq + 1, -1)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return affected
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/db/test_storyline_repository_editing.py -k delete -v`
Expected: PASS (4 tests). Then run the whole new file: `uv run pytest tests/db/test_storyline_repository_editing.py -v` → all green.

- [ ] **Step 5: Commit**

```bash
git -C server add src/journal/db/storyline_repository.py tests/db/test_storyline_repository_editing.py
git -C server commit -m "feat(storylines): repository delete_chapter"
```

---

# 5. Part 2 — Server API routes

All routes live in `register_storylines_write_routes` in `src/journal/api/storylines_write.py`, follow the existing `@mcp.custom_route(...)` + `@handler(...)` pattern, resolve the storyline with `repo.get_storyline(sid, user_id=user.user_id)` (404 if None), validate the chapter belongs to the storyline, mutate via the repository, then enqueue one `job_runner.submit_storyline_generation(sid, user_id=..., chapter_id=<cid>, mode="replace")` per affected chapter (skip the open chapter's `mode` — pass `mode="replace"` for closed, and for the open chapter omit `mode` so the worker's default append/replace logic applies, matching the existing chapter-regenerate route which always uses `replace`). Return the affected chapter dict(s) via `_chapter_to_dict(repo, chapter)` plus `job_ids`.

### Task 7: Helper to enqueue regeneration for a set of chapters

**Files:**
- Modify: `src/journal/api/storylines_write.py`
- Test: `tests/api/test_storylines_chapter_editing.py`

- [ ] **Step 1: Write the failing test**

Create `tests/api/test_storylines_chapter_editing.py` (reuse the existing API test client fixture — find it with `grep -rn "def client\|TestClient\|register_storylines_write_routes" server/tests/api | head`). First test asserts the helper enqueues one job per affected id:

```python
from journal.api.storylines_write import _enqueue_chapter_regens


class _FakeRunner:
    def __init__(self):
        self.calls = []

    def submit_storyline_generation(self, sid, **kw):
        self.calls.append((sid, kw))
        return type("Job", (), {"id": f"job-{len(self.calls)}", "status": "queued"})()


def test_enqueue_chapter_regens_one_job_per_chapter():
    runner = _FakeRunner()

    class _Ch:
        def __init__(self, cid, state):
            self.id, self.state = cid, state

    jobs = _enqueue_chapter_regens(
        runner, storyline_id=7, user_id=1,
        chapters=[_Ch(10, "closed"), _Ch(11, "open")],
    )
    assert len(jobs) == 2
    assert runner.calls[0] == (7, {"user_id": 1, "chapter_id": 10, "mode": "replace"})
    # open chapter: no explicit mode (worker default applies)
    assert runner.calls[1] == (7, {"user_id": 1, "chapter_id": 11})
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/api/test_storylines_chapter_editing.py -k enqueue -v`
Expected: FAIL (`ImportError`).

- [ ] **Step 3: Implement the module-level helper**

Add to `storylines_write.py` (module level, above `register_storylines_write_routes`):

```python
def _enqueue_chapter_regens(
    job_runner, storyline_id: int, user_id: int, chapters: list,
) -> list[str]:
    """Queue one generation job per affected chapter; return job ids.

    Closed chapters regenerate with ``mode="replace"``; the open
    chapter omits ``mode`` so the worker's default append/replace
    behaviour applies (mirrors the per-chapter regenerate route).
    """
    job_ids: list[str] = []
    for ch in chapters:
        kwargs: dict[str, Any] = {"user_id": user_id, "chapter_id": ch.id}
        if ch.state != "open":
            kwargs["mode"] = "replace"
        try:
            job = job_runner.submit_storyline_generation(storyline_id, **kwargs)
            job_ids.append(job.id)
        except (ValueError, RuntimeError) as exc:
            log.warning(
                "could not queue regen for chapter %d: %s", ch.id, exc,
            )
    return job_ids
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/api/test_storylines_chapter_editing.py -k enqueue -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git -C server add src/journal/api/storylines_write.py tests/api/test_storylines_chapter_editing.py
git -C server commit -m "feat(storylines): chapter-regen enqueue helper"
```

---

### Task 8: `POST …/chapters` (add) + `POST …/chapters/{cid}/split`

**Files:**
- Modify: `src/journal/api/storylines_write.py`
- Test: `tests/api/test_storylines_chapter_editing.py`

- [ ] **Step 1: Write the failing tests** (route-level, via the test client; adapt auth/headers to the existing fixture)

```python
def test_add_chapter_new_latest(client, seeded_storyline):
    sid = seeded_storyline.id
    resp = client.post(f"/api/storylines/{sid}/chapters",
                       json={"start_date": "2026-04-01"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["chapter"]["state"] == "open"
    assert body["chapter"]["start_date"] == "2026-04-01"
    assert isinstance(body["job_ids"], list)


def test_split_chapter_route(client, seeded_storyline_with_closed_chapter):
    sid, cid = seeded_storyline_with_closed_chapter
    resp = client.post(f"/api/storylines/{sid}/chapters/{cid}/split",
                       json={"date": "2026-04-01"})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["chapters"]) == 2
    assert len(body["job_ids"]) == 2


def test_split_bad_date_is_400(client, seeded_storyline_with_closed_chapter):
    sid, cid = seeded_storyline_with_closed_chapter
    resp = client.post(f"/api/storylines/{sid}/chapters/{cid}/split",
                       json={"date": "1999-01-01"})
    assert resp.status_code == 400
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/api/test_storylines_chapter_editing.py -k "add_chapter or split" -v`
Expected: FAIL (routes 404 / not registered).

- [ ] **Step 3: Implement both routes** inside `register_storylines_write_routes`

```python
    @mcp.custom_route(
        "/api/storylines/{storyline_id:int}/chapters",
        methods=["POST"],
        name="api_add_storyline_chapter",
    )
    @handler(services_getter, parse_json="raw")
    def add_storyline_chapter(
        request: Request, services: ServicesDict, raw: bytes
    ) -> JSONResponse:
        """Add a chapter. Body: {start_date, end_date?}. end_date omitted
        => new-latest open chapter (closes the current open one); end_date
        set => ranged closed chapter in a free range."""
        repo = services.get("storyline_repository")
        job_runner = services.get("job_runner")
        if repo is None or job_runner is None:
            return JSONResponse(
                {"error": "Storylines feature not configured"}, status_code=503)
        user = get_authenticated_user(request)
        sid = int(request.path_params["storyline_id"])
        if repo.get_storyline(sid, user_id=user.user_id) is None:
            return JSONResponse({"error": "Storyline not found"}, status_code=404)
        try:
            body = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            body = {}
        if not isinstance(body, dict) or not isinstance(
                body.get("start_date"), str):
            return JSONResponse(
                {"error": "start_date (ISO str) is required"}, status_code=400)
        end_date = body.get("end_date")
        if end_date is not None and not isinstance(end_date, str):
            return JSONResponse({"error": "end_date must be a string"},
                                status_code=400)
        try:
            chapter = repo.add_chapter(sid, body["start_date"], end_date)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        job_ids = _enqueue_chapter_regens(job_runner, sid, user.user_id,
                                          [chapter])
        log.info("POST /api/storylines/%d/chapters — added chapter %d",
                 sid, chapter.id)
        return JSONResponse(
            {"chapter": _chapter_to_dict(repo, chapter), "job_ids": job_ids},
            status_code=201)

    @mcp.custom_route(
        "/api/storylines/{storyline_id:int}/chapters/{chapter_id:int}/split",
        methods=["POST"],
        name="api_split_storyline_chapter",
    )
    @handler(services_getter, parse_json="raw")
    def split_storyline_chapter(
        request: Request, services: ServicesDict, raw: bytes
    ) -> JSONResponse:
        """Split a chapter at {date} into two contiguous chapters."""
        repo = services.get("storyline_repository")
        job_runner = services.get("job_runner")
        if repo is None or job_runner is None:
            return JSONResponse(
                {"error": "Storylines feature not configured"}, status_code=503)
        user = get_authenticated_user(request)
        sid = int(request.path_params["storyline_id"])
        cid = int(request.path_params["chapter_id"])
        if repo.get_storyline(sid, user_id=user.user_id) is None:
            return JSONResponse({"error": "Storyline not found"}, status_code=404)
        chapter = repo.get_chapter(cid)
        if chapter is None or chapter.storyline_id != sid:
            return JSONResponse({"error": "Chapter not found"}, status_code=404)
        try:
            body = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            body = {}
        if not isinstance(body, dict) or not isinstance(body.get("date"), str):
            return JSONResponse({"error": "date (ISO str) is required"},
                                status_code=400)
        try:
            left, right = repo.split_chapter(cid, body["date"])
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        job_ids = _enqueue_chapter_regens(job_runner, sid, user.user_id,
                                          [left, right])
        log.info("POST /api/storylines/%d/chapters/%d/split @ %s",
                 sid, cid, body["date"])
        return JSONResponse(
            {"chapters": [_chapter_to_dict(repo, left),
                          _chapter_to_dict(repo, right)],
             "job_ids": job_ids},
            status_code=200)
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/api/test_storylines_chapter_editing.py -k "add_chapter or split" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git -C server add src/journal/api/storylines_write.py tests/api/test_storylines_chapter_editing.py
git -C server commit -m "feat(storylines): add + split chapter routes"
```

---

### Task 9: `POST …/chapters/merge` + `DELETE …/chapters/{cid}` + extend `PATCH`

**Files:**
- Modify: `src/journal/api/storylines_write.py`
- Test: `tests/api/test_storylines_chapter_editing.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_merge_route(client, seeded_two_adjacent_closed):
    sid, cid_a, cid_b = seeded_two_adjacent_closed
    resp = client.post(f"/api/storylines/{sid}/chapters/merge",
                       json={"chapter_ids": [cid_a, cid_b]})
    assert resp.status_code == 200
    assert resp.json()["chapter"]["id"] == cid_a
    assert len(resp.json()["job_ids"]) == 1


def test_delete_route_absorbs(client, seeded_three_chapters):
    sid, a, b, c = seeded_three_chapters
    resp = client.request("DELETE",
                          f"/api/storylines/{sid}/chapters/{b}", json={})
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True


def test_delete_last_chapter_400(client, seeded_storyline):
    sid = seeded_storyline.id
    only = client.get(f"/api/storylines/{sid}").json()["chapters"][0]["id"]
    resp = client.request("DELETE",
                          f"/api/storylines/{sid}/chapters/{only}", json={})
    assert resp.status_code == 400


def test_patch_chapter_dates(client, seeded_two_adjacent_closed):
    sid, cid_a, cid_b = seeded_two_adjacent_closed
    resp = client.patch(f"/api/storylines/{sid}/chapters/{cid_b}",
                        json={"start_date": "2026-05-01"})
    assert resp.status_code == 200
    assert len(resp.json()["job_ids"]) >= 1


def test_patch_chapter_rename_still_works(client, seeded_storyline):
    sid = seeded_storyline.id
    cid = client.get(f"/api/storylines/{sid}").json()["chapters"][0]["id"]
    resp = client.patch(f"/api/storylines/{sid}/chapters/{cid}",
                        json={"title": "Renamed"})
    assert resp.status_code == 200
    assert resp.json()["title"] == "Renamed"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/api/test_storylines_chapter_editing.py -k "merge or delete or patch_chapter" -v`
Expected: FAIL.

- [ ] **Step 3a: Add the merge route**

```python
    @mcp.custom_route(
        "/api/storylines/{storyline_id:int}/chapters/merge",
        methods=["POST"],
        name="api_merge_storyline_chapters",
    )
    @handler(services_getter, parse_json="raw")
    def merge_storyline_chapters(
        request: Request, services: ServicesDict, raw: bytes
    ) -> JSONResponse:
        """Merge adjacent chapters. Body: {chapter_ids: [int, ...]}."""
        repo = services.get("storyline_repository")
        job_runner = services.get("job_runner")
        if repo is None or job_runner is None:
            return JSONResponse(
                {"error": "Storylines feature not configured"}, status_code=503)
        user = get_authenticated_user(request)
        sid = int(request.path_params["storyline_id"])
        if repo.get_storyline(sid, user_id=user.user_id) is None:
            return JSONResponse({"error": "Storyline not found"}, status_code=404)
        try:
            body = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            body = {}
        ids = body.get("chapter_ids") if isinstance(body, dict) else None
        if (not isinstance(ids, list) or len(ids) < 2
                or not all(isinstance(i, int) for i in ids)):
            return JSONResponse(
                {"error": "chapter_ids must be a list of >= 2 integers"},
                status_code=400)
        # Ownership: every id must belong to this storyline.
        for i in ids:
            ch = repo.get_chapter(i)
            if ch is None or ch.storyline_id != sid:
                return JSONResponse({"error": "Chapter not found"},
                                    status_code=404)
        try:
            merged = repo.merge_chapters(ids)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        job_ids = _enqueue_chapter_regens(job_runner, sid, user.user_id,
                                          [merged])
        log.info("POST /api/storylines/%d/chapters/merge — ids=%s", sid, ids)
        return JSONResponse(
            {"chapter": _chapter_to_dict(repo, merged), "job_ids": job_ids},
            status_code=200)
```

- [ ] **Step 3b: Add the delete route**

```python
    @mcp.custom_route(
        "/api/storylines/{storyline_id:int}/chapters/{chapter_id:int}",
        methods=["DELETE"],
        name="api_delete_storyline_chapter",
    )
    @handler(services_getter, parse_json="raw")
    def delete_storyline_chapter(
        request: Request, services: ServicesDict, raw: bytes
    ) -> JSONResponse:
        """Delete a chapter. Optional body: {allow_gap?: bool}."""
        repo = services.get("storyline_repository")
        job_runner = services.get("job_runner")
        if repo is None or job_runner is None:
            return JSONResponse(
                {"error": "Storylines feature not configured"}, status_code=503)
        user = get_authenticated_user(request)
        sid = int(request.path_params["storyline_id"])
        cid = int(request.path_params["chapter_id"])
        if repo.get_storyline(sid, user_id=user.user_id) is None:
            return JSONResponse({"error": "Storyline not found"}, status_code=404)
        chapter = repo.get_chapter(cid)
        if chapter is None or chapter.storyline_id != sid:
            return JSONResponse({"error": "Chapter not found"}, status_code=404)
        try:
            body = json.loads(raw) if raw else {}
        except (json.JSONDecodeError, ValueError):
            body = {}
        allow_gap = bool(body.get("allow_gap")) if isinstance(body, dict) else False
        try:
            affected_ids = repo.delete_chapter(cid, allow_gap=allow_gap)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        affected = [c for c in repo.list_chapters(sid) if c.id in affected_ids]
        job_ids = _enqueue_chapter_regens(job_runner, sid, user.user_id,
                                          affected)
        log.info("DELETE /api/storylines/%d/chapters/%d (allow_gap=%s)",
                 sid, cid, allow_gap)
        return JSONResponse({"deleted": True, "job_ids": job_ids},
                            status_code=200)
```

- [ ] **Step 3c: Extend the existing `rename_storyline_chapter` PATCH route** to also accept dates. Replace its body-handling tail (from the `title = ...` line through the final `return`) with:

```python
        title_raw = parsed.get("title")
        has_dates = "start_date" in parsed or "end_date" in parsed
        # Rename-only path (back-compat): title present, no date fields.
        if title_raw is not None and not has_dates:
            title = (title_raw or "").strip()
            if not title:
                return JSONResponse(
                    {"error": "title (non-empty str) is required"},
                    status_code=400)
            updated = repo.rename_chapter(cid, title)
            if updated is None:
                return JSONResponse({"error": "Chapter not found"},
                                    status_code=404)
            return JSONResponse(_chapter_to_dict(repo, updated),
                                status_code=200)
        # Date-edit path: ripple neighbors unless allow_gap.
        if not has_dates:
            return JSONResponse(
                {"error": "provide title and/or start_date/end_date"},
                status_code=400)
        job_runner = services.get("job_runner")
        start_date = parsed.get("start_date", chapter.start_date)
        end_date = parsed.get("end_date", chapter.end_date)
        allow_gap = bool(parsed.get("allow_gap"))
        try:
            changed = repo.update_chapter_window(
                cid, start_date, end_date, allow_gap=allow_gap)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        job_ids = (_enqueue_chapter_regens(job_runner, sid, user.user_id,
                                           changed)
                   if job_runner is not None else [])
        log.info("PATCH /api/storylines/%d/chapters/%d — window edit", sid, cid)
        return JSONResponse(
            {"chapters": [_chapter_to_dict(repo, c) for c in changed],
             "job_ids": job_ids},
            status_code=200)
```

(Also update the route docstring to note it now accepts `{title?, start_date?, end_date?, allow_gap?}`.)

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/api/test_storylines_chapter_editing.py -v`
Expected: PASS (all). Then run the full storyline API suite to confirm the PATCH change didn't regress rename: `uv run pytest tests/api -k storyline -v`.

- [ ] **Step 5: Commit**

```bash
git -C server add src/journal/api/storylines_write.py tests/api/test_storylines_chapter_editing.py
git -C server commit -m "feat(storylines): merge, delete, and date-edit chapter routes"
```

---

### Task 10: MCP tool parity

**Files:**
- Modify: `src/journal/mcp_server/tools/storylines.py`
- Test: `tests/mcp_server/test_storyline_chapter_editing_tools.py`

Add five tools mirroring the routes, following the `journal_set_storyline_anchors` pattern (resolve repo via `_get_storyline_repository(ctx)`, user via `_user_id(ctx)`, return a human-readable string; wrap repo `ValueError` into a returned error string; enqueue regen via the runner as the regenerate tool does at line ~311/495). Mark `journal_delete_storyline_chapter` with `annotations={"destructiveHint": True}`.

- [ ] **Step 1: Write the failing test**

Create `tests/mcp_server/test_storyline_chapter_editing_tools.py` (reuse the MCP tool test harness — `grep -rn "journal_regenerate_storyline\|def .*ctx" server/tests/mcp_server | head`). Minimum coverage — one happy-path per tool calling the repo and returning a non-error string:

```python
def test_split_tool_splits_and_reports(mcp_ctx, seeded_closed_chapter):
    sid, cid = seeded_closed_chapter
    from journal.mcp_server.tools.storylines import journal_split_storyline_chapter
    out = journal_split_storyline_chapter(sid, cid, "2026-04-01", ctx=mcp_ctx)
    assert "split" in out.lower()
    # repository now has two chapters
    repo = mcp_ctx_repo(mcp_ctx)  # helper from the harness
    assert len(repo.list_chapters(sid)) == 2
```

(Add analogous tests for add / merge / update-dates / delete. Each asserts the repo state changed and the returned string is not an error.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/mcp_server/test_storyline_chapter_editing_tools.py -v`
Expected: FAIL (`ImportError`).

- [ ] **Step 3: Implement the five tools.** Example (split) — model the other four on it and the existing `journal_set_storyline_anchors`/`journal_regenerate_storyline`:

```python
@mcp.tool()
def journal_split_storyline_chapter(
    storyline_id: Annotated[int, Field(description="Storyline id.")],
    chapter_id: Annotated[int, Field(description="Chapter id to split.")],
    date: Annotated[str, Field(
        description="ISO YYYY-MM-DD; becomes the start of the later half.")],
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Split a chapter into two contiguous chapters at ``date`` and queue
    regeneration of both halves."""
    repo = _get_storyline_repository(ctx)
    if repo is None:
        return "Storylines feature is not configured on this server."
    user_id = _user_id(ctx)
    storyline = repo.get_storyline(storyline_id, user_id=user_id)
    if storyline is None:
        return f"Storyline {storyline_id} not found."
    ch = repo.get_chapter(chapter_id)
    if ch is None or ch.storyline_id != storyline_id:
        return f"Chapter {chapter_id} not found."
    try:
        left, right = repo.split_chapter(chapter_id, date)
    except ValueError as e:
        return f"Could not split: {e}"
    runner = _get_job_runner(ctx)
    if runner is not None:
        for c in (left, right):
            kwargs = {"user_id": user_id, "chapter_id": c.id}
            if c.state != "open":
                kwargs["mode"] = "replace"
            runner.submit_storyline_generation(storyline_id, **kwargs)
    return (f"Split chapter {chapter_id} at {date}: now seq {left.seq} "
            f"({left.start_date}–{left.end_date}) and seq {right.seq} "
            f"({right.start_date}–{right.end_date or 'open'}). "
            "Regeneration queued for both.")
```

The other four tools: `journal_add_storyline_chapter(storyline_id, start_date, end_date=None)`, `journal_merge_storyline_chapters(storyline_id, chapter_ids)`, `journal_update_storyline_chapter(storyline_id, chapter_id, title=None, start_date=None, end_date=None, allow_gap=False)` (rename when only `title`; window edit otherwise — call `repo.rename_chapter` / `repo.update_chapter_window`), and `journal_delete_storyline_chapter(storyline_id, chapter_id, allow_gap=False)`. Each resolves the runner the same way and enqueues regen for the affected chapters.

(If `_get_job_runner` doesn't exist, find how `journal_regenerate_storyline` obtains its runner near line 311 and reuse that accessor.)

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/mcp_server/test_storyline_chapter_editing_tools.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git -C server add src/journal/mcp_server/tools/storylines.py tests/mcp_server/test_storyline_chapter_editing_tools.py
git -C server commit -m "feat(storylines): MCP tools for chapter editing"
```

---

### Task 11: Server docs, journal, full suite, push

**Files:**
- Modify: the active storyline doc under `docs/` + the API-contract doc
- Create: `journal/260615-storyline-chapter-editing.md`

- [ ] **Step 1: Update docs** — document the five new endpoints (method, path, body, response, error codes) and the invariants (one open chapter, book-like + `allow_gap`, overlaps rejected, auto-regeneration). No placeholders.

- [ ] **Step 2: Write the journal entry** — `journal/260615-storyline-chapter-editing.md` capturing the design decisions (manual-first, book-like-with-override, auto-regenerate) and the repo/API/MCP surface added.

- [ ] **Step 3: Run the full unit suite + lint**

Run: `cd server && uv run pytest -m "not integration" -q && uv run ruff check src/ tests/`
Expected: all green, no lint errors.

- [ ] **Step 4: Commit + push + watch CI**

```bash
git -C server add docs/ journal/260615-storyline-chapter-editing.md
git -C server commit -m "docs(storylines): chapter editing endpoints + journal entry"
git -C server push
gh run watch
```

Expected: CI green. If it fails, read logs, fix, re-run the full suite, recommit, push, watch again.

---

# 6. Part 3 — Webapp

Run all webapp commands from inside `webapp/`. Before pushing, run `npm run test:coverage` (not just `test:unit`) to enforce the ≥85% gate.

### Task 12: Types + API client functions

**Files:**
- Modify: `src/types/storyline.ts`
- Modify: `src/api/storylines.ts`
- Test: covered indirectly by store tests in Task 13 (type-only change; build is the check)

- [ ] **Step 1: Add request/response types** to `src/types/storyline.ts`:

```typescript
/** Response for the structural chapter-edit endpoints that return one
 *  affected chapter (add, merge). */
export interface ChapterMutationResponse {
  chapter: StorylineChapterSummary
  job_ids: string[]
}

/** Response for edits that return multiple affected chapters (split,
 *  date-edit). */
export interface ChapterMultiMutationResponse {
  chapters: StorylineChapterSummary[]
  job_ids: string[]
}

export interface AddChapterRequest {
  start_date: string
  /** Omit for a new-latest open chapter; set for a ranged closed one. */
  end_date?: string
}

export interface SplitChapterRequest {
  date: string
}

export interface MergeChaptersRequest {
  chapter_ids: number[]
}

/** PATCH body for a chapter date edit (rename uses RenameChapterRequest). */
export interface UpdateChapterWindowRequest {
  start_date?: string
  end_date?: string
  allow_gap?: boolean
}

export interface DeleteChapterRequest {
  allow_gap?: boolean
}
```

- [ ] **Step 2: Add client functions** to `src/api/storylines.ts`:

```typescript
export function addChapter(
  storylineId: number,
  request: AddChapterRequest,
): Promise<ChapterMutationResponse> {
  return apiFetch<ChapterMutationResponse>(
    `/api/storylines/${storylineId}/chapters`,
    { method: 'POST', body: JSON.stringify(request) },
  )
}

export function splitChapter(
  storylineId: number,
  chapterId: number,
  request: SplitChapterRequest,
): Promise<ChapterMultiMutationResponse> {
  return apiFetch<ChapterMultiMutationResponse>(
    `/api/storylines/${storylineId}/chapters/${chapterId}/split`,
    { method: 'POST', body: JSON.stringify(request) },
  )
}

export function mergeChapters(
  storylineId: number,
  request: MergeChaptersRequest,
): Promise<ChapterMutationResponse> {
  return apiFetch<ChapterMutationResponse>(
    `/api/storylines/${storylineId}/chapters/merge`,
    { method: 'POST', body: JSON.stringify(request) },
  )
}

export function updateChapterWindow(
  storylineId: number,
  chapterId: number,
  request: UpdateChapterWindowRequest,
): Promise<ChapterMultiMutationResponse> {
  return apiFetch<ChapterMultiMutationResponse>(
    `/api/storylines/${storylineId}/chapters/${chapterId}`,
    { method: 'PATCH', body: JSON.stringify(request) },
  )
}

export function deleteChapter(
  storylineId: number,
  chapterId: number,
  request: DeleteChapterRequest = {},
): Promise<{ deleted: boolean; job_ids: string[] }> {
  return apiFetch<{ deleted: boolean; job_ids: string[] }>(
    `/api/storylines/${storylineId}/chapters/${chapterId}`,
    { method: 'DELETE', body: JSON.stringify(request) },
  )
}
```

Add the new type names to the existing `import type { ... }` block at the top of `api/storylines.ts`.

- [ ] **Step 3: Type-check**

Run: `cd webapp && npx vue-tsc --noEmit`
Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git -C webapp add src/types/storyline.ts src/api/storylines.ts
git -C webapp commit -m "feat(storylines): chapter-editing types + api client"
```

---

### Task 13: Store actions

**Files:**
- Modify: `src/stores/storylines.ts`
- Test: `src/stores/__tests__/storylines.editing.spec.ts`

After each mutation the action re-fetches the storyline detail (`loadStoryline(id)`) so the rail reflects the new chapter set and the affected chapters re-render (their `last_generated_at` updates as jobs finish). This mirrors how `removeStoryline`/`setAnchors` keep state authoritative.

- [ ] **Step 1: Write the failing tests**

Create `src/stores/__tests__/storylines.editing.spec.ts` (mock `@/api/storylines` like the existing store specs — `grep -rln "vi.mock('@/api/storylines'" webapp/src` to copy the mock setup):

```typescript
import { setActivePinia, createPinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useStorylinesStore } from '@/stores/storylines'
import * as api from '@/api/storylines'

vi.mock('@/api/storylines')

describe('storylines store — chapter editing', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('addChapter calls api and reloads the storyline', async () => {
    vi.mocked(api.addChapter).mockResolvedValue({
      chapter: { id: 9 } as never, job_ids: ['j1'],
    })
    vi.mocked(api.fetchStoryline).mockResolvedValue({
      id: 1, chapters: [],
    } as never)
    const store = useStorylinesStore()
    const resp = await store.addChapter(1, { start_date: '2026-04-01' })
    expect(api.addChapter).toHaveBeenCalledWith(1, { start_date: '2026-04-01' })
    expect(api.fetchStoryline).toHaveBeenCalledWith(1)
    expect(resp.job_ids).toEqual(['j1'])
  })

  it('splitChapter calls api and reloads', async () => {
    vi.mocked(api.splitChapter).mockResolvedValue({
      chapters: [], job_ids: ['j1', 'j2'],
    } as never)
    vi.mocked(api.fetchStoryline).mockResolvedValue({ id: 1, chapters: [] } as never)
    const store = useStorylinesStore()
    await store.splitChapter(1, 5, '2026-04-01')
    expect(api.splitChapter).toHaveBeenCalledWith(1, 5, { date: '2026-04-01' })
    expect(api.fetchStoryline).toHaveBeenCalledWith(1)
  })

  it('deleteChapter calls api and reloads', async () => {
    vi.mocked(api.deleteChapter).mockResolvedValue({ deleted: true, job_ids: [] })
    vi.mocked(api.fetchStoryline).mockResolvedValue({ id: 1, chapters: [] } as never)
    const store = useStorylinesStore()
    await store.deleteChapter(1, 5, true)
    expect(api.deleteChapter).toHaveBeenCalledWith(1, 5, { allow_gap: true })
    expect(api.fetchStoryline).toHaveBeenCalledWith(1)
  })
})
```

(Add analogous `mergeChapters` and `updateChapterDates` cases.)

- [ ] **Step 2: Run to verify failure**

Run: `cd webapp && npx vitest run src/stores/__tests__/storylines.editing.spec.ts`
Expected: FAIL (actions don't exist).

- [ ] **Step 3: Implement the actions** — add inside the store `setup` function before the `return`, and add each name to the returned object:

```typescript
  async function addChapter(
    storylineId: number,
    request: AddChapterRequest,
  ): Promise<ChapterMutationResponse> {
    const resp = await addChapterApi(storylineId, request)
    await loadStoryline(storylineId)
    return resp
  }

  async function splitChapter(
    storylineId: number,
    chapterId: number,
    date: string,
  ): Promise<ChapterMultiMutationResponse> {
    const resp = await splitChapterApi(storylineId, chapterId, { date })
    await loadStoryline(storylineId)
    return resp
  }

  async function mergeChapters(
    storylineId: number,
    chapterIds: number[],
  ): Promise<ChapterMutationResponse> {
    const resp = await mergeChaptersApi(storylineId, { chapter_ids: chapterIds })
    await loadStoryline(storylineId)
    return resp
  }

  async function updateChapterDates(
    storylineId: number,
    chapterId: number,
    request: UpdateChapterWindowRequest,
  ): Promise<ChapterMultiMutationResponse> {
    const resp = await updateChapterWindowApi(storylineId, chapterId, request)
    await loadStoryline(storylineId)
    return resp
  }

  async function deleteChapter(
    storylineId: number,
    chapterId: number,
    allowGap = false,
  ): Promise<void> {
    await deleteChapterApi(storylineId, chapterId, { allow_gap: allowGap })
    await loadStoryline(storylineId)
  }
```

Add the imports to the `@/api/storylines` import block (aliased, matching the file's convention):

```typescript
  addChapter as addChapterApi,
  splitChapter as splitChapterApi,
  mergeChapters as mergeChaptersApi,
  updateChapterWindow as updateChapterWindowApi,
  deleteChapter as deleteChapterApi,
```

and the type imports (`AddChapterRequest`, `ChapterMutationResponse`, `ChapterMultiMutationResponse`, `UpdateChapterWindowRequest`) to the `@/types/storyline` import block. Add `addChapter, splitChapter, mergeChapters, updateChapterDates, deleteChapter` to the returned object.

- [ ] **Step 4: Run to verify pass**

Run: `cd webapp && npx vitest run src/stores/__tests__/storylines.editing.spec.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git -C webapp add src/stores/storylines.ts src/stores/__tests__/storylines.editing.spec.ts
git -C webapp commit -m "feat(storylines): chapter-editing store actions"
```

---

### Task 14: Rail menu + modal components

**Files:**
- Create: `src/components/storylines/ChapterEditMenu.vue`, `ChapterDateModal.vue`, `ChapterConfirmModal.vue`
- Test: `src/components/storylines/__tests__/ChapterEditMenu.spec.ts`, `ChapterDateModal.spec.ts`, `ChapterConfirmModal.spec.ts`

Keep each component presentational — it emits intents; the view (Task 15) calls the store. This keeps them trivially testable.

- [ ] **Step 1: Write the failing test for `ChapterEditMenu`**

```typescript
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import ChapterEditMenu from '@/components/storylines/ChapterEditMenu.vue'

describe('ChapterEditMenu', () => {
  const chapter = { id: 1, seq: 1, title: 'A', state: 'closed',
                    start_date: '2026-01-01', end_date: '2026-03-31',
                    citation_count: 0, storyline_id: 1, last_generated_at: null }

  it('emits edit/split/merge/delete intents', async () => {
    const wrapper = mount(ChapterEditMenu, {
      props: { chapter, hasNext: true },
    })
    await wrapper.find('[data-test="menu-toggle"]').trigger('click')
    await wrapper.find('[data-test="action-edit"]').trigger('click')
    expect(wrapper.emitted('edit')).toBeTruthy()

    await wrapper.find('[data-test="menu-toggle"]').trigger('click')
    await wrapper.find('[data-test="action-split"]').trigger('click')
    expect(wrapper.emitted('split')).toBeTruthy()
  })

  it('hides "merge with next" when there is no next chapter', async () => {
    const wrapper = mount(ChapterEditMenu, {
      props: { chapter, hasNext: false },
    })
    await wrapper.find('[data-test="menu-toggle"]').trigger('click')
    expect(wrapper.find('[data-test="action-merge"]').exists()).toBe(false)
  })
})
```

- [ ] **Step 2: Run to verify failure**

Run: `cd webapp && npx vitest run src/components/storylines/__tests__/ChapterEditMenu.spec.ts`
Expected: FAIL (component missing).

- [ ] **Step 3: Implement `ChapterEditMenu.vue`**

```vue
<script setup lang="ts">
import { ref } from 'vue'
import type { StorylineChapterSummary } from '@/types/storyline'

defineProps<{ chapter: StorylineChapterSummary; hasNext: boolean }>()
const emit = defineEmits<{
  edit: []
  split: []
  merge: []
  delete: []
}>()
const open = ref(false)
function pick(action: 'edit' | 'split' | 'merge' | 'delete') {
  open.value = false
  emit(action)
}
</script>

<template>
  <div class="relative inline-block">
    <button
      data-test="menu-toggle"
      class="px-2 text-slate-500 hover:text-slate-800"
      @click.stop="open = !open"
    >⋯</button>
    <ul
      v-if="open"
      class="absolute right-0 z-10 mt-1 w-40 rounded border border-slate-200 bg-white shadow dark:border-slate-700 dark:bg-slate-800"
    >
      <li><button data-test="action-edit" class="block w-full px-3 py-1.5 text-left text-sm hover:bg-slate-100 dark:hover:bg-slate-700" @click="pick('edit')">Edit dates</button></li>
      <li><button data-test="action-split" class="block w-full px-3 py-1.5 text-left text-sm hover:bg-slate-100 dark:hover:bg-slate-700" @click="pick('split')">Split here</button></li>
      <li v-if="hasNext"><button data-test="action-merge" class="block w-full px-3 py-1.5 text-left text-sm hover:bg-slate-100 dark:hover:bg-slate-700" @click="pick('merge')">Merge with next</button></li>
      <li><button data-test="action-delete" class="block w-full px-3 py-1.5 text-left text-sm text-red-600 hover:bg-slate-100 dark:hover:bg-slate-700" @click="pick('delete')">Delete</button></li>
    </ul>
  </div>
</template>
```

- [ ] **Step 4: Run to verify pass**

Run: `cd webapp && npx vitest run src/components/storylines/__tests__/ChapterEditMenu.spec.ts`
Expected: PASS.

- [ ] **Step 5: Repeat TDD for `ChapterDateModal.vue`** — props `{ title: string; initialStart?: string; initialEnd?: string; showEnd: boolean }`, emits `submit: [{ start_date?: string; end_date?: string }]` and `cancel: []`. Test: mounting with `showEnd: false` renders one date input; entering a date and clicking Save emits `submit` with `{ start_date }`. Implement with two `<input type="date">` (the second gated by `showEnd`) and Save/Cancel buttons (`data-test="save"` / `data-test="cancel"`).

```typescript
// ChapterDateModal.spec.ts
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import ChapterDateModal from '@/components/storylines/ChapterDateModal.vue'

describe('ChapterDateModal', () => {
  it('emits submit with start_date when showEnd is false', async () => {
    const wrapper = mount(ChapterDateModal, {
      props: { title: 'Split chapter', showEnd: false },
    })
    await wrapper.find('[data-test="start"]').setValue('2026-04-01')
    await wrapper.find('[data-test="save"]').trigger('click')
    expect(wrapper.emitted('submit')?.[0]).toEqual([{ start_date: '2026-04-01' }])
  })
})
```

```vue
<!-- ChapterDateModal.vue -->
<script setup lang="ts">
import { ref } from 'vue'
const props = defineProps<{
  title: string
  initialStart?: string
  initialEnd?: string
  showEnd: boolean
}>()
const emit = defineEmits<{
  submit: [{ start_date?: string; end_date?: string }]
  cancel: []
}>()
const start = ref(props.initialStart ?? '')
const end = ref(props.initialEnd ?? '')
function save() {
  const payload: { start_date?: string; end_date?: string } = {}
  if (start.value) payload.start_date = start.value
  if (props.showEnd && end.value) payload.end_date = end.value
  emit('submit', payload)
}
</script>

<template>
  <div class="fixed inset-0 z-20 flex items-center justify-center bg-black/40">
    <div class="w-80 rounded-lg bg-white p-4 dark:bg-slate-800">
      <h3 class="mb-3 font-semibold">{{ title }}</h3>
      <label class="block text-sm">Start
        <input data-test="start" v-model="start" type="date"
               class="mt-1 w-full rounded border px-2 py-1 dark:bg-slate-700" />
      </label>
      <label v-if="showEnd" class="mt-2 block text-sm">End
        <input data-test="end" v-model="end" type="date"
               class="mt-1 w-full rounded border px-2 py-1 dark:bg-slate-700" />
      </label>
      <div class="mt-4 flex justify-end gap-2">
        <button data-test="cancel" class="px-3 py-1 text-sm" @click="emit('cancel')">Cancel</button>
        <button data-test="save" class="rounded bg-indigo-600 px-3 py-1 text-sm text-white" @click="save">Save</button>
      </div>
    </div>
  </div>
</template>
```

- [ ] **Step 6: Repeat TDD for `ChapterConfirmModal.vue`** — props `{ title: string; message: string; showAllowGap: boolean }`, emits `confirm: [{ allow_gap: boolean }]` and `cancel: []`. Test: clicking Confirm emits `confirm` with the toggle value; the `allow_gap` checkbox renders only when `showAllowGap` is true.

```typescript
// ChapterConfirmModal.spec.ts
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import ChapterConfirmModal from '@/components/storylines/ChapterConfirmModal.vue'

describe('ChapterConfirmModal', () => {
  it('emits confirm with allow_gap=false by default', async () => {
    const wrapper = mount(ChapterConfirmModal, {
      props: { title: 'Delete chapter', message: 'Sure?', showAllowGap: true },
    })
    await wrapper.find('[data-test="confirm"]').trigger('click')
    expect(wrapper.emitted('confirm')?.[0]).toEqual([{ allow_gap: false }])
  })
})
```

```vue
<!-- ChapterConfirmModal.vue -->
<script setup lang="ts">
import { ref } from 'vue'
defineProps<{ title: string; message: string; showAllowGap: boolean }>()
const emit = defineEmits<{ confirm: [{ allow_gap: boolean }]; cancel: [] }>()
const allowGap = ref(false)
</script>

<template>
  <div class="fixed inset-0 z-20 flex items-center justify-center bg-black/40">
    <div class="w-80 rounded-lg bg-white p-4 dark:bg-slate-800">
      <h3 class="mb-2 font-semibold">{{ title }}</h3>
      <p class="text-sm text-slate-600 dark:text-slate-300">{{ message }}</p>
      <label v-if="showAllowGap" class="mt-3 flex items-center gap-2 text-sm">
        <input v-model="allowGap" type="checkbox" data-test="allow-gap" />
        Leave a gap instead of merging into the neighbour
      </label>
      <div class="mt-4 flex justify-end gap-2">
        <button data-test="cancel" class="px-3 py-1 text-sm" @click="emit('cancel')">Cancel</button>
        <button data-test="confirm" class="rounded bg-red-600 px-3 py-1 text-sm text-white" @click="emit('confirm', { allow_gap: allowGap })">Confirm</button>
      </div>
    </div>
  </div>
</template>
```

- [ ] **Step 7: Run all three component specs**

Run: `cd webapp && npx vitest run src/components/storylines/__tests__/`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git -C webapp add src/components/storylines/
git -C webapp commit -m "feat(storylines): chapter edit menu + modal components"
```

---

### Task 15: Wire the components into `StorylineDetailView`

**Files:**
- Modify: `src/views/StorylineDetailView.vue`
- Test: extend the existing view spec if present (`grep -rln "StorylineDetailView" webapp/src/**/__tests__ webapp/src/views/__tests__ 2>/dev/null`); otherwise add a focused test that the `+ Add chapter` button calls the store.

- [ ] **Step 1: Read the current rail markup**

Run: `grep -n "chapter" src/views/StorylineDetailView.vue | head -40`
Identify where each chapter is rendered in the left rail and where to place the `⋯` menu and the `+ Add chapter` button.

- [ ] **Step 2: Write the failing test** — mount the view with a Pinia store whose `currentStoryline` has two chapters; assert clicking `[data-test="add-chapter"]` opens the date modal, and submitting it calls `store.addChapter`. Mock the store actions with `vi.fn()`. (Model on the existing view spec's mounting/stubbing setup.)

- [ ] **Step 3: Implement** — import the three components and the store; render `<ChapterEditMenu>` next to each rail item with `:has-next="index < chapters.length - 1"`; add a `+ Add chapter` button above the rail. Hold modal state (`activeModal: 'edit' | 'split' | 'add' | 'merge' | 'delete' | null` + the target chapter) in the view. Wire each component event to the matching store action:
  - `edit` → open `ChapterDateModal` (`showEnd` = chapter is closed) → `store.updateChapterDates(sid, cid, payload)`
  - `split` → open `ChapterDateModal` (`showEnd:false`, title "Split chapter") → `store.splitChapter(sid, cid, payload.start_date!)`
  - `merge` → `store.mergeChapters(sid, [cid, nextChapterId])`
  - `delete` → open `ChapterConfirmModal` (`showAllowGap:true`) → `store.deleteChapter(sid, cid, allow_gap)`
  - `+ Add chapter` → open `ChapterDateModal` (`showEnd:false`, title "Start a new chapter") → `store.addChapter(sid, { start_date: payload.start_date! })`
  After any action resolves, the store's `loadStoryline` already refreshed the rail; close the modal. Show the existing per-chapter "generating…" affordance for chapters whose `last_generated_at` is null after the edit (reuse whatever the regenerate flow already displays).

- [ ] **Step 4: Run the view + full webapp unit suite**

Run: `cd webapp && npx vitest run`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git -C webapp add src/views/StorylineDetailView.vue src/views/__tests__/ 2>/dev/null
git -C webapp commit -m "feat(storylines): wire chapter editing into detail view"
```

---

### Task 16: Webapp docs, journal, coverage gate, push

**Files:**
- Modify: the active storyline doc under `webapp/docs/`
- Create: `webapp/journal/260615-chapter-editing-ui.md`

- [ ] **Step 1: Update `webapp/docs/`** — document the chapter-editing UI (rail `⋯` menu, add button, modals) and the new store actions / api functions. No placeholders.

- [ ] **Step 2: Write the journal entry.**

- [ ] **Step 3: Lint, format, coverage, build**

Run: `cd webapp && npm run lint && npm run format && npm run test:coverage && npm run build`
Expected: all green; coverage ≥85% on statements/branches/functions/lines. If a metric dips, add targeted tests for the uncovered store/component branches.

- [ ] **Step 4: Commit + push + watch CI**

```bash
git -C webapp add docs/ journal/260615-chapter-editing-ui.md
git -C webapp commit -m "docs(storylines): chapter editing UI docs + journal"
git -C webapp push
gh run watch
```

Expected: CI green. Fix-and-repush up to 3 attempts if it fails, then flag.

---

# 7. Self-review

**Spec coverage** (against `2026-06-15-storyline-chapter-editing-design.md`):
- §3 locked decisions: manual-first (Tasks 2–6); book-like + `allow_gap` (Tasks 5, 6 + `_assert_no_overlap`); auto-regenerate (Task 7 helper used by all routes); one open chapter (split/merge/add/delete tests assert it); inline rail UI (Tasks 14–15); discrete endpoints (Tasks 8–9). ✓
- §4 no schema change. ✓ (no migration task exists, by design).
- §5 operation semantics: move-boundary (Task 5), split (Task 2), merge (Task 3), add both flavors (Task 4), delete with absorb/promote/gap (Task 6); validation summary §5.6 covered by the reject tests in Tasks 2–6 + API 400 tests in Tasks 8–9. ✓
- §6 API surface + 6.1 MCP parity (Tasks 8–10). ✓
- §9 testing & docs (repo/API/MCP tests throughout; docs+journal Tasks 11, 16). ✓

**Placeholder scan:** No "TBD/TODO"; the few "model on the existing X" notes (Tasks 10, 13, 15) point at concrete, named reference code and are accompanied by full code for at least one representative case. The `grep` discovery steps are deliberate (locate the project's existing test fixtures rather than invent them).

**Type/name consistency:** Repository methods (`split_chapter`, `merge_chapters`, `add_chapter`, `update_chapter_window`, `delete_chapter`, `_shift_seqs`, `_day_before`, `_day_after`, `_assert_no_overlap`) are referenced identically across Parts 1–2. API helper `_enqueue_chapter_regens` defined in Task 7, used in Tasks 8–9. Webapp api functions (`addChapter`, `splitChapter`, `mergeChapters`, `updateChapterWindow`, `deleteChapter`) and store actions (`addChapter`, `splitChapter`, `mergeChapters`, `updateChapterDates`, `deleteChapter`) and types (`ChapterMutationResponse`, `ChapterMultiMutationResponse`, `AddChapterRequest`, `SplitChapterRequest`, `MergeChaptersRequest`, `UpdateChapterWindowRequest`, `DeleteChapterRequest`) match between Tasks 12–15. Note the intentional name difference: the webapp store action is `updateChapterDates` while the api client function is `updateChapterWindow` — keep both as written.

One coverage note worth flagging at execution time: the open-chapter append `mode` nuance (Task 7 omits `mode` for the open chapter) should be verified against the actual `submit_storyline_generation` signature when implementing — if it requires an explicit `mode`, pass `mode="append"` for the open chapter.
