# 1. Jobs throughput & observability — parallel runner + per-job token/cost capture

**Date:** 2026-07-11 · **Branch:** `worktree-eng-jobs-throughput-and-observability` · **Run:** `manual-20260711T093400Z`

Cross-cutting change (server W1–W3 here; webapp W4 in the sibling repo) driven by a real pain point: uploading 1–2 months of entries at once produced 20+ minute queue drains. Production Loki logs from today's upload burst confirmed the cause — seven `storyline_generation` jobs (median 122s each, ~13 min total) ran back-to-back and head-of-line-blocked six `ingest_images` jobs behind them. No API errors, rate-limits, or credit exhaustion; the slowness was purely architectural.

## 1.1 What shipped

### 1.1.1 W1 — Two-pool job runner (parallel ingestion)

The runner used a single `ThreadPoolExecutor(max_workers=1)`, chosen deliberately (documented in its docstring) to keep LLM rate usage predictable. Replaced it with **two pools**:

- **Pool A (ingestion/fast):** `max_workers = config.job_worker_count` (new `JOB_WORKER_COUNT` env var, default 4). Handles everything except storyline jobs.
- **Pool B (storyline):** `max_workers = 1`. Handles only `storyline_generation` and `storyline_extension_check`.

This one design solves three problems at once: (1) per-entry ingestion runs in parallel; (2) a dedicated storyline pool means long storyline jobs can never occupy ingestion slots, so no more head-of-line blocking; (3) single-worker Pool B makes the same-storyline regeneration race **structurally impossible** — no lock needed. SQLite was already safe for N concurrent writers (WAL + `busy_timeout=5000` + per-thread connections in `db/factory.py`), so no DB changes were required. `JOB_WORKER_COUNT=1` restores fully-serial Pool A as a zero-deploy kill switch.

**Bug found and fixed in passing:** the parent ingestion worker queues its follow-up children (mood/entity/storyline-check) *before* marking itself succeeded. Under the old single worker the children couldn't start until the parent freed the worker, so the consolidated pipeline notification always fired correctly. Under parallel Pool A, a child can reach a terminal state before the parent's `mark_succeeded`, see the parent still running, and return early — losing the consolidated push. Fixed with a defensive `try_pipeline_notification` sweep after `mark_succeeded` in `image_ingestion.py`/`audio_ingestion.py`; the existing notification lock (`try_acquire_notification_lock`, an atomic check-and-set) dedupes so it can't double-fire.

### 1.1.2 W2 — Per-job LLM token capture

Every LLM call previously discarded `response.usage`. Added capture with near-zero signature churn:

- **`services/usage.py`** — a `contextvars.ContextVar`-scoped `UsageCollector`. Contextvar (not thread-local) was the key decision: it scopes to the worker thread + its synchronous call stack **and** propagates into child threads via `contextvars.copy_context().run(...)`. That matters because the two heaviest token consumers — OCR dual-pass and transcription shadow — fan out to their own `ThreadPoolExecutor(max_workers=2)`; a thread-local would silently miss exactly those calls. Off-job (request-path answerer/reranker/classifiers) there's no active scope, so `record()` is a cheap no-op — token attribution is job-scoped by construction.
- **Providers** call `usage.record_{anthropic,gemini,openai}(model, resp)` one line after their SDK call (11 job-path adapters). The two fan-out sites wrap their `pool.submit` with `copy_context().run` so sub-threads share the parent collector instance.
- **`services/jobs/run_job.py`** — a flush shim every worker is dispatched through (its own leaf module to avoid a `runner` ↔ `save_pipeline` import cycle). Opens the scope, runs the worker, and in a `finally` (so **failed** jobs record too) flushes totals via `jobs.record_usage`.
- **Migration 0034** adds nullable `input_tokens`/`output_tokens`/`cost_usd` to `jobs`; `Job` model, `_row_to_job`, and `_job_to_dict` expose them, so `GET /api/jobs` and `/api/jobs/{id}` now return them on every job.

### 1.1.3 W3 — Best-effort USD cost

Discovered the pricing infrastructure **already existed** (migration `0017_pricing.sql` table + `db/pricing.py` + admin-editable `PATCH /api/settings/pricing`) — so this was "read the table," not "build a pricing system." Added `estimate_cost(conn, per_model)` to `db/pricing.py`, wired into the run_job flush. It sums `tokens/1e6 × price_per_mtok`, excludes models with no pricing row and `transcription`-category models (priced per audio-minute, out of scope), and returns `None` when nothing is priceable so tokens are still recorded with `cost_usd` NULL. **Migration 0035** backfills two models referenced in code but absent from the 0017 seed: `claude-opus-4-7` (storyline narrator default — the biggest consumer) and `whisper-1` (transcription fallback).

## 1.2 Decisions & notes

1. **Two pools over a priority queue.** A `PriorityQueue`-backed executor would need subclassing `ThreadPoolExecutor` (its internal queue isn't exposed) — most code, highest risk. Two pools is mechanical and makes the storyline race impossible for free. A single shared N-worker pool was rejected: N long storyline jobs could still starve ingestion, and it would reintroduce the same-storyline race.
2. **Contextvar over return-threading.** Threading `usage` up through `OCRResult`/transcript/`str`/embeddings return types and every service layer would touch ~15 signatures across several layers — the storyline path already proved this stalls (it added `raw_usage` and never plumbed it up). The contextvar is one line per provider + one flush shim.
3. **Edge-case fix from code review:** `estimate_cost` originally set `priced_any = True` for any non-transcription row, so an `llm`/`embedding` row with *both* cost columns nulled (a possible admin action via the pricing API) would return `0.0` (job looks free) instead of `None` (unpriced). Tightened to only mark priced when a term actually contributes; added a regression test.

## 1.3 Testing

Strict TDD throughout. Full suite: **3018 passed, 11 skipped** (integration auto-skips without Chroma). New/updated coverage: two-pool concurrency (parallel ingestion, no storyline blocking, same-storyline serialization, dual-pool shutdown), `JOB_WORKER_COUNT` config validation, notification-race regression, `UsageCollector`/scope isolation + the three normalizers + `copy_context` propagation (with a negative test proving a bare `submit` would *not* record), migrations 0034/0035, `estimate_cost` (incl. the both-null-price → None edge), and runner flush (priced → cost; transcription-only → NULL cost; failed job still records). Ruff clean.

## 1.4 Docs updated

`docs/jobs.md` rewritten for the two-pool model + a new per-job token/cost capture section; `docs/api.md` gained the three `/api/jobs` fields and corrected the "single-worker" description; `CLAUDE.md` migration range → 0035 and the two new modules added to the tree; `docs/configuration.md` documents `JOB_WORKER_COUNT`; `docs/code-quality-principles.md` gold-standard-docstring reference updated; `docs/jobs.md` added to the README index.

## 1.5 Follow-ups

1. `docs/jobs.md` still says "10 job types as of 2026-05-10" — now ~15. Predates this change; left for a future cleanup.
2. Audio-minute transcription cost is out of scope: the collector captures tokens, not minutes, so `transcription`-category models are excluded from USD. A future unit could plumb audio duration into the flush and add a `cost_per_minute` term.
3. Running totals on the `/jobs` webapp view are per-page (25 jobs); a true grand total would need a server aggregate endpoint.
