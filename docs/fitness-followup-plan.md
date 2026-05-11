**Status:** active. **Last updated:** 2026-05-11. **Supersedes:** none — this is a follow-on plan picked up after
[`fitness-multiuser-plan.md`](./fitness-multiuser-plan.md) reaches its W14 staging gate.

# Fitness follow-up plan — UI consistency, dedup correctness, and sync-run accounting

A round of fixes and consistency work on the fitness surface, driven by user review on 2026-05-11.
Two are correctness bugs (one in the server's sync-run accounting, one in the webapp's cross-source
workout dedup); the rest are UI consistency work that aligns `/fitness` with the dashboard's
established chart and range/bin patterns and reorganises the long `/settings` page.

The multiuser plan's W14 (end-to-end verification with a second user) stays parked here until a
second Strava account exists; nothing in this plan blocks W14, and W14 doesn't block anything here.

## Why now

The fitness page has had enough use that two concrete behavioural problems have surfaced (Norm. is
always 0; dedup misses obvious cross-source pairs) plus a clear set of UI inconsistencies that
would be cheap to address while the surface is still warm. Doing them as one batch is preferable to
trickling fixes — the chart work in particular benefits from extracting a shared component pattern
that several panels will reuse.

## Decisions & tradeoffs

These are the cross-cutting decisions the plan rests on. Work units operationalise them but don't
re-litigate them.

### D1. Cross-source workout dedup is overlap-based, not start+duration-based

The current algorithm requires `|start_strava - start_garmin| ≤ 90s` AND `|duration_strava -
duration_garmin| ≤ 30s` ([`webapp/src/stores/fitness.ts:28-29`](../../webapp/src/stores/fitness.ts)).
The 30s duration tolerance fails on the 2026-05-09 run (42m Strava vs 41m Garmin → 60s diff), and
will fail on any activity where one source reports moving-time and the other total-time, or where
one source counts an extended cooldown the other doesn't.

The replacement is **time-window overlap**: two activities are the same if their `[start, end)`
intervals (in UTC) overlap by more than a threshold of the *shorter* activity's duration. The
physical premise — a user cannot be doing two activities simultaneously — is robust. Choose
threshold = **75%** of the shorter activity's duration: high enough that two genuinely distinct
back-to-back activities don't merge, low enough to tolerate the moving-time vs total-time skew. UTC
normalisation is already done on ingest (`_normalize_iso` /
`_gmt_str_to_iso` in [`normalize.py:482-501`](../src/journal/services/fitness/normalize.py)), so a
timezone trip doesn't perturb anything.

Rejected alternative: start-time-only matching. Loose enough to catch the moving-vs-total case but
unsafe — two morning yoga sessions starting within the window would collapse. Overlap protects
against this.

Rejected alternative: tighten duration tolerance further. The data shows the divergence is
real and recurring; widening the duration tolerance to compensate (e.g. 90s) papers over the cause
and creates a different failure mode where short activities can merge incorrectly.

### D2. `rows_normalized` is recorded on the fetch's sync_run, not as a separate run

Today the fetch service creates `fitness_sync_runs` and calls `finish_sync_run(rows_fetched=N)`;
normalize never touches that row except via `_record_drift_if_any`, which inserts a *separate*
`normalize_drift` row when drift > 0 ([`normalize.py:440-458`](../src/journal/services/fitness/normalize.py)).
`rows_normalized` on the success row is always 0.

The fix is to thread the `run_id` returned by `fetch.run()` into `normalize.run()` and have
normalize call `repo.finish_sync_run(run_id, ..., rows_normalized=N)` to amend the existing row.
The alternative — emitting a second sync_run row for the normalize step — would double the row
count in the UI and conflate "the sync attempt" with "what the sync did".

Drift handling stays as-is (separate row with `status=normalize_drift`) so operators can still
spot the drift in `last_runs` independently of the success row.

### D3. Range + Bin controls become a shared component, not a copy-paste

