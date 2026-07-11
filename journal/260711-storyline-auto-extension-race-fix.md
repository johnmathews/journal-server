# 1. Storyline auto-extension: race fix + user_id propagation (W1, W3)

**Date:** 2026-07-11

## 1.1 Symptom

Ingesting ~1 month of handwritten pages (OCR / image ingestion) in one morning updated **zero** storylines — every storyline's `last_generated_at` stayed 3–11 Jun. Also, every existing storyline showed a single over-long chapter.

## 1.2 Root causes found

1. **Ordering race (the cause of "nothing updated").** The `storyline_extension_check` was enqueued by `_queue_post_ingestion_jobs` as a **concurrent sibling** of `entity_extraction`: entity extraction on Pool A (`runner._executor`), the check on Pool B (`runner._storyline_executor`), with no dependency. The classifier's reliable Stage-1 signal (`extension.py`) needs the entry's extracted entity-mentions already committed. On a burst ingest the check kept winning the race, read an empty mention set, and Stage-2 (literal anchor-name substring in messy OCR text) rarely matched → everything classified "no". The docstring even *asserted* the check "fires AFTER entity extraction has already run" — nothing enforced it.
2. **`user_id` silent drop.** `image_ingestion.py`/`audio_ingestion.py` created the entry with `job_user_id or 1` but passed the **raw** `job_user_id` to `queue_post_ingestion_jobs`. The `user_id is not None` gate then silently dropped the storyline check when a job carried no user_id.
3. **Single long chapter (separate issue, not yet fixed).** These storylines were generated before the chapter feature (migrations 0030/0031, 14–16 Jun). Migration 0030 backfilled each into one open chapter; multi-chapter sectioning is generation-time-only (`resegment_storyline`) with no bulk backfill. Remediation = W5 CLI backfill (pending).

## 1.3 Changes (W1, W3)

- **W1:** Removed the check from `_queue_post_ingestion_jobs`. Added `WorkerContext.queue_storyline_extension_check` (optional seam) bound to new `JobRunner._maybe_queue_storyline_extension_check` (no-op when storylines unwired; logs, never silently drops, when user unknown; swallows queue errors so it never fails the parent job). The entity-extraction worker calls it on the **single-entry** path after `mark_succeeded`, so mentions are committed first. Batch extraction deliberately does not trigger it (avoids one check per entry). **Bonus:** because text/file ingestion already queue entity extraction, they now trigger storyline updates too — the old code never did (this was a separate latent bug).
- **W3:** Both ingestion workers resolve `resolved_user_id = job_user_id or 1` once and use it for the entry *and* `queue_post_ingestion_jobs`, so follow-ups share the entry's attribution.

## 1.4 Tests

- `tests/test_storyline_jobs.py::TestEntityExtractionTriggersStorylineCheck` — single-entry extraction queues the check; batch does not.
- `TestJobRunnerStorylineSubmit` — `_maybe_queue_storyline_extension_check` creates a job when wired, no-ops when unwired, no-ops (logs) when user unknown.
- `tests/test_services/test_jobs_runner.py::...::test_image_ingest_without_user_propagates_default_user` — user_id=None ingest attributes follow-ups to user 1.
- Full unit suite green (3024 passed).

## 1.5 Follow-ups — all shipped same day

- **W4:** coalesce regenerations per batch. `jobs_repository.find_pending_open_regeneration` finds a queued full-refresh for a storyline; the extension-check worker skips queuing a duplicate. `coalesced_storyline_ids` on the job result. A burst of matching entries → one refresh on single-worker Pool B.
- **W5:** `journal backfill-storyline-chapters` (dry-run default) + `services/storylines/backfill.py` — re-sections existing one-chapter storylines via `resegment_storyline`. Skips already-multichapter unless `--include-multichapter`.
- **W6:** `journal recheck-storylines --since` + `services/storylines/recheck.py` — synchronous catch-up that re-classifies entries since a date and regenerates matches (coalesced). Plus an **embedding-relevance fallback** in the classifier: when entity-overlap and surface-form both miss, escalate to the Haiku decider if the entry embedding is within `STORYLINE_EXTENSION_RELEVANCE_THRESHOLD` (default 0.5) cosine of the storyline summary embedding. Wired at bootstrap.

## 1.6 To recover the current data (user action)

1. `journal backfill-storyline-chapters --execute` — re-section the 4 existing one-chapter storylines.
2. `journal recheck-storylines --since 2026-06-01 --execute` — pull this morning's ingested month into the storylines that match.

(Both are dry-run without `--execute`.)

Full unit suite green (3043 passed). Engineering-team run dir: `.engineering-team/runs/manual-20260711T151121Z/`.
