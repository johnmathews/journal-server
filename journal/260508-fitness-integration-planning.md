# 2026-05-08 — Fitness integration planning

Design conversation that produced `docs/fitness-integration-plan.md`. This entry captures the
reasoning, the alternatives weighed, and — most importantly — the cited reliability research on
`python-garminconnect` that the plan rests on. Future agents reading the plan should land here
when they want sources or want to understand *why* a decision went the way it did.

## Why now

Exercise is a significant part of the user's life (Garmin watch, Garmin Connect iOS app, Strava).
Fitness data is a natural complementary signal for the journal — running mileage, sleep quality,
HRV, training load are all things that plausibly correlate with mood, themes, and what the user
chooses to write about. The journal already has structured ingestion + correlation infrastructure
(SQLite + ChromaDB + entity extraction + mood scoring), so adding fitness as a sibling data
source is a low-friction, high-value extension.

Original prompt was "could I send screenshots into the journal-server?" Quickly redirected to a
proper structured ingestion strategy because (a) the data already exists in structured form on
Garmin/Strava servers, and (b) the user wants long-term value and robustness, not a cheap
prototype.

## Sources considered

| Source | Verdict | Why |
|---|---|---|
| Strava API (official OAuth) | **Primary** | Sanctioned, well-documented, generous rate limits for personal use, stable. |
| `python-garminconnect` (unofficial) | **Secondary enrichment** | Only path to Garmin-only metrics (sleep, HRV, Body Battery, training readiness/load). Fragile — see research below. |
| Garmin Health API (official) | **Rejected** | Gated to approved partners (Whoop, MyFitnessPal-tier). Not realistically available for personal projects. |
| `.fit` file export | **Out of scope, possibly future** | Useful as archival format, but not for daily ingestion. |
| Apple HealthKit / Health Auto Export | **Out of scope** | User's data lives in Garmin/Strava, not Apple Health. |

Posture: Strava is the stable backbone; Garmin is best-effort enrichment that the system must
tolerate being broken for weeks at a time.

## `python-garminconnect` reliability — research summary

Verdict: **moderately unreliable**. One major multi-week breakage in the last 12 months, plus
chronic background friction on login. Currently actively maintained post-crisis.

### Timeline (May 2025 – May 2026)

