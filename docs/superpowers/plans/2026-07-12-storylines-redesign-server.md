# Storylines Redesign (Server) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the date-window chapter engine with draft/published chapters whose boundaries are decided by LLM judgment over explicit entry-id membership, per `docs/superpowers/specs/2026-07-12-storylines-redesign-design.md`.

**Architecture:** Chapters own explicit entry sets (`storyline_chapter_entries`), never date windows. One draft chapter per storyline is re-narrated whole as entries arrive; a judge provider decides continue-vs-break; publish is one atomic transaction with no LLM calls inside it. Panels are folded into the chapter row (narrative only). All chapter writes happen on job Pool B.

**Tech Stack:** Python 3.13, uv, pytest, SQLite (migrations via `PRAGMA user_version`), Anthropic SDK (Citations API for narration, forced tool-use for the judge), FastMCP/Starlette REST.

## Global Constraints

- Type annotations everywhere; `uv run pytest` and `uv run ruff check src/ tests/` must pass at every commit.
- TDD: every task writes its failing test first.
- Migrations: re-runnable after partial failure (`DROP IF EXISTS` / `IF NOT EXISTS` guards); tests exercise the data-copy path on prod-shaped state (house rule).
- No LLM call inside any DB transaction. Provider failures leave state untouched.
- Chapter states are exactly `'draft'` and `'published'`. At most one draft per storyline (partial unique index) and it has the highest `seq`.
- Published chapters are immutable except: append addendum, `read_at`, rename, unpublish-newest.
- Config additions: `storyline_judge_model` (default `claude-haiku-4-5`), `storyline_min_publish_entries` (default `3`). Config removals in Task 12.
- The migration renames old panel data to `storyline_panels_legacy`; the DROP of legacy tables is a **separate release** (Task 13 note) — never in this deploy.
- Commit after every task; push and watch CI per house rules.

---

### Task 1: Migration 0036 — draft/published schema

**Files:**
- Create: `src/journal/db/migrations/0036_storylines_draft_published.sql`
- Test: `tests/test_db/test_migration_0036.py`

**Interfaces:**
- Produces tables consumed by Task 3: `storylines` (reshaped), `storyline_chapters` (reshaped, content folded in), `storyline_chapter_entries`, `storyline_pending_entries`, `storyline_panels_legacy`.

- [ ] **Step 1: Write the failing test**

Prod-shaped state: a storyline with anchors, one closed + one open chapter, panels for both, then migrate.

```python
"""Migration 0036: draft/published reshape of the storylines vertical."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from journal.db.migrations import run_migrations


def _migrate_to(conn: sqlite3.Connection, version: int) -> None:
    run_migrations(conn, target_version=version)


@pytest.fixture
def pre_0036(tmp_path: Path) -> sqlite3.Connection:
    """A DB at version 0035 with prod-shaped storyline data."""
    conn = sqlite3.connect(tmp_path / "m.db")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    _migrate_to(conn, 35)
    conn.execute(
        "INSERT INTO users (email, password_hash, display_name)"
        " VALUES ('u@x.dev', 'h', 'U')"
    )
    conn.execute(
        "INSERT INTO entries (entry_date, source_type, raw_text, final_text,"
        " word_count, user_id) VALUES ('2026-01-05', 'text', 'a', 'a', 1, 1),"
        " ('2026-02-10', 'text', 'b', 'b', 1, 1)"
    )
    conn.execute(
        "INSERT INTO storylines (user_id, name, description, status,"
        " last_generated_at) VALUES (1, 'Running', '', 'active',"
        " '2026-02-11T08:00:00Z')"
    )
    conn.execute("INSERT INTO storyline_entities (storyline_id, entity_id) VALUES (1, 1)")
    # entity row for the FK
    conn.execute(
        "INSERT INTO entities (entity_type, canonical_name) VALUES ('activity', 'Running')"
    ) if _has_no_entity(conn) else None
    conn.execute(
        "INSERT INTO storyline_chapters (storyline_id, seq, title, start_date,"
        " end_date, state, last_generated_at) VALUES"
        " (1, 1, 'Winter', '2026-01-01', '2026-01-31', 'closed', '2026-02-01T00:00:00Z'),"
        " (1, 2, '', '2026-02-01', NULL, 'open', '2026-02-11T08:00:00Z')"
    )
    conn.execute(
        "INSERT INTO storyline_panels (chapter_id, panel_kind, segments_json,"
        " source_entry_ids_json, citation_count, model_used, generated_at)"
        " VALUES (1, 'narrative', ?, '[1]', 1, 'm', '2026-02-01T00:00:00Z'),"
        "        (1, 'curation',  '[]', '[]', 0, 'm', '2026-02-01T00:00:00Z'),"
        "        (2, 'narrative', ?, '[2]', 1, 'm', '2026-02-11T08:00:00Z')",
        (
            json.dumps([{"kind": "text", "text": "Jan prose"},
                        {"kind": "citation", "entry_id": 1, "quote": "a"}]),
            json.dumps([{"kind": "text", "text": "Feb prose"}]),
        ),
    )
    conn.commit()
    return conn


def _has_no_entity(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 0


class TestMigration0036:
    def test_states_mapped_and_content_folded_in(self, pre_0036) -> None:
        run_migrations(pre_0036)
        rows = pre_0036.execute(
            "SELECT id, seq, state, title, segments_json, published_at, read_at"
            " FROM storyline_chapters ORDER BY seq"
        ).fetchall()
        assert [r["state"] for r in rows] == ["published", "draft"]
        assert rows[0]["published_at"] == "2026-02-01T00:00:00Z"  # from last_generated_at
        assert rows[0]["read_at"] is not None  # pre-read: no fake unread badges
        assert rows[1]["published_at"] is None
        assert json.loads(rows[0]["segments_json"])[0]["text"] == "Jan prose"

    def test_membership_backfilled_from_narrative_source_ids(self, pre_0036) -> None:
        run_migrations(pre_0036)
        rows = pre_0036.execute(
            "SELECT chapter_id, entry_id FROM storyline_chapter_entries ORDER BY chapter_id"
        ).fetchall()
        assert [(r["chapter_id"], r["entry_id"]) for r in rows] == [(1, 1), (2, 2)]

    def test_panels_preserved_as_legacy_and_dropped(self, pre_0036) -> None:
        run_migrations(pre_0036)
        names = {r[0] for r in pre_0036.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        assert "storyline_panels_legacy" in names
        assert "storyline_panels" not in names
        assert pre_0036.execute(
            "SELECT COUNT(*) FROM storyline_panels_legacy"
        ).fetchone()[0] == 3

    def test_one_draft_partial_index_enforced(self, pre_0036) -> None:
        run_migrations(pre_0036)
        with pytest.raises(sqlite3.IntegrityError):
            pre_0036.execute(
                "INSERT INTO storyline_chapters (storyline_id, seq, state)"
                " VALUES (1, 3, 'draft')"
            )

    def test_storyline_columns_dropped(self, pre_0036) -> None:
        run_migrations(pre_0036)
        cols = {r["name"] for r in pre_0036.execute(
            "PRAGMA table_info(storylines)"
        )}
        assert "start_date" not in cols
        assert "summary_embedding_json" not in cols
        assert "last_generated_at" not in cols
        assert "last_extension_check_at" in cols

    def test_storyline_without_open_chapter_gets_empty_draft(self, pre_0036) -> None:
        # Prod anomaly guard: a storyline whose only chapter is closed.
        pre_0036.execute(
            "INSERT INTO storylines (user_id, name, status) VALUES (1, 'Odd', 'active')"
        )
        pre_0036.execute(
            "INSERT INTO storyline_chapters (storyline_id, seq, title, start_date,"
            " end_date, state) VALUES (2, 1, 'Only', '2026-01-01', '2026-01-31', 'closed')"
        )
        pre_0036.commit()
        run_migrations(pre_0036)
        states = [r[0] for r in pre_0036.execute(
            "SELECT state FROM storyline_chapters WHERE storyline_id = 2 ORDER BY seq"
        )]
        assert states == ["published", "draft"]

    def test_rerunnable_after_partial_failure(self, pre_0036) -> None:
        run_migrations(pre_0036)
        # Simulate a re-run: reset user_version and run again — must not raise.
        pre_0036.execute("PRAGMA user_version = 35")
        run_migrations(pre_0036)
```

Note: check `run_migrations`'s actual signature in `src/journal/db/migrations.py` first — if it has no `target_version` parameter, follow the pattern used by `tests/test_migration_0030_chapters.py` (it solves the same "stop at version N" problem); adjust `_migrate_to` accordingly, do not add a parameter to production code for the test's sake unless that's what 0030's test does.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_db/test_migration_0036.py -v`
Expected: FAIL (`0036` migration file does not exist; chapters still have `state='open'`).

- [ ] **Step 3: Write the migration**

Mirror 0028's table-rebuild structure (`PRAGMA foreign_keys=OFF`, explicit transaction). Every statement guarded so a re-run completes cleanly.

```sql
-- 0036_storylines_draft_published.sql
--
-- Storylines redesign (spec: docs/superpowers/specs/2026-07-12-storylines-redesign-design.md).
-- Chapters become draft/published with explicit entry membership; the
-- narrative panel folds into the chapter row; curation panels are retired
-- (kept as storyline_panels_legacy until the post-bootstrap cleanup
-- migration drops them in a LATER release).
--
-- Forward-only but re-runnable: rebuilds are guarded by table-shape checks
-- via DROP IF EXISTS on the _new scratch tables and IF NOT EXISTS on
-- creates.

PRAGMA foreign_keys = OFF;

BEGIN;

-- 1. Rebuild `storylines` without start/end dates, summary embedding,
--    last_generated_at.
DROP TABLE IF EXISTS storylines_new;
CREATE TABLE storylines_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived')),
    last_extension_check_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
INSERT INTO storylines_new (id, user_id, name, description, status,
                            last_extension_check_at, created_at, updated_at)
    SELECT id, user_id, name, description, status,
           last_extension_check_at, created_at, updated_at
    FROM storylines;

