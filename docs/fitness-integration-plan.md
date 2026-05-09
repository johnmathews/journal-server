# Fitness Integration Plan

**Status:** active — foundation phase shipped 2026-05-09 (see [`fitness-tier-plan.md`](./fitness-tier-plan.md)).
**Last updated:** 2026-05-09 (Strava rate-limit figures corrected against current docs).
**Created:** 2026-05-08. **Supersedes:** none.
**Related docs:** [`architecture.md`](./architecture.md), [`external-services.md`](./external-services.md),
[`jobs.md`](./jobs.md), [`mood-scoring.md`](./mood-scoring.md), [`roadmap.md`](./roadmap.md),
[`fitness-schema.md`](./fitness-schema.md).
**Code-grounded:** yes — `src/journal/{providers,services,api,mcp_server,db}/` reviewed before writing.

This document captures the architectural decisions and constraints for adding personal fitness data
(activities, sleep, recovery metrics) as a complementary data source alongside journal entries. It
is the single source of truth for **what we're building and why**. Concrete schema lives in
[`fitness-schema.md`](./fitness-schema.md); execution sequencing will be drafted once schema is
settled (planned `fitness-tier-plan.md`, not yet written).

> **Discipline note.** This plan is intentionally a decisions-and-rationale doc, not an
> execution plan. Prefer keeping it short, but length is not capped — if scope or detail genuinely warrants more, add
> it; if a section is restating background, cut it. Update the *Last updated* field on every change. Mark superseded
> sections in place rather than deleting, so future agents can see what changed.

## Contents

