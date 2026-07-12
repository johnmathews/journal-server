# Storylines redesign: draft/published chapters with semantic boundaries

**Date:** 2026-07-12
**Branch:** `storylines-redesign`
**Spec:** [`docs/superpowers/specs/2026-07-12-storylines-redesign-design.md`](../docs/superpowers/specs/2026-07-12-storylines-redesign-design.md)

## Why

A 2026-07-12 codebase review of the round-1 chapters feature (date-window chapters, deterministic
word-count tiling, manual editing) turned up a bug inventory the date-window model made structural,
not incidental:

1. **Dead semantic fallback** — the extension classifier read `storylines.summary_embedding`,
   deprecated by migration 0030 and never written by chapter-era code.
2. **Backdated entries vanish** — ingest only refreshed the *open* chapter's date window; an OCR'd
   entry dated into an already-closed chapter was silently never incorporated.
3. **History instability** — ingest-time auto-split could re-carve every unlocked chapter (new
   boundaries, titles, prose) off the back of one new entry.
4. **Crash-unsafe resegment** — the old `rebuild_chapters` committed (old panels cascade-deleted)
   before per-chapter panels were re-written across multiple LLM calls.
5. **Structural-edit races** — split/merge/delete ran synchronously in REST handlers while
   generation jobs ran on the single-worker storyline pool.
6. **Panel/window inconsistency by construction** — section windows were re-derived from citation
   dates and excerpts re-filtered by window, so a chapter's curation panel could disagree with its
   own narrative's citations.
7. Smaller: replace-mode date params silently ignored; substring surface-form matching ("Ana"
   matched "banana"); raw FTS5 injection of canonical names; a full Opus narration made only to
   *measure* word count then discarded; MCP `journal_get_storyline` passed a storyline id where a
   chapter id was expected.

None of these are fixable by patching the date-window model — they're consequences of deriving
chapter membership from dates instead of storing it. The fix is the architectural change this
branch makes: **chapters own explicit entry sets** (`storyline_chapter_entries`), not date ranges.

There's also a product motivation, independent of the bugs: the reading experience the user
actually wants is "I look forward to reading a new chapter; chapters begin and end for semantic
reasons," which the tiling-math model was never designed to deliver — no unread state, no delivery
moment, no sense that a chapter is *finished*.

## What changed, by layer

**Data model (migration 0036).** `storylines` sheds `start_date`/`end_date`/
`summary_embedding_json`/`last_generated_at` — none of those concepts survive; dates are now
derived from chapter membership. `storyline_chapters` folds the narrative panel directly into the
row (`segments_json`, `source_entry_ids_json`, `citation_count`, `model_used`, `generated_at`) and
gains `state` (`draft`/`published`, partial-unique-indexed to at most one draft per storyline),
`published_at`, `read_at`, `addenda_json`, `draft_embedding_json`. `storyline_panels` is renamed
`storyline_panels_legacy` (not dropped — see rollout below) and a new `storyline_chapter_entries`
join table is the load-bearing addition: membership is now an explicit fact, not a date-range
computation. `storyline_pending_entries` backs the coalescing mechanism (see decisions below).

**Engine (`services/storylines/engine.py`).** The round-1 `StorylineGenerationService`
(deterministic time-bucketed chaptering, ~1,591 lines including curation builders, seam-splicing,
locked-title/locked-boundary bookkeeping) is replaced by `StorylineEngine`, three entry points
(`update`, `bootstrap`, `refresh_draft`) all delegating to a judge for boundary decisions and a
narrator for prose. No append/seam machinery survives — every draft re-narration sees the full,
current membership and produces one coherent piece of prose.