-- 2. Rebuild `storyline_chapters` in the new shape, folding in the
--    narrative panel and mapping open→draft / closed→published.
DROP TABLE IF EXISTS storyline_chapters_new;
CREATE TABLE storyline_chapters_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    storyline_id INTEGER NOT NULL REFERENCES storylines(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'draft' CHECK (state IN ('draft', 'published')),
    segments_json TEXT NOT NULL DEFAULT '[]',
    source_entry_ids_json TEXT NOT NULL DEFAULT '[]',
    citation_count INTEGER NOT NULL DEFAULT 0,
    model_used TEXT NOT NULL DEFAULT '',
    generated_at TEXT,
    published_at TEXT,
    read_at TEXT,
    addenda_json TEXT NOT NULL DEFAULT '[]',
    draft_embedding_json TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (storyline_id, seq)
);
INSERT INTO storyline_chapters_new (id, storyline_id, seq, title, state,
                                    segments_json, source_entry_ids_json,
                                    citation_count, model_used, generated_at,
                                    published_at, read_at,
                                    created_at, updated_at)
    SELECT c.id, c.storyline_id, c.seq, c.title,
           CASE c.state WHEN 'open' THEN 'draft' ELSE 'published' END,
           COALESCE(p.segments_json, '[]'),
           COALESCE(p.source_entry_ids_json, '[]'),
           COALESCE(p.citation_count, 0),
           COALESCE(p.model_used, ''),
           c.last_generated_at,
           CASE c.state WHEN 'closed' THEN COALESCE(c.last_generated_at, c.updated_at) END,
           -- Pre-existing published chapters start read: the migration must
           -- not manufacture a wall of unread badges.
           CASE c.state WHEN 'closed' THEN strftime('%Y-%m-%dT%H:%M:%SZ', 'now') END,
           c.created_at, c.updated_at
    FROM storyline_chapters c
    LEFT JOIN storyline_panels p
        ON p.chapter_id = c.id AND p.panel_kind = 'narrative';

-- 3. Preserve raw panel data (both kinds) for the verification window.
--    CREATE TABLE AS strips constraints, so the legacy table carries no
--    FKs into tables we are about to drop.
CREATE TABLE IF NOT EXISTS storyline_panels_legacy AS
    SELECT * FROM storyline_panels;

-- 4. Swap the rebuilt tables in.
DROP TABLE storyline_panels;
DROP TABLE storyline_chapters;
ALTER TABLE storyline_chapters_new RENAME TO storyline_chapters;
DROP TABLE storylines;
ALTER TABLE storylines_new RENAME TO storylines;

-- 5. Membership + pending tables.
CREATE TABLE IF NOT EXISTS storyline_chapter_entries (
    chapter_id INTEGER NOT NULL REFERENCES storyline_chapters(id) ON DELETE CASCADE,
    entry_id INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    added_late INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chapter_id, entry_id)
);
CREATE INDEX IF NOT EXISTS idx_storyline_chapter_entries_entry
    ON storyline_chapter_entries(entry_id);

-- Matched-but-unassigned entries awaiting the next storyline_update run.
CREATE TABLE IF NOT EXISTS storyline_pending_entries (
    storyline_id INTEGER NOT NULL REFERENCES storylines(id) ON DELETE CASCADE,
    entry_id INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (storyline_id, entry_id)
);

-- 6. Backfill membership from the folded narrative source ids (best-effort
--    placeholder until the bootstrap sweep regenerates everything).
INSERT OR IGNORE INTO storyline_chapter_entries (chapter_id, entry_id)
    SELECT c.id, je.value
    FROM storyline_chapters c, json_each(c.source_entry_ids_json) je
    WHERE EXISTS (SELECT 1 FROM entries e WHERE e.id = je.value);

-- 7. Invariant repair: every storyline gets exactly one draft. Storylines
--    whose chapters were all closed get an empty draft appended.
INSERT INTO storyline_chapters (storyline_id, seq, state)
    SELECT s.id,
           COALESCE((SELECT MAX(seq) FROM storyline_chapters c
                     WHERE c.storyline_id = s.id), 0) + 1,
           'draft'
    FROM storylines s
    WHERE NOT EXISTS (SELECT 1 FROM storyline_chapters c
                      WHERE c.storyline_id = s.id AND c.state = 'draft');

-- 8. Indexes.
CREATE UNIQUE INDEX IF NOT EXISTS idx_storyline_chapters_one_draft
    ON storyline_chapters(storyline_id) WHERE state = 'draft';
CREATE INDEX IF NOT EXISTS idx_storylines_user ON storylines(user_id);

COMMIT;

PRAGMA foreign_keys = ON;
```

Before writing, read `0028_storyline_entities.sql` and copy its exact re-run guard idiom if it differs from the above (the codebase's convention wins).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_db/test_migration_0036.py tests/test_db/test_migrations.py -v`
Expected: PASS. (`test_migrations.py` asserts every file applies cleanly to a fresh DB.)

Some older tests (repository, service) will now FAIL because they use the old schema — that is expected and fixed by Tasks 2–3; do not "fix" them here.

- [ ] **Step 5: Commit**

```bash
git add src/journal/db/migrations/0036_storylines_draft_published.sql tests/test_db/test_migration_0036.py
git commit -m "feat(storylines): migration 0036 — draft/published chapters, entry membership"
```

---

### Task 2: Models reshape

**Files:**
- Modify: `src/journal/models.py:440-531`
- Test: `tests/test_storyline_models.py` (create)

**Interfaces:**
- Produces (consumed by every later task):

```python
StorylineChapterState = Literal["draft", "published"]

@dataclass
class Storyline:
    id: int
    user_id: int
    name: str
    description: str = ""
    status: str = "active"
    last_extension_check_at: str | None = None
    created_at: str = ""
    updated_at: str = ""

@dataclass
class StorylineChapter:
    id: int
    storyline_id: int
    seq: int
    title: str = ""
    state: str = "draft"
    segments: list[dict[str, Any]] = field(default_factory=list)
    source_entry_ids: list[int] = field(default_factory=list)
    citation_count: int = 0
    model_used: str = ""
    generated_at: str | None = None
    published_at: str | None = None
    read_at: str | None = None
    addenda: list[dict[str, Any]] = field(default_factory=list)
    draft_embedding: list[float] | None = None
    # Derived from membership by the repository (not columns):
    entry_count: int = 0
    first_entry_date: str | None = None
    last_entry_date: str | None = None
    created_at: str = ""
    updated_at: str = ""
```

- `StorylinePanel` and `StorylinePanelKind` are **deleted**. `DatedEntryExcerpt` is unchanged.
- Addendum dict shape (documented in the `StorylineChapter` docstring, stored in `addenda_json`): `{"added_at": str, "segments": list[segment], "entry_ids": list[int]}`.

- [ ] **Step 1: Write the failing test**

```python
"""Model shapes for the storylines redesign."""

from journal.models import Storyline, StorylineChapter


def test_chapter_defaults_are_draft_shaped() -> None:
    ch = StorylineChapter(id=1, storyline_id=1, seq=1)
    assert ch.state == "draft"
    assert ch.segments == []
    assert ch.addenda == []
    assert ch.published_at is None and ch.read_at is None


def test_storyline_has_no_window_fields() -> None:
    s = Storyline(id=1, user_id=1, name="Running")
    assert not hasattr(s, "start_date")
    assert not hasattr(s, "summary_embedding")


def test_panel_model_is_gone() -> None:
    import journal.models as m
    assert not hasattr(m, "StorylinePanel")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_storyline_models.py -v`
Expected: FAIL (`start_date` still present; `StorylinePanel` still defined).

- [ ] **Step 3: Apply the model changes**

