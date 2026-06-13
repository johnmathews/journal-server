# Storyline Chapters — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split a storyline into self-contained, per-window **chapters** that are generated independently and read via a left chapter rail — so the system never re-reads the whole history and you never scroll through it.

**Architecture:** A new `storyline_chapters` table sits between `storylines` and `storyline_panels`; the panel FK moves from `storyline_id` to `chapter_id`. Anchors stay storyline-level. The existing `StorylineGenerationService` is refactored so the **chapter** (its date window + open/closed state) is the unit of generation; the storyline-level `regenerate` becomes a thin wrapper over the storyline's single **open** chapter. The webapp gains a chapter rail that lazy-loads one chapter's two-panel reader, with citation numbering restarting per chapter. Phase 1 ships *reading + per-window generation*; the suggestion engine and draggable timeline editor are Phase 2.

**Tech Stack:** Python 3.13 · `uv` · pytest · SQLite (FTS5, `PRAGMA user_version` migrations) · Starlette routes on FastMCP · Vue 3 (script setup) · Pinia · Vitest · TypeScript.

**Spec:** `server/docs/superpowers/specs/2026-06-13-storyline-chapters-design.md`

**Repos:** Tasks 1–5 are in `server/`; Tasks 6–9 are in `webapp/`; Task 10 touches both. Run all `uv`/`pytest` commands from inside `server/` and all `npm` commands from inside `webapp/`. Expect one commit per repo per logical task; the cross-cutting feature produces two PRs (server first, then webapp).

---

## File Structure

**server/ (create):**
- `src/journal/db/migrations/0030_storyline_chapters.sql` — chapters table + panel-FK rebuild + backfill.
- `tests/test_migration_0030_chapters.py` — migration backfill + re-runnability on prod-shaped data.

**server/ (modify):**
- `src/journal/models.py` — add `StorylineChapter`; change `StorylinePanel.storyline_id` → `chapter_id`.
- `src/journal/db/storyline_repository.py` — chapter CRUD; panels keyed by `chapter_id`; `get_open_chapter`.
- `src/journal/services/storylines/service.py` — `regenerate_chapter(chapter_id)` core; `regenerate(storyline_id)` delegates to the open chapter.
- `src/journal/services/jobs/workers/storyline_generation.py` — pass a `chapter_id` when present.
- `src/journal/api/storylines.py` — `chapters[]` on detail; `GET .../chapters/{cid}`; back-compat `panels` shim.
- `src/journal/api/storylines_write.py` — `POST .../chapters/{cid}/regenerate`; `PATCH .../chapters/{cid}`.
- `src/journal/mcp_server/tools/storylines.py` — MCP parity for the two new read/regenerate paths.
- existing tests: `tests/test_storyline_repository.py`, `tests/test_storyline_generation.py`, `tests/test_api_storylines.py`, `tests/test_api_storylines_write.py`, `tests/test_storyline_jobs.py`, `tests/test_mcp_tools_storylines.py`.

**webapp/ (modify):**
- `src/types/storyline.ts` — `StorylineChapterSummary`, `StorylineChapterDetail`, `chapters` on `StorylineDetail`.
- `src/api/storylines.ts` — `fetchStorylineChapter`, `regenerateStorylineChapter`, `renameStorylineChapter`.
- `src/stores/storylines.ts` — `chapters`, `currentChapter`, `loadChapter`, `regenerateChapter`, `renameChapter`.
- `src/views/StorylineDetailView.vue` — left chapter rail + per-chapter citation registry + lazy panel load.
- `src/views/__tests__/StorylineDetailView.spec.ts`, `src/stores/__tests__/storylines.spec.ts` (or co-located equivalents).

**both repos (modify):** `docs/` (storylines model + API contract) and a dated `journal/` entry.

---

## Task 1: Migration 0030 — chapters table + panel-FK rebuild

**Files:**
- Create: `src/journal/db/migrations/0030_storyline_chapters.sql`
- Test: `tests/test_migration_0030_chapters.py`

The chapters table and the backfill are independently idempotent (`IF NOT EXISTS`, `NOT EXISTS`-guarded insert). The panel rebuild is wrapped in `BEGIN; … COMMIT;` so a mid-rebuild failure rolls back to the pristine pre-migration state — the runner (`_executescript_idempotent`) rolls back any transaction left open on error, and `user_version` is only bumped after the script returns, so the migration re-runs cleanly.

- [ ] **Step 1: Survey prod for shape anomalies first**

Per the migration convention, before writing the test, eyeball production for storylines that could break the backfill: NULL `start_date`/`end_date`, archived status, or panel rows whose `storyline_id` has no `storylines` row. Run against a **copy** of prod (never prod directly):

```bash
sqlite3 journal.db "SELECT COUNT(*) total, SUM(start_date IS NULL) null_start, SUM(end_date IS NULL) null_end, SUM(status='archived') archived FROM storylines;"
sqlite3 journal.db "SELECT COUNT(*) FROM storyline_panels p LEFT JOIN storylines s ON s.id=p.storyline_id WHERE s.id IS NULL;"
```
Expected: counts that confirm the backfill copies NULL dates verbatim and that there are no orphan panels (the `JOIN` in the rebuild silently drops orphans — note any found in the journal entry).

- [ ] **Step 2: Write the migration SQL**

Create `src/journal/db/migrations/0030_storyline_chapters.sql`:

