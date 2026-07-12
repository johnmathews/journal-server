# Storylines

**Status:** active reference. Last updated 2026-06-15 (Phase A of chapter
editing shipped: add, split, merge, update window, delete; 5 new REST endpoints
+ 5 MCP tools).

A storyline is a synthesized cross-entry narrative anchored on one or
more entities. Multi-entity anchors are an equal-weight set: an entry
that mentions any anchor contributes once; the narrator sees a single
unioned corpus, not per-anchor sub-narratives.

As of Phase 1, a storyline is no longer a single linear document. It is split
into **chapters** — self-contained short stories over date sub-ranges
(`storylines ─1:N─▶ storyline_chapters ─1:2─▶ storyline_panels`). Each chapter
owns its own two panels and is generated independently over its own window, so
the system never re-reads the whole history. Anchors stay storyline-level
(shared across all chapters). See [Chapters](#chapters) below; the design
rationale and the Phase 2 deferral are in
[`superpowers/specs/2026-06-13-storyline-chapters-design.md`](./superpowers/specs/2026-06-13-storyline-chapters-design.md).

Two parallel panels are rendered for each chapter:

* **Curation panel** — chronologically-ordered verbatim excerpts from journal entries that mention any of the storyline's anchor entities, separated by minimal Haiku-generated transition prose ("Three days later:").
* **Narrative panel** — a flowing third-person prose narrative grounded via the Anthropic Citations API. Pointers from narrative text back to source entries are parsed by Anthropic from custom-content documents, so they cannot be fabricated.

This document describes how the feature works in code. The design plan and tradeoffs live in [`archive/storylines-plan.md`](./archive/storylines-plan.md) (closed 2026-05-12); the MCP discoverability + append-mode follow-up cycle is at [`archive/storylines-2026-05-mcp-and-append.md`](./archive/storylines-2026-05-mcp-and-append.md) (closed 2026-05-12).

## Data model

Migrations `0027_storylines.sql` (initial schema),
`0028_storyline_entities.sql` (multi-entity anchors), and
`0030_storyline_chapters.sql` (chapters; panel FK moves down a level). Four
tables:

* `storylines` — one row per storyline. Key columns:
  * No `entity_id` column — anchors live in `storyline_entities`.
  * No DB-level UNIQUE on `(user, name)`. Two storylines can share a
    name if they have different anchor sets; application-level dedup
    (see `SQLiteStorylineRepository.find_by_anchor_set`) prevents
    exact-set + same-name collisions and returns 409.
  * `start_date` / `end_date` — optional ISO date bounds; when null, the service uses the last `STORYLINE_DEFAULT_WINDOW_DAYS` days.
  * `summary_embedding_json` — **deprecated** as of 0030: the embedding
    now lives per-chapter on `storyline_chapters.summary_embedding_json`.
    The column is kept (no longer read) for one release to keep the
    migration re-runnable.
  * `last_generated_at`, `last_extension_check_at` — observability timestamps.
    `last_generated_at` is the value the storylines-list UI displays; it is
    bumped whenever any chapter regenerates (`record_chapter_generation_complete`
    updates both the chapter row and its parent storyline), so the list reflects
    fresh content rather than showing a stale date after a regeneration.
* `storyline_entities` — join table for multi-entity anchors. PK is
  `(storyline_id, entity_id)` (no duplicate anchors). FK to
  `storylines(id) ON DELETE CASCADE`, FK to `entities(id)`. A reverse
  index on `entity_id` powers the extension classifier's
  `list_storylines_with_anchor` lookup. Soft cap of 15 anchors per
  storyline is enforced in service code (`MAX_ANCHORS`). **Anchors are
  storyline-level — shared across all chapters. There is no per-chapter
  anchor override.**
* `storyline_chapters` — one row per chapter (see [Chapters](#chapters)).
  FK to `storylines(id) ON DELETE CASCADE`. `UNIQUE(storyline_id, seq)`
  (1-based order) plus a **partial unique index** `WHERE state='open'`
  that enforces at most one open chapter per storyline.
* `storyline_panels` — one row per `(chapter_id, panel_kind)`. Panels are split so the curation pass doesn't rewrite the narrative when only the glue is iterated. **As of 0030 the FK is `chapter_id`, not `storyline_id`** — panels belong to a chapter, not directly to a storyline.

The `segments_json` column on `storyline_panels` is a list of dicts in one of two shapes (see `services/storylines/segments.py`):

```python
{"kind": "text",     "text": "..."}
{"kind": "citation", "entry_id": 42, "quote": "..."}
```

The webapp renders text runs as plain text and citations as `<RouterLink :to="/entries/${entry_id}">` links. No markdown is involved on the wire.

## Chapters

A storyline is split into chapters — self-contained narratives over date
sub-ranges. `storyline_chapters` columns:

* `id`, `storyline_id` (FK, CASCADE), `seq` (1-based order within the storyline).
* `title` — chapter title; defaults to the storyline name for the seed chapter.
* `start_date` / `end_date` — ISO `YYYY-MM-DD` bounds, nullable. The open
  chapter's `end_date` is typically NULL (open-ended).
* `state` — `'open'` or `'closed'`. A **partial unique index** enforces at
  most one open chapter per storyline. The most recent chapter is `open` and
  grows via append; closed chapters are frozen.
* `last_generated_at` — per-chapter observability timestamp.
* `summary_embedding_json` — per-chapter narrative embedding (moved down from
  the storyline row in 0030).
* `title_locked` — `1` once the user manually renames the chapter. Re-segment
  (below) never overwrites a locked title. Added in **migration 0031**.
* `boundary_locked` — `1` once the user hand-paints the window (creates, splits,
  or date-edits the chapter). Re-segment never carves into a hand-painted
  chapter; it only re-sections the unlocked spans around it. Added in **0031**.
* `narrative_word_count` — cached word count of the chapter's narrative prose,
  used to size chapters and to fire the ingest-time auto-split. Added in **0031**.

**Migration 0030** (`0030_storyline_chapters.sql`) creates the table and
re-keys `storyline_panels` from `storyline_id` to `chapter_id`. For each
existing storyline it backfills **one** open chapter (`seq=1`, title = the
storyline name, dates + embedding copied from the storyline row) and re-points
that storyline's two panels to it. So every pre-existing storyline becomes a
single-chapter storyline with no data loss. The migration is forward-only and
re-runnable: the chapters-table create and the backfill are independently
idempotent (`CREATE … IF NOT EXISTS`, `NOT EXISTS` guard on the backfill); only
the panel rebuild — which SQLite forces because it can't drop the old
`UNIQUE(storyline_id, panel_kind)` constraint in place — runs inside an explicit
atomic transaction, so a partial failure rolls back just the rebuild and a
re-run completes cleanly.

New chapters are seeded on `POST /api/storylines` (a seq-1 open chapter is
created so the auto-kicked generation job has a chapter to write into). Phase A
(manual editing) ships with the chapter-editing feature; the LLM suggestion
engine + draggable timeline editor (Phase B) is deferred — see the
[chapter-editing design spec](./superpowers/specs/2026-06-15-storyline-chapter-editing-design.md).

### Chapter editing invariants

These invariants are enforced by every structural edit:

- `seq` is 1-based and contiguous within a storyline; `seq` order equals date order.
- Exactly one chapter has `state = 'open'` (enforced by a partial unique index); it is the highest-`seq` chapter and has `end_date IS NULL`.
- Closed chapters have both `start_date` and `end_date` set.
- Dates are inclusive `YYYY-MM-DD` days. Adjacent closed chapters are contiguous when `chapter[n].end_date == chapter[n+1].start_date - 1 day`.
- By default the chapter set is gapless and non-overlapping. `allow_gap = true` on an edit/delete permits a gap; overlaps are rejected unconditionally.
- Every structural edit automatically enqueues per-chapter regeneration jobs for each affected chapter.

Re-sequencing during split and delete runs inside a single transaction using a temporary negative-offset pass to avoid UNIQUE constraint collisions.

### Sectioned (word-sized) chapters

Chapters can be **automatically carved** into titled, word-sized units instead of
being painted by hand. This is **re-segmentation** and it is opt-in / triggered,
never the default refresh:

- **Splitting is deterministic, not model-driven.** The sectioning narrator
  returns a single 1,500–1,700-word section per storyline no matter how it is
  prompted or which model runs it (verified on prod with Opus 4.7 and 4.8) — it
  treats a storyline as one coherent topic and won't self-split. So
  `resegment_storyline` splits by **time-bucketing**: it estimates the
  storyline's total narrative length from the cached `narrative_word_count` of
  the chapters it's replacing, computes a target chapter count
  (`round(est_words / max_chapter_words)`, capped at `_MAX_CHAPTERS`), splits the
  date-ordered excerpts into that many **contiguous buckets**
  (`_split_excerpts_contiguous`, snapping boundaries so a same-date run is never
  split across chapters), and calls `generate_sectioned_narrative` **once per
  bucket**. Each bucket's section becomes its own chapter with a model-written
  title. When the estimate is unknown (0), one narration measures it and is
  reused when the result is a single chapter (no extra call).
- Each section's date window is **derived** from the min/max `entry_date` of its
  citations, then clamped so the resulting chapters tile the span contiguously.
  Sections are chronological, so they map cleanly onto the existing
  open/closed/contiguous-window model.
- A transient per-bucket narrator failure (zero sections) aborts the span so the
  existing chapters are **preserved untouched** — a flaky API call never wipes
  good data.
- `resegment_storyline(storyline_id, override_locked=False)` re-carves
  **per unlocked span** (the maximal runs of non-`boundary_locked`
  chapters), then rebuilds the chapter rows atomically. `boundary_locked`
  chapters are preserved untouched (id, window, title, panels); a new section
  inherits a `title_locked` title when its window overlaps a locked chapter by a
  majority of days. `override_locked=True` ignores the locks and re-carves the
  whole timeline.
- The atomic rebuild (`SQLiteStorylineRepository.rebuild_chapters`) avoids
  transient invariant violations: close all rows, offset their `seq` out of the
  target range, delete the non-preserved rows, place every spec at its final
  `seq` (all closed), then promote exactly the final chapter to `open` as the
  last statement — so the single-open partial index only ever sees zero→one open.

Word count is a **soft** target: out-of-band sections are logged but never
rejected, and there is no word-count badge in the UI.

## Generation pipeline

Generation is **per chapter**. `regenerate_chapter(chapter_id, mode="replace")`
is the core: it resolves the chapter and its parent storyline, uses the
**chapter's** `start_date`/`end_date` as the window (in `replace` mode the
chapter window is authoritative — explicit overrides are ignored), resolves the
storyline-level anchors, writes both panels keyed on `chapter_id`, and stamps
`last_generated_at` + `summary_embedding_json` on the chapter row.

`regenerate(storyline_id)` is a back-compat wrapper: it resolves the
storyline's single **open** chapter (raising if there is none) and delegates to
`regenerate_chapter`. The open chapter still supports `mode="append"` for
incremental growth as new entries arrive; closed chapters are `replace`-only.
This is the **refresh** path — it rebuilds existing chapters' panels over their
current windows and changes no boundaries, titles, or chapter count.
`resegment_storyline` (see *Sectioned chapters* above) is the separate,
opt-in **re-carve** path. `regenerate(..., auto_split=True)` — set only by the
ingest hook — re-checks the open chapter's `narrative_word_count` after a
refresh and triggers a one-shot `resegment_storyline` when it exceeds
`STORYLINE_CHAPTER_MAX_WORDS` (resegment never calls back into regenerate, so
there is no loop). The steps below describe a single chapter's generation:

1. Resolve the chapter and its parent storyline; resolve the (start_date, end_date) window (the chapter's own bounds, or the default 90-day window when null).
2. Resolve the anchor set via `SQLiteStorylineRepository.list_anchors(storyline_id)` (sorted by `entity_id` ASC for determinism). Fetch dated entity excerpts per anchor via `SQLiteEntityStore.get_dated_entity_excerpts`. Union across anchors, deduplicate on `entry_id` (an entry that mentions multiple anchors contributes one excerpt, not N), sort by `entry_date` ASC.
3. If fewer than `STORYLINE_FTS_FALLBACK_THRESHOLD` mention-driven excerpts are returned, run **FTS5 fallback** per anchor: search journal entries for each anchor's canonical name in the date window, union across anchors, deduplicate against the entity-mention set, attach a context snippet (±120 chars around the surface form) as the "quote". The fallback catches pronominal mentions ("my son" → Atlas) and gaps from entries ingested before auto-reextraction shipped.
4. Build the narrator's input: one `source="text"` document per excerpt. Each document's `data` is the entry's `final_text`; the entry id and date live in the document's `title` (`Entry N (YYYY-MM-DD)`), which the model can see but cannot cite from. Citations enabled. The Anthropic API auto-chunks each document at sentence boundaries.
5. Call the narrator (`providers/storyline_narrator.AnthropicStorylineNarrator`). System prompt restricts to provided documents, forbids invention, permits "I don't know". Cache control breakpoints: 1h TTL on the system prompt, 5m TTL on the document corpus (`cache_control` attaches to the last document only — a single breakpoint covering every preceding document, well under the four-breakpoint request limit).
6. Parse the response. Each text block with attached citations becomes a `text` segment followed by one `citation` segment per cited source. Citations carry the `char_location` shape; we map `document_index` back to `entry_id` via the index → entry map we built in step 4, and use `cited_text` (a sentence-level excerpt) as the citation's `quote`.
7. Call the glue (`providers/storyline_glue.AnthropicStorylineGlue`). One batched request returns N-1 transition phrases as a JSON array. On API failure or malformed response, fall back to deterministic gap-bucketed phrases (`"Two weeks later:"`).
8. Build the curation panel by interleaving verbatim quotes (or FTS snippets) with transitions.
9. Persist both panels via `SQLiteStorylineRepository.upsert_panel` (keyed on `chapter_id`).
10. If an embedder is wired, embed the narrative text and store it on `storyline_chapters.summary_embedding_json` (per-chapter, not on the storyline row).
11. Record `last_generated_at` on the chapter row.

## Extension classifier

`services/storylines/extension.StorylineExtensionClassifier.classify_for_entry(entry_id, user_id)` iterates the user's active storylines and returns one `ExtensionResult` per storyline. Pipeline per storyline:

1. **Entity overlap** (deterministic). If *any* of the storyline's anchor entity ids appears in the entry's extracted mentions, return `yes` immediately. Zero LLM calls. This is the primary, reliable signal — it depends on the entry's entity mentions already being committed, which the ingestion hook now guarantees (see below).
2. **Surface form + LLM decider**. If *any* anchor entity's `canonical_name` is in the entry text (case-insensitive), call Haiku via `providers/storyline_extension_decider.AnthropicStorylineExtensionDecider` with a `record_decision` tool. Returns `yes`/`no`/`maybe` with one-sentence reasoning. On API failure or malformed response, the decider returns `maybe` so the entry surfaces for manual review.
3. **Embedding fallback** (optional, W6). When neither of the above fires, if an `embedder` is wired and the storyline has a `summary_embedding`, compare the entry embedding to it (cosine). At/above `STORYLINE_EXTENSION_RELEVANCE_THRESHOLD` the entry escalates to the same Haiku decider (stage `embedding_llm`) instead of an outright `no`. Catches semantically-related entries where the anchor name never appears verbatim (paraphrase, pronouns). The entry is embedded once per `classify_for_entry`, not once per storyline. Skipped entirely when no embedder is wired or the storyline has no summary embedding.
4. **No match**. No signal fires — definite `no`, no LLM call.

The classifier records `last_extension_check_at` on every storyline it inspects, not just the matches.

## Ingestion hook

The `storyline_extension_check` is queued by the **entity-extraction worker** (`services/jobs/workers/entity_extraction.py`), on the single-entry path, **after** it commits the entry's mentions — via `JobRunner._maybe_queue_storyline_extension_check`. This ordering is deliberate: the classifier's Stage-1 entity-overlap signal reads the just-written mentions, so it is reliable. (Before W1 the check was queued as a *concurrent sibling* of entity extraction on a separate pool; on a burst ingest it raced ahead of extraction, read an empty mention set, and classified everything `no` — the cause of a month of entries updating zero storylines.) Because every ingestion path (text/file/image/audio) queues entity extraction, all of them now trigger storyline updates. The hook no-ops when the classifier isn't wired and logs — never silently drops — when the entry has no known `user_id`.

The `run_storyline_extension_check` worker:

* Calls the classifier
* For each `yes` decision, queues a `storyline_generation` job via `JobRunner.submit_storyline_generation` **with `auto_split=True`**, so a growing open chapter that crosses `STORYLINE_CHAPTER_MAX_WORDS` is automatically re-segmented (hand-painted chapters stay put — the ingest path never sets `override_locked`)
* **Coalesces** (W4): before queuing, it skips storylines that already have a *queued* full-refresh `storyline_generation` job (`jobs_repository.find_pending_open_regeneration`). A burst of entries all matching one storyline produces a single refresh, not one per entry — important on the single-worker storyline pool. Coalesced ids are recorded on the job result as `coalesced_storyline_ids`.
* Records the classifications (including reasoning) on the job's result blob
* Notifies only on failure (per-ingestion success notifications would be noisy)

## Maintenance CLI

Two one-off commands recover storylines that missed live updates:

* `journal recheck-storylines --since YYYY-MM-DD [--user-id N] [--execute]` — re-runs the classifier over every entry since a date and regenerates each matched storyline (coalesced, synchronous). Dry-run by default. Use this to catch up storylines for entries ingested while auto-extension wasn't firing.
* `journal backfill-storyline-chapters [--user-id N] [--storyline-id N] [--execute] [--include-multichapter] [--override-locked]` — re-sections existing storylines into word-sized chapters via `resegment_storyline`. Fixes storylines generated before the chapter feature (migrations 0030/0031) that are stuck as one long chapter; nothing re-carves them automatically. Dry-run by default; skips already-multichapter storylines unless `--include-multichapter`.

## REST API

Read-side (`api/storylines.py`):

* `GET /api/storylines` — paginated list (standard `{items, total, limit, offset}` envelope), filterable by `status`
* `GET /api/storylines/{id}` — storyline + a `chapters[]` summary array (each: `id`, `storyline_id`, `seq`, `title`, `start_date`, `end_date`, `state`, `last_generated_at`, `citation_count` — **no** panel bodies) **plus** a back-compat `panels` field = the **open chapter's** panels as `{curation: {...}, narrative: {...}}`. The `panels` shim keeps the not-yet-updated webapp rendering and is **temporary** — it is removed once the webapp reads chapters directly (Phase 2).
* `GET /api/storylines/{id}/chapters/{cid}` — one chapter's two panels (`{..chapter fields.., panels: {curation, narrative}}`), lazy-loaded on rail click. 404 if the chapter isn't found or doesn't belong to the storyline.

Write-side (`api/storylines_write.py`):

* `POST /api/storylines` — body `{entity_ids: list[int], name, description?, start_date?, end_date?}`. `entity_ids` must have 1..15 entries (server cap = `MAX_ANCHORS`); duplicates are coalesced. 201 on success with `{..., anchors: [{id, canonical_name}, ...], generation_job_id}`, 409 if a storyline with the same name and the exact same anchor set already exists for this user, 400/422 on bad input. The server also auto-kicks generation and surfaces the `generation_job_id` so the client can poll without a second round-trip. **It also seeds a seq-1 open chapter** (title = the storyline name) so the auto-kicked job has a chapter to write panels into.
* `PATCH /api/storylines/{id}` — body `{name: str}`. Updates editable metadata (currently only the title). The name is trimmed; empty after trimming → 400. 200 with the updated storyline summary (`{id, name, anchors, ...}`); 404 if the storyline doesn't belong to the caller. Metadata-only: a rename does **not** touch the stored panels or kick a regeneration, so the curated/narrative text survives.
* `PUT /api/storylines/{id}/anchors` — body `{entity_ids: list[int]}`. Set-replacement of the storyline's anchors (1..15). 200 with the updated `anchors` list; 404 if the storyline doesn't belong to the caller; 422 on empty/oversized input.
* `POST /api/storylines/{id}/regenerate` — body is optional `{start_date?, end_date?, mode?, resegment?, override_locked?}`. By default (no `resegment`) `mode ∈ {"replace", "append"}` (default `"replace"`) regenerates the storyline's **open** chapter (the service resolves it); append requires `start_date >= last_generated_at` (400 on violation). With **`resegment: true`** the storyline is re-carved into titled word-sized chapters (`resegment` is incompatible with `mode="append"`); **`override_locked: true`** (only with `resegment`) additionally re-carves across hand-painted chapters. Non-boolean `resegment`/`override_locked` → 400. Queues a `storyline_generation` job; 202 with `{"job_id"}`.
* `POST /api/storylines/{id}/chapters/{cid}/regenerate` — regenerate a **single** chapter. Always `mode="replace"` (the chapter's own window is authoritative). Queues a `storyline_generation` job with the `chapter_id`; 202 with `{"job_id"}`. 404 if the chapter doesn't belong to the storyline.
* `PATCH /api/storylines/{id}/chapters/{cid}` — two modes depending on the body:
  * **Rename-only** (back-compat): body `{title: str}`. Metadata-only — no panel change, no regeneration. 200 with the flat chapter dict. 400 on empty/malformed title.
  * **Date-edit** (new): body `{start_date?: ISO, end_date?: ISO, allow_gap?: bool}` (with optional `title`). Ripples the shared edge of the adjacent neighbor to stay contiguous unless `allow_gap=true`. Returns 200 with `{"chapters": [<affected...>], "job_ids": [...]}`. Overlaps always rejected; open chapter's `end_date` cannot be set. 400 on invalid values.
  * Both `title` and date fields may be combined; rename executes first, window update follows.
  * 404 if the chapter isn't found for this storyline/user. 503 if storylines aren't wired.
* `POST /api/storylines/{id}/chapters` — add a chapter. Body `{start_date: ISO, end_date?: ISO}`.
  * **New-latest flavor** (omit `end_date`): closes the current open chapter at `start_date - 1` and opens a new chapter `[start_date, NULL)` as the new highest `seq`. Both the closed former-open chapter and the new chapter regenerate.
  * **Ranged flavor** (`end_date` present): inserts a closed chapter into a currently-uncovered date range. The range must not overlap an existing chapter. The new chapter regenerates.
  * Returns 201 with `{"chapter": <chapter dict>, "job_ids": [...]}`. 400 on missing/invalid body or if the repo rejects the window (overlap, etc.). 404 if the storyline isn't found for this user. 503 if storylines aren't wired.
* `POST /api/storylines/{id}/chapters/{cid}/split` — body `{date: ISO}`. Splits the chapter into two contiguous halves: left `[start, date-1]` closed, right `[date, end]` (open if source was open). Later chapters shift `seq` up by one. Both halves enqueued for regeneration. Returns 200 with `{"chapters": [left, right], "job_ids": [...]}`. 400 if `date` is outside the chapter window or body is missing. 404 if storyline or chapter not found. 503 if not wired.
* `POST /api/storylines/{id}/chapters/merge` — body `{chapter_ids: list[int]}` (at least 2 IDs). The IDs must be adjacent (contiguous `seq` run) and all belong to this storyline. Produces one chapter spanning the union of their windows, keeping the lowest `seq` and earliest title. Result is `open` if any input was open. Tail chapters shift `seq` down. Returns 200 with `{"chapter": <merged chapter dict>, "job_ids": [...]}`. 400 if `chapter_ids` invalid or non-contiguous. 404 if storyline or any chapter not owned by caller. 503 if not wired.
* `DELETE /api/storylines/{id}/chapters/{cid}` — optional body `{allow_gap?: bool}`. By default absorbs the deleted range into the previous neighbor (extends its `end_date`; promotes it to `open` if the deleted chapter was `open`). With `allow_gap=true` the range is left empty. Rejects deleting the only chapter. Returns 200 with `{"deleted": true, "job_ids": [...]}`. 400 if attempting to delete the last chapter. 404 if storyline or chapter not found. 503 if not wired.
* `DELETE /api/storylines/{id}` — removes the storyline (CASCADE drops its chapters, panels, and anchors).

All routes return 503 when the storylines feature is not configured on this server (missing `ANTHROPIC_API_KEY`).

## MCP tools

In `mcp_server/tools/storylines.py`:

* `journal_storylines_guide` — zero-param Markdown overview of the feature and the other tools. Works even without `ANTHROPIC_API_KEY` (no model call). Designed as the "read me first" tool for a fresh MCP client.
* `journal_list_storylines` — text-formatted list (`readOnlyHint`).
* `journal_get_storyline` — detail view with both panels printed inline (`readOnlyHint`). Also lists the storyline's chapters (id, seq, title, date range, state) so a client can pick a `chapter_id` to regenerate.
* `journal_create_storyline` — seed a storyline; takes `entity_ids: list[int]` (1..15). Refuses with a 409-equivalent message if a storyline with the same name and the exact same anchor set already exists. Auto-kicks generation and polls until the job reaches a terminal state (default 120s), falling back to a "still running, job id …" message on timeout.
* `journal_set_storyline_anchors` — set-replacement of an existing storyline's anchors. Takes `entity_ids: list[int]` (1..15). Sibling of the REST `PUT /api/storylines/{id}/anchors`.
* `journal_regenerate_storyline` — queues a regeneration job (`idempotentHint`). Accepts optional `start_date`, `end_date`, and `mode` (`replace` / `append`). Also accepts an optional **`chapter_id`** to regenerate one specific chapter (the chapter's own window is authoritative, replace mode); without it, the storyline's open chapter is regenerated. Pass **`resegment=True`** to re-carve the storyline into titled ~200-word chapters (mutually exclusive with `chapter_id`), and **`override_locked=True`** (only with `resegment`) to also re-carve across hand-painted chapters. Polls until terminal (default 120s).
* `journal_delete_storyline` — removes the storyline (`destructiveHint`). Cascades to panels and anchors.

**Chapter editing tools** (Phase A — mirrors the five new REST endpoints):

* `journal_add_storyline_chapter(storyline_id, start_date, end_date=None)` — adds a chapter. Omit `end_date` for a new-latest open chapter; supply it for a closed ranged chapter. Auto-queues regeneration for the new chapter.
* `journal_split_storyline_chapter(storyline_id, chapter_id, date)` — splits the chapter at `date`. Left half ends the day before `date`; right half starts on `date`. If the source was open, the right half stays open. Regeneration queued for both halves.
* `journal_merge_storyline_chapters(storyline_id, chapter_ids)` — merges adjacent chapters into one (contiguous `seq` run, at least 2 IDs). Result is open if any input was open. Regeneration queued for the merged chapter.
* `journal_update_storyline_chapter(storyline_id, chapter_id, title=None, start_date=None, end_date=None, allow_gap=False)` — rename-only if only `title` is supplied (no regeneration); window update (with optional rename) if any date arg is supplied. Ripples the adjacent neighbor unless `allow_gap=True`. Regeneration queued when the window changes.
* `journal_delete_storyline_chapter(storyline_id, chapter_id, allow_gap=False)` — removes a chapter (`destructiveHint`). By default the previous neighbor absorbs the deleted range. With `allow_gap=True` the range is left empty. Regeneration queued for affected neighbors.

Each tool returns an actionable string when the storylines feature isn't configured. MCP clients (Nanoclaw, Claude Code, etc.) can use these tools to seed and read storylines without a webapp.

## Configuration

All env vars are optional; defaults make the feature work out of the box once `ANTHROPIC_API_KEY` is set.

| Env var                                  | Default               | Purpose                                              |
| ---------------------------------------- | --------------------- | ---------------------------------------------------- |
| `ANTHROPIC_API_KEY`                      | (none)                | Gates the entire feature on/off                      |
| `STORYLINE_NARRATOR_MODEL`               | `claude-opus-4-7`     | Model for the narrative panel                        |
| `STORYLINE_NARRATOR_MAX_TOKENS`          | `4096`                | Max output tokens for narrative                      |
| `STORYLINE_GLUE_MODEL`                   | `claude-haiku-4-5`    | Model for curation transitions                       |
| `STORYLINE_EXTENSION_DECIDER_MODEL`      | `claude-haiku-4-5`    | Model for the extension classifier's decider stage   |
| `STORYLINE_DEFAULT_WINDOW_DAYS`          | `90`                  | Default window when storyline has no explicit bounds |
| `STORYLINE_FTS_FALLBACK_THRESHOLD`       | `3`                   | Below this many entity mentions, FTS fallback fires  |
| `STORYLINE_EXTENSION_RELEVANCE_THRESHOLD`| `0.5`                 | Cosine at/above which the classifier's embedding fallback escalates to the decider |
| `STORYLINE_CHAPTER_TARGET_WORDS`         | `210`                 | Target narrative words per sectioned chapter         |
| `STORYLINE_CHAPTER_MIN_WORDS`            | `180`                 | Soft lower bound; out-of-band sections are logged    |
| `STORYLINE_CHAPTER_MAX_WORDS`            | `240`                 | Soft upper bound; also the ingest auto-split trigger |

## Providers

* `AnthropicStorylineNarrator` — Citations API with one `source="text"` document per entry; two-breakpoint caching (1h system, 5m corpus). Tested via canned response fakes; the parser handles missing or unknown `document_index`, plain text blocks without citations, and tool_use blocks (ignored).
* `AnthropicStorylineGlue` — Haiku batched call; the response parser accepts plain JSON, fenced-code-block JSON, and JSON embedded in prose. Deterministic fallback on failure.
* `AnthropicStorylineExtensionDecider` — Haiku tool-use (`record_decision` tool). `maybe` fallback on any non-happy path.

* `tests/test_storyline_repository.py` — CRUD, panel upsert, dated mentions query, segments helpers, plus multi-anchor `TestStorylineCRUD` + `TestAnchors` covering create-with-list, exact-set find, `set_anchors`, `add_anchor`, `remove_anchor`, `list_storylines_with_anchor`, and cascade behavior.
* `tests/test_storyline_generation.py` — citation parser, glue parser, FTS fallback, end-to-end service with fake providers, append-mode (`TestAppendMode` including future-stamped `last_generated_at` to exercise the boundary), and per-chapter generation (`regenerate_chapter` core + `regenerate` open-chapter delegation).
* `tests/test_migration_0030_chapters.py` — the 0030 rebuild on prod-shaped state: fresh DB, backfill of one open chapter per storyline, panel re-key onto `chapter_id`, forward-only re-run no-op, and prod data anomalies (NULL dates, 0/1 panels, archived storylines).
* `tests/test_storyline_jobs.py` — worker + classifier + decider + JobRunner integration, including append-mode param plumbing.
* `tests/test_api_storylines.py` + `tests/test_api_storylines_write.py` — REST endpoints with TestClient: multi-anchor create, `anchors` in responses, `chapters[]` + back-compat `panels` shim on detail, single-chapter `GET`/regenerate/rename routes, `PUT /anchors` success / 404 / empty-rejection, regenerate body variants, `mode=append` validation.
* `tests/test_mcp_tools_storylines.py` — `TestStorylinesGuide`, `TestDeleteStoryline`, `TestSetStorylineAnchors`, `TestCreateStoryline` (timeout fallback, not-configured, soft-fail), plus chapter listing in `journal_get_storyline` and the `chapter_id` param on `journal_regenerate_storyline` (including the cross-storyline chapter 404 path). Phase A tools covered by `TestAddChapter`, `TestSplitChapter`, `TestMergeChapters`, `TestUpdateChapter`, `TestDeleteChapter`.
* Migration: `TestStorylineEntitiesMigration` covers the 0028 rebuild — fresh DB, prod-shaped backfill, dirty-fixture re-run, cascade delete, PK duplicate rejection, user_version check.

Real Anthropic API calls are never made in tests — providers accept an injected `client=` to receive a fake.

## Related docs

* [`superpowers/specs/2026-06-15-storyline-chapter-editing-design.md`](./superpowers/specs/2026-06-15-storyline-chapter-editing-design.md) — Phase A chapter-editing design: locked decisions, operation semantics, API surface (active)
* [`superpowers/plans/2026-06-15-storyline-chapter-editing.md`](./superpowers/plans/2026-06-15-storyline-chapter-editing.md) — Phase A implementation plan
* [`superpowers/specs/2026-06-13-storyline-chapters-design.md`](./superpowers/specs/2026-06-13-storyline-chapters-design.md) — Phase 1 chapters design + locked decisions + Phase B deferral (active)
* [`superpowers/plans/2026-06-13-storyline-chapters-phase1.md`](./superpowers/plans/2026-06-13-storyline-chapters-phase1.md) — Phase 1 task-by-task implementation plan
* [`archive/storylines-plan.md`](./archive/storylines-plan.md) — original design plan with decisions and tradeoffs (closed 2026-05-12)
* [`archive/storylines-2026-05-mcp-and-append.md`](./archive/storylines-2026-05-mcp-and-append.md) — MCP discoverability + append-mode follow-up (closed 2026-05-12)
* [`entity-tracking.md`](./entity-tracking.md) — entity store this feature is anchored on
* [`mood-scoring.md`](./mood-scoring.md) — precedent for LLM-output baked into service data
* [`jobs.md`](./jobs.md) — job runner this feature plugs into
* [`architecture.md`](./architecture.md) — high-level service architecture
