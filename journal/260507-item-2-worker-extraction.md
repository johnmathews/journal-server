# Item 2 — `services/jobs/runner.py` worker extraction

Date: 2026-05-07

Closes item 2 from `docs/refactor-follow-ups.md`. `runner.py` was
1214 lines (over the soft cap and over the original Unit 4 DoD).
Workers were instance methods on `JobRunner`, so each was untestable
without standing up the full executor. Both bullets are now resolved.

## Final layout

| File | Lines | What |
|---|---:|---|
| `runner.py` | 423 | Submission API + executor + in-memory blob queues |
| `notifier.py` | 208 | `JobNotifier` — notify_*/get_strategy/try_pipeline_notification |
| `save_pipeline.py` | 186 | `submit_save_entry_pipeline` (3-children fan-out) |
| `retry.py` | 96 | `run_with_retry[T]` — exponential backoff for image + audio |
| `workers/__init__.py` | 60 | `WorkerContext` dataclass |
| `workers/audio_ingestion.py` | 105 | `run_audio_ingestion` |
| `workers/image_ingestion.py` | 115 | `run_image_ingestion` |
| `workers/entity_extraction.py` | 89 | `run_entity_extraction` |
| `workers/mood_score_entry.py` | 82 | `run_mood_score_entry` |
| `workers/reprocess_embeddings.py` | 68 | `run_reprocess_embeddings` |
| `workers/mood_backfill.py` | 58 | `run_mood_backfill` |
| `workers/entity_reembed.py` | 46 | `run_entity_reembed` |

## Architecture

`WorkerContext` is a frozen-shape dataclass holding every dependency
a worker function needs:

```python
@dataclass
class WorkerContext:
    jobs: SQLiteJobRepository
    notifier: JobNotifier
    extraction: EntityExtractionService
    reembedder: EntityReembedder
    mood_backfill: Callable[..., MoodBackfillResult]
    mood_scoring: MoodScoringService
    entries: EntryRepository
    ingestion: IngestionService | None
    pop_pending_images: Callable[[str], list[tuple[bytes, str, str]]]
    pop_pending_audio: Callable[[str], list[tuple[bytes, str, str]]]
    queue_post_ingestion_jobs: Callable[
        [str, str, int, int | None], dict[str, str]
    ]
```

`JobRunner.__init__` constructs the context once and passes it on
every `executor.submit`. The two oddballs that need runner-bound
state — the `_pending_images` / `_pending_audio` blob queues and the
`_queue_post_ingestion_jobs` follow-up dispatcher — are exposed as
callables on the context so workers stay free of any
`runner._pending_*` poke.

Each worker is a free function `run_<name>(ctx, job_id, params)`
with the same terminal-state guarantee the old methods had: every
exit path lands in `mark_succeeded` or `mark_failed`, and exceptions
never escape the executor.

`submit_save_entry_pipeline` got its own module because it doesn't
fit the worker shape (synchronous, called from the API thread, fans
out into 3 children + deferred dispatch + defensive sweep). The
`JobRunner` method is a thin delegating shim so api/ callers keep
their existing call site.

## Direct worker test

`tests/test_services/test_jobs/test_worker_entity_reembed.py`
exercises `run_entity_reembed` without constructing `JobRunner`. It
builds a `WorkerContext` from a real `SQLiteJobRepository`, a real
`JobNotifier`, and a hand-written reembedder fake — proves the seam
the original plan called the worker's "independently testable"
property.

## Decisions worth remembering

- **`WorkerContext` over per-function kwargs.** Free functions with
  explicit kwargs would have read cleaner inside each worker, but
  the executor.submit call site would have to know each worker's
  exact dependency set. The dataclass scales without churning the
  dispatch surface — adding a new worker dependency means one new
  field, not threading kwargs through `JobRunner.submit_*`.
- **Callables on the context for runner-bound state.** Image/audio
  workers used to read `self._pending_images` / `self._pending_audio`
  directly. Tests can't easily inject those dicts into a context.
  Exposing `pop_pending_images: Callable[[str], list[...]]` lets
  tests pass `lambda _: []` and lets production wire to the runner's
  dict.pop. Same shape for `queue_post_ingestion_jobs` —
  follow-up submission needs `self.submit_*` and stays bound to the
  runner.
- **`save_pipeline.py` as its own module.** It's 130-ish lines of
  load-bearing fan-out logic with thread-safety subtleties (the
  shared-connection race fix from item 1). Pulling it out of
  `runner.py` cut 100+ lines and reads better at the call site:
  the runner method becomes a 9-line shim that names the dependency
  set the orchestrator needs.
- **`run_with_retry[T]` uses PEP 695 type-parameter syntax.** Ruff's
  UP047 rule prefers it over `TypeVar`. Project is on Python 3.13
  so the syntax is supported.

## Verification

- `uv run pytest -m 'not integration'` → 1800 passed (1796 baseline
  + 4 new direct worker tests).
- `uv run ruff check src/ tests/` → clean.
- `runner.py` is 423 lines — comfortably under the 500-line target.
- The `max_workers=1` SQLite-threading invariant docstring is
  preserved verbatim on both the module header and the `JobRunner`
  class.

## What this unlocks

The remaining workers can each acquire direct unit tests on the same
seam if they grow new behaviour. Adding a new worker is now a single
file under `workers/` plus one `submit_*` method on `JobRunner`,
instead of ~80 lines wedged into a 1200-line class.
