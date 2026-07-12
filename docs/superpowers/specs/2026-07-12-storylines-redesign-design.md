# Storylines Redesign — Draft/Published Chapters with Semantic Boundaries

**Date:** 2026-07-12
**Status:** approved design, pending implementation plan.
**Supersedes (on ship):** [2026-06-13-storyline-chapters-design.md](../../archive/2026-06-13-storyline-chapters-design.md),
[2026-06-15-storyline-chapter-editing-design.md](../../archive/2026-06-15-storyline-chapter-editing-design.md), and the
chapter/panel sections of `docs/storylines.md`. **Shipped 2026-07-12** — both specs archived to `docs/archive/`.

## Why

The current implementation treats chapters as **date-range partitions** of a storyline's
timeline, sized by word-count arithmetic and kept consistent by deterministic tiling math.
The reading experience the user actually wants — *"I look forward to reading a new chapter;
chapters begin and end for semantic reasons"* — was never designed for: there is no unread
state, no delivery moment, and any new entry can trigger a resegmentation that rewrites the
entire reading history (the `title_locked`/`boundary_locked` flags exist only to defend
against this self-inflicted churn).

Bugs found in the 2026-07-12 review that this design eliminates by construction:

1. **Dead semantic fallback** — the extension classifier reads `storylines.summary_embedding`,
   deprecated in migration 0030 and never written by chapter-era code (`extension.py:193`).
2. **Backdated entries vanish** — ingest only refreshes the *open* chapter's date window; an
   OCR'd entry whose date falls in a closed chapter is silently never incorporated.
3. **History instability** — ingest-time auto-split can re-carve every unlocked chapter
   (new boundaries, titles, prose) from one new entry.
4. **Crash-unsafe resegment** — `rebuild_chapters` commits (old panels cascade-deleted)
   before per-chapter panels are written across multiple LLM calls.
5. **Structural-edit races** — split/merge/delete run synchronously in REST handlers while
   generation jobs run on Pool B.
6. **Panel/window inconsistency by construction** — section windows are re-derived from
   citation dates and excerpts re-filtered by window, so a chapter's curation panel can
   disagree with its own narrative's citations.
7. Smaller: replace-mode date params silently ignored; substring surface-form matching
   ("Ana" matches "banana"); raw FTS5 injection of canonical names; a full Opus narration
   made only to *measure* word count then discarded; MCP `journal_get_storyline` passes a
   storyline id to `list_panels()` (expects chapter id).

## Product decisions (locked with user, 2026-07-12)

1. **Chapter model: published + one draft.** Published chapters are immutable episodes,
   delivered to the reader. Exactly one draft chapter per storyline grows as entries arrive.
2. **Publish trigger: LLM judgment on new material.** No word counts, no date math. When new
   entries extend a storyline, the model decides continue-vs-break. At publish, the draft
   gets a light closure revision — matching "the penultimate chapter's end may be edited a
   little as the following chapter takes shape".
3. **Narrative only.** The curation panel (verbatim excerpts + Haiku transition glue) is
   deleted; citations/footnotes carry the verbatim material.
4. **Manual chapter editing is deleted** (add/split/merge/window-edit/locks). Escape
   hatches: rename a chapter; unpublish the newest chapter(s) back into the draft.
5. **Delivery: unread badges + Pushover push** on publish.
6. **Scope: rebuild the vertical; regenerate existing data** through the bootstrap path.
   Old generated panels are discarded (reproducible LLM output).
7. **Engine: continue-or-break** (draft fully re-narrated on extension; no append/seam
   machinery).

## Core architectural principle

**Chapters own explicit entry sets, not date windows.** A chapter is defined by the list of
entry ids the model grouped into it (`storyline_chapter_entries`). Date ranges are display
metadata derived from members (min/max `entry_date`). All window/tiling/overlap/ripple code
disappears, and backdated entries become ordinary unassigned entries rather than a hole in
the model.

## §1 Data model

Migration `0036` (forward-only, re-runnable, `DROP IF EXISTS` guards, FK-off table-rebuild
wrapper as in 0028).

- **`storylines`** — keep `id, user_id, name, description, status, created_at, updated_at,
  last_extension_check_at`. Drop `start_date`, `end_date`, `summary_embedding_json`,
  `last_generated_at` (derived from chapters when needed).
- **`storyline_entities`** — unchanged (anchor join table, soft cap 15).
- **`storyline_chapters`** — narrative folded in (panels table deleted):
  - `id, storyline_id, seq` (1-based contiguous), `title`
  - `state` — `'draft' | 'published'`; **partial unique index: at most one draft per
    storyline**; draft is highest seq (code-enforced)
  - `segments_json` (same `text`/`citation{entry_id, quote, entry_date}` shapes),
    `source_entry_ids_json`, `citation_count`, `model_used`, `generated_at`
  - `published_at` (NULL for draft), `read_at` (NULL = unread)
  - `addenda_json` — list of `{added_at, segments, entry_ids}`; original narrative untouched
  - `draft_embedding_json` — embedding of the draft narrative, written by the same code path
    that writes the draft segments (cannot silently go stale); used by the extension
    classifier's semantic stage
  - **No** `start_date`/`end_date`/`title_locked`/`boundary_locked`/`narrative_word_count`