The dashboard has a `RANGE` (Last month / 3 / 6 months / Last year / All time) and `BIN WIDTH`
(Week / Month / Quarter / Year) selector inline in `DashboardView.vue`. Copy-pasting it into
`/fitness` would create immediate drift. Extract `RangeBinControls.vue` with typed props/emits
mirroring the dashboard's existing `DashboardRange` / `DashboardBin` types. Dashboard adopts the
component in the same unit as `/fitness` — non-negotiable, otherwise it's not actually shared.

Range / bin state for `/fitness` lives on the fitness store, not on URL query params, to match
how the dashboard does it today. Out of scope: URL-deep-link state (separate concern, not raised).

### D4. Chart interactivity for `/fitness` reuses the dashboard's options builder

The dashboard's chart options (hover crosshair, legend toggle behaviour, colors, grid lines, axis
formatting) are produced via a builder in [`webapp/src/utils/chartjs-config.ts`](../../webapp/src/utils/chartjs-config.ts).
Fitness charts construct options ad-hoc and diverge. Consolidate into a single
`buildLineChartOptions(...)` and `buildSeriesToggleHandler(...)` exported from
`chartjs-config.ts`, with both surfaces consuming it. Acceptance: a user toggling a series on
`/fitness` sees identical interaction (cursor, fade, legend marker) to the dashboard.

### D5. Moving averages are a presentation overlay, not a stored series

For Sleep / HRV / RHR, compute a centred 3-day moving average client-side from the existing daily
series and render as the bold line; render daily values as a faded scatter or thin line beneath.
Server doesn't materialise an MA series. Keeps the data layer simple and lets the user change the
window later without a server round-trip. Edge handling: forward-fill on the first/last days
(window truncated rather than skipped) so the line spans the full range visibly.

### D6. Plan is a single document spanning both repos

The multiuser plan demonstrated that one cross-cutting plan with `[server]` / `[webapp]` tags per
unit reads better than parallel plans. Same pattern here. Roadmap links to this doc; both repos'
journal entries link back here per work unit.

## Non-goals

1. **Not** changing the underlying fitness data model or schema. F1 amends an existing row's
   counters; no new columns, no migration.
2. **Not** adding new fitness metrics or panels. Scope is interactivity, layout, and accuracy of
   what's already there.
3. **Not** redesigning the chart visual language. Style cues (faded daily under bold MA) follow
   established conventions; no new color palette work.
4. **Not** building dashboard tab persistence (e.g. remembering which Settings tab was last
   visited across navigations). If the user wants this later it's a separate unit.
5. **Not** addressing W14 (second-user end-to-end verification). Stays parked on the multiuser
   plan; gated on availability of a second Strava account.
6. **Not** revisiting whether wellness rows (sleep/HRV/RHR) should produce a separate `Norm.` count
   or share the activities count. Current behaviour — all rows roll into one `rows_normalized` —
   stays. F1 just makes the count truthful.

## Ordering rationale

Foundation-first then risk-first: D2 (server bug) and D1 (webapp dedup bug) ship first because they
are correctness fixes that surface truth in the UI everyone reads, and they are isolated from the
restructuring work. Then the shared component extractions (D3, D4) because the layout work depends
on them. Then layout / overlays / docs.

## Work units

### F1. Fix `rows_normalized` always-0 on success runs `[server]`

- **Priority:** Critical (the UI is currently lying about whether normalisation happened).
- **Risk:** Low. Single row UPDATE on an existing row; no schema change; behaviour change is
  monotonic (0 → real count).
- **Size:** S.
- **Changes:**
  - [`src/journal/services/fitness/normalize.py`](../src/journal/services/fitness/normalize.py):
    `normalize_strava(...)` and `normalize_garmin(...)` take an optional `sync_run_id: str | None`
    parameter; when provided, call `repo.finish_sync_run(sync_run_id, status="success",
    rows_fetched=<unchanged>, rows_normalized=N)` after the row loop. Drift path stays separate.
  - [`src/journal/services/jobs/workers/fitness_sync_strava.py`](../src/journal/services/jobs/workers/fitness_sync_strava.py)
    and `fitness_sync_garmin.py`: pass `fetch_result.run_id` into the normalize call.
  - [`src/journal/db/fitness_repository.py`](../src/journal/db/fitness_repository.py): extend
    `finish_sync_run` so a second call with the same `run_id` updates `rows_normalized` without
    clobbering `rows_fetched` or `status` (idempotent amend). Read the existing implementation
    (line 388) before changing the signature.
