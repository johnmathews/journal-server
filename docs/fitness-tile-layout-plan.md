**Status:** active. **Last updated:** 2026-05-11 (T2–T4 closed; T5 superseded — same
arrow-button reorder model adopted on `/fitness` rather than HTML5 drag-drop, which
the dashboard never shipped). **Supersedes:** none — picks up after
[`archive/fitness-followup-plan.md`](./archive/fitness-followup-plan.md) (F1–F8) closed.

# Fitness tile-layout plan — bring dashboard tile customization to /fitness

User feedback after the F1–F8 deploy (2026-05-11): the charts on `/fitness` should adopt the
dashboard's full design language and customization model, not just the Range/Bin chips and the
hover behaviour shipped in F5/F6. Concretely:

1. Rearrange charts by drag-and-drop.
2. Hide / show charts.
3. Resize charts wide or narrow (1- or 2-column span).
4. Edit-layout mode that gates the above (matching the dashboard's "Edit layout" button).
5. Layout persists per user (server-side preference).
6. Tooltip implementation that matches the dashboard (the user flagged this as different — exact
   delta TBD; see "Open questions" below).

This is the second pass on `/fitness` chart consistency: F5 shipped the Range/Bin chips, F6 the
shared line-chart options builder. The remaining gap is the *layout shell* — the tile container,
the edit-mode controls, the drag-drop reordering, the persistent layout state.

## Why now

The user reviewed the deploy and explicitly asked for it. The infrastructure already exists on the
dashboard (`DashboardLayout` type, `tileOrder` / `hiddenTiles` / `tileWidths` store state,
`editingLayout` flag, drag-and-drop wiring, server-side preference persistence). The work is
largely an *extraction* — generalise the dashboard's tile shell into something `/fitness` can
adopt, then mount fitness charts inside it.

## Decisions & tradeoffs

### D1. Extract a generic `TileGrid` component shared by Dashboard and Fitness

The dashboard's tile layout is currently woven into `DashboardView.vue` — tile definitions live in
`types/dashboard.ts`, store state lives on the dashboard store, drag handlers live inline. Two
options:

1. **Extract a `TileGrid.vue` component** that takes a typed list of tile definitions, a layout
   state object, and slot renders for each tile body. Dashboard and Fitness both consume it.
2. **Duplicate the pattern on the fitness store** without extracting a shared component.

Choose **1**. Duplication would immediately drift (the F5/F6 work in this repo demonstrates how
quickly two near-identical UIs diverge). The lift is real — the dashboard's tile system isn't
trivially generic today — but the alternative is a third "consistency follow-up plan" six months
from now.

Rejected sub-option: extract only the *types* (`TileDef`, `TileLayout`) and leave the render
inline. Tried this informally during scoping — the inline render is where the divergence happens
(edit-mode buttons, drag handles, width toggles), so types alone don't prevent drift.

### D1a. Tile widths: support thirds on fitness, keep halves on dashboard

User asked (2026-05-11 follow-up review) for three width options on `/fitness`: **1/3**, **1/2**,
**full**. The dashboard today supports only 1/2 (`span: 1`) and full (`span: 2`) on a 2-column
grid, and its tests pin specific options shapes that would break under a width-model rewrite.

Resolution: parameterize `TileGrid` on **column count**. Adopters declare the grid (e.g.
dashboard = 2 cols, fitness = 6 cols) and the named widths they offer. The tile data layer uses
*named* widths (`'third' | 'half' | 'full'`) so the type reads cleanly; `TileGrid` maps each
named width to a CSS `grid-column: span N` based on its column count:

- 2-col grid: `half` = span 1, `full` = span 2. Dashboard semantics unchanged.
- 6-col grid: `third` = span 2, `half` = span 3, `full` = span 6.

Adopters can omit named widths they don't support (e.g. dashboard only declares `'half' | 'full'`).
This keeps dashboard adoption a behavioural no-op and gives fitness the three-width choice.

Rejected sub-options:

- *Migrate dashboard to 6 columns too* — its current half/full split was chosen with the dashboard
  tile inventory in mind, and re-flowing it risks visible regressions. Defer until there's a
  reason.
- *Allow arbitrary numeric spans on every grid* — too unconstrained. Named widths surface the
  design choice (third / half / full) instead of hiding it behind a number.
- *Add a fourth width (2/3)* — not asked for; would push the strip of width-toggle buttons past
  what fits comfortably in the edit-mode UI.

### D2. Layout state lives on the *consuming* store, not on the TileGrid

`TileGrid` is a pure presentation component. The fitness store owns `tileOrder`, `hiddenTiles`,
`tileWidths`, `editingLayout` (mirroring the dashboard store). Persistence goes through the same
`preferences` API the dashboard uses today — add a `fitness_layout` key alongside
`dashboard_layout`.

Rejected alternative: a shared "layouts" store with sub-namespaces. Adds indirection for no
real saving — both stores already exist and own everything else about their domain.

### D3. Persist via the existing `/api/preferences` endpoint

Add `fitness_layout` as a top-level key on the preferences object. Server-side: extend the
preferences schema to accept it; webapp: extend the typed wrapper. No new endpoint needed.
Layout schema mirrors `DashboardLayout` exactly — `{ tileOrder: FitnessTileId[], hiddenTiles:
FitnessTileId[], tileWidths?: Partial<Record<FitnessTileId, 1 | 2>> }`.

Migration: if the user has no `fitness_layout` saved, fall back to a default order baked into
`FITNESS_TILES`. No server-side migration — the new key is opt-in, absent = use defaults.

### D4. Tooltip parity (resolved 2026-05-11)

User followed up with a specific diff: the daily-wellness tooltips were too verbose because
each dataset label repeated the chart panel's title (`"HRV overnight (ms) (3-day avg): 71"` /
`"HRV overnight (ms) (daily): 76"`). Fix: drop the panel-title prefix from the dataset labels
so the tooltip reads `"3-day avg: 71"` / `"Daily: 76"`. Same change applies to Sleep score and
Resting HR panels.