- **`storyline_chapter_entries`** — `(chapter_id, entry_id, added_late INTEGER DEFAULT 0)`,
  PK on the pair, index on `entry_id`. An entry belongs to at most one chapter per storyline
  (write-time query check).
- **`storyline_panels`** — deleted (renamed `_legacy` first; see Migration section).

**Invariants (repository-enforced, loud failures):**

1. Exactly one draft chapter per active storyline; created with the storyline; highest seq.
2. Published chapters are immutable. The repository refuses updates to a published chapter's
   `segments_json`/`title`/membership except: append addendum, set/clear `read_at`, rename
   (escape hatch), unpublish (newest first).
3. Publish is a single transaction: draft → published (+`published_at`), insert new draft,
   move memberships. No LLM calls inside any transaction.

## §2 Engine lifecycle

One service (`StorylineEngine`), four flows, all writes on job Pool B.

**Flow 1 — Extension (per ingested entry).** The extension-check job (queued after entity
extraction commits, as today) classifies cheap-first per active storyline:
anchor-entity overlap → yes; **word-boundary** surface match → Haiku decider; embedding
similarity vs **draft embedding** → Haiku decider; else no. Each "yes" queues one coalesced
`storyline_update` job, which:

1. **Gathers** the draft's member entries + all matched entries not in any chapter of this
   storyline (set difference on membership — this is what fixes backdated entries).
2. **Judges** (one structured tool call): input = draft narrative + draft entry metadata +
   new entries' text. Output per new entry: `assign: "draft" | "new_chapter" |
   "chapter:<id>"` (addendum), plus `draft_arc_complete: bool` and one-sentence reasoning
   (recorded on the job result).
3. **Acts:**
   - draft assignments → membership rows; **draft re-narrated whole** (coherent prose, no
     seams); draft embedding refreshed.
   - `draft_arc_complete` or `new_chapter` assignments → **publish**: one closure narration
     (final prose + title via tool call), atomic publish transaction, fresh draft narrated
     from leftover entries. Pushover notification fires here only.
   - addendum verdicts → short addendum narration appended to `addenda_json`; chapter's
     `read_at` cleared (regains unread badge).
4. **Guards:** minimum draft size before publish (config, default 3 entries) unless the
   judge signals a hard break; at most one publish per job run.

**Flow 2 — Bootstrap** (storyline creation over history; also the migration sweep). One
**partition call**: judge reads the chronological corpus metadata and returns chapters as
lists of entry ids + working titles (model's grouping verbatim — no date derivation). Each
chapter narrated independently; all but the last published (closure mode), last becomes the
draft. Large corpora: overlapping windows of ~50 entries, the last group of each window
seeding the next.

**Flow 3 — Redo.** `POST .../chapters/unpublish` folds the newest published chapter's
members back into the draft (delete row, move memberships, queue draft re-narration).
Repeatable back to seq 1.

**Flow 4 — Manual refresh.** Re-narrate the draft from current members. No modes, no params.

**Cost:** ~1 judge + 1 narration per extending entry (≈ today, minus glue); 2 narrations per
publish.

## §3 Providers

- **Narrator — kept, simplified.** Citations-API grounding, document-per-entry, caching, and
  citation→entry_id parsing survive unchanged. Deleted: sectioning prompt, `##` parsing,
  `NarrativeSection`, word-band constants, `prior_narrative` append plumbing. One method
  with `mode: draft | closure | addendum` selecting the framing instruction; `closure`
  prompts the model to open with a `# <title>` line, which the narrator parses off the
  first text segment only — a single bounded first-line regex match, never applied to
  draft/addendum responses or to any later `#` in the prose.
- **Judge — new provider** (replaces `storyline_extension_decider` + all boundary logic).
  Two tool-call methods sharing a system prompt: `judge_extension(...)` (Flow 1) and
  `partition(...)` (Flow 2). Forced tool choice, structured output. Model: Haiku, config
  knob to escalate to Sonnet. On API failure: "no decision" — entries stay
  matched-but-unassigned (queryable state) and retry on the next update.
- **Glue provider — deleted** (file, tests, config, wiring).
- **Classifier — kept, three fixes:** word-boundary matching; embedding stage reads
  `draft_embedding_json`; FTS fallback replaced by a plain `LIKE` match (it serves a handful
  of sparse cases; simpler and injection-proof).