- **March 17, 2026 — Total auth breakage.** Garmin changed their SSO flow and deployed
  Cloudflare-class bot detection. Every login via `garth`/`python-garminconnect` started returning
  `401 Unauthorized` on `/oauth-service/oauth/preauthorized`. Users with cached OAuth1 tokens kept
  working; new logins failed. Source:
  [issue #332](https://github.com/cyberjunky/python-garminconnect/issues/332).
- **March 28, 2026 — `garth` deprecated.** Upstream auth library `garth` (used by
  `python-garminconnect`) shipped v0.8.0 and was officially deprecated. Maintainer `matin` cited
  inability to keep up with Garmin's auth changes. Source:
  [garth deprecation discussion #222](https://github.com/matin/garth/discussions/222).
- **April 2–11, 2026 — Recovery.** `python-garminconnect` shipped 0.3.0 (Apr 2), 0.3.1 (Apr 3),
  0.3.2 (Apr 11) replacing `garth` with native auth using mobile-app SSO and adding a web-widget
  login strategy to bypass 429s. Most major issues (#332, #337, #344, #348, #350) closed Apr
  11–13. Source: [PyPI release history](https://pypi.org/project/garminconnect/).
- **April 26, 2026 — China-region routing fix** for the DI token endpoint. Source:
  [commit log](https://github.com/cyberjunky/python-garminconnect/commits/master).
- **Background, chronic:** 429 rate-limiting on `client.login()` since at least mid-2024. Blocks
  reportedly lift after ~1 hour. Sources:
  [#213](https://github.com/cyberjunky/python-garminconnect/issues/213),
  [#337](https://github.com/cyberjunky/python-garminconnect/issues/337).
- **Other 2025 issues:** MFA + OAuth1 token-persistence bug
  ([#312](https://github.com/cyberjunky/python-garminconnect/issues/312)); 403 on activity search
  ([#303](https://github.com/cyberjunky/python-garminconnect/issues/303)). Both addressed in the
  0.3.x rewrite.

**Net:** roughly **3.5 weeks of complete breakage** (Mar 17 → Apr 11, 2026) in the last 12 months,
plus two earlier non-blocking bugs.

### Dominant failure modes

1. **Garmin SSO changes** — by far the biggest. Mobile-app auth approach broke, then Cloudflare
   bot detection layered on top.
2. **Login rate-limiting / 429s** — triggered by repeated `client.login()` calls.
3. **MFA edge cases** — token-persistence bugs after fresh MFA login.

### Maintainer responsiveness

Currently active: April 2026 saw a burst of work (auth rewrite, retries with backoff, persistent
session reuse, regional routing, multi-strategy MFA fallback). Pre-March 2026 cadence was modest
(~9 PyPI releases over 11 months). `garth`'s maintainer has explicitly stepped back, but
`python-garminconnect` no longer depends on it.

### Operational mitigations the community has converged on

- **Cache OAuth1 tokens aggressively.** Tokens valid ~1 year and survived the March outage.
- **Schedule once-daily pulls, not per-minute.** One login per token lifetime is ideal.
- **Upgrade to ≥ 0.3.2** for the SSO web-widget strategy that sidesteps 429s.
- **Have a manual re-auth path.** When Garmin changes flows, expect to re-enter credentials + MFA.
- For high-stakes setups: Playwright-based browser auth or `curl_cffi` TLS-fingerprint
  impersonation have been proposed
  ([garth #222](https://github.com/matin/garth/discussions/222)) — but this is cat-and-mouse and
  not appropriate for a personal project.

### What I could not confirm

- Exact 429 thresholds (requests/hour, IP reputation).
- Whether residential vs datacenter IPs matter for the Cloudflare layer (referenced anecdotally
  but not in primary issue threads).
- Whether the Home Assistant community had a separate 2025–2026 forum thread documenting the
  outage; only GitHub-side discussion was found.

### Bottom line for our pipeline

Plan for **at least one multi-week outage per year** when Garmin changes auth. Persist tokens
aggressively. Rate-limit logins (one per token lifetime). Pin a known-working version. The
library is usable, but absolutely not "set and forget." This is the load-bearing reliability
fact behind decisions D2, D4, and D5 in `docs/fitness-integration-plan.md`.

### Primary sources

- [`python-garminconnect` repo](https://github.com/cyberjunky/python-garminconnect)
- [`python-garminconnect` commits](https://github.com/cyberjunky/python-garminconnect/commits/master)
- [`python-garminconnect` PyPI history](https://pypi.org/project/garminconnect/)
- [Issue #332 — auth API change](https://github.com/cyberjunky/python-garminconnect/issues/332)
- [Issue #337 — 429 on OAuth Preauthorized](https://github.com/cyberjunky/python-garminconnect/issues/337)
- [Issue #344 — SSO web widget bypass](https://github.com/cyberjunky/python-garminconnect/issues/344)
- [Issue #312 — OAuth1 token MFA bug](https://github.com/cyberjunky/python-garminconnect/issues/312)
- [Issue #303 — 403 on activity search](https://github.com/cyberjunky/python-garminconnect/issues/303)
- [Issue #213 — login rate limit](https://github.com/cyberjunky/python-garminconnect/issues/213)
- [`garth` repo (DEPRECATED)](https://github.com/matin/garth)
- [`garth` deprecation discussion #222](https://github.com/matin/garth/discussions/222)
- [Home Assistant `garmin_connect` integration](https://github.com/cyberjunky/home-assistant-garmin_connect)

## Architecture: in-process module, not a separate service

Considered splitting into a standalone `fitness-server` (own MCP, own REST, webapp aggregates).
Rejected for this stage. Reasoning:

1. **Correlation is the value.** The killer queries (mood × mileage, sleep × next-day sentiment,
   long-run-day journal entries) are SQL joins between fitness and journal tables. Splitting
   pushes those joins into the webapp over HTTP — slower, more code, more bugs.
2. **Operational cost multiplies.** Two services means two CI pipelines, two Dockerfiles, two
   deploys, two secret stores, two backup strategies, two MCP registrations. Real ongoing tax for
   one developer.
3. **Failure isolation is achievable in code, not deployment.** A Garmin auth break shouldn't take
   down journal ingestion — but that's a code property (separate worker, separate error handler,
   separate package), not a deployment property. We get ~90% of the isolation benefit without the
   infrastructure tax.
4. **Extraction later is a refactor, not a rewrite.** If we ever do hit a real reason to split
   (another consumer, scaling, runtime change), strict internal boundaries make extraction cheap.

Trigger to revisit: another consumer wants the data without going through `journal-server`,
genuine independent scaling need, or a runtime that journal-server can't provide.

## Pipeline shape & alerting

Locked in:

- **Four layers**: fetch → persist raw → normalize → integrate. Raw archive is sacred and
  append-only. Normalization is idempotent and re-runnable from raw.
- **Metric units only** at the normalize boundary (km, m, kg, °C, bpm, ms).
- **Daily cadence**, aggressive token caching, version-pinned upstream libraries.
- **Alerting taxonomy**: auth-broken → Pushover once + persistent banner; transient → log + retry
  + banner after N failures; normalize drift → banner only (code bug, not a page); integrate bug
  → caught by tests in CI.
- **Reuse `services/notifications.py`** — extend the `TOPICS` list with new fitness keys, do not
  fork the notification system.
- **MCP-first**: every meaningful query exposed as an MCP tool under a `fitness.*` namespace.
  Driven by the user's explicit goal of consuming this data from other apps.

## Code surface

Follows existing repo conventions (verified by reading `src/journal/` before writing the plan):

- `providers/strava.py`, `providers/garmin.py` — Protocol + adapter, mirroring `providers/ocr.py`.
- `services/fitness/` subpackage — mirrors `services/ingestion/`, `services/entity_extraction/`.
- `services/jobs/workers/fitness_*.py` — new worker types in the existing job runner.
- `api/fitness.py` — REST routes.
- `mcp_server/tools/fitness.py` — MCP tools.
- `db/migrations/00NN_fitness_*.sql` — numbered SQL files, next free number at implementation.
- `fitness_*` table prefix.

Earlier in the conversation I had suggested `src/journal/fitness/` as a top-level subpackage.
After reading the actual repo, that diverges from the existing conventions — entity-extraction
and mood-scoring are organized as flat providers + services subpackages, not top-level modules.
The plan reflects the corrected understanding.

## What got locked in

See `docs/fitness-integration-plan.md` for the full, structured version. Headline:

- D1: in-process module, not separate service.
- D2: Strava primary, Garmin secondary enrichment.
- D3: four-layer pipeline with sacred raw archive.
- D4: daily cadence, aggressive token caching.
- D5: alerting taxonomy distinguishing auth-broken / transient / drift / bug.
- D6: MCP is a first-class surface.
- D7: metric units only.

## Process notes (for future planning sessions)

The user pushed back on the repo's existing planning discipline — too many sprawling refactor
docs, status not visible, length too long to re-read in a session, plans drift from the
canonical roadmap. The fitness plan was written under a tighter discipline to address those:

- Status header + dates at the top.
- Decisions-and-rationale separated from execution sequencing (sequencing deferred to a separate
  doc).
- Length cap (~12k chars).
- Each decision lists alternatives considered and the load-bearing reason.
- Kill criteria — what would make us abandon or significantly redesign.
- Code-grounded from v1: source tree, providers, jobs system, notifications, migrations all read
  before writing, so the plan's structural claims are accurate the first time.

Worth carrying forward to other planning docs in this repo.

## Next steps

1. User review of `docs/fitness-integration-plan.md`.
2. Resolve open questions (§6 of the plan): Strava client library reliability check, token
   storage decision, webhook vs poll, first three correlation queries, historical data depth,
   privacy/export.
3. Write `docs/fitness-schema.md` once §6 is resolved.
4. Write `docs/fitness-tier-plan.md` (execution sequencing) once schema is settled.
5. Index this initiative in `docs/roadmap.md`.
