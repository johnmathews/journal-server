# 2026-05-10 — Fitness W14: documentation completion

W14 is the doc-completion unit for the fitness pipeline. After W4–W13 shipped
the end-to-end server-side pipeline (schema → providers → fetch → normalize →
workers → REST → MCP → CLI re-auth → health → backfill → first live smoke),
the operator and engineer documentation surfaces were uneven: the four W13
smoke findings lived in a journal entry rather than `docs/`, and several
existing docs (configuration, external-services, api, jobs, roadmap, README,
architecture) had zero fitness coverage. This unit closes those gaps.

**No code changes.** Pure docs. `uv run pytest -m "not integration"` reports
**2131 passed** (unchanged from the W13 baseline). `uv run ruff check src/
tests/` is clean.

## What shipped

### Two new docs

1. **`docs/fitness-operations.md`** *(new — 18k)* — operator-facing runbook.
   Covers configuration prerequisites, initial re-auth (Strava + Garmin, with
   the headless-deployment workaround for the `:8400` port collision), the
   historical backfill flow, routine sync, status / health / integrity, an
   explicit troubleshooting section, and a "known limitations" appendix.

2. **`docs/fitness-pipeline.md`** *(new — 11k)* — engineer-facing data flow.
   Walks the four-layer pipeline (provider → fetch → raw → normalize)
   end-to-end with an ASCII diagram and a "where to look in code" table. The
   doc is deliberately thin; it points at `fitness-integration-plan.md` for
   decisions, `fitness-schema.md` for tables/columns, `fitness-tier-plan.md`
   for execution sequencing, and `fitness-operations.md` for runbooks.

### Eight existing docs updated

3. **`docs/architecture.md`** — added a "Fitness Pipeline" subsection under
   Storage Layer, with pointers to the four fitness docs. Kept the addition
   small so the architecture doc retains its prose-heavy character.

4. **`docs/external-services.md`** — added a "Fitness Data Sources" section
   between Stage 4 and Infrastructure Services with Strava and Garmin
   entries (auth model, rate limits, reliability notes, library pins).
   Updated the SDK Versions block with the actual `stravalib~=2.2` and
   `garminconnect==0.3.3` pins from `pyproject.toml`.

5. **`docs/configuration.md`** — added an "Optional — fitness integration"
   section documenting `STRAVA_*`, `GARMIN_*`, `FITNESS_BACKFILL_START`,
   `FITNESS_TRANSIENT_FAILURE_THRESHOLD`, and
   `FITNESS_HEALTH_BROKEN_DEGRADED_HOURS` with defaults, descriptions, and
   a cross-reference to `fitness-operations.md` for the headless workaround.

6. **`docs/api.md`** — extended `GET /api/health` with the per-user `fitness`
   block shape, added a "Fitness endpoints" REST cluster (five routes), and
   a "Fitness Tools" MCP cluster (eight tools including the three correlation
   queries). Response shapes are lifted from the actual code (`api/fitness.py`,
   `api/ingestion.py`, `mcp_server/tools/fitness.py`) so the doc is the API
   contract W15 will build against.

7. **`docs/jobs.md`** — bumped the job-types list from 8 to 10
   (`fitness_sync_strava`, `fitness_sync_garmin`), documented the
   `POST /api/fitness/sync/{source}` REST endpoint with its dedup posture,
   and added a result-payload section showing the `{fetch, normalize}`
   shape plus the `auth_broken` / `transient_failure` mark-failed semantics.

8. **`docs/roadmap.md`** — flipped Tier 1 #1 from
   "active, foundation + Strava provider shipped" to "server-side complete,
   W15 webapp pending"; updated the active-planning-docs entry to link both
   new docs; bumped the top-of-file status header.

9. **`README.md`** — added fitness to the project's "What It Does" tagline
   and inserted Fitness Pipeline + Fitness Operations into the Documentation
   list.

10. **`docs/fitness-tier-plan.md`** — top-level status header bumped to
    reflect W4–W14 done (only W15 remaining); added a "W14 ship note"
    callout summarising what landed and why no archives were performed;
    annotated the W14 section itself with the actual file list and three
    plan-drift notes vs the planned set.

## Doc-strategy decisions

**One ops doc, not several.** The open question in the W14 brief was whether
to fold fitness ops into `docs/development.md` (which already has a
"Local Full-Stack Quickstart"), create a single
`docs/fitness-operations.md`, or split into `fitness-operations.md` +
`fitness-troubleshooting.md`. `development.md` is 13k and squarely focused
on local-dev concerns (pytest config, chunking knobs, ChromaDB setup) —
mixing in re-auth and backfill recipes would hurt its discoverability.
A separate `fitness-operations.md` keeps the surface coherent. Splitting
troubleshooting into its own doc was rejected because the four W13 findings
fold naturally into a "Troubleshooting" + "Known limitations" section
inside operations; a separate doc would be < 3k and force the reader to
context-switch between two adjacent files.

**Engineer-facing pipeline overview was a real gap.** The brief asked
whether `fitness-pipeline.md` was needed or if the existing trio
(`fitness-integration-plan.md`, `fitness-schema.md`, `fitness-tier-plan.md`)
already covered it. Reading the existing trio confirmed they don't —
`integration-plan.md` is decisions and rationale, `schema.md` is DB-level,
`tier-plan.md` is execution sequencing. None answers "I'm a new engineer;
how does data flow from a Garmin watch to a query in the webapp?" An 11k
walkthrough closes the gap and avoids forcing readers to reverse-engineer
the layers from the work-unit-by-work-unit tier plan.