Shipped 2026-05-11 alongside this plan amendment (single-commit fix in `FitnessView.vue` —
the F7 helper signature loses its unused `label` argument). Chart style guide (`webapp/docs/
chart-style-guide.md`) updated to codify the rule: "panel header names the metric; dataset
labels disambiguate within the chart only".

T6 below is closed because the only remaining tooltip ask was this label fix.

### D5. Fitness tiles inventory + default widths

The chart tiles currently rendered on `/fitness` map to these `FITNESS_TILES` entries (default
widths per D1a):

1. `weekly-distinct` — distinct workouts per week (stacked bar). Default: **full**.
2. `sleep` — Sleep score line+MA. Default: **third**.
3. `hrv` — HRV overnight line+MA. Default: **third**.
4. `rhr` — Resting heart rate line+MA. Default: **third**.
5. `recent-workouts` — recent workouts table. Default: **full**.

Three thirds in a row matches the current side-by-side layout the user already accepted, so the
default visual is byte-equivalent to today. Users opting into wider Sleep / HRV / RHR is the
new degree of freedom.

The page header, the Manage-sync link, and the Range/Bin chip strip are **not** tiles — they're
fixed page chrome. Same convention as `/admin` and the dashboard. Errors banners
(`fitness-status-error` / `fitness-activities-error` / `fitness-daily-error`) also stay outside
the grid.

## Non-goals

1. **Not** adding new chart types. This plan is about the *shell*, not new content.
2. **Not** changing what the existing tiles display. Sleep / HRV / RHR keep their bold-MA +
   faded-daily pattern from F7.
3. **Not** building drag-and-drop between Dashboard and Fitness, or any cross-page layout
   sharing. They're independent grids.
4. **Not** redesigning the dashboard's tile system. The extraction should preserve its current
   behaviour to the byte — visual diffs on the dashboard are a regression.
5. **Not** building a "reset to defaults" affordance unless the dashboard has one (it doesn't
   today). Defer.

## Ordering rationale

Foundation-first: T1 (server prefs extension) and T2 (TileGrid extraction) unblock everything
else; T3 adopts the grid on Dashboard (no visible change, but proves the extraction is faithful);
T4 adopts on Fitness with the new tile inventory. T5 wires persistence. T6 is the tooltip
discovery + parity pass — sits at the end because it depends on having the layout work shipped
to compare side-by-side. Risk-front-loaded: T2 and T3 are the highest-risk units (regression
on the dashboard) and ship first.

## Work units

### T1. Extend `/api/preferences` to accept `fitness_layout` `[server]` — closed 2026-05-11

Turned out the endpoint already accepts arbitrary JSON keys, so no schema or
endpoint change was needed. Shipped a round-trip test pinning the contract
and a co-existence test confirming `fitness_layout` and `dashboard_layout`
don't clobber each other. Original work-unit body kept below as a record.

- **Priority:** Medium.
- **Risk:** Low — additive schema change, no new endpoint, no migration.
- **Size:** S.
- **Changes:**
  - `src/journal/api/settings.py` (or wherever preferences live): allow `fitness_layout` as a
    JSON-object key alongside `dashboard_layout`. Same loose-shape validation pattern.
  - Tests: extend the preferences round-trip test to cover `fitness_layout`.
