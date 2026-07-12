**Status:** superseded by [2026-07-12 storylines redesign](../superpowers/specs/2026-07-12-storylines-redesign-design.md) (2026-07-12).

# Storyline Chapters — Design

**Date:** 2026-06-13
**Status:** superseded — see header above
**Scope of this spec:** Phase 1 (steps 1–4) — schema, per-chapter generation, and a
readable chapter rail. The suggestion engine and draggable timeline editor (steps 5–8)
are deferred to a follow-up spec; their design is recorded here for context but is **not**
in scope for this spec's implementation plan.

Cross-cutting: touches both `server/` (schema, repository, generation, API, MCP) and
`webapp/` (detail view, store, types). Expect two commits, one per repo.

## Problem

A storyline today is a single linear document — two panels (`curation` + `narrative`)
attached directly to the storyline row, extended forever via append mode. To reach recent
material you read from the beginning of time every time, and every regeneration/append
reasons over the whole history. The user wants to chop a storyline into **chapters**
("Jan–Mar: The Move", "Mar–Jun: Settling In", …) — self-contained short stories over
date sub-ranges.

## Locked decisions

These were settled during brainstorming and are the fixed premises of the design:

1. **Boundaries — system proposes, you edit.** An LLM suggests natural chapter breaks +
   draft titles; the user accepts/adjusts. (Suggestion engine itself is Phase 2.)
2. **Chapter model — self-contained stories.** Each chapter has its own `curation` +
   `narrative` panels, generated independently over its own date window. Generating or
   appending a chapter only reads entries inside that chapter's window — the system never
   re-reads the whole history.
3. **Anchors — per-storyline (shared).** All chapters share the storyline's anchor
   entities. No per-chapter anchor override.
4. **Growth — grow latest, suggest a cut.** The most recent chapter is `open` and new
   matching entries extend it via the existing append mechanism. When it grows long /
   a natural break appears, the system suggests a cut (Phase 2); on confirm the open
   chapter is closed at the boundary and a fresh open chapter begins.
5. **Reading layout — left chapter rail.** A vertical chapter list on the left; clicking a
   chapter loads its two-panel reader. Latest chapter selected by default.
6. **Review layout — visual timeline with draggable cuts** (Phase 2).

## Data model

One new table; the panel FK moves down a level. Anchors are untouched.

```
storylines ──1:N──▶ storyline_chapters ──1:2──▶ storyline_panels
     │
     └──1:N──▶ storyline_entities   (anchors — unchanged, storyline-level)
```

### `storyline_chapters` (new)

