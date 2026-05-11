# Tolerate shared-connection commit race in `SQLiteJobRepository`

## Context

Production crash 2026-05-11 12:10:06: editing an entry triggered a
reprocessing pipeline; the `mood_score_entry` worker failed at
`jobs_repository.py:111` with

```
sqlite3.OperationalError: cannot commit - no transaction is active
```

before any Anthropic API call was made. Sibling job
`c7ac5fd5...` reached `succeeded` in the same second on the same
shared `sqlite3.Connection` — same class of race already documented
in [`docs/sqlite-threading.md`](../docs/sqlite-threading.md), new
flavour.

The user also asked whether the deployed app uses their Claude.ai
subscription or the paid API (since they'd hit their Claude.ai limit
around the same time). Verified by inspecting prod env: the app reads
`ANTHROPIC_API_KEY` and constructs `anthropic.Anthropic(api_key=...)`
in `providers/mood_scorer.py` — that's the paid API, billed per token
and entirely separate from the Claude.ai subscription. The two share
no quota. So the user's Claude.ai limit had zero causal relationship
to this crash; the failure was purely the SQLite race in
`mark_running`, before mood scoring even started its LLM call.

Discussed switching to Postgres as a possible architectural fix.
Concluded against it: the bug lives in Python's `sqlite3` driver
pattern (shared `Connection` across threads), not in SQLite the
database. Postgres would solve it for the wrong reason and at much
higher cost (port 22 migrations + FTS5/JSON-extract rewrites + new
ops surface). The right-sized fix is per-thread `sqlite3.Connection`
objects — see the plan doc that landed in this commit.

## Changes shipped

1. **Narrow workaround in `SQLiteJobRepository`.** New
   `_commit(op: str)` helper catches the specific `OperationalError`
   with `"no transaction is active"` in its message, logs a WARNING
   pointing at the plan doc, and continues. All eight commit sites
   in the repo route through it. The pending UPDATE/INSERT is already
   persisted by the concurrent writer's transaction in the race
   scenario, so swallowing the error doesn't lose data — and the log
   keeps the residual hazard observable. Any other `OperationalError`
   (`database is locked`, schema errors, etc.) still propagates.

2. **Five regression tests** in
   `tests/test_db/test_jobs_repository.py::TestSharedConnectionCommitRace`.
   Four of them wrap the connection in a `_RacyConn` proxy that
   simulates the prod failure deterministically: when the repo's UPDATE
   matches, the proxy commits the underlying connection (persisting the
   pending row as if a concurrent writer captured it) and then arms
   the proxy's own `commit()` to raise the exact prod
   `OperationalError`. The fifth test confirms unrelated
   `OperationalError`s still raise — narrowness check on the workaround.
   Without the workaround all four race tests fail with the exact
   prod stack; with it, all five pass.

3. **New plan doc:
   [`docs/sqlite-per-thread-connections-plan.md`](../docs/sqlite-per-thread-connections-plan.md)**
   for the structural fix (W1–W5). Indexed from
   `docs/roadmap.md` under "Active planning docs"; cross-referenced
   from `docs/sqlite-threading.md` with the 2026-05-11 update note.

## Diagnostic notes

- Could not reproduce the exact `OperationalError` with a plain
  double-`commit()` in a single-threaded reproducer on either Python
  3.13.11 (local) or 3.13.13 (prod) — both silently no-op. The error
  is only surfaced by a specific multi-thread interleaving on a
  shared `Connection`. The test injects the failure at the proxy
  layer to make it deterministic; the post-state we assert (the
  UPDATE is persisted, no exception escapes) matches what prod
  would have produced had the workaround been in place.
- `docs/sqlite-threading.md` already covers why per-repo locks
  aren't sufficient; this incident is the second time that has
  bitten. The plan doc commits us to retiring the shared-connection
  model.

## Files

- `src/journal/db/jobs_repository.py` — `_commit()` helper + 8 call
  sites routed through it.
- `tests/test_db/test_jobs_repository.py` — `TestSharedConnectionCommitRace`
  added (5 tests, 1 `_RacyConn` proxy helper).
- `docs/sqlite-per-thread-connections-plan.md` — new plan, active.
- `docs/sqlite-threading.md` — update note linking to the new plan.
- `docs/roadmap.md` — plan registered under "Active planning docs".
