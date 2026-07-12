# Storylines

**Status:** active reference. Last updated 2026-07-12 (draft/published redesign — judge-driven
continue-or-break chaptering with explicit entry membership, replacing the round-1 date-window
chapter model). Design: [`superpowers/specs/2026-07-12-storylines-redesign-design.md`](./superpowers/specs/2026-07-12-storylines-redesign-design.md).

A storyline is a synthesized cross-entry narrative anchored on one or more entities. Multi-entity
anchors are an equal-weight set: an entry that mentions any anchor is a candidate member; there is
no per-anchor sub-narrative.

A storyline is split into **chapters** — arcs with a beginning and an end, like chapters of a
memoir. Each storyline has **at most one draft chapter** (the newest, still growing) plus zero or
more **published** chapters (immutable, delivered episodes). There is no curation panel and no
separate narrative panel: the chapter row *is* the narrative, grounded via the Anthropic Citations
API. Chapter boundaries are a **semantic judgment call by an LLM**, not a date range or a word
count — an AI judge decides, per new entry, whether it continues the draft's arc, starts a new
arc, or is a late addendum to an already-finished arc.

## Data model

Migration `0036_storylines_draft_published.sql` reshapes the schema (forward-only, re-runnable,
`DROP IF EXISTS` guards, `PRAGMA foreign_keys` toggled off for the table rebuild, matching the
pattern in `0028_storyline_entities.sql`). Tables:

* `storylines` — one row per storyline: `id, user_id, name, description, status
  ('active'|'archived'), last_extension_check_at, created_at, updated_at`. No date-range,
  summary-embedding, or `last_generated_at` columns — those all moved onto (or are derived from)
  chapters.
* `storyline_entities` — unchanged anchor join table, `(storyline_id, entity_id)` PK, soft cap
  `MAX_ANCHORS = 15` enforced in application code.
* `storyline_chapters` — the narrative panel is folded directly into this row (no more
  `storyline_panels`):
  * `id, storyline_id, seq` (1-based), `title`
  * `state` — `'draft' | 'published'`. A **partial unique index** enforces at most one draft per
    storyline; the draft is always the highest `seq`.
  * `segments_json` (same `{"kind": "text", "text": ...}` / `{"kind": "citation", "entry_id",
    "quote", ...}` shapes as before, in `services/storylines/segments.py`), `source_entry_ids_json`,
    `citation_count`, `model_used`, `generated_at`
  * `published_at` — NULL for the draft.
  * `read_at` — NULL means unread. Set by `POST .../chapters/{id}/read`, cleared again whenever an
    addendum lands on a published chapter (it resurfaces as updated).
  * `addenda_json` — list of `{added_at, segments, entry_ids}`; the original narrative text is
    never rewritten by an addendum, only appended to.
  * `draft_embedding_json` — embedding of the *draft's* narrative, written by the same code path
    that writes the draft's segments (so it can't silently go stale) and read by the extension
    classifier's semantic fallback stage.
  * No `start_date`/`end_date`/`title_locked`/`boundary_locked`/`narrative_word_count` — dates are
    **derived** from member entries (`first_entry_date`/`last_entry_date` on `StorylineChapter`,
    computed by the repository), not stored.
* `storyline_chapter_entries` — `(chapter_id, entry_id, added_late)`, PK on the pair, index on
  `entry_id`. This is the core structural change: **a chapter is defined by the explicit list of
  entry ids the judge grouped into it**, not a date window. An entry belongs to at most one
  chapter per storyline.
* `storyline_pending_entries` — `(storyline_id, entry_id)`. Entries the extension classifier
  matched to a storyline but that haven't been folded into a chapter by a `storyline_update` run
  yet (see the pending-entries mechanism below).
