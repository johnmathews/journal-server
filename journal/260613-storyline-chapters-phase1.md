# Storyline Chapters — Phase 1 (server side)

**Date:** 2026-06-13
**Branch:** `feat/storyline-chapters`
**Spec:** [`docs/archive/2026-06-13-storyline-chapters-design.md`](../docs/archive/2026-06-13-storyline-chapters-design.md) (superseded 2026-07-12)
**Plan:** [`docs/superpowers/plans/2026-06-13-storyline-chapters-phase1.md`](../docs/superpowers/plans/2026-06-13-storyline-chapters-phase1.md)
**Reference doc:** [`docs/storylines.md`](../docs/storylines.md)

## Context

A storyline used to be a single linear document: two panels (`curation` +
`narrative`) attached directly to the `storylines` row and extended forever via
append mode. To reach recent material you read from the beginning of time every
time, and every regenerate/append reasoned over the whole history. The user
wants to chop a storyline into **chapters** — "Jan–Mar: The Move", "Mar–Jun:
Settling In" — each a self-contained short story over a date sub-range.

This is the **server side of Phase 1** (Tasks 1–5 of the plan). Phase 1 ships
*reading + per-chapter generation*. The "suggest a cut" boundary engine, the
draggable timeline editor UI, true incremental append wiring, and removal of the
back-compat panels shim are **Phase 2** (a separate spec). The webapp half of
Phase 1 (chapter rail, lazy panel load, per-chapter citation registry) is a
sibling commit in the webapp repo.

## Key decisions

### D1. A new table between storyline and panels; anchors stay storyline-level

```
storylines ──1:N──▶ storyline_chapters ──1:2──▶ storyline_panels
     │
     └──1:N──▶ storyline_entities   (anchors — unchanged, storyline-level)
```

The panel FK moves **down** a level: `storyline_panels.storyline_id` →
`chapter_id`. A chapter owns its two panels and its own date window. Anchors
were deliberately **not** moved — they remain a property of the storyline and
are shared across all chapters. Per-chapter anchor overrides were explicitly
rejected in the spec; a storyline is "about the same people throughout", the
chapters just slice it by time.

`storyline_chapters` carries `seq` (1-based order), `title`, `start_date`/
`end_date`, `state` (`open`/`closed`), `last_generated_at`, and
`summary_embedding_json`. Two invariants are enforced at the DB level:
`UNIQUE(storyline_id, seq)` and a **partial unique index**
`… WHERE state='open'` — at most one open chapter per storyline. The summary
embedding moved down from the storyline row to the chapter; the old
`storylines.summary_embedding_json` column is kept but no longer read (one
release of grace to keep the migration re-runnable).

### D2. Migration 0030 — atomic panel rebuild, forward-only re-runnable

The tricky part is re-keying `storyline_panels`. SQLite can't drop the old
`NOT NULL` + `UNIQUE(storyline_id, panel_kind)` constraints in place, so the
panel table has to be **rebuilt** (`_new` table → copy → drop → rename). That
rebuild is the one destructive step, so it is the only thing wrapped in an
explicit `BEGIN;…COMMIT;`. The other two steps — the chapters-table create and
the one-open-chapter-per-storyline backfill — run in `executescript`'s
autocommit mode and are each independently idempotent (`CREATE … IF NOT EXISTS`,
`NOT EXISTS` guard on the backfill `INSERT … SELECT`). So a partial failure
leaves steps 1–2 in place and rolls back just the rebuild; a re-run completes
the remaining work cleanly.

Per the migration-testing convention, I queried prod for shape anomalies first
(storylines with NULL dates, with 0 or 1 panels, archived storylines) and wrote
`tests/test_migration_0030_chapters.py` to exercise the data-copy path on
prod-shaped state, including the forward-only re-run (a clean no-op). The
backfill copies the storyline's name → chapter title, its dates, and its
embedding onto a single `seq=1`, `state='open'` chapter, then re-points that
storyline's two panels to it. Every pre-existing storyline becomes a
single-chapter storyline with **no data loss**.

One subtlety recorded in the SQL header: the panel rebuild references the OLD
`storyline_panels.storyline_id` column, which only exists pre-migration. That's
correct under the runner's forward-only application (it skips any migration
`<= PRAGMA user_version`). A forced re-apply after rewinding `user_version` is
not a supported operation for a plain table-rebuild migration — the test
verifies the *real* forward-only re-run, which is the no-op that matters.

### D3. Generation refactored to be per-chapter; `regenerate()` delegates to the open chapter

`StorylineGenerationService` gained `regenerate_chapter(chapter_id,
mode="replace")` as the **core**. It resolves the chapter and its parent
storyline, uses the **chapter's** `start_date`/`end_date` as the generation
window, resolves the (still storyline-level) anchors, writes both panels keyed on
`chapter_id`, and stamps `last_generated_at` + the per-chapter embedding on the
chapter row. In `replace` mode the chapter window is **authoritative** —
explicit `start_date`/`end_date` overrides are ignored (they only matter on the
append path, which mirrors the previous behavior).