- **Test impact:** New test in the preferences module. No regressions expected.
- **Reversibility:** Revert commit. No persisted state implications — extra keys in
  preferences are inert until the webapp reads them.
- **Dependencies:** None.
- **Acceptance:** A PUT to `/api/preferences` with `{"fitness_layout": {...}}` round-trips and a
  subsequent GET returns it.

### T2. Extract `TileGrid.vue` from DashboardView `[webapp]` — closed 2026-05-11

Shipped. `TileGrid<TId>` is generic over the page's tile-id union; the
consumer provides `tiles`, `tileOrder`, `hiddenTiles`, `editing`,
`gridClass`, `getSpan`, optional `getWidthTitle`, and `testIdPrefix`,
and gets events for `move` / `hide` / `show` / `cycle-width` / `reset`.
`sectionEls` is exposed via `defineExpose` so the dashboard's calendar
heatmap can still size against the tile's `<section>`. Dashboard
adopted first; all 59 existing tile-editing tests pass unchanged.

Original work-unit body kept below as a record.



- **Priority:** Medium.
- **Risk:** Medium. Touching the dashboard's tile rendering is exactly the kind of refactor that
  introduces subtle drag-drop or layout-edit regressions; manual visual diff is required.
- **Size:** L. Recommend a spike commit first that prototypes the extraction on a feature
  branch, verifies dashboard parity via screenshot diff, then refines into landable form.
- **Changes:**
  - New: `webapp/src/components/TileGrid.vue` taking
    `tiles: TileDef[]`, `layout: TileLayout`, `editing: boolean`, and slot renders.
  - New: `webapp/src/types/tiles.ts` with shared `TileDef`, `TileLayout`, `TileSpan` types
    (move out of `types/dashboard.ts` and re-export from there for backward compat).
  - `DashboardView.vue`: replace inline tile-grid markup with `<TileGrid>`. No store
    changes; props are bound to the dashboard store as before.
- **Test impact:**
  - New: `components/__tests__/TileGrid.spec.ts` covering tile order, hiding, width toggling,
    edit-mode gating, and drag-drop handlers.
  - Existing dashboard tests: re-point selectors if the testids change (avoid — keep the
    existing dashboard testids stable by passing a `test-id-prefix` prop, same pattern as
    `RangeBinControls`).
  - Browser walk-through on the dashboard to confirm zero visible regression.
- **Reversibility:** Pure code change; revert. The component's API is internal so the blast
  radius is bounded.
- **Dependencies:** None (T1 is parallel).
- **Acceptance:** Dashboard renders, drags, hides, and resizes tiles identically to today. No
  visible diff in screenshot review.

### T3. Add fitness tile inventory and adopt `TileGrid` on `/fitness` `[webapp]` — closed 2026-05-11

Shipped. `FITNESS_TILES` ships five tiles (weekly-distinct, sleep, hrv,
rhr, recent-workouts) on a 6-column grid. Default widths per D5:
weekly-distinct + recent-workouts = `full`, sleep/hrv/rhr = `third` —
three thirds in a row matches the prior side-by-side daily-wellness
layout. Fitness store gained the mirror surface: `tileOrder`,
`hiddenTiles`, `tileWidths` (named widths `'third' | 'half' | 'full'`),
`editingLayout`, `moveTile`, `hideTile`, `showTile`, `resetLayout`,
`cycleTileWidth`, `setTileWidth`, `getTileWidth`, `applyLayout`.
FitnessView wraps each chart in a `<TileGrid>` named slot; page chrome
(header / Manage-sync link / RangeBinControls / error banners) stays
outside the grid.

Original work-unit body kept below as a record.



- **Priority:** Medium.
- **Risk:** Low–Medium. The fitness charts already exist as standalone canvas renders; the
  work is mostly wrapping them in tile slots.
- **Size:** M.
- **Changes:**
  - New: `webapp/src/types/fitness.ts` (or extension): `FITNESS_TILES` constant matching D5
    above, `FitnessTileId`, default layout.
  - `webapp/src/stores/fitness.ts`: add `tileOrder`, `hiddenTiles`, `tileWidths`,
    `editingLayout` refs; mirror the dashboard store's getters (`visibleTileIds`,
    `tileDefById`, etc.).
  - `webapp/src/views/FitnessView.vue`: wrap each chart in a tile slot inside `<TileGrid>`.
    Page header, Manage-sync link, and Range/Bin chips stay outside the grid.
