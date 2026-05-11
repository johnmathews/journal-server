# Fitness tile-layout plan — T2 + T3 + T4 closed (no server code)

Date: 2026-05-11 (late, second pass). Plan:
[`docs/fitness-tile-layout-plan.md`](../docs/fitness-tile-layout-plan.md).

## What landed (on the server)

Only the plan doc moved: T2, T3, T4 marked closed and T5 marked
"closed by model substitution" (same arrow-button reorder model
adopted on `/fitness` as the dashboard already shipped — no native
HTML5 drag-drop, because the dashboard never had it). The status
header updated accordingly.

The actual T2/T3/T4 work is all webapp-side; see
[`webapp/journal/260511-fitness-tile-layout-t2-t4.md`](../../webapp/journal/260511-fitness-tile-layout-t2-t4.md)
for the design notes and what-I'd-do-differently.

T1 had closed in the prior session because
`/api/users/me/preferences` already accepts arbitrary JSON keys; T4
reads / writes `fitness_layout` against that endpoint and the
contract test from T1 pins the round-trip.

## Plan status after this commit

| Unit | Status | Notes |
|------|--------|-------|
| T1   | closed 2026-05-11 | server contract test added; no schema/route change |
| T2   | closed 2026-05-11 | `TileGrid.vue` extracted, dashboard adopts it |
| T3   | closed 2026-05-11 | `FITNESS_TILES` + fitness store layout state |
| T4   | closed 2026-05-11 | `fitness_layout` round-trips via preferences API |
| T5   | closed 2026-05-11 (model substitution) | arrow buttons, not drag-drop |
| T6   | closed 2026-05-11 | tooltip parity shipped same day as plan amendment |
| T7   | closed 2026-05-11 | Garmin Workouts/Wellness split |

The plan is effectively done. Next move on `/fitness` (e.g. per-tile
metric pickers — swap Sleep score for Body Battery) is explicitly
out of scope per the plan's open-questions resolution.

## Cross-references

- Plan: [`../docs/fitness-tile-layout-plan.md`](../docs/fitness-tile-layout-plan.md)
- Webapp counterpart: [`../../webapp/journal/260511-fitness-tile-layout-t2-t4.md`](../../webapp/journal/260511-fitness-tile-layout-t2-t4.md)
- Prior session: [`./260511-fitness-tile-layout-t1-t7.md`](./260511-fitness-tile-layout-t1-t7.md)