- **Test impact:**
  - `tests/services/fitness/test_normalize.py`: extend the existing success tests to assert the
    sync_run row's `rows_normalized` matches the upsert count. Drift tests stay as-is.
  - `tests/services/jobs/workers/test_fitness_sync_strava.py` and `test_fitness_sync_garmin.py`:
    add an assertion that after a successful run, the `fitness_sync_runs` row has
    `rows_normalized > 0` when rows are present.
  - `tests/db/test_fitness_repository.py`: add a test for the idempotent-amend behaviour of
    `finish_sync_run` (calling twice with `run_id` updates `rows_normalized` on the second call,
    leaves `status` and `rows_fetched` alone).
- **Reversibility:** Pure code change; revert commit. No persistent state migration. Existing
  `Norm. = 0` rows in `fitness_sync_runs` stay as historical noise — they're not retroactively
  correct, but they're not wrong in a way that breaks anything either. No need to backfill.
- **Dependencies:** None.
- **Acceptance:**
  1. After a successful Garmin sync that pulled N wellness rows, the `last_runs` panel shows
     `Norm. = N` (or at minimum > 0).
  2. The `_record_drift_if_any` path still emits a separate row tagged `normalize_drift` — drift
     row appears in addition to the success row when drift > 0.
  3. All existing tests pass.

### F2. Cross-source workout dedup → time-window overlap `[webapp]`

- **Priority:** High. UI shows duplicate rows for activities that the user knows are the same.
- **Risk:** Medium. The function is pure but it's load-bearing for the activities table, the
  weekly-distinct chart, and any downstream analytic; touching it without tests catching all
  three call sites would create subtle counting bugs.
- **Size:** M.
- **Changes:**
  - [`webapp/src/stores/fitness.ts`](../../webapp/src/stores/fitness.ts): replace `dedupActivities`
    with an overlap-based implementation. Algorithm: sort all rows by `start_time` (UTC), then
    for each pair `(a, b)` with `start_b < end_a`, compute
    `overlap = min(end_a, end_b) - max(start_a, start_b)` and `shorter = min(duration_a,
    duration_b)`; merge if `overlap / shorter >= 0.75`. Pick a deterministic `representative` —
    prefer the longer activity, break ties by source order (Strava first), then by `source_id`.
    Drop the `DEDUP_DURATION_TOLERANCE_S` constant; rename `DEDUP_START_TOLERANCE_MS` →
    `DEDUP_OVERLAP_THRESHOLD = 0.75` (typed `number`, in [0, 1]).
  - [`webapp/src/views/FitnessView.vue`](../../webapp/src/views/FitnessView.vue) line 642: update
    the footer text from "rows from both Strava and Garmin within ±90s collapse to one entry" to
    something accurate, e.g. "rows from both Strava and Garmin whose time windows overlap by
    ≥75% collapse to one entry."