1. [Goal](#1-goal)
2. [Scope](#2-scope)
3. [Decisions (with alternatives considered)](#3-decisions-with-alternatives-considered)
4. [Code surface](#4-code-surface-grounded-in-existing-conventions)
5. [Operational discipline](#5-operational-discipline-locked-in-posture)
6. [Resolved questions](#6-resolved-questions)
7. [Kill criteria](#7-kill-criteria)
8. [References](#8-references)

---

## 1. Goal

Ingest, store, and expose personal fitness data (Garmin watch + Strava) so it can be correlated
with journal entries — answering questions like "how does mood track with weekly mileage?" and
"what was I writing about during my high-training-load weeks?" Data must be available via MCP for
consumption by other applications, not just the journal webapp.

## 2. Scope

**In scope (this initiative):**
- Strava activities (runs, rides, swims, walks) via the official OAuth API.
- Garmin Connect data (sleep, HRV, Body Battery, training load, training readiness, stress) via
  the unofficial `python-garminconnect` library.
- Daily and per-activity metrics at metric units (km, m, kg, °C, bpm, ms).
- Raw payload archive (precious data — assume it cannot be re-fetched).
- MCP tools and REST endpoints for query/correlation.
- Operational alerting that distinguishes auth-broken from transient failures.

**Explicitly out of scope (this initiative):**
- The official Garmin Health API (gated to approved partners; not available for personal projects).
- `.fit` file ingestion (possible later as archival format; not now).
- Real-time / sub-daily polling. Daily cadence only.
- Multi-user fitness data. Single-user (`user_id = 1`) at the storage layer for now, but schema
  carries `user_id` so multi-user is a future migration, not a rewrite.
- Wearable integrations beyond Garmin/Strava (Whoop, Oura, Apple Health, etc.).
- Workout planning, training load forecasts, or any predictive modelling.

---

## 3. Decisions (with alternatives considered)

Each decision lists what we picked, what we considered and rejected, and the load-bearing reason.
This section is the durable part of the plan — keep it accurate even when downstream details
change.

### D1. In-process module, not a separate service.
**Picked:** fitness ingestion lives inside `journal-server` as a feature area, following the same
conventions as entity-extraction and mood-scoring (flat providers, services subpackage, prefixed
tables, dedicated API and MCP modules).
**Considered:** standalone `fitness-server` with its own MCP/REST surface; the webapp would
aggregate.
**Why:** the project's primary value is *correlation* between fitness and journal data. Splitting
forces those joins into the webapp layer over HTTP, which is slower and more complex than SQL
joins on a shared database. Operational cost of two services (CI, deploys, secrets, auth, backups,
monitoring) is real for one developer. Code-level isolation (separate package, separate schema
namespace, separate background job, separate MCP namespace) gives ~90% of the failure-isolation
benefit at a fraction of the cost. Extraction to a service later is a refactor, not a rewrite —
the internal boundaries make it cheap if it's ever needed.
**Trigger to revisit:** another consumer wants the data without going through `journal-server`,
the fitness pipeline genuinely needs independent scaling, or fitness ingestion needs a runtime
(language/process) journal-server cannot provide.

### D2. Strava primary, Garmin secondary enrichment.
**Picked:** Strava is the stable backbone (official OAuth API). Garmin via `python-garminconnect`
provides Garmin-only metrics (sleep, HRV, Body Battery, training readiness/load) on top.
**Considered:** Garmin-only (richer data, but fragile); Strava-only (stable, but missing recovery
metrics); the official Garmin Health API (not available to personal projects).
**Why:** evidence-based reliability research (see `journal/260508-fitness-integration-planning.md`
for sources) shows `python-garminconnect` had ~3.5 weeks of total breakage in March–April 2026
when Garmin changed SSO and the upstream `garth` library was deprecated. Plan for ≥1 multi-week
Garmin outage per year. Strava is sanctioned, stable, and rate-limit-generous for personal use.
Designing the system so Garmin can be down for weeks without losing existing data or breaking
journal ingestion is non-negotiable.
**Trigger to revisit:** if Garmin permanently kills unofficial library access (then either fall
back to Strava-only or pivot to FIT-file ingestion); if we get access to the official Health API
(then prefer it).

### D3. Four-layer pipeline with sacred raw archive.
**Picked:** strict separation of (1) **fetch**, (2) **persist raw**, (3) **normalize**,
(4) **integrate**. Raw payloads are stored verbatim with provenance and treated as append-only.
Normalization is idempotent and re-runnable from raw without re-fetching.
**Considered:** single-step "fetch and normalize" pipeline (cheaper but loses fidelity if the
source schema changes or normalization has bugs).
**Why:** "this data is precious and may disappear." A schema-change bug or normalization mistake
must not require re-fetching from a fragile source. The raw archive is the source of truth and
the disaster-recovery substrate.
**Implication:** every raw row carries `source`, `source_id`, `fetched_at`, `payload_json`,
`payload_sha256`. Normalization writes derived rows that can be `DELETE`d and re-derived at any
time.

### D4. Daily cadence, aggressive token caching.
**Picked:** one fetch run per source per day. OAuth tokens cached and reused; `client.login()`
called only when the cached token is rejected.
**Considered:** hourly polling; per-request login; opportunistic fetch on user action.
**Why:** Garmin's 429 rate-limiting is triggered by repeated logins. OAuth1 tokens persist ~1
year and survived the March 2026 outage. Fitness data is not time-critical for journal
correlation (yesterday's run is fine; we don't need within-the-hour freshness).
**Implication:** `fitness_auth_state` table (or encrypted file) holds tokens, refresh metadata,
`last_successful_login_at`. A separate `fitness_sync_runs` table records each scheduled fetch
(success/failure/error class) for observability.

### D5. Alerting taxonomy, not a single channel.
**Picked:** failures classified by category and routed differently:
- **Auth-broken** (token rejected, library returns 401/403 from auth flow): Pushover **once** on
  transition to broken; **silent** until recovery; webapp banner persists while broken.
- **Transient fetch failure** (network, 5xx, 429): logged, retried with backoff via existing
  `services/jobs/retry.py`; webapp banner only after N consecutive failures.
- **Schema drift / normalize failure** (raw row cannot be normalized): logged loudly, webapp
  banner ("12 activities pending re-normalization"), no Pushover. This is a code bug to fix, not
  a page.
- **Integrate / correlation bug**: caught by tests in CI. Should never reach production alerting.

**Considered:** Pushover on every failure (becomes a page-storm during the inevitable Garmin
outage); webapp-only (misses critical auth breakage when user isn't looking).
**Why:** Garmin will be broken for weeks at a time. The notification system must distinguish
"something needs your attention now" from "things will fix themselves" from "this is a code-level
issue surfaced for the next session."
**Implementation:** extend the existing `services/notifications.py` `TOPICS` list with new fitness
keys (e.g. `notif_fitness_auth_broken`, `notif_fitness_normalize_drift`). Do **not** introduce a
parallel notification system.

### D6. MCP is a first-class surface, not an afterthought.
**Picked:** every meaningful query the webapp can run is also exposed as an MCP tool under a
`fitness.*` namespace. Tool design precedes UI design.
**Considered:** MCP-as-bonus, REST-first.
**Why:** the user has explicitly stated "absolutely intend to use the MCP server feature to make
the data available to other apps — there is a lot of value in being able to access that data from
different places." Treating MCP as primary forces clean, queryable APIs from day one.
**Implementation:** new tools registered under `src/journal/mcp_server/tools/fitness.py`,
mirroring the convention of existing tool modules.

### D7. Metric units only, enforced at the normalization boundary.
**Picked:** distances in metres or kilometres, mass in kilograms, temperature in °C, heart rate
in bpm, HRV in milliseconds. All conversions happen during normalization; raw retains source
units verbatim.
**Considered:** mixed units, configurable units.
**Why:** simplicity, and the user has said so explicitly. Configurable units would be a UI/display
concern handled in the webapp, not in storage.

---

## 4. Code surface (grounded in existing conventions)

| Concern | Location | Convention followed |
|---|---|---|
| Strava provider | `src/journal/providers/strava.py` | Mirrors `providers/ocr.py` — Protocol + adapter, single file |
| Garmin provider | `src/journal/providers/garmin.py` | Same |
| Fetch / normalize services | `src/journal/services/fitness/` (subpackage) | Mirrors `services/ingestion/`, `services/entity_extraction/` |
| Background workers | `src/journal/services/jobs/workers/fitness_*.py` | New worker types in existing job runner |
| REST routes | `src/journal/api/fitness.py` | New module in existing api package |
| MCP tools | `src/journal/mcp_server/tools/fitness.py` | New module in existing tools package |
| Schema migrations | `src/journal/db/migrations/00NN_fitness_*.sql` | Numbered SQL, next free number at implementation time |
| Tables | `fitness_*` prefix | Mirrors `entity_*`, `mood_*` namespaces |
| Notification topics | New keys in `services/notifications.py` `TOPICS` list | Extend, don't fork |
| Config | New fields in `src/journal/config.py` | Same `Config` dataclass, env-var-backed |

**Boundary discipline:** nothing under `services/fitness/` imports from `services/ingestion/`,
`services/entity_extraction/`, or `services/mood_scoring.py` / `services/mood_dimensions.py`, or vice versa, except through the small
explicit interfaces of `db.repository`, `services/jobs`, and `services/notifications`. Enforce by
convention and code review; consider an import-linter rule if drift becomes a problem.

---

## 5. Operational discipline (locked-in posture)

- **Backfill from 2026-01-01.** First successful auth pulls activities from 2026-01-01 onward
  (matching the start of journal entries). Pre-2026 data is left in place at the source — we can
  fetch it later if a use case emerges. Backfill is incremental and resumable — if it dies after
  activity 400 of 800, the next run starts at 401.
- **Raw archive is sacred.** No `UPDATE`s on `fitness_raw_*` rows. New normalization runs read raw,
  write to normalized tables, never modify raw.
- **Idempotent normalization.** Each raw row has a deterministic `(source, source_id)` natural
  key. Re-normalizing the same raw row produces the same normalized output.
- **Version pin upstream libraries.** `python-garminconnect` and `stravalib` (or whichever Strava
  client we settle on) pinned exactly. Upgrades go through a test-environment dry run before
  production.
- **One login per token lifetime.** `client.login()` is the rate-limit hot spot. Cached tokens
  must be reused across runs.
- **Manual re-auth path documented.** When Garmin breaks SSO, expect to re-enter credentials and
  MFA. The CLI surface (`uv run journal fitness reauth`) is part of the deliverable, not an
  afterthought.
- **Observability before features.** Every fetch run writes a `fitness_sync_runs` row. The
  `/health` endpoint surfaces last-success-at per source. The webapp shows a sync-status panel.
  This lands before we build correlation features.

---

## 6. Resolved questions

All six original open questions resolved 2026-05-08. Original questions preserved below for
context, with each resolution inline.

### Q1. Strava client library — **`stravalib`** (confirmed).
Use `stravalib`. Reliability research (see `journal/260508-fitness-integration-planning.md`)
confirmed it is in a different reliability class from `python-garminconnect`: ~0 major breakages
in the last 12 months, OAuth refresh and rate limiting built-in, only additive changes to the
underlying Strava V3 API. Default rate limits (verified 2026-05-09 against
<https://developers.strava.com/docs/rate-limits/>) are **200 / 15 min, 2000 / day** overall and
**100 / 15 min, 1000 / day** for the non-upload budget that covers most read endpoints — still
generous for our daily-cadence single-user use. (Earlier draft of this section quoted 600 / 30k
figures; that was incorrect and has been corrected against the live docs.) One non-technical note:
Strava's Nov 2024 API Agreement restricts third-party
apps to displaying a user's data back to that user only and prohibits AI/ML training on the data
— fine for our single-user personal pipeline, worth flagging if scope ever expands.

### Q2. Token storage — **SQLite table (`fitness_auth_state`)**.
Mirrors how session/auth tokens already live in the database. Single backup story for everything;
auditable; consistent with existing patterns. Encryption-at-rest is a separate concern handled at
the SQLite level if/when needed, not a reason to use files.

### Q3. Strava sync mechanism — **poll, daily**.
No webhooks for now. Polling matches the Garmin cadence, avoids the public-ingress + signature
verification + delivery-guarantee complexity, and fitness data isn't time-critical for journal
correlation. Revisit only if same-day correlation becomes a real need.

### Q4. Correlation primitives — first three locked in.
These shape the integrate-layer schema (daily rollups, time-windowing, what gets joined).

1. **Sleep quality × energy and joy** (mood dimensions). Daily granularity. Tests whether sleep
   score (Garmin) or duration / efficiency (Strava-derived) tracks self-reported energy and joy
   on the same or following day.
2. **Weekly running distance × stress** (mood dimension). Weekly aggregate granularity. Tests
   whether training volume correlates with self-reported stress — both directions of the
   hypothesis (does running help, or does heavy training elevate stress?) are interesting.
3. **HRV trend × mood trends.** Rolling window (probably 7–14 day rolling means). Tests whether
   recovery state (HRV trend) tracks mood trends over multi-day windows, where individual-day
   noise is averaged out.

These three drive the v1 of integrate-layer queries. Additional primitives (training load,
long-run days × topics, rest days × entry length, training-stress entity correlation, etc.) are
deferred — additive once the schema supports the first three.

### Q5. Historical data depth — **2026-01-01 onward**.
Backfill from January 2026 only. Journal entries begin February 2026, so this is the meaningful
correlation window. Pre-2026 fitness data stays at the source and can be fetched later if a use
case emerges. This is a much smaller backfill (months, not years) which simplifies the initial
job.

### Q6. Privacy / export — **fold into existing user-data export**.
No parallel export path. Fitness data is part of the user's data and should leave the system the
same way journal entries do. Implementation lands in whichever module owns user-data export
(check `services/backfill.py` or the user-export endpoints during implementation).

---

## 7. Kill criteria

We would abandon or significantly redesign this initiative if:

- Garmin permanently kills unofficial library access **and** Strava's data fidelity proves
  insufficient for the correlations we actually want.
- Maintaining two providers becomes a substantial fraction of total maintenance time on the
  project (>20%) for more than two consecutive months.
- Storage cost of raw payloads exceeds reasonable bounds (unlikely at personal scale, but worth
  monitoring — flag for review at 1 GB).
- The correlation queries we build prove unhelpful in practice (i.e., the data doesn't actually
  inform anything the user cares about). Plan for a 3-month post-launch review against this.

---

## 8. References

- `architecture.md` — overall server architecture; new module follows its conventions
- `external-services.md` — existing third-party adapter patterns (extend, don't diverge)
- `jobs.md` — existing job runner; fitness workers slot in here
- `docs/api.md` — REST endpoint conventions
- `roadmap.md` — index this initiative there once approved
- `journal/260508-fitness-integration-planning.md` — design conversation, including
  `python-garminconnect` reliability research with citations
