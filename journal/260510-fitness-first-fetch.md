# 2026-05-10 — Fitness pipeline first live fetch

The W13 unit (`journal/260509-fitness-w13-backfill.md`) shipped the
backfill orchestrator + CLI + 18 unit tests but deliberately
deferred the live exercise against real Strava and Garmin
credentials. This entry is the record of that live exercise: what
the smoke tested, what the data looks like, and what surprises
surfaced that the unit tests didn't catch.

**Outcome:** smoke is green. Both sources backfilled cleanly from
2026-01-01 to 2026-05-09 after one mid-smoke bug fix. End-to-end
pipeline (W1 schema → W4/W5 providers → W6 fetch → W7 normalize →
W11 CLI → W12 health → W13 backfill) is now exercised against
production credentials and production data.

## Final counts (post-backfill, post-force-normalize)

- **Strava:** 80 activities, dates 2026-01-07 → 2026-05-09. Type
  breakdown: 52 run / 27 strength / 1 other (the lone "other" is a
  Rowing entry the activity_type collapse map doesn't recognise —
  see finding #5 below). Monthly distribution Jan 2 / Feb 15 /
  Mar 29 / Apr 24 / May 10. Operator visually verified the total
  count and three randomly-sampled rows against the Strava UI.
- **Garmin:** 80 activities + 129 daily wellness rows. Daily rows
  cover **all 129 calendar days** in the range, no gaps. 854 raw
  rows fan in 1:1 for activities + 6:1 for daily endpoints (854 =
  80 + 6 × 129). The 80 Garmin activities overlap with the 80
  Strava activities — same workouts, both sources, since the
  operator uploads from Garmin → Strava. (Cross-source dedup is an
  analysis-time concern, not a storage-time one.)
- **W12 health summary** surfaces both sources with `auth_status=ok`,
  `auth_broken_since=None`, `last_success_at` populated with the
  most recent backfill window's `started_at`. Verified via direct
  call to `FitnessRepository.get_health_summary` (the prod
  `/api/health` endpoint needs prod auth credentials we didn't
  exercise here; the underlying SQL is identical).

A recent-five-day sample of Garmin daily metrics (sleep duration ~6–7.5h,
HRV 70–85ms, resting HR 38–43 bpm, body battery max 55–75, stress
20–31, training readiness 1–85) all sit in plausible physiological
ranges, with no NULL columns where data was expected.

## Findings

### #1 — W6 Garmin auth check missed `tokens_blob` (BUG, fixed)

The first Garmin backfill window short-circuited to
`status="auth_broken"` with `error_class="MissingAuthState"` despite
W11's reauth having successfully persisted a `tokens_blob` row to
`fitness_auth_state` minutes earlier. Diagnosis: W6's
missing-credentials guard hard-coded `not auth.access_token`, but
Garmin's W11 reauth pattern leaves `access_token=None` and stores
the live credential in `extra_state["tokens_blob"]`.

The bug pre-dates W13 — every Garmin sync since W11 merged would
have failed identically. The reason no test caught it: the existing
`_seed_auth` helper in `test_fetch.py` populated `access_token="atok"`
for both sources, so the buggy check happened to be satisfied. The
test was inadvertently testing against the wrong auth shape.

Fix shipped as commit `e70b60c` (merge), `3a51ffb` (squash) on main:
`_FetchServiceBase` gains a `_has_credentials(auth)` hook,
`GarminFetchService` overrides it to check `extra_state["tokens_blob"]`,
both `_seed_auth` helpers updated to mirror what each source's W11
CLI actually persists. Two new tests pin the regression and the
"Strava still requires `access_token`" invariant. Full write-up at
`journal/260510-fitness-w6-garmin-auth-check.md`.

After the fix landed, the auth_state row from the original W11
reauth was still valid (the buggy attempts hit the early-return
path which doesn't transition `auth_status`), so no re-auth was
needed — just pull the new image and re-run the backfill.

### #2 — W11 OAuth listener collides with the long-running server

W11's CLI re-auth opens an HTTP listener on whatever port
`STRAVA_REDIRECT_URI` specifies (default 8400). In any deployment
where the long-running journal-server is also bound to 8400, the
listener cannot bind:

```
OSError: [Errno 98] Address already in use
```

Two viable workarounds, neither obvious:

1. **Stop journal-server + run a one-off container with `--service-ports`.**
   `docker compose stop journal-server` frees 8400, then
   `docker compose run --rm --service-ports journal-server uv run journal fitness-reauth-strava`
   runs the listener in a fresh container that publishes 8400 to
   the host. Restart journal-server when done. Brief downtime
   (30–60s plus however long the operator takes to authorise).
2. **Skip the listener entirely.** Open the authorize URL,
   authorise, copy the `code` from the redirected URL bar (the
   browser fails to load — that's fine), exchange it via an inline
   python one-liner that calls `journal.providers.strava.exchange_code`
   and `FitnessRepository.upsert_auth_state` with the same shape
   `cmd_fitness_reauth_strava` uses. No code change, no container
   restart. This is what we used during the smoke.

The clean long-term fix is to add a `--code <code>` flag to
`journal fitness-reauth-strava` that bypasses the listener
entirely. About 10 lines. Worth shipping as a small follow-up
commit; W14 docs would then document the headless recipe (option
B) as the recommended path, with option A as a fallback for
operators who want the OAuth roundtrip to feel native.

### #3 — Headless VM + browser-on-laptop case

The W11 listener assumes browser and listener run on the same
host. When journal-server runs on a remote/headless VM (production
case), the browser-on-laptop hits the laptop's `localhost:8400` —
which has nothing listening unless an SSH tunnel forwards to the
VM. Even then, the VM's port 8400 is occupied (finding #2).

Combined recipe documented in the smoke runbook: SSH `-L` tunnel
on laptop **plus** stop-server + one-off container on the VM.
Works, but it's a two-step ritual requiring two SSH sessions. The
`--code` flag from finding #2 short-circuits this completely.

### #4 — Garmin login transport-fallback noise

`fitness-reauth-garmin` printed:

```
mobile+cffi returned 429: Mobile login returned 429 — IP rate limited by Garmin
mobile+requests returned 429: Mobile login returned 429 — IP rate limited by Garmin
Garmin re-auth complete — token blob persisted.
```

The `garminconnect` library tries multiple transports in sequence
(mobile API via cffi, mobile API via requests, then a legacy
fallback). The first two failed with 429; one of the fallbacks
succeeded and the persist callback fired. The CLI printed
`complete` correctly — the W11 CLI's exit-on-empty check (`if not
persisted: error`) only treats "no transport succeeded" as a
failure.

The 429 lines are misleading without context — they look like
errors but the operation succeeded. A small UX improvement (W14
docs note or a CLI hint): note that intermittent transport 429s
during login are normal, not a failure indicator. The `complete`
line is the authoritative signal.

### #5 — `Rowing` Strava sport_type collapses to `other`

The activity_type collapse map (`_activity_type_map.coarse_strava`)
maps the seven canonical values (`run`, `ride`, `swim`, `walk`,
`hike`, `strength`, `other`); anything outside the explicit
mapping falls to `other`. The smoke's only `other`-typed activity
was a Strava `Rowing` (`source_id=16971789898`, 2026-01-07,
10m10s). `source_subtype="Rowing"` is preserved on the row, so
the data isn't lost — just bucketed coarsely.

Defensible behaviour. Possible follow-ups (out of scope for this
smoke): add `Rowing` → `other` as an explicit map entry (semantic
no-op but documents intent), OR introduce a `row` activity_type
to the canonical seven. The latter is a schema change (CHECK
constraint widening) and a fitness-tier-plan amendment, not a
quick patch.

### #6 — W7 incremental normalize watermark loses rows on dense backfill

After the live Strava backfill: `windows=5/5 fetched=80
normalized=50`. After the live Garmin backfill: `windows=5/5
fetched=854 normalized=209` — but the *eventual* total of normalized
rows after a force-renormalize was 80 + 209 (= 80 activities + 129
daily, all matching the raw count). So 30 Strava activities and a
chunk of Garmin rows sat in the raw archive without a normalized
projection until a follow-up `normalize_*(since="")` projected
them.

Root cause: W7's `normalize_*` uses `repo.max_normalized_fetched_at`
as the watermark and reads raw rows where `fetched_at > watermark`
(strict `>`). SQLite's `strftime('%Y-%m-%dT%H:%M:%SZ', 'now')`
default has 1-second resolution. During a fast backfill, multiple
raw rows land at the same wall-clock second, so the strict `>`
filter excludes the second-and-later rows in each "tied" group
from the next window's normalize pass.

Recovery is trivial: `normalize_strava(repo, user_id=1, since="")`
or `normalize_garmin(repo, user_id=1, since="")` projects all
unprojected raws on a single pass. Idempotent (upserts).

I called this out in the W13 implementation journal entry
(`260509-fitness-w13-backfill.md`) as a *predicted* W7 quirk; the
smoke has now confirmed it occurs in practice. Fix is one of:

1. Switch the watermark to a composite `(fetched_at, id)` tuple
   so ties are broken by row id. Requires changing `list_raw_since`
   and `max_normalized_fetched_at`. Cleanest semantically; touches
   two repo methods + the normalize entry points.
2. Bump `fetched_at` to sub-second resolution (`strftime('%Y-%m-%dT%H:%M:%f', 'now')`
   or store as a Unix timestamp with µs precision). Schema change
   + migration; impacts every existing raw row.
3. Run a final force-normalize at the end of every backfill (the
   workaround we used here). Trivial code change in
   `services/fitness/backfill.py` — call `normalize_*(since="")`
   once after the window loop completes. Doesn't fix routine syncs
   that hit the same dense-second problem (rare in steady-state
   but possible).

Defer to a separate follow-up unit. Leaning toward option (3) for
backfill specifically (single line of code, addresses the dense
case where it matters) plus option (1) for routine syncs (correct
for all callers but more invasive).

### #7 — Process lesson: env var dump leaked secrets

Early in the smoke I ran `docker exec journal-server printenv |
grep -E "(STRAVA|GARMIN|DB_PATH|FITNESS)"` to inspect environment
configuration. The output captured `STRAVA_CLIENT_SECRET`,
`STRAVA_REFRESH_TOKEN`, and `GARMIN_PASSWORD` into the assistant
conversation context (and therefore into Anthropic's API logs).

This is not a code bug — it's a process error in how I (operator)
was directing inspection. The right pattern going forward is an
*allowlist* of non-sensitive var names rather than a deny-list:

```bash
docker exec journal-server printenv | \
    grep -E '^(DB_PATH|FITNESS_|STRAVA_REDIRECT_URI|STRAVA_CLIENT_ID|GARMIN_USERNAME)='
```

Operator deferred rotation for now. Recommendation stands:
regenerate Strava client secret + Garmin password, deauthorize
the Strava app to invalidate the leaked refresh token, redo
fitness-reauth-strava + fitness-reauth-garmin once new credentials
are in `.env`.

## Time / cost notes

End-to-end smoke (excluding the bug-fix detour):

- Strava reauth (manual code-paste path due to listener collision): ~2 min
- Garmin reauth: ~1 min (2 transport 429s before success)
- Strava backfill: 5 windows in ~5 sec total
- Garmin backfill: 5 windows in ~3 min total (the 6×129 = 774
  daily endpoint calls are the slow part; Garmin's API is gentler
  than feared — no rate-limit cliffs hit during the actual backfill,
  only at login time)
- W6 bug-fix detour: ~25 min (failing test → fix → suite → lint →
  worktree commit → merge → push → CI watch → image pull → restart)

Total operator time including diagnosis and fix: ~45 min. A
re-run from a known-good state (no bugs to find, secrets already
rotated) would be closer to 10 min plus the operator's visual
spot-check time.

## Follow-ups for W14

1. **Document the OAuth headless recipe** (finding #2/#3). Either
   inline the option-B inline-python recipe in the docs, or — if
   we ship the `--code <code>` flag first — document just that.
2. **Document the dense-backfill normalize follow-up** (finding #6).
   Operators who run a backfill should know to either expect the
   per-window `normalized=N < fetched=M` discrepancy or run a
   final force-normalize themselves.
3. **Document Garmin login transport noise** (finding #4). One
   sentence in the troubleshooting section.
4. **Document the secrets-allowlist pattern** (finding #7). One
   sentence in the operations section.

## Follow-ups not for W14 (file as separate units when prioritised)

- **`fitness-reauth-strava --code <code>` flag** (findings #2/#3
  cleanest fix). ~10 lines of CLI code + a unit test. Could fold
  into W14 if convenient, otherwise its own small commit.
- **W7 watermark fix** (finding #6). Two-line code change for the
  backfill workaround (option 3); larger change for the proper
  composite-watermark fix (option 1). Decide scope before
  starting.
- **`Rowing` activity_type mapping** (finding #5). Optional —
  defensible-as-is.

## What's still ahead in the tier plan

- **W14:** docs (now informed by everything above).
- **W15:** webapp views for fitness data. The 80 activities + 129
  daily Garmin rows + 80 Strava activities living in
  `fitness_activities` + `fitness_daily` are real data the webapp
  can render against, so W15 design decisions (charts, list
  views, source-deduplication strategy) can be grounded in
  what's actually there rather than synthetic fixtures.

The Strava ↔ Garmin activity overlap (same workout in both
sources) is worth resolving early in W15 — without dedup,
"weekly run count" doubles. The operator's intent is presumably
"distinct workouts", which means a join on a tolerance window of
`start_time` and `duration_s`. Out of scope for this entry; flag
for W15 architecture discussion.