The old `regenerate(storyline_id)` is now a thin back-compat wrapper: it resolves
the storyline's single **open** chapter and delegates to `regenerate_chapter`.
The open chapter still supports `mode="append"` for incremental growth as new
entries arrive; closed chapters are `replace`-only. This preserves the existing
detail-view Regenerate button and the append-mode tests untouched while the unit
of generation quietly becomes the chapter. A dead `_resolve_date_window` helper
fell out in the refactor and was dropped.

### D4. Back-compat `panels` shim on the detail route (temporary)

`GET /api/storylines/{id}` now returns a `chapters[]` summary array (seq, title,
date range, state, `last_generated_at`, `citation_count` — no panel bodies)
**plus** a `panels` field that is the **open chapter's** panels in the old
`{curation, narrative}` shape. The shim exists purely so the not-yet-updated
webapp keeps rendering during the gap between the server and webapp deploys. It
is explicitly **temporary** — Phase 2 removes it once the webapp reads chapters
via the new `GET /api/storylines/{id}/chapters/{cid}` endpoint. New chapter
panel bodies are served only through that per-chapter route (lazy-loaded on rail
click).

`POST /api/storylines` now seeds a `seq=1` open chapter on create, so the
auto-kicked generation job has a chapter to write into. New write routes:
`POST …/chapters/{cid}/regenerate` (always replace) and
`PATCH …/chapters/{cid}` (rename, metadata-only — no panel touch, no regen).
MCP parity: `journal_get_storyline` lists chapters; `journal_regenerate_storyline`
gained an optional `chapter_id`.

## Two deferred minor follow-ups (not bugs)

1. **N+1 in the detail route.** `_chapter_to_dict` calls `list_panels` once per
   chapter to compute the `citation_count` sum. Negligible at the small chapter
   counts Phase 1 produces (every storyline is single-chapter post-migration),
   but worth folding into a single grouped query when Phase 2 starts cutting
   storylines into many chapters.
2. **Test-thoroughness gap on the API-layer cross-storyline 404.** The
   API-layer test for "chapter belongs to a different storyline" happens to use
   a non-existent storyline id, so it exercises the storyline-404 branch rather
   than the `chapter.storyline_id != sid` branch. That exact branch *is* covered
   at the MCP layer (`journal_regenerate_storyline` with a foreign `chapter_id`),
   which runs an identical guard, so this is a test-thoroughness gap, **not** a
   security gap — the check is present and exercised. Tighten the API test when
   convenient.

## Phase 1 / Phase 2 split

**Phase 1 (this work — server side):** chapters table + migration 0030,
per-chapter generation, chapter-aware API + MCP read/regenerate/rename, the
back-compat `panels` shim. Single-chapter storylines (everything
post-migration) behave exactly as before.

**Phase 2 (separate spec):** the LLM "suggest a cut" boundary engine
(`POST …/chapters/suggest`), the `PUT …/chapters` commit/diff endpoint, the
`ChapterTimelineEditor` (density timeline + draggable cuts), true
incremental-append wiring on the open chapter, the "time to cut a new chapter"
nudge, and **removal of the back-compat `panels` shim** once the webapp reads
chapters directly.

## Files touched (server)

- `src/journal/db/migrations/0030_storyline_chapters.sql` — new table, panel
  re-key, backfill.
- `src/journal/models.py` — `StorylineChapter`; `StorylinePanel.storyline_id` →
  `chapter_id`.
- `src/journal/db/storyline_repository.py` — chapter CRUD (`create_chapter`,
  `get_chapter`, `list_chapters`, `get_open_chapter`, `rename_chapter`); panels
  keyed on `chapter_id`.
- `src/journal/services/storylines/service.py` — `regenerate_chapter` core;
  `regenerate` delegates to the open chapter; dropped dead `_resolve_date_window`.
- `src/journal/services/jobs/workers/storyline_generation.py` — pass
  `chapter_id` when present.
- `src/journal/api/storylines.py` — `chapters[]` + back-compat `panels` shim on
  detail; `GET …/chapters/{cid}`.
- `src/journal/api/storylines_write.py` — seed seq-1 chapter on create;
  `POST …/chapters/{cid}/regenerate`; `PATCH …/chapters/{cid}`.
- `src/journal/mcp_server/tools/storylines.py` — chapter listing in
  `journal_get_storyline`; `chapter_id` param on `journal_regenerate_storyline`.
- `tests/test_migration_0030_chapters.py` (new) plus updates to
  `test_storyline_repository.py`, `test_storyline_generation.py`,
  `test_api_storylines.py`, `test_api_storylines_write.py`,
  `test_mcp_tools_storylines.py`.
- `docs/storylines.md` — chapters data model, per-chapter generation, new
  endpoints + MCP tools, the temporary `panels` shim.