```sql
-- Storyline chapters (docs/superpowers/specs/2026-06-13-storyline-chapters-design.md, Phase 1).
--
-- A new storyline_chapters table sits between storylines and
-- storyline_panels. Panels move from referencing storyline_id to
-- chapter_id. Anchors (storyline_entities) stay storyline-level.
--
-- Re-runnability: the chapters table uses IF NOT EXISTS; the backfill is
-- NOT EXISTS-guarded; the panel rebuild is wrapped in an explicit
-- transaction so a partial failure rolls back to the pre-migration state
-- (the runner rolls back any open transaction on error). Each existing
-- storyline becomes a single open chapter (seq 1) with no data loss.

-- 1. Chapters table + indexes (idempotent).
CREATE TABLE IF NOT EXISTS storyline_chapters (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    storyline_id             INTEGER NOT NULL REFERENCES storylines(id) ON DELETE CASCADE,
    seq                      INTEGER NOT NULL,
    title                    TEXT    NOT NULL DEFAULT '',
    start_date               TEXT,
    end_date                 TEXT,
    state                    TEXT    NOT NULL DEFAULT 'open'
                                 CHECK(state IN ('open', 'closed')),
    last_generated_at        TEXT,
    summary_embedding_json   TEXT,
    created_at               TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at               TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE(storyline_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_storyline_chapters_storyline
    ON storyline_chapters(storyline_id);
-- At most one open chapter per storyline.
CREATE UNIQUE INDEX IF NOT EXISTS idx_storyline_chapters_one_open
    ON storyline_chapters(storyline_id) WHERE state = 'open';

-- 2. Backfill: one open chapter per existing storyline (idempotent).
INSERT INTO storyline_chapters
    (storyline_id, seq, title, start_date, end_date, state,
     last_generated_at, summary_embedding_json)
SELECT s.id, 1, s.name, s.start_date, s.end_date, 'open',
       s.last_generated_at, s.summary_embedding_json
FROM storylines s
WHERE NOT EXISTS (
    SELECT 1 FROM storyline_chapters c WHERE c.storyline_id = s.id
);

-- 3. Rebuild storyline_panels to key on chapter_id with
--    UNIQUE(chapter_id, panel_kind). SQLite can't drop the old
--    NOT NULL + UNIQUE(storyline_id, panel_kind), so rebuild atomically.
DROP TABLE IF EXISTS storyline_panels_new;
BEGIN;
CREATE TABLE storyline_panels_new (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    chapter_id              INTEGER NOT NULL REFERENCES storyline_chapters(id) ON DELETE CASCADE,
    panel_kind              TEXT    NOT NULL
                                CHECK(panel_kind IN ('curation', 'narrative')),
    segments_json           TEXT    NOT NULL DEFAULT '[]',
    source_entry_ids_json   TEXT    NOT NULL DEFAULT '[]',
    citation_count          INTEGER NOT NULL DEFAULT 0,
    model_used              TEXT    NOT NULL DEFAULT '',
    generated_at            TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE(chapter_id, panel_kind)
);
INSERT INTO storyline_panels_new
    (id, chapter_id, panel_kind, segments_json,
     source_entry_ids_json, citation_count, model_used, generated_at)
SELECT p.id, c.id, p.panel_kind, p.segments_json,
       p.source_entry_ids_json, p.citation_count, p.model_used, p.generated_at
FROM storyline_panels p
JOIN storyline_chapters c
    ON c.storyline_id = p.storyline_id AND c.seq = 1;
DROP TABLE storyline_panels;
ALTER TABLE storyline_panels_new RENAME TO storyline_panels;
COMMIT;

CREATE INDEX IF NOT EXISTS idx_storyline_panels_chapter
    ON storyline_panels(chapter_id);
```

- [ ] **Step 3: Write the failing migration test**

Create `tests/test_migration_0030_chapters.py`. It builds a DB at version 0029 (apply all migrations, since the runner is forward-only), then asserts the post-migration shape and that a second `run_migrations` is a no-op. Because the `factory` fixture already runs every migration, the assertions verify the *end state*; a dedicated raw-SQL fixture exercises prod-shaped rows.

```python
"""Migration 0030: storylines split into chapters; panels re-keyed."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from journal.db.migrations import run_migrations

if TYPE_CHECKING:
    from journal.db.factory import ConnectionFactory


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}


def test_chapters_table_exists_and_panels_rekeyed(factory: ConnectionFactory) -> None:
    conn = factory.get()
    assert "storyline_chapters" in {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    cols = _columns(conn, "storyline_panels")
    assert "chapter_id" in cols
    assert "storyline_id" not in cols  # FK moved


def test_backfill_creates_one_open_chapter_and_moves_panels(
    factory: ConnectionFactory,
) -> None:
    conn = factory.get()
    # Seed a user + entity + storyline + two panels the way prod looks.
    conn.execute(
        "INSERT INTO users (id, email, password_hash, display_name)"
        " VALUES (1, 'a@b.c', 'x', 'A')"
    )
    conn.execute(
        "INSERT INTO entities (id, user_id, entity_type, canonical_name)"
        " VALUES (5, 1, 'activity', 'Running')"
    )
    conn.execute(
        "INSERT INTO storylines (id, user_id, name, start_date, end_date,"
        " last_generated_at) VALUES"
        " (9, 1, 'Running thread', '2026-01-01', '2026-03-01', '2026-03-02T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO storyline_chapters (storyline_id, seq, title, start_date,"
        " end_date, state) VALUES (9, 1, 'Running thread', '2026-01-01',"
        " '2026-03-01', 'open')"
    )
    chapter_id = conn.execute(
        "SELECT id FROM storyline_chapters WHERE storyline_id = 9"
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO storyline_panels (chapter_id, panel_kind, segments_json,"
        " source_entry_ids_json, citation_count, model_used) VALUES"
        " (?, 'narrative', '[]', '[]', 0, 'm')",
        (chapter_id,),
    )
    conn.commit()

    # Exactly one open chapter, seq 1, dates copied verbatim.
    row = conn.execute(
        "SELECT seq, state, start_date, end_date FROM storyline_chapters"
        " WHERE storyline_id = 9"
    ).fetchone()
    assert (row[0], row[1], row[2], row[3]) == (1, "open", "2026-01-01", "2026-03-01")
    # Panel is reachable by chapter_id.
    cnt = conn.execute(
        "SELECT COUNT(*) FROM storyline_panels WHERE chapter_id = ?",
        (chapter_id,),
    ).fetchone()[0]
    assert cnt == 1


def test_migration_is_rerunnable(factory: ConnectionFactory) -> None:
    conn = factory.get()
    # Force a re-apply by rewinding user_version below 0030, then re-run.
    conn.execute("PRAGMA user_version = 29")
    conn.commit()
    run_migrations(conn)  # must not raise and must not duplicate chapters
    dupes = conn.execute(
        "SELECT storyline_id, COUNT(*) c FROM storyline_chapters"
        " GROUP BY storyline_id HAVING c > 1"
    ).fetchall()
    assert dupes == []
```

> Note: `test_migration_is_rerunnable` rewinds `user_version` to 29 and re-runs. On re-apply, the chapters `IF NOT EXISTS` + `NOT EXISTS` backfill are no-ops, but step 3 will fail because `storyline_panels` no longer has `storyline_id`. **This is the partial-failure guard's job:** make step 3 tolerant by skipping the rebuild when already rebuilt. Add this guard as the very first line of step 3 in the SQL, replacing the bare `DROP TABLE IF EXISTS storyline_panels_new;`:

```sql
-- Skip the rebuild entirely if storyline_panels is already chapter-keyed.
-- (Guards re-apply after user_version rewind / partial completion.)
DROP TABLE IF EXISTS storyline_panels_new;
CREATE TEMP TABLE IF NOT EXISTS _mig0030_skip AS
    SELECT 1 AS done FROM pragma_table_info('storyline_panels')
    WHERE name = 'chapter_id' LIMIT 1;
```

Then wrap the `BEGIN; … COMMIT;` rebuild so it only runs when `_mig0030_skip` is empty. Plain `.sql` can't branch, so implement the guard in the test by asserting re-run safety and, if the `pragma_table_info` guard proves insufficient, split the rebuild into its own conditional applied from `migrations.py`. **Simplest robust choice:** keep the rebuild in SQL, and in `test_migration_is_rerunnable` assert that a fresh forward-only apply (no rewind) plus a second `run_migrations()` (which version-skips 0030) leaves a single open chapter — this matches how the runner actually behaves in production (forward-only). Drop the `user_version` rewind from the test if the in-SQL guard is not added.

