# Storylines

**Status:** active reference. Last updated 2026-06-13 (Phase 1 of storyline
chapters shipped server-side: chapters data model, per-chapter generation, and
the chapter-aware API/MCP surface).

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
created so the auto-kicked generation job has a chapter to write into). Phase 1
ships **reading + per-chapter generation**; cutting a storyline into multiple
chapters (the "suggest a cut" boundary engine + draggable timeline editor) is
Phase 2 — see the [spec](./superpowers/specs/2026-06-13-storyline-chapters-design.md).

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
The steps below describe a single chapter's generation:

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

1. **Entity overlap** (deterministic). If *any* of the storyline's anchor entity ids appears in the entry's extracted mentions, return `yes` immediately. Zero LLM calls.
2. **Surface form + LLM decider**. If *any* anchor entity's `canonical_name` is in the entry text (case-insensitive), call Haiku via `providers/storyline_extension_decider.AnthropicStorylineExtensionDecider` with a `record_decision` tool. Returns `yes`/`no`/`maybe` with one-sentence reasoning. On API failure or malformed response, the decider returns `maybe` so the entry surfaces for manual review.
3. **No match**. Neither signal fires — definite `no`, no LLM call.

The classifier records `last_extension_check_at` on every storyline it inspects, not just the matches.

## Ingestion hook

`JobRunner._queue_post_ingestion_jobs`, called from the text/image/audio ingest paths, queues a `storyline_extension_check` job alongside the existing `mood_score_entry` + `entity_extraction` jobs — but only when the classifier service is wired AND the entry has a known `user_id` (storylines are user-scoped).

The `run_storyline_extension_check` worker:

* Calls the classifier
* For each `yes` decision, queues a `storyline_generation` job via `JobRunner.submit_storyline_generation`
* Records the classifications (including reasoning) on the job's result blob
* Notifies only on failure (per-ingestion success notifications would be noisy)

## REST API

Read-side (`api/storylines.py`):

* `GET /api/storylines` — paginated list (standard `{items, total, limit, offset}` envelope), filterable by `status`
* `GET /api/storylines/{id}` — storyline + a `chapters[]` summary array (each: `id`, `storyline_id`, `seq`, `title`, `start_date`, `end_date`, `state`, `last_generated_at`, `citation_count` — **no** panel bodies) **plus** a back-compat `panels` field = the **open chapter's** panels as `{curation: {...}, narrative: {...}}`. The `panels` shim keeps the not-yet-updated webapp rendering and is **temporary** — it is removed once the webapp reads chapters directly (Phase 2).
* `GET /api/storylines/{id}/chapters/{cid}` — one chapter's two panels (`{..chapter fields.., panels: {curation, narrative}}`), lazy-loaded on rail click. 404 if the chapter isn't found or doesn't belong to the storyline.

Write-side (`api/storylines_write.py`):