**Architecture.md kept small.** The plan suggested a "Fitness pipeline"
section in `architecture.md`; I added a small one (~250 words) with
pointers to the new dedicated doc rather than pulling the whole layered
diagram into the architecture overview. `architecture.md` is already 16k
and prose-heavy; bloating it to ~25k just to describe a parallel pipeline
that has its own home would have hurt the existing doc's readability.

## Archive decisions

**Nothing archived.** The brief explicitly invited considering whether
`fitness-tier-plan.md`, `fitness-integration-plan.md`, or `fitness-schema.md`
should be moved to `docs/archive/` per the project's docs-lifecycle
conventions. Decision: keep all three active.

- **`fitness-tier-plan.md`** — execution sequencing for W1–W15. With W14
  done, only W15 (webapp) remains. The plan is still active until W15
  ships; archiving it now would orphan the single remaining work unit.
- **`fitness-integration-plan.md`** — decisions and rationale (sacred raw
  archive, four-layer pipeline, daily-cadence single-user posture, library
  pins, Garmin reliability stance). Load-bearing context for any future
  fitness work — adding a new metric, deciding on cross-source dedup,
  evaluating the official Garmin Health API, etc. Not a planning doc that
  closes when work units ship; a decisions doc whose value persists.
- **`fitness-schema.md`** — concrete tables, columns, indexes, migration
  sequencing, and the canonical correlation queries. Load-bearing as
  long as the schema is in production; archive only when superseded by
  a different schema design (not happening on the W14/W15 timeline).

The W4–W14 journal series captures the per-unit history; the active docs
capture the current state. That's the right split — both kinds of
content survive, but only the load-bearing material clutters the active
`docs/` listing. The post-W15 doc audit will revisit the tier plan
specifically.

## Plan-drift notes

The tier plan's W14 file list (`docs/architecture.md`,
`docs/external-services.md`, `docs/jobs.md`, `docs/api.md`,
`docs/configuration.md`, `docs/roadmap.md`,
`journal/<YYMMDD>-fitness-implementation-summary.md`) drifted in three places:

1. **Two new docs not in the plan.** `fitness-operations.md` and
   `fitness-pipeline.md` weren't on the original list. Both came out of
   actually reading `development.md` and the existing fitness trio and
   discovering the gaps the W14 brief flagged. The plan was written
   pre-W13-smoke; the four findings the smoke surfaced (OAuth headless
   recipe, dense-backfill normalize quirk, Garmin transport-fallback noise,
   secrets-allowlist pattern) demanded a place for operators to land —
   `docs/operations.md` doesn't exist, and `architecture.md` is the wrong
   shape.
2. **README.md not in the plan.** The project tagline didn't mention
   fitness; that's a discoverability hit for any new operator landing on
   the GitHub page. Added.
3. **Implementation-summary journal entry redundant.** The plan called for
   `journal/<YYMMDD>-fitness-implementation-summary.md` consolidating
   W1–W13. The W4–W13 journal series already does that; the W13
   first-fetch entry is the natural consolidation point because it has
   the actual data counts. The W14 journal entry (this one) records
   only what W14 itself shipped.

This is the second unit in a row to land clean on the **paths** the plan
named — W13 ended a five-unit drift streak, and W14 only deviated on
*adding* docs not in the plan, not on misnaming existing ones.

## What's deferred (not for W14)

Three optional follow-ups from the W13 smoke entry remain explicitly out
of scope:

1. **`fitness-reauth-strava --code <code>` flag.** The clean fix for the
   port-collision and headless-VM cases (~10 lines of CLI + a unit test).
   Until shipped, the inline-python recipe in `fitness-operations.md` §2b
   is the documented workaround. `fitness-operations.md` §7 is honest
   about it being a workaround, not the long-term fix.
2. **W7 normalize watermark fix.** The dense-backfill `normalized < fetched`
   under-projection. Three implementation options (composite watermark,
   sub-second timestamps, tail-call force-renormalize per backfill) are
   recorded in `fitness-operations.md` §7 and `journal/260510-fitness-first-fetch.md` §6.
   Operators have a one-liner to recover today; pick a path next time
   the watermark logic is touched.
3. **Explicit `Rowing → other` activity-type map entry.** Documented as a
   known limitation in `fitness-operations.md` §7. Defensible as-is;
   `source_subtype` preserves the original Strava label.

## Test results

- `uv run pytest -m "not integration"`: **2131 passed**, 8 deselected
  (Chroma-dependent), 35 warnings. ~48s. No change from W13 baseline —
  W14 only touched docs.
- `uv run ruff check src/ tests/`: **All checks passed**. (Smoke check
  to confirm I didn't accidentally edit a code file.)

## What's still ahead in the tier plan

- **W15:** webapp views for fitness data. The 80 activities + 129 Garmin
  daily wellness rows + 80 Strava activities currently in production are
  real data the webapp can render against. Charts, list views, and the
  Strava ↔ Garmin distinct-workout reconciliation can be grounded in
  what's actually stored rather than synthetic fixtures. The
  Strava ↔ Garmin overlap (same workout in both sources, since the user
  uploads from Garmin → Strava) is the big design question for W15 —
  cross-source dedup is an analysis-time concern, not a storage-time
  one. Worth resolving early.

After W15, this tier plan can be revisited for archive (along with the
question of whether `fitness-integration-plan.md` and `fitness-schema.md`
have drifted enough from the running system to warrant the same).