- [ ] **Step 4: Run the tests to verify they fail**

Run: `uv run pytest tests/test_migration_0030_chapters.py -v`
Expected: FAIL (`storyline_chapters` missing / `chapter_id` not a column) until the SQL file is picked up.

- [ ] **Step 5: Run the full unit suite to catch fallout**

Run: `uv run pytest -m "not integration" -q`
Expected: failures in `test_storyline_repository.py` / `test_storyline_generation.py` / `test_api_storylines*` because the panel table changed shape. These are fixed in Tasks 2–5; note them and proceed.

- [ ] **Step 6: Commit**

```bash
git add src/journal/db/migrations/0030_storyline_chapters.sql tests/test_migration_0030_chapters.py
git commit -m "feat(storylines): migration 0030 — chapters table + panel re-key"
```

---

## Task 2: `StorylineChapter` model + repository chapter CRUD

**Files:**
- Modify: `src/journal/models.py:439-482`
- Modify: `src/journal/db/storyline_repository.py`
- Test: `tests/test_storyline_repository.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_storyline_repository.py` (uses existing `storyline_repo`, `seed_user`, `seed_entity` fixtures):

```python
def test_create_and_list_chapters(
    storyline_repo: SQLiteStorylineRepository, seed_user: int, seed_entity: int,
) -> None:
    sl = storyline_repo.create_storyline(
        user_id=seed_user, entity_ids=[seed_entity], name="Run thread",
        start_date="2026-01-01", end_date="2026-03-01",
    )
    # Backfilled open chapter exists after create? No — create_storyline does
    # not make a chapter. The repo creates the first chapter explicitly:
    ch = storyline_repo.create_chapter(
        storyline_id=sl.id, seq=1, title="Ch 1",
        start_date="2026-01-01", end_date="2026-03-01", state="open",
    )
    assert ch.seq == 1 and ch.state == "open"
    chapters = storyline_repo.list_chapters(sl.id)
    assert [c.id for c in chapters] == [ch.id]
    assert storyline_repo.get_open_chapter(sl.id).id == ch.id


def test_rename_chapter(
    storyline_repo: SQLiteStorylineRepository, seed_user: int, seed_entity: int,
) -> None:
    sl = storyline_repo.create_storyline(
        user_id=seed_user, entity_ids=[seed_entity], name="X",
    )
    ch = storyline_repo.create_chapter(storyline_id=sl.id, seq=1, title="Old")
    updated = storyline_repo.rename_chapter(ch.id, "New Title")
    assert updated is not None and updated.title == "New Title"
```

> **Decision recorded here:** `create_storyline` stays unchanged (it does NOT auto-create a chapter); the API/service create the first chapter explicitly. The migration backfills chapters for *existing* storylines; new storylines get their first chapter from the create path (Task 5, Step 5).

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_storyline_repository.py::test_create_and_list_chapters -v`
Expected: FAIL with `AttributeError: 'SQLiteStorylineRepository' object has no attribute 'create_chapter'`.

- [ ] **Step 3: Add the `StorylineChapter` model**

In `src/journal/models.py`, after the `Storyline` dataclass (line ~461) add:

```python
@dataclass
class StorylineChapter:
    """One time-windowed chapter of a storyline.

    Each chapter owns its two panels and is generated over its own date
    window. Exactly one chapter per storyline is ``open`` (the live,
    append-extended chapter); the rest are ``closed`` and stable.
    """

    id: int
    storyline_id: int
    seq: int
    title: str = ""
    start_date: str | None = None
    end_date: str | None = None
    state: str = "open"
    last_generated_at: str | None = None
    summary_embedding: list[float] | None = None
    created_at: str = ""
    updated_at: str = ""