* `POST /api/storylines` — body `{entity_ids: list[int], name, description?, start_date?, end_date?}`. `entity_ids` must have 1..15 entries (server cap = `MAX_ANCHORS`); duplicates are coalesced. 201 on success with `{..., anchors: [{id, canonical_name}, ...], generation_job_id}`, 409 if a storyline with the same name and the exact same anchor set already exists for this user, 400/422 on bad input. The server also auto-kicks generation and surfaces the `generation_job_id` so the client can poll without a second round-trip. **It also seeds a seq-1 open chapter** (title = the storyline name) so the auto-kicked job has a chapter to write panels into.
* `PATCH /api/storylines/{id}` — body `{name: str}`. Updates editable metadata (currently only the title). The name is trimmed; empty after trimming → 400. 200 with the updated storyline summary (`{id, name, anchors, ...}`); 404 if the storyline doesn't belong to the caller. Metadata-only: a rename does **not** touch the stored panels or kick a regeneration, so the curated/narrative text survives.
* `PUT /api/storylines/{id}/anchors` — body `{entity_ids: list[int]}`. Set-replacement of the storyline's anchors (1..15). 200 with the updated `anchors` list; 404 if the storyline doesn't belong to the caller; 422 on empty/oversized input.
* `POST /api/storylines/{id}/regenerate` — body is optional `{start_date?, end_date?, mode?}` where `mode ∈ {"replace", "append"}` (default `"replace"`). Redefined for chapters: this regenerates the storyline's **open** chapter (the service resolves it). Append requires `start_date >= last_generated_at`; 400 on violation. Queues a `storyline_generation` job; 202 with `{"job_id"}`. Back-compat for the current detail view's Regenerate button.
* `POST /api/storylines/{id}/chapters/{cid}/regenerate` — regenerate a **single** chapter. Always `mode="replace"` (the chapter's own window is authoritative). Queues a `storyline_generation` job with the `chapter_id`; 202 with `{"job_id"}`. 404 if the chapter doesn't belong to the storyline.
* `PATCH /api/storylines/{id}/chapters/{cid}` — body `{title: str}`. Renames a chapter (metadata-only — does **not** touch panels or kick a regeneration). 200 with the chapter dict; 400 on empty/malformed title; 404 if the chapter isn't found for this storyline/user.
* `DELETE /api/storylines/{id}` — removes the storyline (CASCADE drops its chapters, panels, and anchors).

All routes return 503 when the storylines feature is not configured on this server (missing `ANTHROPIC_API_KEY`).

## MCP tools

In `mcp_server/tools/storylines.py`:

* `journal_storylines_guide` — zero-param Markdown overview of the feature and the other tools. Works even without `ANTHROPIC_API_KEY` (no model call). Designed as the "read me first" tool for a fresh MCP client.
* `journal_list_storylines` — text-formatted list (`readOnlyHint`).
* `journal_get_storyline` — detail view with both panels printed inline (`readOnlyHint`). Also lists the storyline's chapters (id, seq, title, date range, state) so a client can pick a `chapter_id` to regenerate.
* `journal_create_storyline` — seed a storyline; takes `entity_ids: list[int]` (1..15). Refuses with a 409-equivalent message if a storyline with the same name and the exact same anchor set already exists. Auto-kicks generation and polls until the job reaches a terminal state (default 120s), falling back to a "still running, job id …" message on timeout.
* `journal_set_storyline_anchors` — set-replacement of an existing storyline's anchors. Takes `entity_ids: list[int]` (1..15). Sibling of the REST `PUT /api/storylines/{id}/anchors`.
* `journal_regenerate_storyline` — queues a regeneration job (`idempotentHint`). Accepts optional `start_date`, `end_date`, and `mode` (`replace` / `append`). Also accepts an optional **`chapter_id`** to regenerate one specific chapter (the chapter's own window is authoritative, replace mode); without it, the storyline's open chapter is regenerated. Polls until terminal (default 120s).
* `journal_delete_storyline` — removes the storyline (`destructiveHint`). Cascades to panels and anchors.

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

## Providers

* `AnthropicStorylineNarrator` — Citations API with one `source="text"` document per entry; two-breakpoint caching (1h system, 5m corpus). Tested via canned response fakes; the parser handles missing or unknown `document_index`, plain text blocks without citations, and tool_use blocks (ignored).
* `AnthropicStorylineGlue` — Haiku batched call; the response parser accepts plain JSON, fenced-code-block JSON, and JSON embedded in prose. Deterministic fallback on failure.
* `AnthropicStorylineExtensionDecider` — Haiku tool-use (`record_decision` tool). `maybe` fallback on any non-happy path.

* `tests/test_storyline_repository.py` — CRUD, panel upsert, dated mentions query, segments helpers, plus multi-anchor `TestStorylineCRUD` + `TestAnchors` covering create-with-list, exact-set find, `set_anchors`, `add_anchor`, `remove_anchor`, `list_storylines_with_anchor`, and cascade behavior.
* `tests/test_storyline_generation.py` — citation parser, glue parser, FTS fallback, end-to-end service with fake providers, append-mode (`TestAppendMode` including future-stamped `last_generated_at` to exercise the boundary), and per-chapter generation (`regenerate_chapter` core + `regenerate` open-chapter delegation).
* `tests/test_migration_0030_chapters.py` — the 0030 rebuild on prod-shaped state: fresh DB, backfill of one open chapter per storyline, panel re-key onto `chapter_id`, forward-only re-run no-op, and prod data anomalies (NULL dates, 0/1 panels, archived storylines).
* `tests/test_storyline_jobs.py` — worker + classifier + decider + JobRunner integration, including append-mode param plumbing.
* `tests/test_api_storylines.py` + `tests/test_api_storylines_write.py` — REST endpoints with TestClient: multi-anchor create, `anchors` in responses, `chapters[]` + back-compat `panels` shim on detail, single-chapter `GET`/regenerate/rename routes, `PUT /anchors` success / 404 / empty-rejection, regenerate body variants, `mode=append` validation.
* `tests/test_mcp_tools_storylines.py` — `TestStorylinesGuide`, `TestDeleteStoryline`, `TestSetStorylineAnchors`, `TestCreateStoryline` (timeout fallback, not-configured, soft-fail), plus chapter listing in `journal_get_storyline` and the `chapter_id` param on `journal_regenerate_storyline` (including the cross-storyline chapter 404 path).
* Migration: `TestStorylineEntitiesMigration` covers the 0028 rebuild — fresh DB, prod-shaped backfill, dirty-fixture re-run, cascade delete, PK duplicate rejection, user_version check.

Real Anthropic API calls are never made in tests — providers accept an injected `client=` to receive a fake.

## Related docs

* [`superpowers/specs/2026-06-13-storyline-chapters-design.md`](./superpowers/specs/2026-06-13-storyline-chapters-design.md) — chapters design + locked decisions + Phase 2 deferral (active)
* [`superpowers/plans/2026-06-13-storyline-chapters-phase1.md`](./superpowers/plans/2026-06-13-storyline-chapters-phase1.md) — Phase 1 task-by-task implementation plan
* [`archive/storylines-plan.md`](./archive/storylines-plan.md) — original design plan with decisions and tradeoffs (closed 2026-05-12)
* [`archive/storylines-2026-05-mcp-and-append.md`](./archive/storylines-2026-05-mcp-and-append.md) — MCP discoverability + append-mode follow-up (closed 2026-05-12)
* [`entity-tracking.md`](./entity-tracking.md) — entity store this feature is anchored on
* [`mood-scoring.md`](./mood-scoring.md) — precedent for LLM-output baked into service data
* [`jobs.md`](./jobs.md) — job runner this feature plugs into
* [`architecture.md`](./architecture.md) — high-level service architecture