Replace the storyline section of `models.py` with the dataclasses from the Interfaces block above (keep the module's existing docstring style; update the section comment to reference the 2026-07-12 spec). Delete `StorylinePanelKind` and `StorylinePanel`.

- [ ] **Step 4: Run the test**

Run: `uv run pytest tests/test_storyline_models.py -v`
Expected: PASS. Widespread import failures elsewhere are expected until Tasks 3–12 land.

- [ ] **Step 5: Commit**

```bash
git add src/journal/models.py tests/test_storyline_models.py
git commit -m "feat(storylines): reshape chapter/storyline models; drop panel model"
```

---

### Task 3: Repository rewrite

**Files:**
- Rewrite: `src/journal/db/storyline_repository.py`
- Test: rewrite `tests/test_storyline_repository.py`; delete `tests/test_db/test_storyline_repository_editing.py`

**Interfaces:**
- Consumes: Task 1 schema, Task 2 models.
- Produces `SQLiteStorylineRepository` with exactly this surface (later tasks call these names):

```python
class SQLiteStorylineRepository:
    def __init__(self, factory: ConnectionFactory) -> None: ...
    # storylines
    def create_storyline(self, user_id: int, entity_ids: list[int], name: str,
                         description: str = "") -> Storyline  # seeds draft chapter seq=1
    def get_storyline(self, storyline_id: int, user_id: int | None = None) -> Storyline | None
    def list_storylines(self, user_id: int, status: str | None = None,
                        limit: int = 50, offset: int = 0) -> list[Storyline]
    def count_storylines(self, user_id: int, status: str | None = None) -> int
    def update_storyline_status(self, storyline_id: int, status: str, user_id: int) -> Storyline | None
    def update_storyline_name(self, storyline_id: int, name: str, user_id: int) -> Storyline | None
    def delete_storyline(self, storyline_id: int, user_id: int) -> bool
    def record_extension_check(self, storyline_id: int) -> None
    def unread_counts(self, user_id: int) -> dict[int, int]
    def find_by_anchor_set(self, user_id: int, entity_ids: list[int], name: str) -> Storyline | None
    # anchors — unchanged from the old file
    def list_anchors(self, storyline_id: int) -> list[int]
    def set_anchors(self, storyline_id: int, entity_ids: list[int]) -> list[int]
    def list_storylines_with_anchor(self, user_id: int, entity_id: int,
                                    status: str | None = None) -> list[Storyline]
    # chapters
    def list_chapters(self, storyline_id: int) -> list[StorylineChapter]  # seq ASC, derived fields filled
    def get_chapter(self, chapter_id: int) -> StorylineChapter | None
    def get_draft(self, storyline_id: int) -> StorylineChapter | None
    def rename_chapter(self, chapter_id: int, title: str) -> StorylineChapter | None
    def set_read(self, chapter_id: int, read: bool) -> StorylineChapter | None  # published only
    # membership
    def assigned_entry_ids(self, storyline_id: int) -> set[int]
    def chapter_entry_ids(self, chapter_id: int) -> list[int]
    def add_entries_to_draft(self, chapter_id: int, entry_ids: list[int]) -> None  # draft only
    # pending (matched-but-unassigned)
    def add_pending_entry(self, storyline_id: int, entry_id: int) -> None
    def list_pending_entries(self, storyline_id: int) -> list[int]
    def clear_pending_entries(self, storyline_id: int, entry_ids: list[int]) -> None
    # narrative writes
    def set_draft_narrative(self, chapter_id: int, *, segments: list[dict[str, Any]],
                            source_entry_ids: list[int], citation_count: int,
                            model_used: str, embedding: list[float] | None) -> None  # draft only
    def append_addendum(self, chapter_id: int, *, segments: list[dict[str, Any]],
                        entry_ids: list[int]) -> None  # published only; clears read_at
    # lifecycle transactions
    def publish_draft(self, storyline_id: int, *, title: str,
                      segments: list[dict[str, Any]], source_entry_ids: list[int],
                      citation_count: int, model_used: str,
                      new_draft_entry_ids: list[int]) -> tuple[StorylineChapter, StorylineChapter]
    def unpublish_newest(self, storyline_id: int) -> StorylineChapter  # returns the enlarged draft
    def replace_all_chapters(self, storyline_id: int,
                             chapters: list[BootstrapChapterSpec]) -> list[StorylineChapter]

@dataclass
class BootstrapChapterSpec:
    """One pre-narrated chapter for replace_all_chapters (bootstrap)."""
    title: str
    state: str  # 'draft' | 'published'; exactly the last spec may be 'draft'
    segments: list[dict[str, Any]]
    source_entry_ids: list[int]
    citation_count: int
    model_used: str
    entry_ids: list[int]
    mark_read: bool = False  # bootstrap sweep sets True for pre-existing content
```

Deleted from the old file: `upsert_panel`, `get_panel`, `list_panels`, `create_chapter`, `get_open_chapter`, `merge_chapters`, `add_chapter`, `update_chapter_window`, `delete_chapter`, `split_chapter`, `rebuild_chapters`, `ChapterSpec`, `_shift_seqs`, `_day_before`/`_day_after`, `record_generation_complete`, `update_summary_embedding`, `update_chapter_summary_embedding`, `set_chapter_word_count`, `record_chapter_generation_complete`.

- [ ] **Step 1: Write the failing tests**

Rewrite `tests/test_storyline_repository.py`. Keep the existing `seed_user` / `seed_entity` / entry-seeding fixtures verbatim (they still match the schema). Replace the panel/editing test classes with:

```python
class TestChapterLifecycle:
    def test_create_storyline_seeds_one_draft(self, repo, seed_user, seed_entity):
        s = repo.create_storyline(seed_user, [seed_entity], "Running")
        chapters = repo.list_chapters(s.id)
        assert len(chapters) == 1
        assert chapters[0].state == "draft" and chapters[0].seq == 1
        assert repo.get_draft(s.id).id == chapters[0].id

    def test_publish_draft_is_atomic_and_seeds_new_draft(self, repo, storyline, entry_ids):
        draft = repo.get_draft(storyline.id)
        repo.add_entries_to_draft(draft.id, entry_ids[:2])
        published, new_draft = repo.publish_draft(
            storyline.id, title="The Start",
            segments=[{"kind": "text", "text": "prose"}],
            source_entry_ids=entry_ids[:2], citation_count=2, model_used="m",
            new_draft_entry_ids=[entry_ids[2]],
        )
        assert published.state == "published" and published.title == "The Start"
        assert published.published_at is not None and published.read_at is None
        assert new_draft.state == "draft" and new_draft.seq == published.seq + 1
        assert repo.chapter_entry_ids(new_draft.id) == [entry_ids[2]]

    def test_publish_without_draft_raises(self, repo, seed_user):
        with pytest.raises(ValueError, match="no draft"):
            repo.publish_draft(9999, title="x", segments=[], source_entry_ids=[],
                               citation_count=0, model_used="m", new_draft_entry_ids=[])

    def test_unpublish_newest_folds_members_into_draft(self, repo, storyline, entry_ids):
        draft = repo.get_draft(storyline.id)
        repo.add_entries_to_draft(draft.id, entry_ids[:2])
        published, new_draft = repo.publish_draft(
            storyline.id, title="t", segments=[], source_entry_ids=[],
            citation_count=0, model_used="m", new_draft_entry_ids=[entry_ids[2]],
        )
        merged = repo.unpublish_newest(storyline.id)
        assert merged.state == "draft"
        assert set(repo.chapter_entry_ids(merged.id)) == set(entry_ids)
        assert len(repo.list_chapters(storyline.id)) == 1

    def test_unpublish_with_no_published_raises(self, repo, storyline):
        with pytest.raises(ValueError, match="no published chapter"):
            repo.unpublish_newest(storyline.id)


class TestImmutability:
    def test_set_draft_narrative_refuses_published(self, repo, storyline, entry_ids):
        draft = repo.get_draft(storyline.id)
        repo.add_entries_to_draft(draft.id, entry_ids[:2])
        published, _ = repo.publish_draft(
            storyline.id, title="t", segments=[], source_entry_ids=[],
            citation_count=0, model_used="m", new_draft_entry_ids=[],
        )
        with pytest.raises(ValueError, match="published"):
            repo.set_draft_narrative(published.id, segments=[], source_entry_ids=[],
                                     citation_count=0, model_used="m", embedding=None)

    def test_add_entries_refuses_published(self, repo, storyline, entry_ids): ...
        # same publish setup; expect ValueError on add_entries_to_draft(published.id, ...)

    def test_addendum_refuses_draft(self, repo, storyline):
        draft = repo.get_draft(storyline.id)
        with pytest.raises(ValueError, match="draft"):
            repo.append_addendum(draft.id, segments=[], entry_ids=[])

    def test_addendum_clears_read_and_marks_late(self, repo, storyline, entry_ids):
        # publish, mark read, append addendum with entry_ids[2]
        # assert read_at is None afterwards, addenda has one block,
        # and membership row for entry_ids[2] has added_late=1

    def test_set_read_refuses_draft(self, repo, storyline):
        draft = repo.get_draft(storyline.id)
        with pytest.raises(ValueError, match="draft"):
            repo.set_read(draft.id, True)


class TestDerivedFieldsAndUnread:
    def test_list_chapters_derives_dates_and_counts(self, repo, storyline, entry_ids):
        draft = repo.get_draft(storyline.id)
        repo.add_entries_to_draft(draft.id, entry_ids)  # dates 2026-02-20 .. 2026-04-25
        ch = repo.list_chapters(storyline.id)[0]
        assert (ch.entry_count, ch.first_entry_date, ch.last_entry_date) == (
            3, "2026-02-20", "2026-04-25")

    def test_unread_counts(self, repo, seed_user, storyline, entry_ids):
        # publish twice; mark one read; unread_counts == {storyline.id: 1}


class TestPendingEntries:
    def test_pending_roundtrip(self, repo, storyline, entry_ids):
        repo.add_pending_entry(storyline.id, entry_ids[0])
        repo.add_pending_entry(storyline.id, entry_ids[0])  # idempotent
        assert repo.list_pending_entries(storyline.id) == [entry_ids[0]]
        repo.clear_pending_entries(storyline.id, [entry_ids[0]])
        assert repo.list_pending_entries(storyline.id) == []


class TestBootstrapReplace:
    def test_replace_all_chapters(self, repo, storyline, entry_ids):
        specs = [
            BootstrapChapterSpec(title="One", state="published", segments=[],
                                 source_entry_ids=[], citation_count=0,
                                 model_used="m", entry_ids=entry_ids[:2],
                                 mark_read=True),
            BootstrapChapterSpec(title="", state="draft", segments=[],
                                 source_entry_ids=[], citation_count=0,
                                 model_used="m", entry_ids=[entry_ids[2]]),
        ]
        chapters = repo.replace_all_chapters(storyline.id, specs)
        assert [c.state for c in chapters] == ["published", "draft"]
        assert chapters[0].read_at is not None
        assert repo.chapter_entry_ids(chapters[1].id) == [entry_ids[2]]

    def test_replace_rejects_non_final_draft(self, repo, storyline):
        specs = [BootstrapChapterSpec(title="", state="draft", segments=[],
                                      source_entry_ids=[], citation_count=0,
                                      model_used="m", entry_ids=[]),
                 BootstrapChapterSpec(title="x", state="published", segments=[],
                                      source_entry_ids=[], citation_count=0,
                                      model_used="m", entry_ids=[])]
        with pytest.raises(ValueError, match="draft must be the final"):
            repo.replace_all_chapters(storyline.id, specs)
```

Flesh out the two elided bodies (`test_add_entries_refuses_published`, `test_addendum_clears_read_and_marks_late`, `test_unread_counts`) with the same fixtures — each is 6–10 lines following the neighboring pattern. `repo` fixture: `SQLiteStorylineRepository(factory)`; `storyline` fixture: `repo.create_storyline(seed_user, [seed_entity], "Running")`.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_storyline_repository.py -v`
Expected: FAIL (methods don't exist).

- [ ] **Step 3: Rewrite the repository**

Key implementations (the rest are mechanical CRUD following the old file's idiom — per-thread `self._factory.get()`, explicit commit, rollback on exception):

```python
def _require_state(self, chapter_id: int, expected: str) -> StorylineChapter:
    ch = self.get_chapter(chapter_id)
    if ch is None:
        raise ValueError(f"Chapter {chapter_id} not found")
    if ch.state != expected:
        raise ValueError(
            f"Chapter {chapter_id} is {ch.state}; operation requires {expected}"
        )
    return ch

def publish_draft(self, storyline_id, *, title, segments, source_entry_ids,
                  citation_count, model_used, new_draft_entry_ids):
    draft = self.get_draft(storyline_id)
    if draft is None:
        raise ValueError(f"Storyline {storyline_id} has no draft chapter")
    conn = self._conn()
    try:
        conn.execute(
            "UPDATE storyline_chapters SET state='published', title=?,"
            " segments_json=?, source_entry_ids_json=?, citation_count=?,"
            " model_used=?, generated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now'),"
            " published_at=strftime('%Y-%m-%dT%H:%M:%SZ','now'),"
            " read_at=NULL, draft_embedding_json=NULL,"
            " updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')"
            " WHERE id=?",
            (title.strip(), json.dumps(segments), json.dumps(source_entry_ids),
             int(citation_count), model_used, draft.id),
        )
        cursor = conn.execute(
            "INSERT INTO storyline_chapters (storyline_id, seq, state)"
            " VALUES (?, ?, 'draft')",
            (storyline_id, draft.seq + 1),
        )
        new_id = cursor.lastrowid
        conn.executemany(
            "INSERT OR IGNORE INTO storyline_chapter_entries (chapter_id, entry_id)"
            " VALUES (?, ?)",
            [(new_id, eid) for eid in new_draft_entry_ids],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    published = self.get_chapter(draft.id)
    new_draft = self.get_chapter(new_id)
    assert published is not None and new_draft is not None
    return published, new_draft

def unpublish_newest(self, storyline_id):
    draft = self.get_draft(storyline_id)
    if draft is None:
        raise ValueError(f"Storyline {storyline_id} has no draft chapter")
    chapters = self.list_chapters(storyline_id)
    published = [c for c in chapters if c.state == "published"]
    if not published:
        raise ValueError(f"Storyline {storyline_id} has no published chapter")
    newest = published[-1]
    conn = self._conn()
    try:
        conn.execute(
            "UPDATE OR IGNORE storyline_chapter_entries SET chapter_id=?"
            " WHERE chapter_id=?", (draft.id, newest.id),
        )
        conn.execute(  # duplicates skipped by IGNORE above are deleted with the row
            "DELETE FROM storyline_chapters WHERE id=?", (newest.id,),
        )
        conn.execute(
            "UPDATE storyline_chapters SET seq=?, segments_json='[]',"
            " source_entry_ids_json='[]', citation_count=0,"
            " updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?",
            (newest.seq, draft.id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    merged = self.get_chapter(draft.id)
    assert merged is not None
    return merged
```

The draft's stale narrative is cleared here (the caller queues a re-narration job). `list_chapters` fills the derived fields with one aggregate query:

```python
def list_chapters(self, storyline_id):
    rows = self._conn().execute(
        "SELECT c.*, COUNT(ce.entry_id) AS entry_count,"
        "       MIN(e.entry_date) AS first_entry_date,"
        "       MAX(e.entry_date) AS last_entry_date"
        " FROM storyline_chapters c"
        " LEFT JOIN storyline_chapter_entries ce ON ce.chapter_id = c.id"
        " LEFT JOIN entries e ON e.id = ce.entry_id"
        " WHERE c.storyline_id = ? GROUP BY c.id ORDER BY c.seq ASC",
        (storyline_id,),
    ).fetchall()
    return [_row_to_chapter(r) for r in rows]

def unread_counts(self, user_id):
    rows = self._conn().execute(
        "SELECT c.storyline_id, COUNT(*) AS cnt"
        " FROM storyline_chapters c"
        " JOIN storylines s ON s.id = c.storyline_id"
        " WHERE s.user_id = ? AND c.state = 'published' AND c.read_at IS NULL"
        " GROUP BY c.storyline_id",
        (user_id,),
    ).fetchall()
    return {int(r["storyline_id"]): int(r["cnt"]) for r in rows}
```

`append_addendum` appends `{"added_at": now, "segments": segments, "entry_ids": entry_ids}` to the JSON list, sets `read_at=NULL`, and inserts membership rows with `added_late=1`, all in one transaction. `replace_all_chapters` validates exactly-one-final-draft, then in one transaction: `DELETE FROM storyline_chapters WHERE storyline_id=?` (membership cascades), inserts each spec at seq 1..N (published first, draft last — the partial index sees at most one draft), inserts membership rows, sets `read_at`/`published_at` per `mark_read`/state. `create_storyline` keeps the old anchor logic and adds the seq-1 draft INSERT in the same transaction. `get_draft` selects `WHERE storyline_id=? AND state='draft'`.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_storyline_repository.py -v`
Expected: PASS. Also delete `tests/test_db/test_storyline_repository_editing.py` now.

- [ ] **Step 5: Commit**

```bash
git add src/journal/db/storyline_repository.py tests/test_storyline_repository.py
git rm tests/test_db/test_storyline_repository_editing.py
git commit -m "feat(storylines): repository rewrite — membership, publish/unpublish, immutability"
```

---

### Task 4: Judge provider

**Files:**
- Create: `src/journal/providers/storyline_judge.py`
- Test: `tests/test_providers/test_storyline_judge.py`
- Delete: `src/journal/providers/storyline_extension_decider.py` stays for now (the classifier still uses it — Task 7 keeps it; nothing to delete here).

**Interfaces:**
- Produces:

```python
@dataclass
class EntryAssignment:
    entry_id: int
    target: str            # 'draft' | 'new_chapter' | 'published_chapter'
    chapter_id: int | None = None   # set iff target == 'published_chapter'

@dataclass
class ExtensionJudgment:
    assignments: list[EntryAssignment]
    draft_arc_complete: bool
    reasoning: str
    model_used: str = ""
    failed: bool = False   # True on API failure / malformed response

@dataclass
class PartitionChapter:
    entry_ids: list[int]
    working_title: str

@dataclass
class PartitionResult:
    chapters: list[PartitionChapter]
    model_used: str = ""
    failed: bool = False

@dataclass
class EntryForJudge:
    entry_id: int
    entry_date: str
    text: str

@runtime_checkable
class StorylineJudgeProtocol(Protocol):
    def judge_extension(self, *, storyline_name: str, storyline_description: str,
                        draft_narrative: str, draft_entries: list[EntryForJudge],
                        new_entries: list[EntryForJudge],
                        published_chapters: list[tuple[int, str, str, str]],
                        ) -> ExtensionJudgment: ...
        # published_chapters: (chapter_id, title, first_entry_date, last_entry_date)
    def partition(self, *, storyline_name: str, storyline_description: str,
                  entries: list[EntryForJudge]) -> PartitionResult: ...

class AnthropicStorylineJudge:
    def __init__(self, api_key: str, model: str = "claude-haiku-4-5",
                 max_tokens: int = 2048, client: Any | None = None) -> None: ...
    model: str  # property
```

- [ ] **Step 1: Write the failing tests**

Follow the canned-response pattern of `tests/test_providers/test_mood_scorer.py` (fake client whose `messages.create` returns dict-shaped tool_use blocks):

```python
class _FakeClient:
    def __init__(self, response): self._response = response; self.calls = []
    @property
    def messages(self): return self
    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._response


def _tool_response(name, tool_input):
    return {"content": [{"type": "tool_use", "name": name, "input": tool_input}],
            "usage": {"input_tokens": 1, "output_tokens": 1}}


def test_judge_extension_parses_assignments():
    resp = _tool_response("record_judgment", {
        "assignments": [
            {"entry_id": 7, "target": "draft"},
            {"entry_id": 8, "target": "new_chapter"},
            {"entry_id": 9, "target": "published_chapter", "chapter_id": 3},
        ],
        "draft_arc_complete": True,
        "reasoning": "The race happened; a new training block starts.",
    })
    judge = AnthropicStorylineJudge(api_key="k", client=_FakeClient(resp))
    result = judge.judge_extension(
        storyline_name="Running", storyline_description="",
        draft_narrative="He trained.", draft_entries=[],
        new_entries=[EntryForJudge(7, "2026-07-01", "ran"),
                     EntryForJudge(8, "2026-07-02", "signed up"),
                     EntryForJudge(9, "2026-03-01", "old page")],
        published_chapters=[(3, "Spring", "2026-03-01", "2026-03-31")],
    )
    assert not result.failed and result.draft_arc_complete
    assert [(a.entry_id, a.target, a.chapter_id) for a in result.assignments] == [
        (7, "draft", None), (8, "new_chapter", None), (9, "published_chapter", 3)]


def test_judge_extension_unknown_entry_ids_are_dropped():
    # response mentions entry_id 999 not in new_entries → dropped, not failed
    ...


def test_judge_extension_api_failure_returns_failed():
    class _Boom:
        @property
        def messages(self): return self
        def create(self, **kwargs): raise RuntimeError("api down")
    judge = AnthropicStorylineJudge(api_key="k", client=_Boom())
    result = judge.judge_extension(storyline_name="R", storyline_description="",
                                   draft_narrative="", draft_entries=[],
                                   new_entries=[], published_chapters=[])
    assert result.failed


def test_partition_parses_chapters_and_validates_coverage():
    resp = _tool_response("record_partition", {
        "chapters": [
            {"entry_ids": [1, 2], "working_title": "The Move"},
            {"entry_ids": [3], "working_title": "Settling In"},
        ]})
    judge = AnthropicStorylineJudge(api_key="k", client=_FakeClient(resp))
    result = judge.partition(storyline_name="House", storyline_description="",
                             entries=[EntryForJudge(i, f"2026-0{i}-01", "t")
                                      for i in (1, 2, 3)])
    assert not result.failed
    assert [c.entry_ids for c in result.chapters] == [[1, 2], [3]]


def test_partition_missing_entries_folded_into_last_chapter():
    # model omits entry 3 → parser appends it to the final chapter
    # (every candidate entry must land somewhere; losing entries silently
    #  is the old system's bug class)
    ...
```

Fill the two elided tests following the shown pattern.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_providers/test_storyline_judge.py -v`
Expected: FAIL (module doesn't exist).

- [ ] **Step 3: Implement the provider**

One class, two forced tool calls. System prompt (module constant):

```python
SYSTEM_PROMPT = """\
You are the chapter editor for a personal journal's "storylines" — evolving
narrative threads about subjects in the author's life. Chapters are arcs with
a beginning and an end, like chapters of a memoir: a phase, a project, a
build-up to an event, an aftermath. You will be asked either to judge whether
new journal entries continue the current draft chapter or begin a new arc, or
to partition a full history of entries into chapters.

Judgment principles — all load-bearing:

* Boundaries are SEMANTIC: a new arc starts when the situation, goal, or
  emotional register genuinely shifts (an event happens, a decision lands, a
  phase ends). Never split on word count, entry count, or elapsed time alone.
* Prefer continuing the draft when in doubt. Chapters should feel complete;
  a premature break produces fragments.
* An entry dated long before the draft's period that clearly belongs to an
  earlier, already-published arc should be assigned to that published
  chapter (it becomes an addendum there).
* Base every decision only on the provided material. Keep reasoning to one
  or two sentences; it is shown to the user.
"""
```

`judge_extension` builds a user message containing the storyline name/description, the draft narrative, a compact listing of draft entries (`- [id 12] 2026-06-01: first 300 chars…`), the published-chapter index (`- chapter 3 "Spring" (2026-03-01 → 2026-03-31)`), and the new entries in full, then calls `messages.create` with `tools=[_JUDGMENT_TOOL]`, `tool_choice={"type": "tool", "name": "record_judgment"}`. Tool schema:

```python
_JUDGMENT_TOOL = {
    "name": "record_judgment",
    "description": "Record the chapter assignment for each new entry.",
    "input_schema": {
        "type": "object",
        "properties": {
            "assignments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "entry_id": {"type": "integer"},
                        "target": {"type": "string",
                                   "enum": ["draft", "new_chapter", "published_chapter"]},
                        "chapter_id": {"type": "integer",
                                       "description": "Required when target is published_chapter."},
                    },
                    "required": ["entry_id", "target"],
                },
            },
            "draft_arc_complete": {
                "type": "boolean",
                "description": "True if the draft chapter's arc has concluded and it should be published.",
            },
            "reasoning": {"type": "string"},
        },
        "required": ["assignments", "draft_arc_complete", "reasoning"],
    },
}
```

Parsing (reuse the `_attr_or_key` bridging idiom from `storyline_narrator.py`): find the `tool_use` block, validate each assignment (`entry_id` must be in `new_entries`; `published_chapter` requires a `chapter_id` present in `published_chapters`, else demote that assignment to `draft`), drop unknown ids with a `log.warning`. Any exception or missing tool block → `ExtensionJudgment(assignments=[], draft_arc_complete=False, reasoning="judge unavailable", failed=True)`. Every new entry absent from the response is appended as `target="draft"` (nothing is ever lost).

`partition` mirrors this with `_PARTITION_TOOL` (`chapters: [{entry_ids: [int], working_title: str}]`); the parser drops unknown ids, dedupes across chapters (first chapter wins), and appends any un-mentioned candidate ids to the final chapter. Empty/failed → `PartitionResult(chapters=[], failed=True)`. Both methods call `usage.record_anthropic(self._model, response)` (same as the old decider) and cache-breakpoint the system prompt.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_providers/test_storyline_judge.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/journal/providers/storyline_judge.py tests/test_providers/test_storyline_judge.py
git commit -m "feat(storylines): judge provider — extension judgment + history partition"
```

---

### Task 5: Narrator simplification

**Files:**
- Modify: `src/journal/providers/storyline_narrator.py`
- Test: modify `tests/test_storyline_sectioning.py` → delete; extend the narrator tests inside `tests/test_storyline_generation.py` (move narrator-specific ones into `tests/test_providers/test_storyline_narrator.py`, create if the move makes sense — keep it simple: put the new tests where the old flat-narrative tests live).

**Interfaces:**
- Consumes: nothing new.
- Produces:

```python
NarratorMode = Literal["draft", "closure", "addendum"]

@dataclass
class NarrativeResult:
    segments: list[dict[str, Any]] = field(default_factory=list)
    source_entry_ids: list[int] = field(default_factory=list)
    citation_count: int = 0
    title: str | None = None          # set in closure mode
    model_used: str = ""
    raw_usage: dict[str, Any] | None = None

class StorylineNarratorProtocol(Protocol):
    def generate_narrative(self, excerpts: list[DatedEntryExcerpt],
                           storyline_name: str, storyline_description: str = "",
                           *, mode: NarratorMode = "draft",
                           prior_narrative: str | None = None) -> NarrativeResult: ...
```

Deleted: `SECTIONING_SYSTEM_PROMPT`, `generate_sectioned_narrative`, `NarrativeSection`, `SectionedNarrativeResult`, `_parse_sectioned_response`, `_split_heading`, `_finalize_section`, `_HEADING_RE`, `WORD_BAND_*` constants, the old `prior_narrative` continuation framing.

- [ ] **Step 1: Write the failing tests**

```python
def test_closure_mode_extracts_title_line():
    # canned response whose first text block starts "# The Comeback Week\n..."
    resp = {"content": [{"type": "text",
                         "text": "# The Comeback Week\nHe returned to the track."}],
            "usage": {"input_tokens": 1, "output_tokens": 1}}
    narrator = AnthropicStorylineNarrator(api_key="k", client=_FakeClient(resp))
    result = narrator.generate_narrative(
        [_excerpt(1, "2026-07-01")], "Running", mode="closure")
    assert result.title == "The Comeback Week"
    assert result.segments[0]["text"] == "He returned to the track."


def test_draft_mode_has_no_title_and_ignores_stray_heading():
    resp = {"content": [{"type": "text", "text": "# Not a title request\nprose"}],
            "usage": {}}
    narrator = AnthropicStorylineNarrator(api_key="k", client=_FakeClient(resp))
    result = narrator.generate_narrative([_excerpt(1, "2026-07-01")], "Running")
    assert result.title is None
    assert result.segments[0]["text"].startswith("# Not a title request")


def test_mode_selects_framing_instruction():
    # capture the request; assert the user query contains the mode-specific
    # sentence ("arc is ongoing" / "give it a proper ending" / "brief addendum")
    ...


def test_addendum_mode_requires_prior_narrative():
    narrator = AnthropicStorylineNarrator(api_key="k", client=_FakeClient({}))
    with pytest.raises(ValueError, match="prior_narrative"):
        narrator.generate_narrative([_excerpt(1, "2026-07-01")], "Running",
                                    mode="addendum")


def test_sectioned_api_is_gone():
    assert not hasattr(AnthropicStorylineNarrator, "generate_sectioned_narrative")
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_providers/test_storyline_narrator.py -v` — FAIL.

- [ ] **Step 3: Implement**

Keep `SYSTEM_PROMPT`, `_build_documents`, `_build_index_maps`, `_call_api`, `_parse_narrative_response`, `_attr_or_key`, `_extract_usage` unchanged. Replace `_build_user_query` with a mode switch appended to the base query:

```python
_MODE_FRAMING: dict[str, str] = {
    "draft": (
        "This chapter's arc is ongoing. Narrate what is here; do not force "
        "an ending or a sense of resolution the entries do not contain."
    ),
    "closure": (
        "This chapter is complete. Give the narrative a proper ending, and "
        "begin your response with a single title line of the form "
        "'# <short chapter title>' (3-6 words) before the prose. Do not use "
        "any other headings."
    ),
    "addendum": (
        "The chapter below has already been written and published. The new "
        "entries surfaced later but belong to its period. Write a brief "
        "addendum (2-4 sentences, cited) that reads as a postscript; do not "
        "retell the chapter.\n\nPUBLISHED CHAPTER:\n{prior}"
    ),
}
```

`generate_narrative(..., mode, prior_narrative)`: raise `ValueError` if `mode == "addendum"` and not `prior_narrative`; build the query as base + framing (formatting `{prior}` for addendum). After parsing segments, in closure mode only: if the first segment is a text segment whose first line matches `^#\s+(.+)$`, pop that line into `result.title` and keep the remainder (this is the single bounded title parse; other modes never inspect headings). If the title line is absent in closure mode, `title=None` and the caller falls back (engine uses the judge's working title or `"Chapter {seq}"`).

Delete everything listed under Interfaces → Deleted, and delete `tests/test_storyline_sectioning.py` + `tests/test_storyline_bucketing.py` (they test deleted behavior).

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_providers/test_storyline_narrator.py -v` — PASS.

- [ ] **Step 5: Commit**

```bash
git add -A src/journal/providers/storyline_narrator.py tests/
git commit -m "feat(storylines): narrator modes draft/closure/addendum; delete sectioning"
```

---

### Task 6: Engine service

**Files:**
- Create: `src/journal/services/storylines/engine.py`
- Delete: `src/journal/services/storylines/service.py`, `backfill.py`, `recheck.py` (Task 11 replaces the CLIs)
- Test: create `tests/test_storyline_engine.py`; delete `tests/test_storyline_generation.py`, `tests/test_storyline_resegment.py`, `tests/test_storyline_backfill.py`, `tests/test_storyline_recheck.py`

**Interfaces:**
- Consumes: repository (Task 3), judge (Task 4), narrator (Task 5), `EntityStore.get_dated_entity_excerpts(entity_id=, user_id=, start_date=None, end_date=None)`.
- Produces:

```python
@dataclass
class PublishedInfo:
    chapter_id: int
    title: str

@dataclass
class UpdateResult:
    storyline_id: int
    new_entry_count: int = 0
    draft_entry_count: int = 0
    published: PublishedInfo | None = None
    addenda_chapter_ids: list[int] = field(default_factory=list)
    chapter_count: int = 0          # bootstrap only
    reasoning: str = ""
    warnings: list[str] = field(default_factory=list)

@runtime_checkable
class StorylineEngineProtocol(Protocol):
    def update(self, storyline_id: int) -> UpdateResult: ...
    def bootstrap(self, storyline_id: int, *, mark_read: bool = ...) -> UpdateResult: ...
    def refresh_draft(self, storyline_id: int) -> UpdateResult: ...

class StorylineEngine:
    def __init__(self, *, entity_store, entry_repository, storyline_repository,
                 narrator: StorylineNarratorProtocol, judge: StorylineJudgeProtocol,
                 embedder: Callable[[str], list[float]] | None = None,
                 min_publish_entries: int = 3) -> None: ...
```

- [ ] **Step 1: Write the failing tests**

Fakes: `FakeNarrator` (records calls; returns configurable `NarrativeResult` per mode), `FakeJudge` (returns configured `ExtensionJudgment`/`PartitionResult`). Real repository over the `factory` fixture (cheap, and it exercises the transactions). Core cases:

```python
class TestUpdateContinue:
    def test_new_entries_join_draft_and_draft_renarrated(self, engine, repo, storyline, entry_ids, fake_narrator, fake_judge):
        fake_judge.judgment = ExtensionJudgment(
            assignments=[EntryAssignment(entry_ids[0], "draft")],
            draft_arc_complete=False, reasoning="continues")
        result = engine.update(storyline.id)
        draft = repo.get_draft(storyline.id)
        assert entry_ids[0] in repo.chapter_entry_ids(draft.id)
        assert fake_narrator.calls[-1].mode == "draft"
        assert draft.segments  # narrative written
        assert result.published is None

    def test_no_new_entries_is_a_noop(self, engine, storyline, fake_judge):
        result = engine.update(storyline.id)
        assert fake_judge.calls == [] and result.new_entry_count == 0

    def test_judge_failure_leaves_state_untouched_and_pending(self, engine, repo, storyline, entry_ids, fake_judge):
        fake_judge.judgment = ExtensionJudgment([], False, "", failed=True)
        result = engine.update(storyline.id)
        assert "judge" in result.warnings[0].lower()
        assert repo.chapter_entry_ids(repo.get_draft(storyline.id).id) == []
        # candidates remain unassigned → retried next update

    def test_narrator_failure_keeps_old_draft_narrative(self, engine, repo, storyline, entry_ids, fake_narrator, fake_judge):
        # pre-write a draft narrative; fake_narrator returns empty segments;
        # assert draft segments unchanged and a warning recorded


class TestUpdatePublish:
    def test_arc_complete_publishes_with_closure_title(self, engine, repo, storyline, entry_ids, fake_narrator, fake_judge):
        # 3 entries pre-assigned to draft (min_publish_entries met),
        # judge: draft_arc_complete=True, new entry → new_chapter
        # narrator closure returns title "The End of Winter"
        result = engine.update(storyline.id)
        chapters = repo.list_chapters(storyline.id)
        assert chapters[-2].state == "published"
        assert chapters[-2].title == "The End of Winter"
        assert chapters[-2].read_at is None            # unread!
        assert result.published.title == "The End of Winter"
        assert repo.chapter_entry_ids(chapters[-1].id)  # new draft got the new entry
        assert [c.mode for c in fake_narrator.calls] == ["closure", "draft"]

    def test_min_entries_guard_blocks_publish(self, engine, repo, storyline, entry_ids, fake_judge):
        # draft has 1 entry; judge says arc complete →
        # publish suppressed, everything folds into draft, warning recorded

    def test_at_most_one_publish_per_run(self, ...):
        # judge assigns new_chapter AND arc_complete with plenty of entries:
        # exactly one publish transaction happens

    def test_closure_without_title_falls_back(self, ...):
        # narrator closure returns title=None → published title == "Chapter {seq}"


class TestAddenda:
    def test_backdated_entry_becomes_addendum_and_unreads_chapter(self, engine, repo, storyline, entry_ids, fake_narrator, fake_judge):
        # publish a chapter, mark read; judge assigns entry → published_chapter
        # assert addenda block appended, read_at cleared,
        # narrator called with mode="addendum",
        # membership row added_late=1


class TestBootstrap:
    def test_partitions_and_publishes_all_but_last(self, engine, repo, storyline, entry_ids, fake_narrator, fake_judge):
        fake_judge.partition_result = PartitionResult(chapters=[
            PartitionChapter([entry_ids[0], entry_ids[1]], "The Build-Up"),
            PartitionChapter([entry_ids[2]], "Now"),
        ])
        result = engine.bootstrap(storyline.id)
        chapters = repo.list_chapters(storyline.id)
        assert [c.state for c in chapters] == ["published", "draft"]
        assert chapters[0].read_at is None  # NEW storyline: unread is correct
        assert result.chapter_count == 2

    def test_bootstrap_mark_read_flag(self, ...):
        # engine.bootstrap(storyline.id, mark_read=True) → published read_at set
        # (the migration sweep uses this)

    def test_partition_failure_makes_no_writes(self, ...):
        # failed PartitionResult → chapters unchanged, warning


class TestRefresh:
    def test_refresh_renarrates_draft_members_only(self, engine, repo, storyline, entry_ids, fake_narrator, fake_judge):
        draft = repo.get_draft(storyline.id)
        repo.add_entries_to_draft(draft.id, entry_ids[:2])
        result = engine.refresh_draft(storyline.id)
        assert fake_judge.calls == []                      # no judgment on refresh
        assert fake_narrator.calls[-1].mode == "draft"
        assert {e.entry_id for e in fake_narrator.calls[-1].excerpts} == set(entry_ids[:2])
        assert result.published is None
```

Flesh out the elided bodies following the shown idiom; each pairs one judge/narrator configuration with 2–4 asserts on repository state.

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_storyline_engine.py -v` — FAIL.

- [ ] **Step 3: Implement the engine**

`engine.py`, target ≤ 450 lines. Core flow:

```python
def update(self, storyline_id: int) -> UpdateResult:
    storyline = self._require_storyline(storyline_id)
    draft = self._repo.get_draft(storyline_id)
    if draft is None:
        raise ValueError(f"Storyline {storyline_id} has no draft chapter")
    result = UpdateResult(storyline_id=storyline_id)

    candidates = self._candidate_entries(storyline)          # mention union
    pending = self._repo.list_pending_entries(storyline_id)  # surface-form/embedding matches
    assigned = self._repo.assigned_entry_ids(storyline_id)
    new_ids = [e.entry_id for e in candidates if e.entry_id not in assigned]
    new_ids += [eid for eid in pending if eid not in assigned and eid not in new_ids]
    if not new_ids:
        return result
    result.new_entry_count = len(new_ids)

    excerpt_by_id = {e.entry_id: e for e in candidates}
    new_entries = [self._to_judge_entry(eid, excerpt_by_id) for eid in new_ids]
    draft_member_ids = self._repo.chapter_entry_ids(draft.id)
    judgment = self._judge.judge_extension(
        storyline_name=storyline.name,
        storyline_description=storyline.description,
        draft_narrative=_join_text(draft.segments),
        draft_entries=[self._to_judge_entry(eid, excerpt_by_id) for eid in draft_member_ids],
        new_entries=new_entries,
        published_chapters=self._published_index(storyline_id),
    )
    if judgment.failed:
        result.warnings.append("Judge unavailable; entries left pending for the next run.")
        return result
    result.reasoning = judgment.reasoning

    to_draft, to_new, addenda = _split_assignments(judgment.assignments)

    # Addenda first (independent of the draft's fate).
    for chapter_id, eids in addenda.items():
        self._apply_addendum(storyline, chapter_id, eids, result)

    # Publish decision + guards.
    draft_total = len(draft_member_ids) + len(to_draft)
    wants_publish = judgment.draft_arc_complete or bool(to_new)
    if wants_publish and draft_total < self._min_publish_entries:
        result.warnings.append(
            f"Draft has {draft_total} entries < min {self._min_publish_entries}; "
            "deferring publish.")
        to_draft, to_new, wants_publish = to_draft + to_new, [], False

    if to_draft:
        self._repo.add_entries_to_draft(draft.id, to_draft)
    self._repo.clear_pending_entries(storyline_id, new_ids)

    if wants_publish:
        self._publish(storyline, draft.id, to_new, result)   # closure narration → publish txn → draft narration
    else:
        self._renarrate_draft(storyline, draft.id, result)   # draft narration → set_draft_narrative
    result.draft_entry_count = len(self._repo.chapter_entry_ids(
        self._repo.get_draft(storyline_id).id))
    return result
```

`_publish`: fetch the draft's member excerpts (`_excerpts_for(entry_ids)` — see below), narrate `mode="closure"`; if segments empty → warning, fall back to `_renarrate_draft` path unpublished (never publish an empty chapter). Title: `narrative.title or f"Chapter {draft.seq}"`. Then `repo.publish_draft(...)`, then if `to_new`: narrate the new draft (`mode="draft"`) and `set_draft_narrative` with embedding. Sets `result.published = PublishedInfo(published.id, published.title)`.

`_renarrate_draft`: excerpts for current members; `mode="draft"`; empty segments → warning + keep existing (do not call `set_draft_narrative`); else write with embedding (`self._embedder(_join_text(segments))` in try/except, warning on failure — embedding is best-effort, mirroring the old code).

`_apply_addendum`: validate the chapter is published (else fold ids into `to_draft` with a warning); narrate `mode="addendum"` with `prior_narrative=_join_text(chapter.segments)` over those entries' excerpts; empty → warning, leave ids pending (do NOT clear them); else `repo.append_addendum`.

`_candidate_entries`: the old `_fetch_excerpts` minus FTS and minus date windows — union `get_dated_entity_excerpts(entity_id, user_id)` across anchors, dedup by entry_id, sort by `(entry_date, entry_id)`. Sparse-storyline recall fallback (spec §3): when the union has fewer than 3 entries, supplement with a plain parameterised `LIKE` scan per anchor (`WHERE user_id = ? AND (final_text LIKE '%' || ? || '%' OR raw_text LIKE '%' || ? || '%')` via a small repository helper `find_entries_mentioning(user_id, name) -> list[DatedEntryExcerpt]` added to this task), dedup'd against the mention set — injection-proof, replacing the old FTS5 fallback. `_excerpts_for(entry_ids)`: from the candidate map; for pending ids not in it (surface-form matches without mentions), build a `DatedEntryExcerpt` from `entry_repository.get_entry(eid)` (`final_text or raw_text`).

`bootstrap(storyline_id, *, mark_read: bool = False)`: candidates + pending as above (ignore existing assignment — bootstrap replaces everything); `judge.partition(...)`; failed/empty → warning, return. Narrate every chapter first (closure mode for all but last, draft mode for last) collecting `BootstrapChapterSpec`s (published specs get `mark_read=mark_read`); any narration returning empty segments → abort with warning before any write. Then one `repo.replace_all_chapters`, then `set_draft_narrative` embedding for the final draft, then `clear_pending_entries`. `refresh_draft`: `_renarrate_draft` only.

`_join_text(segments)`: copy of the old `_join_narrative_text` (text-kind join, 32k char cap) — move it into `engine.py`. `_to_judge_entry` truncates text to 2,000 chars for draft entries (context economy) and passes new entries whole up to 6,000 chars.

Delete `service.py`, `backfill.py`, `recheck.py` and the four old test files; keep `segments.py` (unchanged) and `extension.py` (Task 7).

- [ ] **Step 4: Run tests** — `uv run pytest tests/test_storyline_engine.py -v` — PASS.

- [ ] **Step 5: Commit**

```bash
git add -A src/journal/services/storylines tests/
git commit -m "feat(storylines): continue-or-break engine — update/bootstrap/refresh flows"
```

---

### Task 7: Extension classifier fixes

**Files:**
- Modify: `src/journal/services/storylines/extension.py`
- Test: `tests/test_storyline_extension.py` (create; move any surviving classifier tests from wherever they live — check `grep -rl StorylineExtensionClassifier tests/`)

**Interfaces:**
- Consumes: `repo.get_draft()` (Task 3). Signature of `classify_for_entry(entry_id, user_id) -> list[ExtensionResult]` and the `AnthropicStorylineExtensionDecider` provider are unchanged.

- [ ] **Step 1: Write the failing tests**

```python
def test_surface_form_requires_word_boundary(classifier_env):
    # anchor canonical_name "Ana"; entry text "we ate a banana" → stage no_match
    # entry text "Ana called" → decider invoked (stage surface_form_llm)

def test_embedding_stage_reads_draft_embedding(classifier_env):
    # storyline draft has draft_embedding set close to the entry's embedding
    # (fake embedder returns fixed vectors) → decider invoked (stage embedding_llm)

def test_embedding_stage_skipped_when_draft_has_none(classifier_env):
    # draft_embedding None → stage no_match, no decider call
```

- [ ] **Step 2: Run to verify failure** — FAIL (`in` substring match; storyline.summary_embedding attribute gone → AttributeError proves the dead path).

- [ ] **Step 3: Implement**

In `_classify_one`: replace the substring check with

```python
if re.search(rf"\b{re.escape(entity.canonical_name)}\b",
             entry_text_lower, re.IGNORECASE):
```

(compile per anchor; `entry_text_lower` can stay lowercase — keep `re.IGNORECASE` anyway for safety). Replace the `storyline.summary_embedding` read with:

```python
draft = self._storyline_repository.get_draft(storyline.id)
draft_embedding = draft.draft_embedding if draft is not None else None
if entry_embedding is not None and draft_embedding is not None:
    similarity = cosine_similarity(entry_embedding, draft_embedding)
    ...
```

Also move `record_extension_check` out of the per-storyline loop body into a single stamp after the loop (it updates the same row-set per storyline; keep per-storyline stamping but batch the commits by passing through one connection — smallest change: leave as-is if the repo method already commits; note in the docstring). Keep stages/naming otherwise identical.

- [ ] **Step 4: Run tests** — PASS.

- [ ] **Step 5: Commit**

```bash
git add src/journal/services/storylines/extension.py tests/test_storyline_extension.py
git commit -m "fix(storylines): word-boundary surface match; live draft embedding for semantic stage"
```

---

### Task 8: Jobs — storyline_update worker, runner, publish notification

**Files:**
- Create: `src/journal/services/jobs/workers/storyline_update.py`
- Delete: `src/journal/services/jobs/workers/storyline_generation.py`
- Modify: `src/journal/services/jobs/workers/storyline_extension_check.py`, `src/journal/services/jobs/runner.py:417-517` (replace `submit_storyline_generation`), `src/journal/db/jobs_repository.py` (`find_pending_open_regeneration` → `find_pending_storyline_update`), `src/journal/services/jobs/workers/__init__.py` (WorkerContext: `storyline_generation` → `storyline_engine`), `src/journal/services/notifications.py` (add `notify_chapter_published`), params-validation key sets (find via `grep -n STORYLINE_GENERATION_KEYS src/journal/services/jobs/`)
- Test: rewrite `tests/test_storyline_jobs.py`

**Interfaces:**
- Produces:

```python
# runner
def submit_storyline_update(self, storyline_id: int, *, user_id: int,
                            parent_job_id: str | None = None,
                            bootstrap: bool = False,
                            refresh_only: bool = False,
                            unpublish: bool = False) -> Job
# job type string: "storyline_update"; params keys:
#   {storyline_id, user_id, parent_job_id?, bootstrap?, refresh_only?, unpublish?}
# At most one of bootstrap/refresh_only/unpublish may be True (ValueError).
# unpublish branch: repo.unpublish_newest(storyline_id) then engine.refresh_draft
# (Task 9's unpublish route enqueues this).

# jobs_repository
def find_pending_storyline_update(self, *, user_id: int, storyline_id: int) -> Job | None
#   queued (not running) storyline_update with matching storyline_id and
#   neither bootstrap nor refresh_only set

# notifications
def notify_chapter_published(self, user_id: int, storyline_name: str,
                             chapter_title: str) -> None
#   topic key "storyline_published"; title f"New chapter: {storyline_name}",
#   message f"“{chapter_title}” is ready to read."
```

- [ ] **Step 1: Write the failing tests**

Rewrite `tests/test_storyline_jobs.py` keeping its existing harness style (it already fakes `WorkerContext`; read the current file first and preserve the fixture approach). Cases:

```python
def test_worker_update_calls_engine_and_records_summary(...):
    # engine.update returns UpdateResult(published=PublishedInfo(5, "T"), ...)
    # → job succeeded; summary contains published_chapter_id, title, reasoning

def test_worker_publish_fires_pushover(...):
    # published set → notifier.notify_chapter_published called once with
    # (user_id, storyline_name, "T"); not called when published is None

def test_worker_bootstrap_param_routes_to_bootstrap(...):
def test_worker_refresh_only_routes_to_refresh_draft(...):
def test_worker_unconfigured_engine_fails_job(...):
def test_extension_check_yes_adds_pending_and_queues_coalesced(...):
    # classifier yes → repo.add_pending_entry called; second yes while a
    # queued update exists → no duplicate submit
def test_submit_rejects_bootstrap_plus_refresh(...):  # ValueError
```

- [ ] **Step 2: Run to verify failure** — FAIL.

- [ ] **Step 3: Implement**

`run_storyline_update` mirrors the old `run_storyline_generation`'s error/notification skeleton (mark_running → progress → engine call → mark_succeeded / friendly_error on except) with the branch:

```python
engine = ctx.storyline_engine
if params.get("bootstrap"):
    result = engine.bootstrap(int(params["storyline_id"]))
elif params.get("refresh_only"):
    result = engine.refresh_draft(int(params["storyline_id"]))
else:
    result = engine.update(int(params["storyline_id"]))
...
if result.published is not None and user_id is not None:
    try:
        ctx.notifier.notify_chapter_published(
            user_id, storyline_name, result.published.title)
    except Exception:
        log.exception("Publish notification failed (job %s)", job_id)
```

(`storyline_name` via `ctx` repositories — the worker context already carries the storyline repository; fetch by id.) Summary dict: `{storyline_id, new_entry_count, draft_entry_count, published_chapter_id?, published_title?, addenda_chapter_ids?, chapter_count?, reasoning?, warnings?}` — include keys only when truthy, mirroring the old worker.

Extension-check worker: replace the `submit_regenerate` callback usage with

```python
if r.decision == "yes":
    ctx.storyline_repository.add_pending_entry(r.storyline_id, entry_id)
    if ctx.jobs.find_pending_storyline_update(
            user_id=user_id, storyline_id=r.storyline_id) is None:
        submit_update(r.storyline_id, user_id=user_id)
```

(the pending row makes coalescing lossless: a queued update picks up every pending entry when it runs). Runner: replace `submit_storyline_generation` with `submit_storyline_update` (validation: `bootstrap and refresh_only` → ValueError; engine wired check → RuntimeError), still submitting to `self._storyline_executor`. Update `_maybe_queue_storyline_extension_check`'s callback plumbing (it passes `self.submit_storyline_generation` today → pass `self.submit_storyline_update`). `notify_chapter_published` follows the shape of `notify_fitness_auth_broken` (resolve credentials, topic gate, `_post_to_pushover`). WorkerContext: rename field, fix construction sites (`grep -rn "storyline_generation=" src/`).

- [ ] **Step 4: Run tests** — `uv run pytest tests/test_storyline_jobs.py tests/test_services/test_jobs_runner.py tests/test_services/test_notifications.py -v` — PASS (update the runner/notification test files where they reference the old names).

- [ ] **Step 5: Commit**

```bash
git add -A src/journal/services/jobs src/journal/services/notifications.py src/journal/db/jobs_repository.py tests/
git commit -m "feat(storylines): storyline_update job, lossless pending coalescing, publish push"
```

---

### Task 9: REST API rewrite

**Files:**
- Rewrite: `src/journal/api/storylines.py`, `src/journal/api/storylines_write.py`
- Test: rewrite `tests/test_api_storylines.py`, `tests/test_api_storylines_write.py`

**Interfaces:**
- Consumes: repository (Task 3), `submit_storyline_update` (Task 8).
- Produces the route surface from spec §5 (exact route names below). Response shapes:

```
storyline summary: {id, name, description, status, anchors: [{entity_id, canonical_name}],
                    unread_count, chapter_count, updated_at, created_at}
chapter meta:      {id, seq, title, state, entry_count, first_entry_date,
                    last_entry_date, published_at, read_at, citation_count}
chapter detail:    meta + {segments, addenda: [{added_at, segments, entry_ids}], model_used, generated_at}
```

- [ ] **Step 1: Write the failing tests**

Rewrite the two API test files keeping their existing TestClient/auth fixtures (read them first; they already have login helpers). Cases per route:

- `GET /api/storylines` → 200, includes `unread_count` (seed via repo publish + set_read).
- `GET /api/storylines/{id}` → 200 with `chapters` (meta only, seq ASC, draft last); 404 wrong user.
- `GET /api/storylines/{id}/chapters/{cid}` → 200 with segments + addenda; 404 cross-storyline cid.
- `POST /api/storylines` → 201 + `bootstrap_job_id`; 409 duplicate anchor-set+name; 422 zero/16 anchors.
- `POST /api/storylines/{id}/refresh` → 202 `{job_id}` (asserts `submit_storyline_update(..., refresh_only=True)`).
- `POST /api/storylines/{id}/chapters/{cid}/read` and `/unread` → 200, `read_at` toggles; 400 on draft.
- `PATCH /api/storylines/{id}/chapters/{cid}` body `{title}` → 200 rename; 400 empty title.
- `POST /api/storylines/{id}/chapters/unpublish` → 202 `{job_id}`; 400 when no published chapter (repo raises → 400 before queueing: do the validation read first, but the fold itself happens in the job — see Step 3).
- `PATCH /{id}`, `DELETE /{id}`, `PUT /{id}/anchors` — port the existing tests unchanged.
- Deleted routes (`/chapters` POST, `/split`, `/merge`, chapter DELETE, chapter `/regenerate`) → 404: one test asserting each old path now 404s.

- [ ] **Step 2: Run to verify failure** — FAIL.

- [ ] **Step 3: Implement**

Keep the `@mcp.custom_route` + `@handler(services_getter)` idiom and the auth/ownership checks from the current files. `create_storyline`: port the existing anchor validation + 409 dedup, then `repo.create_storyline(...)` and `job_runner.submit_storyline_update(sid, user_id=..., bootstrap=True)`; response includes `bootstrap_job_id`. `refresh`: ownership check → `submit_storyline_update(sid, user_id=..., refresh_only=True)` → 202. `unpublish`: ownership check + `any published?` read; if none → 400; else `submit_storyline_update(sid, user_id=..., unpublish=True)` (the worker branch is defined in Task 8). `read`/`unread`: `repo.set_read(cid, True/False)` with ValueError → 400. Serializers `_storyline_to_dict`/`_chapter_to_dict` rebuilt for the new shapes (list endpoint fetches `repo.unread_counts(user_id)` once and joins in memory).

- [ ] **Step 4: Run tests** — `uv run pytest tests/test_api_storylines.py tests/test_api_storylines_write.py -v` — PASS.

- [ ] **Step 5: Commit**

```bash
git add -A src/journal/api tests/
git commit -m "feat(storylines): REST surface — read-state, refresh, unpublish; drop editing routes"
```

---

### Task 10: MCP tools rewrite

**Files:**
- Rewrite: `src/journal/mcp_server/tools/storylines.py`
- Test: rewrite `tests/test_mcp_tools_storylines.py`; delete `tests/test_mcp_storyline_chapter_editing.py`

**Interfaces:**
- Consumes: repository, runner (Task 8).
- Produces MCP tools: `journal_list_storylines`, `journal_get_storyline` (chapters + draft, correct panel-less shape — this deletes the `list_panels(storyline.id)` bug), `journal_get_storyline_chapter` (new: full segments), `journal_create_storyline` (submits bootstrap, polls to terminal like today), `journal_refresh_storyline`, `journal_unpublish_storyline_chapter`, `journal_rename_storyline_chapter`, `journal_set_storyline_anchors`, `journal_delete_storyline`, `journal_storylines_guide` (rewrite the guide text for the new model). Deleted: `journal_add_storyline_chapter`, `journal_split_storyline_chapter`, `journal_merge_storyline_chapters`, `journal_update_storyline_chapter`, `journal_delete_storyline_chapter`, `journal_regenerate_storyline`.

- [ ] **Step 1: Write the failing tests** — mirror the surviving tools' current test style (fake services dict); assert `journal_get_storyline` returns chapter meta with `state`/`read_at`, and that the deleted tools are no longer registered (`assert "journal_split_storyline_chapter" not in registered_tool_names`).

- [ ] **Step 2: Run to verify failure** — FAIL.

- [ ] **Step 3: Implement** — port handlers onto the new repo/runner surface; `journal_storylines_guide` gets a rewritten `_STORYLINES_GUIDE` describing draft/published semantics, unread state, unpublish, and bootstrap (keep it under ~60 lines; it is the MCP client's primer).

- [ ] **Step 4: Run tests** — `uv run pytest tests/test_mcp_tools_storylines.py tests/test_mcp_server.py -v` — PASS.

- [ ] **Step 5: Commit**

```bash
git add -A src/journal/mcp_server tests/
git commit -m "feat(storylines): MCP tools for draft/published model; fix get_storyline panels bug"
```

---

### Task 11: CLI — bootstrap-storylines

**Files:**
- Modify: `src/journal/cli/__init__.py` (replace `cmd_backfill_storyline_chapters`:201 and `cmd_recheck_storylines`:248 and their argparse registrations at :876/:924), `src/journal/cli/_services.py:144-176` (wire engine + judge instead of glue/generation service)
- Test: extend `tests/test_cli.py` (replace the backfill/recheck command tests)

**Interfaces:**
- Consumes: `StorylineEngine.bootstrap(storyline_id, mark_read=...)` (Task 6).
- Produces: `journal bootstrap-storylines --user-id N [--storyline-id N] [--mark-read] [--execute]`. Default is dry-run: list each storyline with its chapter/entry counts and "would bootstrap"; `--execute` runs `engine.bootstrap` per storyline and prints resulting chapter counts + warnings. `--mark-read` is for the one-time migration sweep.

- [ ] **Step 1: Write the failing test** — invoke the command via the existing CLI test harness (see how `test_cli.py` drives `cmd_backfill_storyline_chapters` today); assert dry-run makes no engine calls and `--execute` calls `bootstrap` once per active storyline with `mark_read` forwarded.

- [ ] **Step 2: Run to verify failure** — FAIL.

- [ ] **Step 3: Implement** — `cmd_bootstrap_storylines(args, config)` builds services via `_services.py` (which now constructs `AnthropicStorylineJudge` + narrator + engine; delete the glue construction), pages storylines like the old backfill did, honors `--storyline-id`.

- [ ] **Step 4: Run tests** — `uv run pytest tests/test_cli.py -v` — PASS.

- [ ] **Step 5: Commit**

```bash
git add -A src/journal/cli tests/test_cli.py
git commit -m "feat(storylines): bootstrap-storylines CLI replaces backfill/recheck"
```

---

### Task 12: Wiring, config prune, glue deletion, full-suite green

**Files:**
- Modify: `src/journal/mcp_server/bootstrap.py:615-660` (construct judge + engine; drop glue), `src/journal/config.py:392-449` (remove `storyline_glue_model`, `storyline_chapter_target_words`/`min`/`max`, `storyline_fts_fallback_threshold`, `storyline_default_window_days`; add `storyline_judge_model` default `claude-haiku-4-5`, `storyline_min_publish_entries` default `3`)
- Delete: `src/journal/providers/storyline_glue.py` + its tests, `tests/test_storyline_segments.py` stays (segments still used), any remaining references (`grep -rn "storyline_glue\|StorylineGlue\|glue" src/ tests/`)
- Test: `tests/test_config.py` additions; then the WHOLE suite

- [ ] **Step 1: Write the failing config test**

```python
def test_storyline_judge_config_defaults(monkeypatch):
    cfg = Config.from_env()
    assert cfg.storyline_judge_model == "claude-haiku-4-5"
    assert cfg.storyline_min_publish_entries == 3

def test_removed_storyline_knobs_are_gone():
    cfg = Config.from_env()
    assert not hasattr(cfg, "storyline_glue_model")
    assert not hasattr(cfg, "storyline_chapter_max_words")
```

- [ ] **Step 2: Run to verify failure** — FAIL.

- [ ] **Step 3: Implement** — config fields + env names (`STORYLINE_JUDGE_MODEL`, `STORYLINE_MIN_PUBLISH_ENTRIES`); bootstrap constructs `AnthropicStorylineJudge(api_key=cfg.anthropic_api_key, model=cfg.storyline_judge_model)` and `StorylineEngine(...)` and passes it into `WorkerContext` as `storyline_engine`; delete glue everywhere.

- [ ] **Step 4: Run the FULL suite and lint**

Run: `uv run pytest && uv run ruff check src/ tests/`
Expected: everything green. This is the sweep step — fix any test still importing deleted symbols (fix the test, not by resurrecting code).

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat(storylines): wire engine/judge, prune config, delete glue provider"
```

---

### Task 13: Docs, journal entry, rollout notes

**Files:**
- Rewrite: `docs/storylines.md` (new model; keep it shorter than the old one)
- Archive: `git mv docs/superpowers/specs/2026-06-13-storyline-chapters-design.md docs/archive/` and same for `2026-06-15-storyline-chapter-editing-design.md`, each gaining a `**Status:** superseded by [2026-07-12 spec](../superpowers/specs/2026-07-12-storylines-redesign-design.md) (2026-07-12).` header; update `docs/architecture.md`, `docs/api.md`, `docs/jobs.md`, `docs/configuration.md` storyline sections
- Create: `journal/260712-storylines-redesign.md` (decisions + bug inventory reference)
- Create: `docs/rollout-storylines-0036.md` — the prod runbook:

```markdown
1. Deploy this release (migration 0036 runs on boot; old panels → _legacy).
2. ssh media; run `journal bootstrap-storylines --user-id 1 --mark-read --execute`.
3. Verify chapters read well in the webapp; spot-check 2-3 storylines.
4. NEXT release: add migration 0037 dropping storyline_panels_legacy
   (single DROP TABLE IF EXISTS). Never ship 0036 and 0037 together.
```

- [ ] **Step 1: Write/update the docs** (no code, no test step). Cross-check every claim against the shipped code — the old `storylines.md`'s staleness was a review finding; don't repeat it.

- [ ] **Step 2: Commit**

```bash
git add -A docs journal
git commit -m "docs(storylines): redesign reference, rollout runbook, archive superseded specs"
```

Then push and watch CI (`git push && gh run watch`) — the plan is done only when CI is green.