```

- [ ] **Step 4: Add the chapter row mapper + CRUD to the repository**

In `src/journal/db/storyline_repository.py`, import the new model (`from journal.models import Storyline, StorylineChapter, StorylinePanel`) and add a mapper next to `_row_to_storyline`:

```python
def _row_to_chapter(row: sqlite3.Row) -> StorylineChapter:
    summary_raw = row["summary_embedding_json"]
    summary = json.loads(summary_raw) if summary_raw else None
    return StorylineChapter(
        id=row["id"],
        storyline_id=row["storyline_id"],
        seq=row["seq"],
        title=row["title"] or "",
        start_date=row["start_date"],
        end_date=row["end_date"],
        state=row["state"],
        last_generated_at=row["last_generated_at"],
        summary_embedding=[float(x) for x in summary] if summary else None,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
```

Then add a `# ── chapters ──` section with these methods:

```python
def create_chapter(
    self,
    storyline_id: int,
    seq: int,
    title: str = "",
    start_date: str | None = None,
    end_date: str | None = None,
    state: str = "open",
) -> StorylineChapter:
    conn = self._conn()
    cursor = conn.execute(
        "INSERT INTO storyline_chapters"
        " (storyline_id, seq, title, start_date, end_date, state)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (storyline_id, seq, title.strip(), start_date, end_date, state),
    )
    conn.commit()
    chapter_id = cursor.lastrowid
    assert chapter_id is not None
    ch = self.get_chapter(chapter_id)
    assert ch is not None
    return ch

def get_chapter(self, chapter_id: int) -> StorylineChapter | None:
    row = self._conn().execute(
        "SELECT * FROM storyline_chapters WHERE id = ?", (chapter_id,),
    ).fetchone()
    return _row_to_chapter(row) if row else None

def list_chapters(self, storyline_id: int) -> list[StorylineChapter]:
    rows = self._conn().execute(
        "SELECT * FROM storyline_chapters"
        " WHERE storyline_id = ? ORDER BY seq ASC",
        (storyline_id,),
    ).fetchall()
    return [_row_to_chapter(r) for r in rows]

def get_open_chapter(self, storyline_id: int) -> StorylineChapter | None:
    row = self._conn().execute(
        "SELECT * FROM storyline_chapters"
        " WHERE storyline_id = ? AND state = 'open'"
        " ORDER BY seq DESC LIMIT 1",
        (storyline_id,),
    ).fetchone()
    return _row_to_chapter(row) if row else None

def rename_chapter(
    self, chapter_id: int, title: str,
) -> StorylineChapter | None:
    conn = self._conn()
    cursor = conn.execute(
        "UPDATE storyline_chapters"
        " SET title = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"
        " WHERE id = ?",
        (title.strip(), chapter_id),
    )
    conn.commit()
    if cursor.rowcount == 0:
        return None
    return self.get_chapter(chapter_id)

def record_chapter_generation_complete(self, chapter_id: int) -> None:
    conn = self._conn()
    conn.execute(
        "UPDATE storyline_chapters"
        " SET last_generated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),"
        "     updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"
        " WHERE id = ?",
        (chapter_id,),
    )
    conn.commit()

def update_chapter_summary_embedding(
    self, chapter_id: int, embedding: list[float] | None,
) -> None:
    conn = self._conn()
    conn.execute(
        "UPDATE storyline_chapters"
        " SET summary_embedding_json = ?,"
        "     updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"
        " WHERE id = ?",
        (json.dumps(embedding) if embedding is not None else None, chapter_id),
    )
    conn.commit()
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_storyline_repository.py -k chapter -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/journal/models.py src/journal/db/storyline_repository.py tests/test_storyline_repository.py
git commit -m "feat(storylines): StorylineChapter model + repository chapter CRUD"
```

---

## Task 3: Re-key panels on `chapter_id`

**Files:**
- Modify: `src/journal/models.py` (`StorylinePanel.storyline_id` → `chapter_id`)
- Modify: `src/journal/db/storyline_repository.py` (`_row_to_panel`, `upsert_panel`, `get_panel`, `list_panels`)
- Test: `tests/test_storyline_repository.py`

- [ ] **Step 1: Update the failing panel tests**

In `tests/test_storyline_repository.py`, the existing panel tests call `upsert_panel(storyline_id=..., panel_kind=...)`. Update them (and add one) to use `chapter_id`:

```python
def test_upsert_and_get_panel_by_chapter(
    storyline_repo: SQLiteStorylineRepository, seed_user: int, seed_entity: int,
) -> None:
    sl = storyline_repo.create_storyline(
        user_id=seed_user, entity_ids=[seed_entity], name="X",
    )
    ch = storyline_repo.create_chapter(storyline_id=sl.id, seq=1, title="Ch1")
    panel = storyline_repo.upsert_panel(
        chapter_id=ch.id, panel_kind="narrative",
        segments=[{"kind": "text", "text": "hi"}],
        source_entry_ids=[1], citation_count=0, model_used="m",
    )
    assert panel.chapter_id == ch.id
    got = storyline_repo.get_panel(ch.id, "narrative")
    assert got is not None and got.segments[0]["text"] == "hi"
    assert [p.panel_kind for p in storyline_repo.list_panels(ch.id)] == ["narrative"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_storyline_repository.py::test_upsert_and_get_panel_by_chapter -v`
Expected: FAIL (`upsert_panel` still expects `storyline_id`).

- [ ] **Step 3: Change the model field**

In `src/journal/models.py`, `StorylinePanel`: rename `storyline_id: int` → `chapter_id: int` (first field). Leave the rest unchanged.

- [ ] **Step 4: Update the repository panel methods**

In `src/journal/db/storyline_repository.py`:

`_row_to_panel` — change `storyline_id=row["storyline_id"]` to `chapter_id=row["chapter_id"]`.

`upsert_panel` — new signature and SQL:

```python
def upsert_panel(
    self,
    chapter_id: int,
    panel_kind: str,
    segments: list[dict[str, Any]],
    source_entry_ids: list[int],
    citation_count: int,
    model_used: str,
) -> StorylinePanel:
    conn = self._conn()
    conn.execute(
        "INSERT INTO storyline_panels"
        " (chapter_id, panel_kind, segments_json,"
        "  source_entry_ids_json, citation_count, model_used, generated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))"
        " ON CONFLICT(chapter_id, panel_kind) DO UPDATE SET"
        "  segments_json = excluded.segments_json,"
        "  source_entry_ids_json = excluded.source_entry_ids_json,"
        "  citation_count = excluded.citation_count,"
        "  model_used = excluded.model_used,"
        "  generated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')",
        (
            chapter_id, panel_kind, json.dumps(segments),
            json.dumps(source_entry_ids), int(citation_count), model_used,
        ),
    )
    conn.commit()
    panel = self.get_panel(chapter_id, panel_kind)
    assert panel is not None
    return panel
```

`get_panel` / `list_panels` — replace the `storyline_id` parameter and WHERE clause with `chapter_id`:

```python
def get_panel(self, chapter_id: int, panel_kind: str) -> StorylinePanel | None:
    row = self._conn().execute(
        "SELECT * FROM storyline_panels"
        " WHERE chapter_id = ? AND panel_kind = ?",
        (chapter_id, panel_kind),
    ).fetchone()
    return _row_to_panel(row) if row else None

def list_panels(self, chapter_id: int) -> list[StorylinePanel]:
    rows = self._conn().execute(
        "SELECT * FROM storyline_panels"
        " WHERE chapter_id = ? ORDER BY panel_kind",
        (chapter_id,),
    ).fetchall()
    return [_row_to_panel(r) for r in rows]
```

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest tests/test_storyline_repository.py -v`
Expected: PASS (the chapter + panel tests).

- [ ] **Step 6: Commit**

```bash
git add src/journal/models.py src/journal/db/storyline_repository.py tests/test_storyline_repository.py
git commit -m "feat(storylines): re-key storyline panels on chapter_id"
```

---

## Task 4: Generation service operates per chapter

**Files:**
- Modify: `src/journal/services/storylines/service.py`
- Modify: `src/journal/services/jobs/workers/storyline_generation.py`
- Test: `tests/test_storyline_generation.py`, `tests/test_storyline_jobs.py`

The core change: panels/embedding/`record_*` calls move from `storyline_id` to `chapter_id`, and the window comes from the chapter. `_fetch_excerpts(storyline, …)` is unchanged (anchors are storyline-level). Introduce `regenerate_chapter(chapter_id, …)` as the core and make `regenerate(storyline_id, …)` resolve the storyline's open chapter and delegate — preserving the existing job/button behavior.

- [ ] **Step 1: Write the failing test**

In `tests/test_storyline_generation.py` (which already injects fake narrator/glue/repos), add:

```python
def test_regenerate_chapter_writes_panels_to_chapter(
    generation_service, storyline_repo, seed_user, seed_entity,
):
    sl = storyline_repo.create_storyline(
        user_id=seed_user, entity_ids=[seed_entity], name="X",
        start_date="2026-01-01", end_date="2026-03-01",
    )
    ch = storyline_repo.create_chapter(
        storyline_id=sl.id, seq=1, title="Ch1",
        start_date="2026-01-01", end_date="2026-03-01", state="open",
    )
    result = generation_service.regenerate_chapter(ch.id)
    assert result.storyline_id == sl.id
    # Panels are reachable by chapter_id.
    assert storyline_repo.get_panel(ch.id, "narrative") is not None
    assert storyline_repo.get_chapter(ch.id).last_generated_at is not None
```

> Reuse the existing module fixtures. If the current suite exposes the service as a different fixture name (e.g. `service`), match it; the construction is unchanged from today.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_storyline_generation.py::test_regenerate_chapter_writes_panels_to_chapter -v`
Expected: FAIL (`regenerate_chapter` undefined).

- [ ] **Step 3: Refactor the service**

In `src/journal/services/storylines/service.py`:

Add `regenerate_chapter` as the new core. It mirrors today's `regenerate` body but (a) resolves the chapter + its storyline, (b) uses the chapter's `start_date`/`end_date` as the window, (c) writes panels with `chapter_id=chapter.id`, (d) calls `record_chapter_generation_complete` / `update_chapter_summary_embedding`, and (e) picks `append` vs `replace` from the chapter `state` plus the caller's `mode`:

```python
def regenerate_chapter(
    self,
    chapter_id: int,
    *,
    mode: GenerationMode = "replace",
) -> GenerationResult:
    chapter = self._storyline_repository.get_chapter(chapter_id)
    if chapter is None:
        raise ValueError(f"Chapter {chapter_id} not found")
    storyline = self._storyline_repository.get_storyline(chapter.storyline_id)
    if storyline is None:
        raise ValueError(f"Storyline {chapter.storyline_id} not found")

    excerpts, fts_count = self._fetch_excerpts(
        storyline, start_date=chapter.start_date, end_date=chapter.end_date,
    )
    result = GenerationResult(
        storyline_id=storyline.id,
        entry_count=len(excerpts),
        entity_mention_count=len(excerpts) - fts_count,
        fts_fallback_count=fts_count,
    )
    if not excerpts:
        self._storyline_repository.upsert_panel(
            chapter_id=chapter_id, panel_kind="curation",
            segments=[], source_entry_ids=[], citation_count=0,
            model_used=self._glue.model,
        )
        self._storyline_repository.upsert_panel(
            chapter_id=chapter_id, panel_kind="narrative",
            segments=[], source_entry_ids=[], citation_count=0,
            model_used=self._narrator.model,
        )
        self._storyline_repository.record_chapter_generation_complete(chapter_id)
        result.warnings.append("No entries found in chapter window.")
        return result

    narrative = self._narrator.generate_narrative(
        excerpts=excerpts,
        storyline_name=storyline.name,
        storyline_description=storyline.description,
    )
    result.narrative_citation_count = narrative.citation_count
    result.narrative_model = narrative.model_used
    if narrative.segments:
        self._storyline_repository.upsert_panel(
            chapter_id=chapter_id, panel_kind="narrative",
            segments=narrative.segments,
            source_entry_ids=narrative.source_entry_ids,
            citation_count=narrative.citation_count,
            model_used=narrative.model_used,
        )
    else:
        result.warnings.append(
            "Narrative generation produced no segments; existing preserved."
        )

    glue = self._glue.generate_transitions(excerpts)
    curation_segments = _build_curation_segments(excerpts, glue.transitions)
    result.curation_citation_count = count_citations(curation_segments)
    result.curation_model = glue.model_used
    self._storyline_repository.upsert_panel(
        chapter_id=chapter_id, panel_kind="curation",
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
                log.exception("Embedder failed for chapter %d", chapter_id)
                result.warnings.append("Embedder failed; embedding not updated.")
            else:
                self._storyline_repository.update_chapter_summary_embedding(
                    chapter_id, embedding,
                )

    self._storyline_repository.record_chapter_generation_complete(chapter_id)
    return result
```

> The append-mode path (`_regenerate_append`) carries over for the **open** chapter; for Phase 1 keep it operating on the open chapter by adding a `chapter_id` parameter that mirrors the `regenerate_chapter` panel-write changes. If wiring append fully is large, Phase 1 may keep append delegating to `regenerate_chapter(chapter_id, mode="replace")` for the open chapter and defer true incremental append to Phase 2 — record whichever choice in the journal entry. **Recommended Phase 1:** `regenerate_chapter` with `mode="replace"` only; the open chapter is simply replaced over its (open-ended) window. This is correct, just less token-efficient than append, and keeps the refactor small.

Make `regenerate(storyline_id, …)` delegate:

```python
def regenerate(
    self,
    storyline_id: int,
    *,
    start_date: date | str | None = None,
    end_date: date | str | None = None,
    mode: GenerationMode = "replace",
) -> GenerationResult:
    """Back-compat entry point: regenerate the storyline's open chapter."""
    open_chapter = self._storyline_repository.get_open_chapter(storyline_id)
    if open_chapter is None:
        raise ValueError(f"Storyline {storyline_id} has no open chapter")
    return self.regenerate_chapter(open_chapter.id, mode=mode)
```

Add `regenerate_chapter` to `StorylineGenerationServiceProtocol`.

- [ ] **Step 4: Update the job worker**

In `src/journal/services/jobs/workers/storyline_generation.py`, accept an optional `chapter_id` in the job payload and call `regenerate_chapter` when present, else fall back to `regenerate(storyline_id)`. Mirror the existing payload-parsing style in that file:

```python
chapter_id = payload.get("chapter_id")
if chapter_id is not None:
    result = service.regenerate_chapter(int(chapter_id), mode=mode)
else:
    result = service.regenerate(int(storyline_id), mode=mode)
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_storyline_generation.py tests/test_storyline_jobs.py -v`
Expected: PASS. Fix any fixture call sites still passing `storyline_id=` to `upsert_panel`.

- [ ] **Step 6: Commit**

```bash
git add src/journal/services/storylines/service.py src/journal/services/jobs/workers/storyline_generation.py tests/test_storyline_generation.py tests/test_storyline_jobs.py
git commit -m "feat(storylines): generate panels per chapter; regenerate() delegates to open chapter"
```

---

## Task 5: API + MCP read paths and chapter regenerate/rename

**Files:**
- Modify: `src/journal/api/storylines.py`
- Modify: `src/journal/api/storylines_write.py`
- Modify: `src/journal/mcp_server/tools/storylines.py`
- Modify: `create_storyline` callers to seed the first chapter
- Test: `tests/test_api_storylines.py`, `tests/test_api_storylines_write.py`, `tests/test_mcp_tools_storylines.py`

- [ ] **Step 1: Write the failing API tests**

In `tests/test_api_storylines.py` (use its existing client/auth fixtures):

```python
def test_detail_includes_chapters_and_backcompat_panels(client, seed_storyline):
    sid, chapter_id = seed_storyline  # fixture creates storyline + 1 open chapter + panels
    resp = client.get(f"/api/storylines/{sid}")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["chapters"], list) and len(body["chapters"]) == 1
    assert body["chapters"][0]["state"] == "open"
    # Back-compat: storyline-level panels still present (open chapter's panels).
    assert "panels" in body


def test_get_single_chapter_returns_panels(client, seed_storyline):
    sid, chapter_id = seed_storyline
    resp = client.get(f"/api/storylines/{sid}/chapters/{chapter_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == chapter_id
    assert "panels" in body and "narrative" in body["panels"]
```

In `tests/test_api_storylines_write.py`:

```python
def test_regenerate_single_chapter_queues_job(client, seed_storyline):
    sid, chapter_id = seed_storyline
    resp = client.post(f"/api/storylines/{sid}/chapters/{chapter_id}/regenerate")
    assert resp.status_code == 202
    assert "job_id" in resp.json()


def test_rename_chapter(client, seed_storyline):
    sid, chapter_id = seed_storyline
    resp = client.patch(
        f"/api/storylines/{sid}/chapters/{chapter_id}",
        json={"title": "The Move"},
    )
    assert resp.status_code == 200
    assert resp.json()["title"] == "The Move"
```

> Add a `seed_storyline` fixture (or extend the existing storyline fixture) that creates a storyline, a `seq=1` open chapter via `repo.create_chapter`, and two panels via `repo.upsert_panel(chapter_id=...)`. Returns `(storyline_id, chapter_id)`.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_api_storylines.py tests/test_api_storylines_write.py -k chapter -v`
Expected: FAIL (404 / missing keys).

- [ ] **Step 3: Add `chapters[]` + back-compat panels to the detail route**

In `src/journal/api/storylines.py`, extend `storyline_detail`. Replace the panel fetch with chapter-aware logic and add a chapter serializer:

```python
chapters = repo.list_chapters(storyline.id)
open_chapter = next((c for c in chapters if c.state == "open"), None)
# Back-compat: storyline-level panels = the open chapter's panels.
panels = repo.list_panels(open_chapter.id) if open_chapter else []
return JSONResponse({
    **_storyline_to_dict(storyline, anchors),
    "chapters": [_chapter_to_dict(repo, c) for c in chapters],
    "panels": {p.panel_kind: _panel_to_dict(p) for p in panels},
})
```

Add the serializer:

```python
def _chapter_to_dict(
    repo: SQLiteStorylineRepository, c: StorylineChapter,
) -> dict[str, Any]:
    panels = repo.list_panels(c.id)
    citation_count = sum(p.citation_count for p in panels)
    return {
        "id": c.id,
        "storyline_id": c.storyline_id,
        "seq": c.seq,
        "title": c.title,
        "start_date": c.start_date,
        "end_date": c.end_date,
        "state": c.state,
        "last_generated_at": c.last_generated_at,
        "citation_count": citation_count,
    }
```

Add the single-chapter route in `register_storylines_routes`:

```python
@mcp.custom_route(
    "/api/storylines/{storyline_id:int}/chapters/{chapter_id:int}",
    methods=["GET"], name="api_storyline_chapter_detail",
)
@handler(services_getter)
def storyline_chapter_detail(
    request: Request, services: ServicesDict, body: None
) -> JSONResponse:
    repo = services.get("storyline_repository")
    if repo is None:
        return JSONResponse({"error": "Storylines feature not configured"}, status_code=503)
    user = get_authenticated_user(request)
    sid = int(request.path_params["storyline_id"])
    cid = int(request.path_params["chapter_id"])
    storyline = repo.get_storyline(sid, user_id=user.user_id)
    if storyline is None:
        return JSONResponse({"error": "Storyline not found"}, status_code=404)
    chapter = repo.get_chapter(cid)
    if chapter is None or chapter.storyline_id != sid:
        return JSONResponse({"error": "Chapter not found"}, status_code=404)
    panels = repo.list_panels(cid)
    return JSONResponse({
        **_chapter_to_dict(repo, chapter),
        "panels": {p.panel_kind: _panel_to_dict(p) for p in panels},
    })
```

Import `StorylineChapter` in the `TYPE_CHECKING` block.

- [ ] **Step 4: Add chapter regenerate + rename write routes**

In `src/journal/api/storylines_write.py`, following the existing `regenerate` and `PATCH` handlers' style (auth, 404 on wrong owner, job enqueue via the jobs service), add:

- `POST /api/storylines/{storyline_id:int}/chapters/{chapter_id:int}/regenerate` — verify the chapter belongs to the user's storyline, enqueue a `storyline_generation` job with payload `{"storyline_id": sid, "chapter_id": cid, "mode": "replace"}`, return `202 {"job_id": ...}`. Mirror the existing storyline-level regenerate handler exactly, just adding `chapter_id` to the payload.
- `PATCH /api/storylines/{storyline_id:int}/chapters/{chapter_id:int}` — parse `{"title": str}`, reject empty (400), call `repo.rename_chapter(cid, title)`, return the chapter dict (reuse `_chapter_to_dict`, importing it or duplicating the small serializer).

- [ ] **Step 5: Seed the first chapter on storyline create**

So newly-created storylines (not just migrated ones) have an open chapter, update the `POST /api/storylines` handler in `storylines_write.py`: right after `repo.create_storyline(...)` succeeds, call:

```python
repo.create_chapter(
    storyline_id=storyline.id, seq=1, title=storyline.name,
    start_date=storyline.start_date, end_date=storyline.end_date,
    state="open",
)
```

This guarantees the generation job (which resolves the open chapter) has a target. Verify the existing create test still passes and add an assertion that one open chapter exists post-create.

- [ ] **Step 6: MCP parity**

In `src/journal/mcp_server/tools/storylines.py`, the existing `journal_get_storyline` tool returns panels; extend its return to include `chapters` (reuse the repo + a small serializer). Add a `journal_regenerate_storyline` `chapter_id` optional arg that, when set, calls `service.regenerate_chapter`. Keep the tool signatures backward-compatible (new arg defaults to `None`).

- [ ] **Step 7: Run the API + MCP tests**

Run: `uv run pytest tests/test_api_storylines.py tests/test_api_storylines_write.py tests/test_mcp_tools_storylines.py -v`
Expected: PASS.

- [ ] **Step 8: Full suite + lint**

Run: `uv run pytest -m "not integration" -q && uv run ruff check src/ tests/`
Expected: all green, no lint errors. Bring up Chroma and run the integration tier too if storylines integration tests exist: `docker compose -f docker-compose.dev.yml up -d && uv run pytest -m integration -q`.

- [ ] **Step 9: Commit**

```bash
git add src/journal/api/storylines.py src/journal/api/storylines_write.py src/journal/mcp_server/tools/storylines.py tests/test_api_storylines.py tests/test_api_storylines_write.py tests/test_mcp_tools_storylines.py
git commit -m "feat(storylines): chapter read/regenerate/rename API + MCP parity"
```

---

## Task 6: Webapp types

**Files:**
- Modify: `webapp/src/types/storyline.ts`
- Test: covered by Tasks 8–9.

- [ ] **Step 1: Add chapter types**

In `src/types/storyline.ts`, add:

```typescript
export interface StorylineChapterSummary {
  id: number
  storyline_id: number
  seq: number
  title: string
  start_date: string | null
  end_date: string | null
  state: 'open' | 'closed'
  last_generated_at: string | null
  /** Sum of citation_count across the chapter's panels — rail badge. */
  citation_count: number
}

export interface StorylineChapterDetail extends StorylineChapterSummary {
  panels: Partial<Record<StorylinePanelKind, StorylinePanel>>
}

export interface RenameChapterRequest {
  title: string
}
```

And add `chapters: StorylineChapterSummary[]` to `StorylineDetail`:

```typescript
export interface StorylineDetail extends StorylineSummary {
  chapters: StorylineChapterSummary[]
  panels: Partial<Record<StorylinePanelKind, StorylinePanel>>
}
```

- [ ] **Step 2: Type-check**

Run: `npm run build`
Expected: passes (types are additive; `chapters` may surface errors in tests/fixtures fixed in Tasks 8–9 — if `npm run build` fails only on the spec/store, proceed; it goes green after those tasks).

- [ ] **Step 3: Commit**

```bash
git add src/types/storyline.ts
git commit -m "feat(storylines): chapter TypeScript types"
```

---

## Task 7: Webapp API client

**Files:**
- Modify: `webapp/src/api/storylines.ts`

- [ ] **Step 1: Add the three client functions**

In `src/api/storylines.ts`, import `StorylineChapterDetail`, `RegenerateStorylineResponse`, `RenameChapterRequest`, and a new `RenameChapterResponse` (= `StorylineChapterSummary`; add that alias to the import or types). Then:

```typescript
export function fetchStorylineChapter(
  storylineId: number,
  chapterId: number,
): Promise<StorylineChapterDetail> {
  return apiFetch<StorylineChapterDetail>(
    `/api/storylines/${storylineId}/chapters/${chapterId}`,
  )
}

export function regenerateStorylineChapter(
  storylineId: number,
  chapterId: number,
): Promise<RegenerateStorylineResponse> {
  return apiFetch<RegenerateStorylineResponse>(
    `/api/storylines/${storylineId}/chapters/${chapterId}/regenerate`,
    { method: 'POST' },
  )
}

export function renameStorylineChapter(
  storylineId: number,
  chapterId: number,
  request: RenameChapterRequest,
): Promise<StorylineChapterSummary> {
  return apiFetch<StorylineChapterSummary>(
    `/api/storylines/${storylineId}/chapters/${chapterId}`,
    { method: 'PATCH', body: JSON.stringify(request) },
  )
}
```

- [ ] **Step 2: Type-check**

Run: `npm run build`
Expected: no new errors from this file.

- [ ] **Step 3: Commit**

```bash
git add src/api/storylines.ts
git commit -m "feat(storylines): chapter API client functions"
```

---

## Task 8: Webapp store — chapters state + actions

**Files:**
- Modify: `webapp/src/stores/storylines.ts`
- Test: `src/stores/__tests__/storylines.spec.ts` (create if absent; follow the entities store test pattern)

- [ ] **Step 1: Write the failing store test**

```typescript
import { setActivePinia, createPinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useStorylinesStore } from '@/stores/storylines'
import * as api from '@/api/storylines'

describe('storylines store — chapters', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('loadChapter sets currentChapter', async () => {
    vi.spyOn(api, 'fetchStorylineChapter').mockResolvedValue({
      id: 3, storyline_id: 9, seq: 1, title: 'Ch1',
      start_date: null, end_date: null, state: 'open',
      last_generated_at: null, citation_count: 0,
      panels: { narrative: undefined, curation: undefined },
    } as never)
    const store = useStorylinesStore()
    await store.loadChapter(9, 3)
    expect(store.currentChapter?.id).toBe(3)
  })
})
```

- [ ] **Step 2: Run to verify it fails**

Run: `npm run test:unit -- storylines`
Expected: FAIL (`loadChapter` undefined).

- [ ] **Step 3: Add chapter state + actions to the store**

Add refs and actions, and return them. State:

```typescript
const currentChapter = ref<StorylineChapterDetail | null>(null)
const chapterLoading = ref(false)
```

Actions:

```typescript
async function loadChapter(storylineId: number, chapterId: number): Promise<void> {
  chapterLoading.value = true
  error.value = null
  try {
    currentChapter.value = await fetchStorylineChapter(storylineId, chapterId)
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Failed to load chapter'
  } finally {
    chapterLoading.value = false
  }
}