* `storyline_panels_legacy` / a frozen pre-migration snapshot of `storyline_chapters` — both panel
  kinds and the old chapter shape are preserved verbatim by the migration for a verification
  window; a follow-up migration (0037, never shipped in the same release) drops them. See
  [Migration & rollout](#migration--rollout).

**Why entry-set membership over date windows** (the core decision this redesign makes): a date
window forces every boundary decision through tiling arithmetic, and an entry dated into an
already-closed window is invisible to the system. An explicit membership table has no such hole —
a backdated OCR'd entry is just another entry the judge can assign to any chapter, published or
draft, regardless of when it's ingested.

## Engine lifecycle

`services/storylines/engine.py::StorylineEngine` is the single orchestrator, with three public
entry points (all return `UpdateResult`):

**`update(storyline_id)`** — the steady-state call, queued after an entry is judged to extend a
storyline (see [Extension classifier](#extension-classifier--jobs) below). A no-op when nothing
new has been said about the anchors since the last call.

1. Gather candidates: every entry that mentions an anchor, unioned across anchors (see
   `_candidate_entries` — entity-mention excerpts, with a `LIKE`-based surface-form fallback below
   `_SPARSE_RECALL_THRESHOLD` matches). Combine with anything left in `storyline_pending_entries`
   from a prior run whose write step never completed. New ids not already in any chapter's
   membership are what gets judged.
2. One structured tool call to the judge (`providers/storyline_judge.py::judge_extension`): input
   is the draft's current narrative + its member entries (truncated) + the new entries
   (truncated) + a summary of already-published chapters (id, title, date range). Output: one
   `assign: "draft" | "new_chapter" | "published_chapter"` (+ `chapter_id` for the last) per new
   entry, plus `draft_arc_complete: bool` and one-sentence `reasoning` (recorded on the job
   result). A judge failure aborts with no writes — every candidate stays in the pending table for
   the next run.
3. Apply the verdict:
   * `draft` assignments → membership rows on the draft; the draft is **re-narrated whole** (no
     append/seam machinery — every draft re-narration sees the full up-to-date membership).
   * `draft_arc_complete` (or any `new_chapter` assignment) → **publish**: one closure narration
     (final prose + title, via a paired tool call — nothing regex-parsed off the prose), an atomic
     publish transaction (draft → published with `published_at`; a fresh empty draft opens behind
     it; any `new_chapter`-assigned entries seed the fresh draft), then a Pushover notification.
     Never publishes an empty closure — a closure narration with no segments folds the
     `new_chapter` entries back into the still-open draft instead.
   * `published_chapter` assignments → a short addendum narration appended to that chapter's
     `addenda_json`; the chapter's `read_at` is cleared so it resurfaces as updated.
4. Guards: **at most one publish per `update()` call**; a would-be publish below
   `storyline_min_publish_entries` (default 3) defers — everything folds into the still-open draft
   instead. Every LLM call completes (or fails) before any repository write for that step; a
   narrator failure on an addendum leaves the entries in the pending table for retry, without
   touching the chapter it would have appended to.

**`bootstrap(storyline_id, mark_read=False)`** — one-time full-history partition (storyline
creation, and the migration sweep). One judge call (`partition`) reads the storyline's entire
candidate corpus and returns an ordered list of chapters as **lists of entry ids** (the judge's
grouping is authoritative — nothing is date-derived). Each chapter is narrated independently
(`closure` mode for all but the last, `draft` mode for the last), then
`storyline_repository.replace_all_chapters` swaps in the new set atomically. `mark_read=True` (used
by the migration sweep) seeds the resulting published chapters as already-read so a bulk
re-bootstrap doesn't manufacture a wall of unread badges.

**`refresh_draft(storyline_id)`** — re-narrates the draft from its *existing* membership. No judge
call, no membership change. The manual "nudge the draft" escape hatch (`POST .../refresh`).

**Unpublish** (`storyline_repository.unpublish_newest` + `refresh_draft`, driven by
`storyline_update(..., unpublish=True)`) folds the newest published chapter's members back into
the draft and deletes the chapter row — the escape hatch for "published too early." Repeatable
back to chapter 1.

### Pending-entries mechanism

`storyline_pending_entries` is what makes the coalescing in the extension-check worker (below)
lossless: the classifier records a match there *before* deciding whether to queue a new
`storyline_update` job or rely on one already queued. Whichever `update()` call runs next reads
the full pending set, not just the one entry that triggered it — so a burst of 30 matching entries
in a row produces one judge call, not 30. `update()` also sweeps stale pending ids at the top of
every call (ones already present in chapter membership from a prior run whose final "clear
pending" step didn't get reached because an exception propagated first) — nothing is lost, and
nothing lingers past one extra call.

## Extension classifier + jobs

`services/storylines/extension.py::StorylineExtensionClassifier.classify_for_entry` is unchanged
in shape from round 1, with the fixes the redesign's bug inventory called for:

1. **Entity overlap** (deterministic) — an anchor entity id in the entry's extracted mentions →
   `yes`, no LLM call.
2. **Surface-form match** (deterministic, **word-boundary** regex — `\bAna\b` no longer matches
   "banana") → escalate to the Haiku decider (`providers/storyline_extension_decider.py`).
3. **Embedding fallback** — cosine similarity between the entry's embedding and the storyline's
   **draft chapter's** `draft_embedding_json` (not a dead `storylines.summary_embedding` column
   that nothing wrote after 0030 — that was one of the bugs this redesign fixes by construction).
   At/above `STORYLINE_EXTENSION_RELEVANCE_THRESHOLD`, also escalates to the decider.
4. No match → `no`, no LLM call.

Two job types (`services/jobs/workers/storyline_extension_check.py`,
`services/jobs/workers/storyline_update.py`), both on the single-worker storyline pool (Pool B —
see [`jobs.md`](jobs.md#jobrunner)):

* **`storyline_extension_check`** — queued by the entity-extraction worker after it commits an
  entry's mentions (so the classifier's entity-overlap stage reads a populated mention set — see
  `jobs.md`'s [automatic job triggering](jobs.md#automatic-job-triggering) section). For each
  `yes` classification: record the entry in `storyline_pending_entries`, then queue a
  `storyline_update` job **unless one is already pending** for that storyline
  (`find_pending_storyline_update`) — that's the coalescing described above.
* **`storyline_update`** — runs `engine.update`, or `engine.bootstrap` (`bootstrap=True`),
  `engine.refresh_draft` (`refresh_only=True`), or unpublish-then-refresh (`unpublish=True`)
  depending on params. At most one of the three flags may be set. A publish fires exactly one
  Pushover notification (`notify_chapter_published`); the plain steady-state path fires no
  success notification (it runs on every matching entry and would be noisy) but failures always
  notify.

## Providers

* `providers/storyline_narrator.py::AnthropicStorylineNarrator` — Citations API, one
  `source="text"` document per entry, two-breakpoint caching (1h system prompt, 5m entry corpus).
  One method, `generate_narrative(excerpts, name, description, mode=...)`, with three modes:
  `draft` (arc ongoing, no forced resolution), `closure` (ends the arc, returns a parsed
  `# <title>` line as `NarrativeResult.title`), `addendum` (short postscript against
  `prior_narrative`, no retelling). No sectioning prompt, no word-band constants, no
  `prior_narrative` append plumbing — those belonged to the deleted date-window model.
* `providers/storyline_judge.py::AnthropicStorylineJudge` — Haiku, two forced-tool-choice methods
  sharing one system prompt: `judge_extension` (per-update continue-or-break) and `partition`
  (bootstrap). Parsing is defensive by construction: unknown entry ids are dropped, a
  `published_chapter` assignment with an invalid `chapter_id` is demoted to `draft`, and any
  entry the model's response omits is appended as `draft` (extension) or folded into the final
  chapter (partition) — nothing is ever silently dropped. Any exception or malformed response
  yields `failed=True`.
* `providers/storyline_extension_decider.py::AnthropicStorylineExtensionDecider` — Haiku tool-use,
  unchanged from round 1; `maybe` fallback on any non-happy path.
* **Deleted:** `providers/storyline_glue.py` (curation-panel transition prose — the curation panel
  no longer exists; verbatim material lives in citations, not a separate excerpt list).

## REST API

Read-side (`api/storylines.py`):

* `GET /api/storylines` — paginated list, `{items, total, limit, offset}`; each item carries
  `unread_count` and `chapter_count` (batch-fetched, no N+1).
* `GET /api/storylines/{id}` — storyline summary + `chapters: [meta]` (`id, seq, title, state,
  entry_count, first_entry_date, last_entry_date, published_at, read_at, citation_count`), `seq`
  ASC so the draft is naturally last.
* `GET /api/storylines/{id}/chapters/{cid}` — one chapter's full detail: meta + `segments` +
  `addenda` + `model_used` + `generated_at`.

Write-side (`api/storylines_write.py`):

* `POST /api/storylines` — body `{entity_ids: list[int], name, description?}`, 1..15 anchors.
  Creates the storyline and immediately queues a `storyline_update` bootstrap job; 201 with
  `{"storyline": <detail>, "bootstrap_job_id"}`. 409 on an identical name+anchor-set duplicate,
  422 on a bad anchor count. A missing/unwired engine is tolerated (storyline still created,
  `bootstrap_job_id: null`) — mirrors the old create behavior.
* `PATCH /api/storylines/{id}` — body `{name?, status?}` (`status ∈ {"active","archived"}`), at
  least one required. Metadata-only.
* `DELETE /api/storylines/{id}` — cascades to chapters, memberships, addenda, anchors.
* `PUT /api/storylines/{id}/anchors` — set-replacement of the anchor set (1..15).
* `POST /api/storylines/{id}/refresh` — queue `refresh_draft`; 202 `{"job_id", "status"}`.
* `POST /api/storylines/{id}/chapters/unpublish` — queue the unpublish-then-refresh flow; 400 if
  there's no published chapter to fold back; 202 otherwise.
* `POST /api/storylines/{id}/chapters/{cid}/read` / `.../unread` — mark a published chapter's read
  state; 400 on a draft chapter (drafts have no read state).
* `PATCH /api/storylines/{id}/chapters/{cid}` — body `{title: str}`. Rename only — the only direct
  (non-job) mutation of chapter content, by design (see [Jobs, concurrency, failure
  handling](#extension-classifier--jobs) above: nothing outside Pool B writes narrative/membership).

All routes return 503 when storylines aren't configured (missing `ANTHROPIC_API_KEY`).

**Deleted from round 1:** chapter add/split/merge/window-edit/delete, per-chapter regenerate,
whole-storyline `regenerate` with `mode`/`resegment`/`override_locked` params. Manual chapter
editing is gone; rename and unpublish are the only edits.

## MCP tools

In `mcp_server/tools/storylines.py`:

* `journal_storylines_guide` — zero-param concept/workflow primer, works without an API key.
* `journal_list_storylines` — list with unread/chapter counts (`readOnlyHint`).
* `journal_get_storyline` — one storyline + its chapters' meta (`readOnlyHint`). No panels — the
  narrative lives on the chapter itself, fetched via the next tool.
* `journal_get_storyline_chapter` — one chapter's full narrative + addenda (`readOnlyHint`).
* `journal_create_storyline` — seed a storyline (1..15 `entity_ids` + `name`), auto-kicks a
  bootstrap job and polls to terminal (default 120s timeout).
* `journal_refresh_storyline` — re-narrate the draft on demand (`idempotentHint`), polls to
  terminal.
* `journal_unpublish_storyline_chapter` — fold the newest published chapter back into the draft
  (`destructiveHint`), polls to terminal.
* `journal_rename_storyline_chapter` — metadata-only rename.
* `journal_set_storyline_anchors` — set-replacement of an existing storyline's anchors.
* `journal_delete_storyline` — cascade delete (`destructiveHint`).

Every tool returns an actionable string when storylines aren't wired on this server. This surface
mirrors the REST API 1:1 (fixing, by construction, the round-1 bug where `journal_get_storyline`
passed a storyline id into a chapter-scoped panel lookup — panels don't exist anymore).

## CLI

`journal bootstrap-storylines --user-id N [--storyline-id N] [--mark-read] [--execute]` —
replaces the round-1 `backfill-storyline-chapters` and `recheck-storylines` commands, neither of
which has a meaning under the judge/narrator engine (there's no date-window resegmentation to run,
and extension catch-up is now just a bootstrap re-run). Dry-run by default (lists candidates with
current chapter/entry counts, no LLM call, no engine construction — only the storyline repository
is opened). `--execute` calls `StorylineEngine.bootstrap` per targeted storyline (one judge
partition call + one narrator call per resulting chapter — LLM-costed). `--mark-read` is for the
one-time migration sweep (see [Migration & rollout](#migration--rollout)) — leave it off for a
routine re-bootstrap so newly-generated chapters show up as unread.

## Configuration

All env vars are optional; defaults make the feature work once `ANTHROPIC_API_KEY` is set.

| Env var                                    | Default              | Purpose                                                       |
| ------------------------------------------- | -------------------- | -------------------------------------------------------------- |
| `ANTHROPIC_API_KEY`                        | (none)               | Gates the entire feature on/off                                |
| `STORYLINE_NARRATOR_MODEL`                 | `claude-opus-4-7`    | Model for chapter narration (Citations API)                    |
| `STORYLINE_NARRATOR_MAX_TOKENS`            | `4096`               | Max output tokens for narration                                |
| `STORYLINE_JUDGE_MODEL`                    | `claude-haiku-4-5`   | Model for `judge_extension` / `partition`                      |
| `STORYLINE_EXTENSION_DECIDER_MODEL`        | `claude-haiku-4-5`   | Model for the extension classifier's decider stage             |
| `STORYLINE_EXTENSION_RELEVANCE_THRESHOLD`  | `0.5`                | Cosine at/above which the embedding fallback escalates to the decider |
| `STORYLINE_MIN_PUBLISH_ENTRIES`            | `3`                  | Guard: a would-be publish below this entry count defers instead |

**Removed** by this redesign (no replacement — the concepts they configured don't exist anymore):
`STORYLINE_GLUE_MODEL`, `STORYLINE_DEFAULT_WINDOW_DAYS`, `STORYLINE_FTS_FALLBACK_THRESHOLD`,
`STORYLINE_CHAPTER_TARGET_WORDS`, `STORYLINE_CHAPTER_MIN_WORDS`, `STORYLINE_CHAPTER_MAX_WORDS`.

## Migration & rollout

See [`rollout-storylines-0036.md`](rollout-storylines-0036.md) for the prod runbook. In short:
migration `0036` reshapes the schema and renames `storyline_panels` → `storyline_panels_legacy`
(both panel kinds preserved verbatim, nothing dropped yet); a manual
`journal bootstrap-storylines --mark-read --execute` sweep regenerates every existing storyline
under the new engine (old generated prose is reproducible LLM output, not hand-authored — it's
discarded and regrown); a follow-up migration drops the `_legacy` tables only after the sweep is
verified, and is never shipped in the same release as `0036`.

## Tests

* `tests/test_db/test_migration_0036.py` — the schema rebuild on prod-shaped state: fresh DB,
  open→draft / closed→published mapping, panel-to-chapter narrative fold, pending/membership
  table creation, forward-only re-run, and the freeze-snapshot mechanism that makes a forced
  same-connection replay safe.
* `tests/test_storyline_repository.py` — CRUD, anchors, membership, publish/unpublish
  transactions, pending-entry table, read-state, unread/chapter count batch queries.
* `tests/test_storyline_engine.py` — all three engine flows against fake judge/narrator: publish
  guards, addendum path, backdated-entry assignment, failed-provider leaves state untouched.
* `tests/test_storyline_extension.py` — classifier stages (entity overlap, word-boundary surface
  form, embedding fallback against the draft embedding), no-match short-circuit.
* `tests/test_storyline_jobs.py` — worker + classifier + JobRunner integration, coalescing.
* `tests/test_providers/test_storyline_judge.py`, `test_providers/test_storyline_narrator.py` —
  parsers against canned tool-call / Citations responses, including malformed input.
* `tests/test_api_storylines.py`, `tests/test_api_storylines_write.py` — REST surface with a fake
  `StorylineEngine` on a real `JobRunner`.
* `tests/test_mcp_tools_storylines.py` — MCP tool surface, including timeout/not-configured paths.
* `tests/test_storyline_models.py`, `tests/test_storyline_segments.py` — dataclass/segment-shape
  helpers.

Real Anthropic API calls are never made in tests — providers accept an injected `client=` to
receive a fake.

## Related docs

* [`superpowers/specs/2026-07-12-storylines-redesign-design.md`](./superpowers/specs/2026-07-12-storylines-redesign-design.md)
  — the design this document describes, including the bug inventory it fixes by construction.
* [`archive/2026-06-13-storyline-chapters-design.md`](./archive/2026-06-13-storyline-chapters-design.md),
  [`archive/2026-06-15-storyline-chapter-editing-design.md`](./archive/2026-06-15-storyline-chapter-editing-design.md)
  — superseded round-1 designs (date-window chapters, manual chapter editing).
* [`entity-tracking.md`](./entity-tracking.md) — the entity store this feature is anchored on.
* [`jobs.md`](./jobs.md) — the job runner (Pool A / Pool B split) this feature plugs into.
* [`architecture.md`](./architecture.md) — high-level service architecture, incl. the LLM-baked-prose exception this feature is one of two instances of.
