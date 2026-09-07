# Rollout: storylines redesign (migration 0036)

**Status:** steps 1–3 completed 2026-07-12/13 (deployed, all four storylines bootstrapped and
verified — storyline 3's sweep initially aborted on an Anthropic usage limit with zero writes, as
designed, and was re-run successfully). **Step 4 (drop legacy tables, next free migration `0039`) is
still pending and must ship in a LATER release.** Companion to [`storylines.md`](storylines.md) and
[`superpowers/specs/2026-07-12-storylines-redesign-design.md`](superpowers/specs/2026-07-12-storylines-redesign-design.md).

Migration `0036_storylines_draft_published.sql` reshapes the storylines schema (draft/published
chapters, explicit entry membership) and is forward-only and re-runnable, but it does **not** by
itself regenerate any storyline's content under the new engine — pre-existing chapters carry
forward as best-effort-migrated data (old narrative kept, membership backfilled from the narrative's
cited entry ids) until the bootstrap sweep below replaces them properly. Follow these steps in
order; do not skip the verification step before dropping legacy tables.

## 1. Deploy this release

The migration runs automatically on boot (`run_migrations` walks forward to the latest file). It:

- Rebuilds `storylines` and `storyline_chapters` into the new shape (`open` → `draft`, `closed` →
  `published` with `published_at` backfilled from the old `last_generated_at`).
- Renames `storyline_panels` → `storyline_panels_legacy` (both panel kinds preserved verbatim —
  nothing is dropped by this migration).
- Backfills `storyline_chapter_entries` from each chapter's `source_entry_ids_json` (best-effort;
  this is what the bootstrap sweep below replaces with real judge-assigned membership).
- Marks pre-existing published chapters as already-read (`read_at` set), so this deploy does not
  manufacture a wall of unread badges for content the user has already seen.

No manual action needed for this step — it's part of normal boot. Confirm via the server logs or
`PRAGMA user_version` that the DB is at 0036.

## 2. Bootstrap sweep

SSH to the host running the server (`ssh media`, stack at `/srv/media` — see the deploy runbook in
project memory) and run:

```bash
journal bootstrap-storylines --user-id 1 --mark-read --execute
```

This calls `StorylineEngine.bootstrap` for every one of user 1's storylines: one judge `partition`
call reads the storyline's full candidate corpus and returns judge-drawn chapters (semantic
boundaries, not the old date-tiling math), each narrated independently and swapped in atomically
via `replace_all_chapters`. `--mark-read` seeds the resulting published chapters as already-read —
**do not omit this flag for the initial sweep**, or every storyline's history re-arrives as unread.
LLM-costed: one judge call + one narrator call per resulting chapter, per storyline. Omit
`--execute` first to dry-run (lists candidate storylines with current chapter/entry counts, no LLM
call, no engine construction).

If the deployment has more than one user, repeat with `--user-id N` for each, or drop `--user-id`
scoping entirely if/when the command grows a fan-out mode (it does not have one as of this
writing — `--user-id` is required).

## 3. Verify

Spot-check 2-3 storylines in the webapp:

- Chapters read as coherent narrative episodes with sensible titles and boundaries (not
  mid-sentence cutoffs or artificially even splits).
- The newest chapter renders as an unstyled/subdued "draft" (in-progress) block, not a finished
  chapter.
- Published chapters show as read (no unread badge) post-sweep.
- Citations resolve to real entries (click through a footnote).

If a storyline's bootstrap produced warnings (printed by the CLI, e.g. "Partition unavailable;
bootstrap aborted"), re-run `--execute` for just that `--storyline-id` after checking Anthropic API
health — a transient failure leaves the storyline in its pre-sweep (best-effort-migrated) state,
which is safe to retry from.

## 4. Drop legacy tables (next release, never same deploy)

Once every storyline has been verified against the new engine, ship a follow-up migration as the
**next free number** that drops the two legacy holdovers. When this rollout was written the drop was
reserved for `0037`, but `0037` (`entry_date_confirmed`) and `0038` (`mood_scores_unique_dimension`)
have since been taken by unrelated work — so the drop is now **`0039`**. Take whatever is next free
at the time you ship it:

```sql
DROP TABLE IF EXISTS storyline_panels_legacy;
```

(`storyline_chapters_legacy` from the 0036 migration's freeze step is a `TEMP` table — connection-
scoped, already gone by the time any later migration runs — so the drop migration only needs to drop
the permanent `storyline_panels_legacy` table.)

**Never ship 0036 and the drop migration in the same release** (moot now that 0036 shipped
2026-07-12, but the principle stands for the drop). The gap between them is the verification window:
0036 is designed to be safely re-run or rolled back to (nothing destructive happens to the old
panel data), but the drop is a one-way door. Shipping them together removes the ability to inspect or
recover the pre-redesign narrative if the bootstrap sweep or the new engine turns out to have a
problem only visible in prod.
