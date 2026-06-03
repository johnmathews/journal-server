# Storyline notifications — drop Pushover on success, plus investigation of "two jobs per entry"

**Date:** 2026-06-03

## What I came in with

Two questions and an ask to assess the storyline-extraction feature:

1. Are storylines complete?
2. Why am I getting two `storyline_generation` jobs per entry in prod?
3. Why do I get a Pushover notification each time a `storyline_generation`
   job finishes — that's noise I don't want.

## Completeness check

Storylines is fully shipped and wired (W1–W12 closed; design doc archived
after W10 acceptance 2026-05-12). End-to-end: SQLite schema (migrations
0027 + 0028), `StorylineGenerationService` (Opus 4.7 Citations + Haiku
glue), `run_storyline_generation` worker, `StorylineExtensionClassifier`
fired from `_queue_post_ingestion_jobs`, REST surface in `api/storylines.py`
and `api/ingestion.py`, MCP surface (7 tools incl. `journal_storylines_guide`).
~143 storyline tests. No stubs, no TODOs, no half-wired paths. The active
reference doc is `docs/storylines.md`; the plan and 2026-05 follow-up
plan live in `docs/`.

## Two jobs per entry — not a code bug

The auto-enqueue path from ingestion is single-shot:

1. `_queue_post_ingestion_jobs` (runner.py:706-717) queues exactly **one**
   `storyline_extension_check` per ingest.
2. `run_storyline_extension_check` (extension_check.py:71-78) loops over
   the classifier results and submits **one `storyline_generation` per
   storyline with decision == "yes"**.
3. `StorylineExtensionClassifier.classify_for_entry` returns one result
   per **active** storyline for the user (extension.py:107-119).

So N active storylines that match a new entry → N `storyline_generation`
jobs. Two-per-entry-every-entry strongly implies two active storylines
whose anchor entities both match each new entry. The user can confirm
against prod with:

```sql
SELECT id, name FROM storylines WHERE status = 'active' AND user_id = ?;
```

If that returns two rows, it's working as designed. If only one, there's
a duplication path I missed and we'd come back to this.

## Pushover spam — actual bug, fixed

`run_storyline_generation` on success had:

```python
if parent_job_id:
    ctx.notifier.try_pipeline_notification(parent_job_id, user_id)
else:
    ctx.notifier.notify_success(user_id, "storyline_generation", summary)
```

`_SUCCESS_TOPIC_MAP` in `services/notifications.py` (lines 150-161)
includes `ingest_images`, `ingest_audio`, `save_entry_pipeline`,
`entity_reembed`, `fitness_sync_*` — **but not `storyline_generation`**.
The dispatcher at notifications.py:466-469 treats a missing topic key
as "always notify." So every auto-fired storyline regeneration sent a
Pushover, with no user-toggle path to disable it.

Worth noting: `run_storyline_extension_check` already does the right
thing and explicitly suppresses success notifications with the comment
*"No success notification by default — this fires on every ingestion
and would be noisy. Failures still notify."* The same reasoning applies
to `storyline_generation` — it fires on every entry that extends an
active storyline.

### Fix

Removed the `else: notify_success(...)` branch. Failures still notify.
Updated `test_happy_path_marks_succeeded` to assert `notifier.successes
== []` and verified red-before-fix → green-after.

The fix is a 6-line code change (`workers/storyline_generation.py`)
plus a 4-line test update. Shipped as PR #18.

## Also landed in this session

Three other PRs got pulled in alongside the storyline fix on the same
day, none of which I authored but all of which got merged together:

- #16 — OCR line-break reflow fix
- #17 — OCR `<<<NEW ENTRY>>>` multi-entry segmentation
- #19 — docs catch-up for multi-entity-anchors + append-mode (companion
  PR webapp#10 same day)

## Open question for next session

Confirm the prod `active` storyline count. If only one, dig into a
duplicate-enqueue path I haven't found yet — most likely a race
between manual regenerate (API/MCP) and the auto extension-check, but
that doesn't match "two-per-entry-every-entry."