- **Test impact:**
  - `views/__tests__/FitnessView.test.ts`: add tile-renders-and-toggles tests. Existing
    chart-rendering tests stay green (charts still mount — they're just wrapped now).
  - New: `stores/__tests__/fitness.test.ts` cases for the layout state mirroring the
    dashboard store coverage.
- **Reversibility:** Revert commit. No persisted state changes (T5 is separate).
- **Dependencies:** T2.
- **Acceptance:**
  1. `/fitness` renders the five tiles in default order.
  2. Edit-layout mode toggles handles + width controls visible.
  3. Drag-drop reorders tiles in-page.

### T4. Persist fitness layout via preferences `[webapp]` — closed 2026-05-11

Shipped. Round-trip wired through the existing
`fetchPreferences` / `updatePreferences` API. Mutations debounce to a
single PUT 500ms after the last edit; `loadLayout` fires on
FitnessView mount. `layoutLoaded` ref gates persistence so mutations
*before* the initial GET settles don't race with the inbound layout
and overwrite it with defaults. Fetch / update errors are swallowed
silently — the in-memory layout is the session source of truth and
the next mutation retries the save.

Acceptance check for deploy: hide a chart, reload the page, confirm
it stays hidden. Confirmed in test coverage (`stores/__tests__/
fitness.test.ts` — "tile layout persistence (T4)" describe block).

Original work-unit body kept below as a record.



- **Priority:** Medium.
- **Risk:** Low.
- **Size:** S.
- **Changes:**
  - `webapp/src/api/preferences.ts`: add `fitness_layout` to the typed wrapper.
  - `webapp/src/stores/fitness.ts`: mirror the dashboard store's `saveLayout` /
    `loadLayout` actions for the fitness key. Debounce same-tick layout edits the same way
    the dashboard does (avoid PUT-per-drag).
- **Test impact:**
  - Store tests: layout round-trip via the preferences mock.
- **Reversibility:** Revert commit. A user's stored `fitness_layout` key becomes inert (the
  reverted code stops reading it). No data loss; key stays in the prefs blob.
- **Dependencies:** T1, T3.
- **Acceptance:** Reload the page; layout persists. Edit on one browser, see it on another
  (preferences are server-side).

### T5. Drag-and-drop on fitness tiles `[webapp]` — closed 2026-05-11 (model substitution)

The dashboard never shipped native HTML5 drag-drop — reorder is via
the per-tile up/down arrow buttons in edit mode. T2's extraction
preserved that model, and T3 inherited it. So `/fitness` users can
reorder tiles via the same arrow controls dashboard users have. No
drag-drop code shipped.

If the user later asks for *literal* drag (release-to-drop), that's a
new unit (`@dragstart` / `@dragover` / `@drop` handlers on `TileGrid`'s
section, with the parent store's `setTileOrder` action receiving the
final array). Not done because nothing in the user feedback that
motivated this plan specifically asked for native drag over the arrow
controls.



- **Priority:** Medium.
- **Risk:** Low (TileGrid owns the handlers; this is just wiring).
- **Size:** S.
- **Changes:** Already covered by T2's extraction if `TileGrid` exposes handlers cleanly.
  This unit only exists as a checkpoint — if T2 + T3 + T4 ship and drag-drop works on
  `/fitness`, this unit is satisfied without code.
- **Test impact:** Already covered by T2 / T3 tests.
- **Dependencies:** T2, T3.
- **Acceptance:** Dragging a tile reorders it; release commits the new order.

### T6. Tooltip parity — daily-wellness label fix `[webapp]` — closed 2026-05-11

Shipped same-day as this plan amendment. The only outstanding tooltip ask after F6 was that
the dataset labels on the daily-wellness panels (Sleep / HRV / RHR) repeated the panel-header
text, making the tooltip read `"HRV overnight (ms) (3-day avg): 71"` instead of just
`"3-day avg: 71"`.

Fix: `renderLineChart` in `FitnessView.vue` drops the unused `label` parameter; the two
datasets now use fixed labels `'3-day avg'` and `'Daily'`. Tests updated to find the sleep
chart by its border color (the unique-label identifier is gone). Chart style guide updated
with the rule.

Kept in the plan as a closed record so future readers can see what "tooltip parity" actually
shook out to.

### T7. Split Garmin "Fetched" / "Norm." into workouts vs. wellness `[server + webapp]` — closed 2026-05-11

