# Fitness tile-layout plan — T1 + T7 shipped

Date: 2026-05-11 (late). Plan:
[`docs/fitness-tile-layout-plan.md`](../docs/fitness-tile-layout-plan.md) — T2–T6 still
to come in a separate session.

## What landed

**T1** turned out to be a no-op on the implementation side: the
`/api/users/me/preferences` PATCH already accepts arbitrary JSON keys, so
`fitness_layout` just works. The plan called for an additive schema change; what
actually needed was a round-trip test pinning the contract from the webapp's
perspective, plus a co-existence test confirming `fitness_layout` and
`dashboard_layout` don't clobber each other. Test added; T1 closed.

**T7** is the substantive server-side change:

- Migration 0026 adds `workouts_fetched`, `wellness_fetched`,
  `workouts_normalized`, `wellness_normalized` to `fitness_sync_runs` (all
  `INTEGER NOT NULL DEFAULT 0`). Existing rows get the default; the legacy
  `rows_fetched` / `rows_normalized` columns stay populated as the
  workouts+wellness sum so anything that hasn't migrated to the split still
  reads sensible totals.
- New `_FetchCounts` dataclass threads workouts/wellness counts out of
  `_do_fetch_and_persist`. Strava returns `(workouts=N, wellness=0)`; Garmin
  separates the daily fan-in loop (wellness) from the activity loop (workouts).
  `run_sync` passes both into `finish_sync_run`.
- `normalize_strava` / `normalize_garmin` similarly pass per-bucket counts to
  `record_normalized_rows`. Strava is workouts-only by construction; Garmin
  tracks wellness during the daily fan-in and workouts during the activity loop.
- API `_sync_run_to_dict` exposes the four new fields on `last_runs`. The
  webapp reads them and renders separate "Workouts F/N" / "Wellness F/N"
  columns.

## Migration-runner change

The plan-level test `test_idempotent_rerun_from_pre_fitness_baseline` sets
`PRAGMA user_version = 22` and re-runs migrations 23+. Migrations 23–25 use
`CREATE TABLE IF NOT EXISTS` so they're no-op on re-run. My 0026 uses `ALTER
TABLE ADD COLUMN`, and SQLite doesn't support `ADD COLUMN IF NOT EXISTS`, so
the second pass errored with `duplicate column name: workouts_fetched`.

Fix: wrap `executescript` in `_executescript_idempotent` that catches
`OperationalError` messages starting with `duplicate column name:` and treats
them as no-ops. The catch is deliberately narrow — anything else propagates.
Documented in the module docstring under "Re-runnability invariant". Cleared
the standing global rule (every migration must be re-runnable after partial
failure) without needing to write a CREATE-TABLE-then-COPY-rows workaround
for what should be a two-line `ALTER`.

## What I'd do differently

1. **The plan over-scoped T1.** I wrote "additive schema change" when the actual
   work was already done. Reading the preferences endpoint code at planning
   time would have caught this. The plan-time cost of code-reading exists for
   precisely this reason — and I had explicit memory feedback about it
   (`feedback_length_caps.md` mentions reading code before specifying changes).
2. **The migration runner's re-runnability invariant should have been
   documented before today.** It's load-bearing for the test suite. The
   module docstring now spells it out, but the failure mode could have been
   anticipated by anyone reading 0019, 0020, or any earlier ALTER TABLE
   migration. (Probably none of them were re-run via that test pattern.)
3. **The 4-column split on the UI is borderline cluttered.** Wider than the
   2-column "Fetched / Norm." it replaces, but the user explicitly asked for
   the split. Worth checking in person after deploy whether the column header
   text ("Workouts F/N") is legible enough or if it needs a two-line header
   with "Fetched" / "Normalized" stacked underneath the bucket name.

## Cross-references

- Plan: [`docs/fitness-tile-layout-plan.md`](../docs/fitness-tile-layout-plan.md)
- Webapp counterpart: `webapp/journal/260511-fitness-tile-layout-t7.md` (T7 webapp side)
- Next session: T2 (TileGrid extraction from DashboardView) → T3 (fitness adoption)
  → T4 (layout persistence via preferences). Hand-off prompt provided at the
  end of this session.