async function regenerateChapter(
  storylineId: number,
  chapterId: number,
): Promise<RegenerateStorylineResponse> {
  regenerating.value = true
  regenerateError.value = null
  try {
    return await regenerateStorylineChapterApi(storylineId, chapterId)
  } catch (e) {
    regenerateError.value =
      e instanceof Error ? e.message : 'Failed to queue regeneration'
    throw e
  } finally {
    regenerating.value = false
  }
}

async function renameChapter(
  storylineId: number,
  chapterId: number,
  title: string,
): Promise<void> {
  const resp = await renameStorylineChapterApi(storylineId, chapterId, { title })
  // Update the matching chapter summary on currentStoryline.
  if (currentStoryline.value?.id === storylineId) {
    const ch = currentStoryline.value.chapters.find((c) => c.id === chapterId)
    if (ch) ch.title = resp.title
  }
  if (currentChapter.value?.id === chapterId) {
    currentChapter.value = { ...currentChapter.value, title: resp.title }
  }
}
```

Add the imports (`fetchStorylineChapter`, `regenerateStorylineChapter as regenerateStorylineChapterApi`, `renameStorylineChapter as renameStorylineChapterApi`, and the chapter types). Add `currentChapter`, `chapterLoading`, `loadChapter`, `regenerateChapter`, `renameChapter` to the returned object.

- [ ] **Step 4: Run to verify it passes**

Run: `npm run test:unit -- storylines`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/stores/storylines.ts src/stores/__tests__/storylines.spec.ts
git commit -m "feat(storylines): store chapter state + actions"
```