Shipped end-to-end: migration 0026, fetch + normalize plumbing, API
serialisation, webapp `FitnessSyncRun` type update, and `FitnessSyncPanels`
source-aware column rendering. The legacy `rows_fetched` / `rows_normalized`
columns stay populated as the workouts+wellness sum so any unmigrated
consumer keeps working. Pre-T7 rows fall back to the legacy total in the
most-likely bucket via `formatCounts` in the webapp.

Also took a related housekeeping change: the migration runner now tolerates
"duplicate column name" errors as no-ops so `ALTER TABLE ADD COLUMN`
migrations are idempotent for the
`test_idempotent_rerun_from_pre_fitness_baseline` test. SQLite has no
native `IF NOT EXISTS` clause for `ADD COLUMN`.

Original work-unit body kept below as a record.



Carried forward from the closed [`archive/fitness-followup-plan.md`](./archive/fitness-followup-plan.md)
"Open follow-ups" item #2: the Garmin Recent-runs table reports one `Fetched` count that pools
workouts (Garmin activities) and wellness rows (Sleep / HRV / RHR / etc.), which obscures what
each sync actually pulled. With F1 making `Norm.` accurate, the next legibility win is the
split.

- **Priority:** Medium.
- **Risk:** Medium. Schema migration on `fitness_sync_runs`, plus the fetch and normalize
  services need to split their counters. Garmin is the affected source; Strava is workouts-only
  so its wellness columns are always 0.
- **Size:** M.
- **Changes:**
  - **Server schema:** new migration adds two nullable INTEGER columns to `fitness_sync_runs`:
    `workouts_fetched`, `wellness_fetched` (and parallel `workouts_normalized`,
    `wellness_normalized`). The existing `rows_fetched` / `rows_normalized` columns stay (sum of
    the two for backward compat); the new columns are the source of truth for the UI.
  - `src/journal/db/fitness_repository.py`: extend `finish_sync_run` and
    `record_normalized_rows` signatures to accept the per-bucket counts. Models
    (`FitnessSyncRun`) add the two new fields.
  - `src/journal/services/fitness/fetch.py`: the Garmin fetch service already iterates raw
    rows per endpoint — track workouts vs. wellness counts during the loop and pass them to
    `finish_sync_run`. Strava passes `wellness_fetched=0`.
  - `src/journal/services/fitness/normalize.py`: `normalize_garmin` already separates the
    daily fan-in from the activity loop — track and report both counts via
    `record_normalized_rows` (extend its signature).
  - `src/journal/api/fitness.py`: serialise the new fields on the `last_runs` response.
  - **Webapp:** `FitnessSyncPanels.vue` table grows two columns (or merges into a single
    "Fetched (workouts / wellness)" presentation — to be decided during the unit based on
    column-count constraints). Strava table can hide the wellness column since it's always 0.
- **Test impact:**
  - Migration test: existing rows get NULL in the new columns; existing tests pass unchanged.
  - `tests/test_db/test_fitness_repository.py`: round-trip the new fields.
  - `tests/test_services/test_fitness/test_fetch.py`,
    `tests/test_services/test_fitness/test_normalize.py`: assert the per-bucket counts.
  - `tests/test_services/test_jobs/test_worker_fitness_sync.py`: re-snapshot the worker
    result envelope if it changes.
  - Webapp: `FitnessSyncPanels.spec.ts` covers the new columns.
- **Reversibility:** Revert webapp commit; the server migration leaves NULL columns that the
  reverted UI ignores. Down-migration isn't needed (additive columns).
- **Dependencies:** None hard. Independent of T1–T6.
- **Acceptance:**
  1. Garmin sync at 22:20 today's data shows wellness count > 0 and workouts count = 0 (no
     activity); future days with a run show workouts ≥ 1 and wellness ≥ 1.
  2. Strava panel shows only the workouts column (wellness always 0; hidden).
  3. Sum of the two equals the legacy `rows_fetched` / `rows_normalized` value the column
     used to display, so the migration is observable but not contradictory.

## Open questions

All resolved 2026-05-11 in the same review pass that produced T7 and the tooltip fix:

1. ~~Tooltip specifics~~ — resolved: drop chart-title prefix from dataset labels. T6 above.
2. ~~Default tile widths on `/fitness`~~ — resolved: three thirds (Sleep / HRV / RHR) +
   two fulls (weekly-distinct, recent-workouts). See D5.
3. **Per-tile content config remains out of scope.** Confirmed: layout (order, width,
   hide/unhide) is configurable; *what* each tile displays is not. Out-of-scope for this
   plan; if the user later wants a per-tile metric picker (e.g. swap Sleep score for Body
   Battery), that's a separate feature.
