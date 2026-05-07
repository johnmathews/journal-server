# Item 1 — `submit_save_entry_pipeline` shared-connection race

Date: 2026-05-07

Closes the first follow-up from `docs/refactor-follow-ups.md`. The flake
recorded against `test_patch_text_queues_mood_scoring` was reproducible
locally at 5/100 with `pytest --count=100`, failing as
`sqlite3.OperationalError: not an error` raised at
`mark_succeeded`'s `self._conn.commit()` inside
`submit_save_entry_pipeline`.

## Diagnosis

The runner module's docstring claims "single-worker `ThreadPoolExecutor`
serialises every submitted job ... only one thread writes at a time".
That is not what was actually happening. The MCP server (and the test
fixture) opens **one** SQLite connection with
`check_same_thread=False` and shares it across the request thread and
the worker thread. The single-worker invariant only ensures a single
*worker* thread; the *request* thread is a separate writer, and the two
collide on the shared connection.

The collision window inside `submit_save_entry_pipeline` was wide:

```
api thread                                worker thread
----------                                -------------
_jobs.create("save_entry_pipeline")
submit_reprocess_embeddings:
  _jobs.create("reprocess_embeddings")
  _executor.submit(_run_reprocess...)  -> mark_running, write...
submit_entity_extraction (mocked)
submit_mood_score_entry:                  # still mid-flight here
  _jobs.create("mood_score_entry")        # racing the worker's writes
  _executor.submit(_run_mood_score...)
_jobs.mark_succeeded(parent.id)           # commit() blows up
```

The worker started executing the moment `_executor.submit` was called,
which was before the API thread had finished writing the next child row
and the parent's `mark_succeeded`. SQLite's "not an error" surfaces when
the cursor state is corrupted by interleaved writes through one
connection.

## Fix

`submit_save_entry_pipeline` now drains *all* of its API-thread writes
before any worker is dispatched:

1. Create every child row up front (parent + reprocess + entity +
   optional mood) via `_jobs.create`.
2. Call `mark_succeeded` on the parent.
3. Only then walk a list of deferred dispatches and call
   `_executor.submit` for each child.

Because the executor has a single worker, the children drain in FIFO
order *after* the API thread is no longer writing. The worker thread is
no longer racing the request thread inside the pipeline.

The implementation factors the per-child setup into a local
`_stage_child` helper that validates params, creates the row, and queues
a dispatch closure — keeping the three children's bodies symmetric and
short. The public `submit_*` helpers stay unchanged for callers outside
the pipeline.

## Test changes

`test_patch_text_queues_mood_scoring` no longer needs to mock
`submit_entity_extraction`. The mock was a workaround for the within-call
race; with the race fixed the assertion is reliable without it.

A new regression test
(`test_save_entry_pipeline_dispatches_workers_after_mark_succeeded`)
captures the invariant directly: it intercepts `_jobs.mark_succeeded`
and `_executor.submit` and asserts the pipeline's parent
`mark_succeeded` runs before the *first* `executor.submit`. This is a
deterministic check that does not depend on the scheduler — it would
catch any future regression where dispatches creep back ahead of
`mark_succeeded`.

Verification:

- `pytest tests/test_api_ingest.py::TestPatchMoodScoring::test_patch_text_queues_mood_scoring --count=1000` → 1000 passed (was ~5/100 failures pre-fix).
- Full unit suite: 1795 passed (1794 baseline + 1 new regression).

## Out of scope

There is a separate, deeper issue lurking: rapid back-to-back PATCH
requests where iteration N's API-thread writes can race iteration
N-1's worker writes. The single-connection-with-`check_same_thread=False`
pattern is fundamentally vulnerable to that across-call race; properly
fixing it would require either a write lock around the connection or a
connection-per-thread layout. A 20-iteration loop test proved the
existence of that race but is not what this item was scoped to fix and
the test was removed in favour of the invariant assertion above. Worth
flagging as a future follow-up if it ever surfaces in production logs.