- **Test impact:**
  - [`webapp/src/stores/__tests__/fitness.test.ts`](../../webapp/src/stores/__tests__/fitness.test.ts):
    the existing dedup test (line 399) covers the simple case; add cases that the old algorithm
    would have failed on:
    1. Strava 42m and Garmin 41m at same start → merge.
    2. Strava 60m starting at 08:00 and Garmin 58m starting at 08:01 (moving-time scenario) →
       merge.
    3. Two genuinely distinct 30m runs starting 35m apart (so end-of-first overlaps start-of-second
       by 0 minutes) → no merge.
    4. UTC timezone correctness: Strava in `Z` form, Garmin originally in non-UTC tz that
       `_normalize_iso` converted → merge if they describe the same window. (This is really
       testing the normalisation upstream is doing its job, but worth pinning here.)
    5. Three-source case (Strava + 1 Garmin + 1 phantom Garmin that doesn't overlap) → 2 distinct.
  - Tests for `recentActivities` and `distinctActivities` count consumers stay unchanged but rerun
    on new fixture data.
- **Reversibility:** Pure code change; revert commit. No persisted state affected — dedup is
  computed on read, so reverting restores the old behaviour for the next render.
- **Dependencies:** None. Independent of F1.
- **Acceptance:**
  1. The 2026-05-09 case (42m Strava run, 41m Garmin run) shows as ONE row in `Recent workouts`,
     labelled `Strava + 1 mirror`.
  2. Two distinct activities on the same day that don't overlap stay as two rows.
  3. All cases in the test list above pass.

### F3. `/settings` becomes a tabbed view `[webapp]`

- **Priority:** Medium.
- **Risk:** Low. The sections already exist as discrete components under
  `webapp/src/components/settings/`; this is a layout wrapper change.
- **Size:** M.
- **Changes:**
  - [`webapp/src/views/SettingsView.vue`](../../webapp/src/views/SettingsView.vue): wrap existing
    sections in a tab strip mirroring the `/admin` view's pattern. Tabs (in order): Profile,
    Notifications, Fitness, Maintenance. The Fitness tab is empty in this unit — populated by F4.
  - Read the `/admin` view first ([`webapp/src/views/admin/`](../../webapp/src/views/admin/)) to
    confirm the tab strip pattern, focus behaviour, and active-class styling, then reuse rather
    than re-invent. If `/admin` uses a shared `TabStrip.vue` component, lift it to
    `components/layout/`; if it's inline, extract before adopting in `/settings`.
  - Update `webapp/src/router/` if any deep-link anchor like `/settings#fitness` is used (the
    multiuser plan's W11 wires a "Reconnect" button to that anchor — preserve it; tabs key off
    the hash).
- **Test impact:**
  - [`webapp/src/views/__tests__/SettingsView.spec.ts`](../../webapp/src/views/__tests__/):
    update to assert the tab strip renders, the right tab is initially active, clicking a tab
    swaps the panel, and that the `#fitness` hash auto-selects the Fitness tab.
  - Existing per-section component tests (profile, notifications, maintenance) keep passing
    unchanged; they're imported into tabs but not refactored.
- **Reversibility:** Pure layout change; revert commit. Settings sections aren't moved or
  renamed.
- **Dependencies:** None.
- **Acceptance:**
  1. `/settings` shows four tabs; switching between them swaps content without a network round
     trip.
  2. `/settings#fitness` opens with the Fitness tab pre-selected (FitnessAuthBanner reconnect
     button keeps working).
  3. Page height per tab is < the current single-page total.

### F4. Move Strava + Garmin sync panels into the Settings Fitness tab `[webapp]`

- **Priority:** Medium.
- **Risk:** Low. The panels are self-contained.
- **Size:** S.
- **Changes:**
  - Extract the two sync panels (the article elements at
    [`FitnessView.vue:~440-551`](../../webapp/src/views/FitnessView.vue)) into a
    `components/settings/FitnessSyncPanels.vue` (taking `source` as a prop or rendering both
    internally — internal is fine if the markup stays parallel).
  - Mount `FitnessSyncPanels` in the Fitness tab created by F3.
  - Remove the panels from `FitnessView.vue`. Keep the auth banner and the error row.
  - On `/fitness`, add a small inline link like "Manage sync → Settings · Fitness" next to the
    page header so users can still find them in one click.
- **Test impact:**
  - New: `components/settings/__tests__/FitnessSyncPanels.spec.ts` mirroring whatever the old
    inline tests covered (sync-now button states, error rendering, last_runs table, auth-status
    transitions).
  - Existing `views/__tests__/FitnessView.spec.ts`: remove assertions on the sync panels;
    assert the "Manage sync" link is present and points to `/settings#fitness`.
- **Reversibility:** Pure layout move; revert commit.
- **Dependencies:** F3 (tab needs to exist).
- **Acceptance:**
  1. Both sync panels render in `/settings#fitness` with full behaviour preserved.
  2. `/fitness` no longer shows the sync panels.
  3. The auth banner still appears site-wide when `auth_status === 'broken'`.

### F5. Extract `RangeBinControls.vue` and mount on `/fitness` `[webapp]`

- **Priority:** Medium.
- **Risk:** Low–Medium. Dashboard adopting the extracted component must not regress any of its
  existing range/bin behaviour. Read the dashboard's existing handlers
  ([`DashboardView.vue:1059-1080`](../../webapp/src/views/DashboardView.vue)) before extracting.
- **Size:** M.
- **Changes:**
  - New: `webapp/src/components/RangeBinControls.vue` with props
    `range: DashboardRange`, `bin: DashboardBin`, `availableRanges?`, `availableBins?` and emits
    `update:range`, `update:bin`. Markup is the floating chip strip in the dashboard's "RANGE /
    BIN WIDTH" header.
  - `DashboardView.vue`: replace inline buttons with `<RangeBinControls>`. Verify the
    `onRangeChange` / `onBinChange` handlers still fire identically.
  - `FitnessView.vue`: add `<RangeBinControls>` at the top of the page, wired to fitness store
    range/bin state. On change, refetch `loadActivities` and `loadDaily` with the new window.
  - `webapp/src/stores/fitness.ts`: add `range` and `bin` refs (defaults: `last_3_months`,
    `week`) and a derived `dateWindow` computed; route `loadActivities`/`loadDaily` through it.
- **Test impact:**
  - New: `components/__tests__/RangeBinControls.spec.ts` — clicking each chip emits the right
    update event; disabled state respected; ARIA roles match the dashboard's existing pattern.
  - `views/__tests__/DashboardView.spec.ts`: re-point any selector that finds the inline buttons
    to the new component; behaviour assertions unchanged.
  - `stores/__tests__/fitness.test.ts`: add tests that changing range/bin triggers refetches with
    the right window.
- **Reversibility:** Pure code change; revert commit.
- **Dependencies:** None (but worth doing before F6/F7 so the chart work has the right window
  state to test against).
- **Acceptance:**
  1. Dashboard range/bin chips look and behave identically to before.
  2. `/fitness` shows the same chip strip; clicking a range refetches activities and daily
     series for the new window.
  3. The visible activity/daily counts in the page header update accordingly.

### F6. Unify chart interactivity across dashboard and `/fitness` `[webapp]`

- **Priority:** Medium.
- **Risk:** Medium. Touches every chart on both pages; visual regression is the main risk.
- **Size:** M (could spill to L if hidden divergence surfaces — see "Spike note" below).
- **Spike note:** Before writing the consolidated options builder, spend ~30 minutes diffing
  dashboard chart options vs fitness chart options to enumerate where they diverge today (axis
  formatters, legend `onClick`, tooltip mode, color sets). If the divergence is broader than
  expected (e.g. fitness uses a different y-axis scale strategy that's intentional, not
  accidental), split this unit into F6a (extract builder, adopt on dashboard) and F6b (port
  fitness charts onto it) before proceeding.
- **Changes:**
  - [`webapp/src/utils/chartjs-config.ts`](../../webapp/src/utils/chartjs-config.ts): export
    `buildLineChartOptions({ yAxisLabel, hover, legend, ... })` and any related helpers; the
    dashboard's existing inline option objects move here, parameterised where they vary by
    chart and frozen where they don't.
  - Dashboard charts (entities, mood, writing stats): adopt the builder.
  - Fitness charts (Sleep, HRV, RHR, weekly distinct, etc.): adopt the builder. Series-toggle
    behaviour (clicking the legend), hover crosshair, tooltip formatting, color palette, grid
    line opacity — all driven from the shared config.
- **Test impact:**
  - `utils/__tests__/chartjs-config.spec.ts`: assert the builder returns options with the
    expected hover/legend/tooltip shape and that color palette resolves correctly in light/dark
    mode.
  - Visual regression: not in test suite — verify in browser per the dev-server runbook in
    `webapp/CLAUDE.md`. Walk each affected chart, screenshot before/after, confirm parity.
- **Reversibility:** Pure code change; revert commit. Inline options can be restored if the
  consolidation breaks something subtle.
- **Dependencies:** None; doesn't strictly need F5, but easier to verify alongside it.
- **Acceptance:**
  1. Hovering a fitness chart shows the same crosshair / tooltip styling as the dashboard.
  2. Clicking a legend entry on a fitness chart toggles its visibility and fades the legend
     marker, matching the dashboard.
  3. Light/dark mode color sets are consistent across both surfaces.
  4. Browser walk-through confirms no visual regressions on the dashboard.

### F7. 3-day moving-average overlay on Sleep / HRV / RHR `[webapp]`

- **Priority:** Medium.
- **Risk:** Low.
- **Size:** S.
- **Changes:**
  - In FitnessView's Sleep / HRV / RHR panel rendering: compute a centred 3-day MA from the
    daily series, render as the bold primary line, render daily values as a faded line beneath
    (alpha ~0.25, no markers). Edge handling: window truncates at the series edges (asymmetric
    average over 2 points or 1) rather than producing NaN gaps.
  - Use the unified options builder from F6 so hover shows both daily and MA values in the
    tooltip.
- **Test impact:**
  - `views/__tests__/FitnessView.spec.ts` or a colocated MA-helper test if extracted: cover
    the centred-MA helper with known inputs (incl. edge truncation and missing-day gaps).
- **Reversibility:** Pure code change; revert commit.
- **Dependencies:** F6 (so tooltip / styling is consistent).
- **Acceptance:**
  1. Sleep / HRV / RHR panels show a smooth bold trend line and a faded daily series beneath.
  2. Hover shows both values for the hovered date.
  3. The first and last days are visible (no NaN gap at the edges).

### F8. Chart style guide `[docs]`

- **Priority:** Medium.
- **Risk:** Low.
- **Size:** S.
- **Changes:**
  - New: `webapp/docs/chart-style-guide.md` documenting the options builder, the canonical
    interaction pattern (hover crosshair, legend toggle, tooltip format), color palette, grid
    line conventions, and the "bold MA + faded daily" pattern from F7.
  - Add a "Charts" subsection to `webapp/docs/architecture.md` linking to the style guide and
    naming `chartjs-config.ts` as the canonical entry point.
  - Reference from `webapp/CLAUDE.md` "Tech Stack" section so it's discoverable from the project
    root.
- **Test impact:** None (docs only).
- **Reversibility:** Pure docs; revert commit.
- **Dependencies:** F6 (the builder needs to exist to be documented), F7 (the MA pattern is
  documented as a worked example).
- **Acceptance:**
  1. A new chart added by following the guide produces visually consistent output without
     consulting either existing implementation.
  2. The guide is findable from `webapp/CLAUDE.md` and `webapp/docs/architecture.md`.

## Open follow-ups (not in this plan)

These came up during intake but didn't warrant inclusion in this batch.

1. Today's Garmin `Norm. = 0` despite `Fetched = 15`: F1 will reveal whether this is purely the
   accounting bug or whether wellness rows are genuinely being skipped. Re-evaluate after F1 ships
   — if `Norm.` is still 0 for a non-zero `Fetched`, file a separate bug.
2. The `Fetched` column is currently misleading because it pools workouts and wellness rows.
   Considered splitting into `Workouts / Wellness` but rejected for this batch — the column
   would need a schema-level distinction in `fitness_sync_runs.notes_json` to populate, and the
   real ask (legibility) is better solved by F1 making `Norm.` accurate than by adding columns.
3. Persist last-used tab in `/settings` across navigations — see Non-goals (4).