---

## Task 9: Webapp — chapter rail in `StorylineDetailView`

**Files:**
- Modify: `webapp/src/views/StorylineDetailView.vue`
- Test: `src/views/__tests__/StorylineDetailView.spec.ts`

The rail lists `currentStoryline.chapters`; selecting one calls `store.loadChapter` and the existing two-panel reader renders `currentChapter.panels`. Citation numbering is built from the **current chapter's** panels, so it restarts per chapter.

- [ ] **Step 1: Write the failing component test**

```typescript
import { mount } from '@vue/test-utils'
import { createTestingPinia } from '@pinia/testing'
import { describe, expect, it, vi } from 'vitest'
import StorylineDetailView from '@/views/StorylineDetailView.vue'

describe('StorylineDetailView — chapter rail', () => {
  it('renders one rail item per chapter and selects the latest by default', async () => {
    const wrapper = mount(StorylineDetailView, {
      props: { id: '9' },
      global: {
        plugins: [createTestingPinia({
          createSpy: vi.fn,
          initialState: {
            storylines: {
              currentStoryline: {
                id: 9, name: 'X', anchors: [], chapters: [
                  { id: 1, storyline_id: 9, seq: 1, title: 'Ch1', state: 'closed', start_date: null, end_date: null, last_generated_at: null, citation_count: 0 },
                  { id: 2, storyline_id: 9, seq: 2, title: 'Ch2', state: 'open', start_date: null, end_date: null, last_generated_at: null, citation_count: 0 },
                ], panels: {},
              },
              currentChapter: { id: 2, seq: 2, title: 'Ch2', state: 'open', panels: {} },
            },
          },
        })],
        stubs: { RouterLink: true },
      },
    })
    const rail = wrapper.findAll('[data-test="chapter-rail-item"]')
    expect(rail).toHaveLength(2)
  })
})
```