- **Corpus fetch** — reduced to candidate discovery (union across anchors, dedup, sort);
  membership is decided by the judge, never re-derived from dates.

## §4 Jobs, concurrency, failure handling

- **Two job types:** `storyline_extension_check` (per entry) and `storyline_update`
  (per storyline; `bootstrap: true` selects Flow 2). All of `mode`/`resegment`/
  `override_locked`/`auto_split`/`chapter_id`/date params are deleted. Pool B single-worker
  stays.
- **No synchronous structural writes.** Unpublish is enqueued on Pool B (202 + job id);
  rename is the only direct mutation. Nothing outside Pool B writes chapter rows → the edit
  race is gone by construction.
- **Uniform failure policy:** every LLM call completes before any write for that transition.
  Failed judge/narration → job warning, state untouched, unassigned entries retried next
  update. The publish transaction is the only multi-row write and is atomic.

## §5 API & webapp

REST (everything else deleted):

| Route | Purpose |
| --- | --- |
| `GET /api/storylines` | list + `unread_count` per storyline |
| `POST /api/storylines` | create → bootstrap job |
| `PATCH /{id}` / `DELETE /{id}` / `PUT /{id}/anchors` | as today |
| `GET /{id}` | chapters (meta, derived date range, read state), draft last |
| `GET /{id}/chapters/{cid}` | narrative segments + addenda |
| `POST /{id}/refresh` | re-narrate draft (202 + job) |
| `POST /{id}/chapters/{cid}/read` (+ `unread`) | read-state |
| `PATCH /{id}/chapters/{cid}` | rename only |
| `POST /{id}/chapters/unpublish` | redo hatch (202 + job) |

Deleted: chapter add/split/merge/window/delete, per-chapter regenerate, regenerate body
params. MCP tools mirror the same surface (fixing the `journal_get_storyline` panel bug by
construction).

Webapp:

- **List:** unread badge per storyline; sidebar total.
- **Detail:** single vertical reader — published chapters as a book (title, derived
  date-range eyebrow, narrative with the existing footnote/citation UI, addenda as distinct
  "Later:" blocks). Draft renders last, visually subdued ("In progress — N entries") with
  Refresh. Chapter strip becomes a slim TOC (titles + unread dots); ⋯ menu = Rename /
  Unpublish (newest only). Scrolling a chapter into view (or opening it) marks it read.
- Deleted: `StorylineCurationList`, date-mode toggle, `ChapterDateModal`,
  `ChapterConfirmModal`, merge/split/delete flows; `generatingChapterIds` shrinks to one
  "updating…" flag per storyline.

## §6 Migration & rollout

1. Migration `0036`: create `storyline_chapter_entries`; reshape `storyline_chapters`
   (`open`→`draft`, `closed`→`published` with `published_at` backfilled from
   `last_generated_at`; add `read_at`, `addenda_json`, `draft_embedding_json`; drop lock and
   word-count columns); **rename** `storyline_panels` → `storyline_panels_legacy`.
2. CLI `journal bootstrap-storylines` (replaces `backfill-storyline-chapters` and
   `recheck-storylines`): Flow-2 sweep per storyline; pre-existing chapters' `read_at` set
   so the migration doesn't create fake unread badges. `dry_run=True` default.
3. Follow-up migration drops `_legacy` tables after the sweep is verified.

Per house migration rules: query prod for anomalies first; tests exercise the data-copy path
on prod-shaped state; every migration re-runnable after partial failure.

## §7 Testing

Failing-test-first throughout.

- **Repository:** single-draft invariant, immutability refusals, atomic publish, membership
  uniqueness, unpublish ordering.
- **Engine (fake judge/narrator):** all four flows; guards (min-entries, one-publish-per-run);
  backdated-entry assignment; addendum path clears `read_at`; failed-provider leaves state
  untouched and entries retryable.
- **Providers:** parsers against canned tool-call responses (malformed included).
- **API:** new surface incl. read-state and unpublish 202s.
- **Webapp:** reader rendering, unread badges, TOC, mark-read-on-view, unpublish flow;
  85% coverage thresholds already enforced.

## §8 Deletion inventory

- `providers/storyline_glue.py`; sectioning prompt/parsing in the narrator.
- `service.py`: `_narrate_bucketed`, `_split_excerpts_contiguous`,
  `_derive_section_windows`, `_find_locked_title`, append mode, `_seam_excerpt_*`, curation
  builders (~1,000 of 1,591 lines).
- Repository: `merge_chapters`, `split_chapter`, `add_chapter`, `update_chapter_window`,
  `delete_chapter`, `rebuild_chapters`, `_shift_seqs`.
- `services/storylines/backfill.py`, `recheck.py` (subsumed by bootstrap).
- 6 REST routes, 5 MCP tools, and the webapp chapter-editing/curation components.
