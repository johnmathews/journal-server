**Status:** active. **Last updated:** 2026-05-11. **Supersedes:** none — picks up after
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

### D4. Tooltip parity is its own work unit, not part of TileGrid extraction

The user said "the way tooltips are implemented is not the same" but didn't specify what
differs. F6 already aligned `interaction.mode='index'` + the styling colors, so the remaining
gap is likely about *content* (which fields are shown, formatting), *positioning*, or
*hover-delay behaviour on specific charts*. Without a concrete diff, scoping this is guessing.

Treat it as a discovery unit that lands as one or more concrete adjustments after a side-by-side
visual diff against the dashboard. See **Open questions** at the bottom.

### D5. Fitness tiles inventory (initial set)

The chart tiles currently rendered on `/fitness` map to these `FITNESS_TILES` entries:

1. `weekly-distinct` — distinct workouts per week (stacked bar), span 2.
2. `sleep` — Sleep score line+MA, span 1.
3. `hrv` — HRV overnight line+MA, span 1.
4. `rhr` — Resting heart rate line+MA, span 1.
5. `recent-workouts` — recent workouts table, span 2.

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

### T1. Extend `/api/preferences` to accept `fitness_layout` `[server]`

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

### T2. Extract `TileGrid.vue` from DashboardView `[webapp]`

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

### T3. Add fitness tile inventory and adopt `TileGrid` on `/fitness` `[webapp]`

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

### T4. Persist fitness layout via preferences `[webapp]`

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

### T5. Drag-and-drop on fitness tiles `[webapp]`

- **Priority:** Medium.
- **Risk:** Low (TileGrid owns the handlers; this is just wiring).
- **Size:** S.
- **Changes:** Already covered by T2's extraction if `TileGrid` exposes handlers cleanly.
  This unit only exists as a checkpoint — if T2 + T3 + T4 ship and drag-drop works on
  `/fitness`, this unit is satisfied without code.
- **Test impact:** Already covered by T2 / T3 tests.
- **Dependencies:** T2, T3.
- **Acceptance:** Dragging a tile reorders it; release commits the new order.

### T6. Tooltip parity audit and fix `[webapp]`

- **Priority:** Medium.
- **Risk:** Low (per-chart options tweaks, no shared-state changes).
- **Size:** S (assuming the gap is one or two options-object diffs; expand if discovery
  reveals deeper divergence).
- **Spike first:** Open dashboard and `/fitness` in two side-by-side browser tabs after T3
  ships. For each fitness chart, hover the dashboard equivalent (or closest analogue) and
  diff: tooltip content (fields shown, ordering), tooltip position relative to cursor, hover
  delay perception, tooltip border / shadow / padding, multi-series ordering, value
  formatting (units, precision). Record the diff as a checklist in the work unit before
  changing code.
- **Changes:** Per-chart options tweaks in `FitnessView.vue`. Where the gap is general,
  push the fix into `buildLineChartOptions` so future charts inherit it. Update
  `webapp/docs/chart-style-guide.md` if any rule changes.
- **Test impact:** Snapshot or attribute assertions on the generated options object where
  feasible; otherwise visual verification.
- **Reversibility:** Revert commit.
- **Dependencies:** T3 (need the post-tile-extraction state to diff against).
- **Acceptance:**
  1. Side-by-side hover on a fitness chart vs. the matching dashboard chart shows identical
     tooltip content shape and positioning.
  2. Any rule changes documented in the chart style guide.

## Open questions

1. **Tooltip specifics — what differs?** The user flagged tooltips as not matching but didn't
   say what. Before T6, ask for a screenshot or written description of the specific behaviour
   the user wants matched (content, position, timing). Without that, T6 risks "fixing" something
   the user didn't actually mean.
2. **Default tile widths on `/fitness`.** Sleep / HRV / RHR are individually narrow today
   (three side-by-side panels). Should the default be three narrow tiles (span=1) on a 2-col
   grid (wraps to one tile per row on the second row), or one of them wide and one narrow?
   The dashboard's default mix is 50/50 narrow-and-wide; the fitness default should mirror
   that aesthetic.
3. **Per-tile config (which metric, which window) — out of scope?** The dashboard's tiles are
   fixed in content; only their layout is configurable. Treating fitness the same way for this
   plan, but worth noting if the user later wants e.g. swapping out Sleep score for Body
   Battery on a tile.