- [ ] **Step 2: Run to verify it fails**

Run: `npm run test:unit -- StorylineDetailView`
Expected: FAIL (no `chapter-rail-item` elements).

- [ ] **Step 3: Implement the rail**

In `StorylineDetailView.vue`:

- Replace the `curationPanel` / `narrativePanel` computeds to read from `store.currentChapter` instead of `store.currentStoryline`:

```typescript
const curationPanel = computed(() => store.currentChapter?.panels.curation ?? null)
const narrativePanel = computed(() => store.currentChapter?.panels.narrative ?? null)
const chapters = computed(() => store.currentStoryline?.chapters ?? [])
const selectedChapterId = ref<number | null>(null)
```

- Build the citation registry from the current chapter's panels (the helper already takes a `panels` map):

```typescript
const citationRegistry = computed(() =>
  buildCitationRegistry(store.currentChapter?.panels ?? {}),
)
```

- On mount / when the storyline loads, default-select the latest chapter (highest `seq`) and lazy-load it:

```typescript
async function selectChapter(chapterId: number): Promise<void> {
  selectedChapterId.value = chapterId
  await store.loadChapter(Number(props.id), chapterId)
  router.replace({ query: { ...router.currentRoute.value.query, chapter: String(chapterId) } })
}

onMounted(async () => {
  await store.loadStoryline(Number(props.id))
  const qp = Number(router.currentRoute.value.query.chapter)
  const initial = Number.isFinite(qp) && qp
    ? qp
    : chapters.value[chapters.value.length - 1]?.id
  if (initial) await selectChapter(initial)
})
```