**Providers.** New `providers/storyline_judge.py` (`AnthropicStorylineJudge`, Haiku): two forced
tool-call methods, `judge_extension` (steady-state continue/break/addendum) and `partition`
(bootstrap's one-shot full-history split). `providers/storyline_narrator.py` keeps its Citations-API
grounding and citation→entry_id parsing but sheds the sectioning prompt, `##`-title regex parsing,
and word-band constants in favor of one `mode: draft | closure | addendum` parameter.
`providers/storyline_glue.py` (curation-panel transition prose) is deleted outright — there's no
curation panel to glue together anymore.

**Jobs.** `storyline_generation` (with its `mode`/`resegment`/`override_locked`/`auto_split`/
`chapter_id`/date-range param surface) is replaced by two simpler types: `storyline_update`
(`bootstrap`/`refresh_only`/`unpublish` flags, at most one set) and `storyline_extension_check`
(unchanged in shape, now writing to the pending-entries table before deciding whether to queue).
Both stay on the single-worker Pool B.

**API/MCP/CLI.** Chapter add/split/merge/window-edit/delete and per-chapter regenerate are gone —
six REST routes and five MCP tools deleted. What's left: create (auto-bootstraps), refresh,
unpublish (the redo hatch), rename, read/unread state, delete, anchor set-replacement. CLI
`journal bootstrap-storylines` replaces both `backfill-storyline-chapters` and
`recheck-storylines` — under the judge/narrator engine there's no date-window resegmentation to
run and no separate extension-catchup mode; a bootstrap re-run does both jobs at once.

**Webapp** (companion branch, not covered by this doc): the chapter-editing UI (curation list,
date-mode toggle, split/merge/delete modals) is deleted in favor of a single vertical reader —
published chapters as a book, draft rendered last and visually subdued, a slim table-of-contents
with unread dots, and a `⋯` menu offering only Rename / Unpublish.

## Decisions worth remembering

- **Entry-set membership over date windows** is the core architectural bet. A date window forces
  every boundary decision through tiling arithmetic and makes an entry dated into an
  already-closed window invisible to the system by construction. An explicit membership table
  (`storyline_chapter_entries`) has no such hole: a backdated entry is just another entry the
  judge can assign to any chapter, published or draft, independent of ingestion order.
- **Publish is atomic.** `publish_draft` is one transaction: close the old draft (+`published_at`),
  open a fresh one, move memberships. No LLM call ever runs inside a transaction — every judge/
  narrator call completes (or fails) before any write for that step, so a crash mid-narration
  never leaves a half-published chapter or a chapter row inconsistent with its own citations (bug
  #4/#6 above, fixed by construction rather than patched).
- **The pending-entries mechanism is what makes coalescing lossless.** The extension-check worker
  records a match in `storyline_pending_entries` *before* deciding whether to queue a new
  `storyline_update` job or rely on one already queued — so whichever `update()` call runs next
  reads the full pending set, not just the entry that triggered it. A burst of 30 matching entries
  produces one judge call, not 30, and nothing is dropped even when 29 of those queue calls are
  skipped as duplicates.
- **Locks are deleted, not defended.** Round-1's `title_locked`/`boundary_locked` flags existed
  purely to protect hand-painted chapter boundaries from being re-carved by an unrelated ingest.
  With boundaries decided per-update by a judge that only ever acts on the draft (published
  chapters are immutable except via addendum), there is nothing left to protect against — the
  self-inflicted churn the locks were defending against doesn't exist anymore.

## Recovering existing data

Old generated narrative is reproducible LLM output, not hand-authored content, so nothing is
migrated forward as "real" data beyond a best-effort backfill of chapter membership from the old
narrative's cited entry ids. The real recovery step is the bootstrap sweep in
[`docs/rollout-storylines-0036.md`](../docs/rollout-storylines-0036.md): every existing storyline
is regenerated from scratch under the new engine, with `--mark-read` so the sweep doesn't
manufacture a wall of unread badges for chapters the user has already read once.

## Verification

Full unit suite green throughout (task-scoped tests per commit across 12 implementation tasks;
full-suite gate from Task 12 onward): `2956 passed, 11 skipped`. `ruff check src/ tests/` clean.
Implementation was tracked task-by-task under `.superpowers/sdd/` (13 tasks, `progress.md` +
per-task briefs/reports); this entry summarizes the branch, not any single task.

## Post-ship addendum (2026-07-13)

- **Final whole-branch review** (after the 13 tasks) found one rollout
  blocker the per-task reviews couldn't see: bootstrap sent the entire corpus
  to the judge in one call with `max_tokens=2048`. Fixed in `0529ffd` with
  50-entry overlapping-window partitioning, a per-run judge batch cap, and an
  8192-token judge budget. `f02c333` added REST entity-ownership validation
  on create/anchors (422 on foreign entity ids), a repository membership-
  uniqueness guard, LIKE-escaping in `find_entries_mentioning`, an addendum
  empty-prior guard, and per-storyline resilience in the bootstrap CLI sweep.
- **Deployed 2026-07-12**: merged fast-forward to main, CI green, containers
  updated on `media`. Migration 0036 applied cleanly (user_version 36, panels
  preserved as `storyline_panels_legacy`).
- **Bootstrap sweep**: Simmons & Simmons (3 chapters), Family (7), Atlas (7)
  regenerated. Fitness initially aborted with **zero writes** when the
  Anthropic monthly usage limit was hit mid-narration — the fail-before-write
  policy working as designed — and was re-run successfully the same evening
  (5 chapters). Operational lesson: run prod sweeps per-storyline
  (`--storyline-id`); a multi-storyline sweep through one ssh/docker-exec
  session dies with the session.
- Remaining: migration 0037 (drop `storyline_panels_legacy`) in a later
  release — tracked in `docs/rollout-storylines-0036.md`'s status header.