| column                  | notes                                                        |
|-------------------------|--------------------------------------------------------------|
| `id`                    | PK                                                            |
| `storyline_id`          | FK → storylines(id) ON DELETE CASCADE                         |
| `seq`                   | INTEGER, 1-based chapter order within the storyline          |
| `title`                 | TEXT                                                          |
| `start_date`            | TEXT ISO `YYYY-MM-DD`, nullable (open-start)                  |
| `end_date`              | TEXT ISO `YYYY-MM-DD`, nullable (open chapter's end is NULL) |
| `state`                 | TEXT CHECK in (`open`, `closed`); exactly one `open` per storyline |
| `last_generated_at`     | TEXT, nullable                                                |
| `summary_embedding_json`| TEXT, nullable — moved down from the storyline row           |
| `created_at`/`updated_at` | TEXT defaults                                              |

Constraints/indexes: `UNIQUE(storyline_id, seq)`; index on `storyline_id`; a partial
unique index enforcing one `open` chapter per storyline
(`CREATE UNIQUE INDEX … ON storyline_chapters(storyline_id) WHERE state='open'`).

### `storyline_panels` (changed)

- FK `storyline_id` → `chapter_id` (FK → storyline_chapters(id) ON DELETE CASCADE).
- `UNIQUE(storyline_id, panel_kind)` → `UNIQUE(chapter_id, panel_kind)`.
- Body columns (`segments_json`, `source_entry_ids_json`, `citation_count`,
  `model_used`, `generated_at`) unchanged.

### `storylines` (changed)

- `summary_embedding_json` is deprecated on this row (moves to the open chapter). Keep the
  column for one release to stay re-runnable; stop reading it.

### Migration 0030 (re-runnable)

For each existing storyline: insert one chapter (`seq=1`, `state='open'`, `title` = the
storyline name, `start_date`/`end_date` copied from the storyline row), then re-point that
storyline's two panels to the new chapter. Copy the storyline's `summary_embedding_json`
to the chapter. Idempotent: `CREATE TABLE IF NOT EXISTS`, and the backfill is a no-op when
a `seq=1` chapter already exists. Existing storylines become single-chapter storylines with
no data loss.

Per the migration-testing convention: query prod for shape anomalies first (storylines with
NULL dates, with 0/1 panels, archived), write a test that exercises the data-copy path on
prod-shaped state, and make the migration safe to re-run after a partial failure
(`DROP … IF EXISTS` guards at the top where needed).

## Generation lifecycle

`StorylineGenerationService.regenerate` already operates over a resolved date window and
writes two panels. The change is to make the **chapter** the unit instead of the storyline:

- The service takes a `chapter_id` (resolving its storyline for anchors/user) and uses the
  chapter's `start_date`/`end_date` as the window. Panels are upserted against `chapter_id`.
- Per-chapter `summary_embedding_json` is written to the chapter row.
- **Closed chapters:** generated once with `mode="replace"`, self-contained — no
  cross-chapter `prior_narrative` context. Each chapter's curation lede ("It begins on…")
  and citation numbering restart at the chapter boundary.
- **Open chapter:** keeps `mode="append"` for incremental growth as entries arrive. The
  seam-transition machinery is unchanged; it now operates within the open chapter only.
- The extension classifier scores new entries against the **open chapter's** summary
  embedding and appends to it (Phase 2 wires the "suggest a cut" trigger on top).

No other tables are touched; the service stays idempotent.

## API surface

Chapters are nested under a storyline. MCP tools mirror these.

| route | purpose | phase |
|-------|---------|-------|
| `GET /api/storylines/{id}` | now includes a `chapters[]` summary array (seq, title, date range, state, `last_generated_at`, `citation_count`) **without** panel bodies | 1 |
| `GET /api/storylines/{id}/chapters/{cid}` | one chapter's two panels (lazy-loaded on rail click) | 1 |
| `POST /api/storylines/{id}/chapters/{cid}/regenerate` | regenerate a single chapter (`replace` closed / `append` open) | 1 |
| `PATCH /api/storylines/{id}/chapters/{cid}` | rename a chapter | 1 |
| `POST /api/storylines/{id}/chapters/suggest` | run heuristic + LLM, return proposed boundaries; persists nothing | 2 |
| `PUT /api/storylines/{id}/chapters` | commit an edited chapter set; server diffs, creates/updates/closes rows, kicks generation jobs | 2 |

The existing storyline-level `regenerate` stays but is redefined as "regenerate the open
chapter," preserving back-compat for the current detail view's Regenerate button.

## Webapp UI (Phase 1 portion)

- **`StorylineDetailView`** gains a **left chapter rail**: list of chapters (title, date
  range, open/closed badge), latest selected by default, deep-linkable via `?chapter=<seq>`.
  The existing `StorylineCurationList` | `StorylineNarrative` two-panel reader renders the
  selected chapter, lazy-fetching panels via the single-chapter endpoint. The citation
  registry is built **per chapter** so numbering restarts at [1] in each chapter.
- **Pinia `storylines` store:** add `chapters`, `currentChapter`, `loadChapter`,
  `regenerateChapter`, `renameChapter`. Extend `types/storyline.ts` with a
  `StorylineChapterSummary` type and a `chapters` field on `StorylineDetail`.
- Single-chapter storylines (everything post-migration) render exactly like today, just
  with a one-item rail — so the change is non-disruptive until chapters are actually cut.

Phase 2 adds the `ChapterTimelineEditor` (density timeline + draggable cuts), the "Review
chapters" entry point, and the "time to cut a new chapter" nudge banner.

## Build sequence

**Phase 1 — this spec:**

1. **Migration 0030 + repository.** New table, panel FK move, backfill one open chapter per
   storyline. New `storyline_repository` methods (`list_chapters`, `get_chapter`,
   `create_chapter`, `rename_chapter`, panel access by `chapter_id`). Migration + repo tests.
2. **Generation by chapter.** Thread `chapter_id` + chapter window through
   `StorylineGenerationService`; per-chapter embedding; open-chapter append. Service tests.
3. **API read paths + single-chapter regenerate/rename + MCP parity.** Route tests.
4. **Webapp read paths.** Chapter rail, lazy panel load, per-chapter citation registry,
   store + types. Component/store tests; coverage ≥85%.

**Phase 2 — follow-up spec:** suggestion engine (`suggest` + `PUT chapters` commit/diff),
`ChapterTimelineEditor`, extension→open-chapter wiring, "suggest a cut" nudge.

## Testing & docs

- **Server (pytest):** migration on prod-shaped state (re-runnable, partial-failure safe);
  repository chapter CRUD + one-open-chapter invariant; per-chapter generation (replace for
  closed, append for open); API read paths + regenerate/rename. Unit tier stays green with
  in-memory SQLite.
- **Webapp (Vitest):** chapter rail rendering + selection, lazy panel load, per-chapter
  citation numbering, store actions. Coverage ≥85% on statements/branches/functions/lines.
- **Docs:** update `server/docs/` (storylines) and `webapp/docs/` for the chapters model and
  the API-contract change; dated journal entries in `server/journal/` and `webapp/journal/`.

## Out of scope (this spec)

- Suggestion engine, timeline editor, nudge banner (Phase 2).
- Per-chapter anchors (explicitly rejected — anchors stay storyline-level).
- Manual reordering/merging of already-generated chapters beyond what `PUT chapters`
  provides in Phase 2.
- URL rename, fitness-page, and dashboard-tooltip items from the original notes (separate
  work streams).
```
