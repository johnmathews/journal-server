# Fitness follow-up F1 shipped — rows_normalized accounting fix

Date: 2026-05-11. Plan: [`docs/archive/fitness-followup-plan.md`](../docs/archive/fitness-followup-plan.md)
(closed same day).

## What landed

Single server-side work unit out of the fitness-followup-plan F1–F8 batch:

- **F1** — `record_normalized_rows(run_id, count)` on `FitnessRepository` amends the existing
  `fitness_sync_runs` row without disturbing `rows_fetched` or `status`. Threaded the run id
  through both `normalize_strava` and `normalize_garmin` via a new `sync_run_id` kwarg, and
  wired both job workers (`fitness_sync_strava`, `fitness_sync_garmin`) to pass
  `fetch_result.run_id` in.

Worker test contract updated so the success-path assertions check that normalize receives
`sync_run_id=<the fetch's id>`. Three new repository / normalize tests cover the
amend-without-clobber behaviour, including a guard test asserting that CLI / backfill paths
(which don't pass `sync_run_id`) leave existing sync_runs rows untouched.

## What it fixes

The webapp's "Recent runs" panel on Settings · Fitness showed `Norm. = 0` on every successful
sync, regardless of how many rows were actually persisted. Cause: the fetch service finalises
the sync_run row with `rows_fetched=N` before normalize runs; only the drift path subsequently
touched that row, and only via a separate `normalize_drift` insert. On success, nothing ever
updated `rows_normalized` so it stayed at its default of 0.

Cause-of-cause: the original W7 design treated normalize as a downstream consumer of raw rows,
not as a continuation of the same "sync attempt". Tying them via `sync_run_id` keeps the
fetch-then-normalize sequence as one logical sync but two physical phases, which matches what
the UI is trying to show.

## Verified in production

Post-deploy syncs at 11 May 22:20:

- Strava: Fetched 1, Norm. 1 — one new activity (the user's run that day).
- Garmin: Fetched 6, Norm. 2 — six raw wellness rows fanned into two daily rows (likely two
  days got updates).

Pre-deploy rows in `fitness_sync_runs` still read `Norm. = 0` because they were finalised under
the old code path. Deliberate: those values aren't retroactively correct but they're inert (no
downstream consumer relies on historical `rows_normalized`), so no backfill. The plan called
this out explicitly under "Reversibility".

## What I'd do differently

Nothing — F1 was a clean find-the-bug-write-the-test-fix-the-bug cycle. The bug was a
behavioural gap in the seam between two services, not in either service alone. The fix lands at
the seam (a new repo method that's the contract between them), which is where the cause-of-
cause actually lived.

One thing the original ticket got right that I'd repeat: the explicit "drift handling stays as-
is — separate row" decision. Conflating drift into the success row's accounting would have
muddied operator-facing diagnostics. Keep failure modes addressable as distinct rows.

## Cross-references

- Plan: [`docs/archive/fitness-followup-plan.md`](../docs/archive/fitness-followup-plan.md) (F1).
- Webapp counterpart entry: `webapp/journal/260511-fitness-followup-shipped.md` (F2–F8, all UI).
- Next: [`docs/fitness-tile-layout-plan.md`](../docs/fitness-tile-layout-plan.md) (T1 is the
  one server-side unit — adding `fitness_layout` to the preferences schema).