> Confirm whether the existing view already calls `store.loadStoryline` in `onMounted`; if so, fold the chapter-selection logic into that existing hook rather than adding a second `onMounted`.

- Add the rail markup to the template, to the left of the existing two-panel reader (the chosen Layout A). Each item carries `data-test="chapter-rail-item"`, shows title + date range + an open/closed badge, and highlights the selected one:

```vue
<aside class="w-56 shrink-0 border-r border-gray-200 dark:border-gray-700/60 pr-3">
  <ul class="space-y-1">
    <li v-for="c in chapters" :key="c.id">
      <button
        data-test="chapter-rail-item"
        class="w-full text-left px-2 py-1.5 rounded-md text-sm"
        :class="c.id === selectedChapterId
          ? 'bg-violet-500/10 text-violet-700 dark:text-violet-300'
          : 'hover:bg-gray-100 dark:hover:bg-gray-700/40'"
        @click="selectChapter(c.id)"
      >
        <span class="font-medium">{{ c.title || `Chapter ${c.seq}` }}</span>
        <span class="block text-xs text-gray-500">
          {{ c.start_date ?? '…' }} – {{ c.end_date ?? 'now' }}
          <span v-if="c.state === 'open'" class="ml-1 text-emerald-600">• open</span>
        </span>
      </button>
    </li>
  </ul>
</aside>
```

Wrap the rail + existing reader in a `flex` container. Keep the existing Regenerate / anchor-edit / title controls; point the per-chapter Regenerate button (if added) at `store.regenerateChapter(Number(props.id), selectedChapterId.value!)`.

- [ ] **Step 4: Run to verify it passes**

Run: `npm run test:unit -- StorylineDetailView`
Expected: PASS.

- [ ] **Step 5: Full webapp checks (match the pre-push hook)**

Run: `npm run format:check && npm run lint && npm run test:coverage && npm run build`
Expected: all pass; coverage stays ≥85% on statements/branches/functions/lines. Add store/view tests if coverage dipped.

- [ ] **Step 6: Commit**

```bash
git add src/views/StorylineDetailView.vue src/views/__tests__/StorylineDetailView.spec.ts
git commit -m "feat(storylines): chapter rail + per-chapter citation numbering"
```

---

## Task 10: Docs + journal entries (both repos)

**Files:**
- Modify: `server/docs/` (storylines doc) + `webapp/docs/` (storylines/API doc)
- Create: `server/journal/260613-storyline-chapters-phase1.md`, `webapp/journal/260613-storyline-chapters-phase1.md`

- [ ] **Step 1: Update server docs**

Update the storylines documentation under `server/docs/` to describe the chapters model (table, open/closed, per-chapter generation) and the new/changed endpoints (`chapters[]` on detail, `GET/POST/PATCH .../chapters/{cid}`). Note the back-compat storyline-level `panels` shim and that it will be removed once the webapp fully uses chapters.

- [ ] **Step 2: Update webapp docs**

Update `webapp/docs/` (the storylines/API reference) for the chapter rail, the chapter endpoints the client calls, and per-chapter citation numbering.

- [ ] **Step 3: Write dated journal entries**

In each repo's `journal/`, add `260613-storyline-chapters-phase1.md` capturing: the data-model decision (chapters between storyline and panels), the migration rebuild approach + any prod anomalies found in Task 1 Step 1, the append-vs-replace Phase 1 choice from Task 4, and the deferred Phase 2 scope.

- [ ] **Step 4: Commit (each repo)**

```bash
# in server/
git add docs/ journal/260613-storyline-chapters-phase1.md
git commit -m "docs(storylines): chapters model + API contract + Phase 1 journal"
# in webapp/
git add docs/ journal/260613-storyline-chapters-phase1.md
git commit -m "docs(storylines): chapter rail + API usage + Phase 1 journal"
```

- [ ] **Step 5: Push each repo and watch CI**

For each repo: push the feature branch, open a PR, and `gh run watch` until green. Server PR first (the webapp depends on its API). Fix → re-run locally → push → watch until green; max 3 fix attempts before flagging.

---

## Self-Review Notes (author)

- **Spec coverage:** schema+migration → Task 1; per-chapter generation → Task 4; REST/MCP read paths → Task 5; chapter rail + per-chapter citations → Task 9. Anchors stay storyline-level (no task changes them — correct). The "open chapter grows via append + suggest-a-cut," suggestion engine, and timeline editor are explicitly Phase 2 (out of scope).
- **Compatibility:** the storyline-level `panels` shim (Task 5 Step 3) keeps the deployed webapp working between the server deploy and the webapp deploy.
- **Append caveat:** Task 4 Step 3 recommends `mode="replace"` for the open chapter in Phase 1 and defers true incremental append to Phase 2 — recorded so the implementer doesn't over-build.
- **Migration re-runnability:** the partial-failure path is the riskiest element; Task 1 Step 3 flags that the bare `user_version` rewind re-run needs either the in-SQL `pragma_table_info` guard or a forward-only test. The implementer must land one of those before merging.
