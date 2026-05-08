# Fitness Schema Design

| Field | Value |
|---|---|
| **Status** | Active — schema design |
| **Created** | 2026-05-09 |
| **Last updated** | 2026-05-09 |
| **Supersedes** | None |
| **Superseded by** | None |
| **Related docs** | `fitness-integration-plan.md` (master), `architecture.md`, `external-services.md` |
| **Code-grounded** | Yes — `db/migrations/0001,0006,0011,0017`, `services/notifications.py`, `services/mood_dimensions.py`, `config/mood-dimensions.toml`, and `models.py` reviewed before writing |

This document specifies concrete tables, indexes, and migration sequencing for the fitness
integration. **It does not relitigate decisions.** The four-layer pipeline, sacred raw archive,
unit policy, `fitness_*` prefix, `user_id` requirement, daily cadence, library choices, and
backfill window are all decided in `fitness-integration-plan.md`. Read that first.

---

## 1. Schema-level decisions (with alternatives)

### S1. One `fitness_activities` table for both sources, not per-source.
**Picked:** a single table keyed by `(source, source_id)` covering Strava and Garmin activities.
**Considered:** `fitness_activities_strava` + `fitness_activities_garmin` mirroring raw layout.
**Why:** the integrate layer (correlation queries) almost always wants "all runs in week W",
not "Strava runs". Per-source tables push a `UNION ALL` into every query and double the index
maintenance. Source-specific fields that don't fit the common columns live in a JSON
`extras_json` column, mirroring how `result_json` is used in the `jobs` table (see
`migrations/0006_jobs.sql:18`). Per-source raw tables remain (S2) — fan-in happens at the
normalize boundary, not at storage.

### S2. Raw payloads in SQLite, not a separate object store.
**Picked:** `fitness_raw_strava` and `fitness_raw_garmin` with `payload_json TEXT` columns.
**Considered:** filesystem (`raw/strava/{date}/{id}.json`); S3-compatible blob store.
**Why:** at personal scale (≤1 GB per the master plan's kill criteria) one backup story
beats two. Joins from raw to normalized are trivial in SQL and impossible across stores.
Existing migrations already store JSON in TEXT columns (`jobs.params_json`,
`jobs.result_json`, `entities.embedding_json`) so this matches house style. If size ever
breaches 1 GB, lifting raw to a blob store is a one-table migration — the rest of the schema
doesn't notice.

### S3. `fitness_workouts` deferred, not built.
**Picked:** ship `fitness_activities` + `fitness_daily` only. No structured-workout table now.
**Considered:** modelling Strava's planned/structured workouts and Garmin's workout steps.
**Why:** none of the three locked-in correlation queries (master plan §6 Q4) touch
intra-activity structure. Raw payloads retain everything; if a use case emerges, normalize
into `fitness_workouts` later from raw without re-fetching. Premature schema is harder to
remove than to add.

### S4. Soft pointer from normalized → raw, not a hard FK.
**Picked:** normalized rows carry `(source, source_id)` matching raw, but **no FOREIGN KEY**
into the raw table. `fitness_activities.raw_ref_id` is a single integer pointing to the
most-detailed raw row used during normalization (policy in §3); `fitness_daily.raw_ref_ids_json`
is a JSON array because daily rollups always span multiple raw endpoints. See §6 for the
cascade rationale and the integrity-check sketch.

### S5. Per-source raw tables, not a polymorphic `fitness_raw` table.
**Picked:** separate `fitness_raw_strava` and `fitness_raw_garmin`.
**Considered:** one `fitness_raw` with a `source` discriminator column.
**Why:** the natural keys differ (Strava activity id is a stable int; Garmin uses
endpoint-specific keys for sleep/HRV/activities) and a unified table either loses that typing
or forces the lowest-common-denominator `TEXT`. Per-source keeps each table's UNIQUE
constraint meaningful and lets us add source-specific columns (e.g. Garmin endpoint name)
without polluting the other.

---

## 2. Raw layer

Append-only. No `UPDATE`s. Re-fetching the same upstream record produces a new row only if
`payload_sha256` differs (callers enforce via `INSERT OR IGNORE` keyed on the UNIQUE; SQLite
itself cannot prevent a caller passing a fresh sha). Both tables share the same provenance
shape.

```sql
CREATE TABLE IF NOT EXISTS fitness_raw_strava (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id),
    source          TEXT    NOT NULL DEFAULT 'strava' CHECK(source = 'strava'),
    source_id       TEXT    NOT NULL,                  -- Strava activity id (stringified)
    endpoint        TEXT    NOT NULL CHECK(endpoint IN
                        ('activities', 'activity_detail', 'athlete')),
    fetched_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    payload_json    TEXT    NOT NULL,
    payload_sha256  TEXT    NOT NULL,
    sync_run_id     INTEGER REFERENCES fitness_sync_runs(id) ON DELETE SET NULL,
    UNIQUE(user_id, source_id, endpoint, payload_sha256)
);

CREATE INDEX IF NOT EXISTS idx_fit_raw_strava_user_fetched
    ON fitness_raw_strava(user_id, fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_fit_raw_strava_source_id
    ON fitness_raw_strava(user_id, source_id);

CREATE TABLE IF NOT EXISTS fitness_raw_garmin (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id),
    source          TEXT    NOT NULL DEFAULT 'garmin' CHECK(source = 'garmin'),
    endpoint        TEXT    NOT NULL CHECK(endpoint IN (
                        'sleep', 'hrv', 'body_battery', 'training_load',
                        'training_readiness', 'stress', 'activities', 'activity_detail'
                    )),
    source_id       TEXT    NOT NULL,                  -- per-endpoint deterministic key (see below)
    fetched_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    payload_json    TEXT    NOT NULL,
    payload_sha256  TEXT    NOT NULL,
    sync_run_id     INTEGER REFERENCES fitness_sync_runs(id) ON DELETE SET NULL,
    UNIQUE(user_id, endpoint, source_id, payload_sha256)
);

CREATE INDEX IF NOT EXISTS idx_fit_raw_garmin_user_fetched
    ON fitness_raw_garmin(user_id, fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_fit_raw_garmin_endpoint_key
    ON fitness_raw_garmin(user_id, endpoint, source_id);
```

**Garmin `source_id` convention** (deterministic, day-grain for daily endpoints):

| `endpoint` | `source_id` format |
|---|---|
| `sleep`, `hrv`, `body_battery`, `training_load`, `training_readiness`, `stress` | ISO date (`2026-05-08`) |
| `activities`, `activity_detail` | Garmin activity id (stringified) |

Including `payload_sha256` in the UNIQUE means an unchanged daily fetch is a deterministic
no-op (`INSERT OR IGNORE`); a changed payload (Garmin retroactively edited yesterday's sleep)
inserts a new row, preserving history.

---

## 3. Normalized layer

Two tables: `fitness_activities` (per discrete activity, both sources) and `fitness_daily`
(per-day rollup of recovery / training-state metrics, Garmin-only at the moment but
schema-ready for any source). All units metric.

```sql
CREATE TABLE IF NOT EXISTS fitness_activities (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id             INTEGER NOT NULL REFERENCES users(id),
    source              TEXT    NOT NULL CHECK(source IN ('strava', 'garmin')),
    source_id           TEXT    NOT NULL,
    activity_type       TEXT    NOT NULL CHECK(activity_type IN (
                            'run', 'ride', 'swim', 'walk', 'hike', 'strength', 'other'
                        )),
    source_subtype      TEXT    NOT NULL,              -- verbatim source string (Strava sport_type, Garmin activity type)
    start_time          TEXT    NOT NULL,              -- ISO 8601 UTC
    local_date          TEXT    NOT NULL,              -- YYYY-MM-DD in athlete's local TZ
    duration_s          INTEGER NOT NULL CHECK(duration_s >= 0),
    moving_time_s       INTEGER CHECK(moving_time_s IS NULL OR moving_time_s >= 0),
    distance_m          REAL    CHECK(distance_m IS NULL OR distance_m >= 0),
    elevation_gain_m    REAL    CHECK(elevation_gain_m IS NULL OR elevation_gain_m >= 0),
    avg_hr_bpm          INTEGER CHECK(avg_hr_bpm IS NULL OR avg_hr_bpm BETWEEN 20 AND 250),
    max_hr_bpm          INTEGER CHECK(max_hr_bpm IS NULL OR max_hr_bpm BETWEEN 20 AND 250),
    avg_pace_s_per_km   REAL    CHECK(avg_pace_s_per_km IS NULL OR avg_pace_s_per_km > 0),
    calories_kcal       INTEGER CHECK(calories_kcal IS NULL OR calories_kcal >= 0),
    perceived_exertion  INTEGER CHECK(perceived_exertion IS NULL OR perceived_exertion BETWEEN 1 AND 10),
    extras_json         TEXT    NOT NULL DEFAULT '{}', -- source-specific fields
    raw_ref_id          INTEGER NOT NULL,              -- soft pointer; see §6 and policy below
    normalized_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE(user_id, source, source_id)
);

CREATE INDEX IF NOT EXISTS idx_fit_act_user_date
    ON fitness_activities(user_id, local_date);
CREATE INDEX IF NOT EXISTS idx_fit_act_user_type_date
    ON fitness_activities(user_id, activity_type, local_date);
CREATE INDEX IF NOT EXISTS idx_fit_act_start
    ON fitness_activities(user_id, start_time);

CREATE TABLE IF NOT EXISTS fitness_daily (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id                  INTEGER NOT NULL REFERENCES users(id),
    source                   TEXT    NOT NULL CHECK(source IN ('strava', 'garmin')),
    local_date               TEXT    NOT NULL,         -- YYYY-MM-DD
    sleep_score              INTEGER CHECK(sleep_score IS NULL OR sleep_score BETWEEN 0 AND 100),
    sleep_duration_s         INTEGER CHECK(sleep_duration_s IS NULL OR sleep_duration_s >= 0),
    sleep_efficiency_pct     REAL    CHECK(sleep_efficiency_pct IS NULL OR sleep_efficiency_pct BETWEEN 0 AND 100),
    hrv_overnight_ms         REAL    CHECK(hrv_overnight_ms IS NULL OR hrv_overnight_ms > 0),
    resting_hr_bpm           INTEGER CHECK(resting_hr_bpm IS NULL OR resting_hr_bpm BETWEEN 20 AND 200),
    body_battery_high        INTEGER CHECK(body_battery_high IS NULL OR body_battery_high BETWEEN 0 AND 100),
    body_battery_low         INTEGER CHECK(body_battery_low IS NULL OR body_battery_low BETWEEN 0 AND 100),
    stress_avg               INTEGER CHECK(stress_avg IS NULL OR stress_avg BETWEEN 0 AND 100),
    training_load_acute      REAL    CHECK(training_load_acute IS NULL OR training_load_acute >= 0),
    training_load_chronic    REAL    CHECK(training_load_chronic IS NULL OR training_load_chronic >= 0),
    training_readiness       INTEGER CHECK(training_readiness IS NULL OR training_readiness BETWEEN 0 AND 100),
    extras_json              TEXT    NOT NULL DEFAULT '{}',
    raw_ref_ids_json         TEXT    NOT NULL DEFAULT '[]',  -- JSON array of raw row ids contributing
    normalized_at            TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE(user_id, source, local_date)
);

CREATE INDEX IF NOT EXISTS idx_fit_daily_user_date
    ON fitness_daily(user_id, local_date);
```

A daily row is built from multiple raw rows (sleep, HRV, body battery, etc. are separate
endpoints), so `raw_ref_ids_json` is a JSON array, not a scalar FK. `(user_id, source,
local_date)` is the natural key — re-normalizing is `INSERT OR REPLACE`.

**Daily authoritativeness rule.** Per §2, the same Garmin daily
`(user_id, endpoint, source_id='2026-05-08')` can produce multiple raw rows when Garmin
re-publishes the day with a corrected payload (the new `payload_sha256` defeats the UNIQUE
and inserts a new row rather than no-opping). Normalization treats the row with the
**largest `fetched_at` per `(endpoint, source_id)`** as authoritative; older rows remain in
raw as an audit trail. Consequently `fitness_daily.raw_ref_ids_json` contains exactly one
raw-row id per contributing endpoint — not every historical fetch — and re-running normalize
after a re-publish via `INSERT OR REPLACE` produces the corrected daily row without losing
the prior raw evidence.

**`raw_ref_id` policy (`fitness_activities`).** When the same activity is fetched both as a
listing row (`endpoint='activities'`) and as a detail row (`endpoint='activity_detail'`),
`raw_ref_id` points to the **most-detailed raw row used during normalization** — the detail
row when present, otherwise the listing row. A single column (rather than a JSON array, as
in `fitness_daily`) is sufficient because a normalize pass for one activity reads only one
authoritative raw row; the listing row's data is a strict subset of the detail row's.

**`activity_type` enum + CHECK** mirrors the `entities.entity_type` pattern at
`migrations/0011_multi_tenant.sql:150`. House style is to enumerate enum values in a CHECK
rather than rely on application-level validation.

**`activity_type` normalization (Strava → coarse).** Strava's `sport_type` enum has 54
values (`Run`, `TrailRun`, `VirtualRun`, `Ride`, `GravelRide`, `MountainBikeRide`, `Rowing`,
`Yoga`, `WeightTraining`, `AlpineSki`, `RockClimbing`, …). The coarse `activity_type` collapses
them as follows:

| `activity_type` | Strava `sport_type` values |
|---|---|
| `run`      | `Run`, `TrailRun`, `VirtualRun` |
| `ride`     | `Ride`, `GravelRide`, `MountainBikeRide`, `EBikeRide`, `EMountainBikeRide`, `VirtualRide` |
| `swim`     | `Swim` |
| `walk`     | `Walk` |
| `hike`     | `Hike` |
| `strength` | `WeightTraining`, `Crossfit`, `HighIntensityIntervalTraining` |
| `other`    | everything else (~35 values incl. Rowing, Yoga, AlpineSki, RockClimbing, Pilates, Kayaking, NordicSki, StairStepper) |

The verbatim source string is preserved in `source_subtype`, so a future query that wants
e.g. trail-runs only or weekly rowing volume can filter on it without a schema change. The
coarse `activity_type` is what the locked-in correlation queries (Q2 in §8) join on.
Garmin's normalize maps from its activity-type strings into the same coarse buckets — exact
mapping deferred to the normalize implementation since Garmin's enum is less stable than
Strava's and the project pulls activities primarily from Strava per master plan D2.

**HR/scale CHECK ranges** mirror the `mood_scores.score CHECK(score >= -1.0 AND score <= 1.0)`
pattern at `migrations/0001_initial_schema.sql:21`. All Garmin 0-100 scores, sleep
efficiency, and physiologically-plausible HR ranges are constrained at the DDL boundary.

---

## 4. Auth & operational state

```sql
CREATE TABLE IF NOT EXISTS fitness_auth_state (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id                     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    source                      TEXT    NOT NULL CHECK(source IN ('strava', 'garmin')),
    access_token                TEXT,                  -- Strava: OAuth2 access; Garmin: OAuth1 token
    refresh_token               TEXT,                  -- Strava only
    token_expires_at            TEXT,                  -- ISO 8601, Strava only
    extra_state_json            TEXT NOT NULL DEFAULT '{}', -- garth tokens, OAuth1 secret, etc.
    last_successful_login_at    TEXT,
    last_refresh_at             TEXT,
    auth_status                 TEXT NOT NULL DEFAULT 'unknown'
                                CHECK(auth_status IN ('unknown', 'ok', 'broken')),
    auth_broken_since           TEXT,                  -- transition timestamp, NULL when ok
    created_at                  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at                  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE(user_id, source)
);

CREATE TABLE IF NOT EXISTS fitness_sync_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    source          TEXT    NOT NULL CHECK(source IN ('strava', 'garmin')),
    started_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    finished_at     TEXT,
    status          TEXT    NOT NULL CHECK(status IN
                        ('running', 'success', 'auth_broken', 'transient_failure',
                         'normalize_drift')),
    error_class     TEXT,                              -- exception class name
    error_message   TEXT,
    rows_fetched    INTEGER NOT NULL DEFAULT 0,
    rows_normalized INTEGER NOT NULL DEFAULT 0,
    notes_json      TEXT    NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_fit_sync_user_started
    ON fitness_sync_runs(user_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_fit_sync_user_source_started
    ON fitness_sync_runs(user_id, source, started_at DESC);
```

`fitness_sync_runs.id` is the FK target referenced by raw rows' `sync_run_id` (§2). The
`/health` endpoint reads `MAX(started_at) WHERE status='success' GROUP BY source`. Status
values match the master plan D5 alerting taxonomy 1:1 — there is no `'partial'` because each
sync run targets a single source, so per-source success/failure is already captured by
writing one row per source per scheduled run.

Token storage shape mirrors `user_sessions` at `migrations/0011_multi_tenant.sql:27` (TEXT
PK, ISO 8601 timestamps, `ON DELETE CASCADE` from users). Encryption-at-rest for
`access_token` / `refresh_token` is handled at the SQLite layer if needed (master plan Q2),
not in this schema.

**`updated_at` is app-managed, not auto-updated.** The `DEFAULT (strftime(...))` clause
runs only on `INSERT`. SQLite has no built-in `ON UPDATE` and the schema deliberately
avoids triggers (none exist elsewhere in the migration set). Code paths that mutate
`fitness_auth_state` — token refresh, status transitions, recording last-successful-login —
must set `updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')` explicitly in the same
`UPDATE`. The same applies to any future column with a similar timestamp-on-write
intention.

---

## 5. Notification topic keys

Append the following entries to `services/notifications.py` `TOPICS`
(`services/notifications.py:47`). All four follow the existing dict shape; do **not** create a
parallel notification system (master plan D5).

| `key` | `label` | `group` | `admin_only` | `default` |
|---|---|---|---|---|
| `notif_fitness_auth_broken` | `Fitness auth broken (re-auth needed)` | `failure` | `false` | `true` |
| `notif_fitness_sync_failure` | `Fitness sync failing repeatedly` | `failure` | `false` | `true` |
| `notif_fitness_normalize_drift` | `Fitness payload could not be normalized` | `admin` | `true` | `true` |
| `notif_fitness_sync_success` | `Fitness sync succeeded` | `success` | `false` | `false` |

Auth-broken fires once on transition; sync-failure only after N consecutive (configurable in
`config.py`); normalize-drift fires per drift event for the admin; success defaults off and
exists for users who want positive confirmation (mirrors `notif_job_success_entity_reembed`
which also defaults off, see `services/notifications.py:91`).

**Note on master plan D5.** The plan explicitly specifies Pushover topics for
`auth_broken` and `normalize_drift` and treats transient sync failures as "webapp banner
only after N consecutive failures." The two topics added here for sync failure/success
extend that posture rather than contradict it: `notif_fitness_sync_failure` is the
Pushover delivery for the "after N consecutive" rule (the banner is still the immediate
surface; Pushover only fires once the threshold is crossed), and `notif_fitness_sync_success`
defaults off so it's opt-in. If a future read of D5 wants to restrict to the original two,
the success/failure topics drop without affecting the rest of the schema.

---

## 6. Foreign keys & cascade posture

- **users → fitness_***: standard `REFERENCES users(id)` on every table. `fitness_auth_state`
  and `fitness_sync_runs` use `ON DELETE CASCADE` (deleting a user removes their tokens and
  ops history). Raw and normalized tables omit cascade — deleting a user must be an explicit,
  audited operation, not a side effect.
- **raw → sync_runs**: `ON DELETE SET NULL`. Pruning old sync rows must not delete raw data.
- **normalized → raw**: **no FK.** `fitness_activities.raw_ref_id` is an integer column with
  no `REFERENCES` clause; `fitness_daily.raw_ref_ids_json` is a JSON array.

**Why no FK from normalized to raw.** Raw is sacred and append-only (master plan D3), so the
question "what if a raw row vanishes?" is "this should never happen — and if it did, the
normalized row is now the only surviving evidence." A hard FK with `CASCADE` would silently
delete that evidence; with `RESTRICT` it would block a hypothetical raw cleanup that should
be loud, not blocked. Treating it as a soft pointer makes the invariant a property of the
ingestion code (which never deletes raw), not of the schema, and keeps re-normalization free
to write `INSERT OR REPLACE` without FK gymnastics.

**Integrity check (separate work unit, out of scope here)** verifies every normalized row's
soft pointer resolves. Sketch:

- `fitness_activities.raw_ref_id` (scalar): `raw_ref_id` is a single integer that resolves
  into either `fitness_raw_strava.id` or `fitness_raw_garmin.id` depending on the row's
  `source`. The two raw tables have independent `AUTOINCREMENT` sequences, so a join that
  ignores `source` can silently match the wrong table by id collision. Run two queries:
  ```sql
  SELECT fa.id FROM fitness_activities fa
  LEFT JOIN fitness_raw_strava r ON r.id = fa.raw_ref_id
  WHERE fa.source = 'strava' AND r.id IS NULL;

  SELECT fa.id FROM fitness_activities fa
  LEFT JOIN fitness_raw_garmin r ON r.id = fa.raw_ref_id
  WHERE fa.source = 'garmin' AND r.id IS NULL;
  ```
  Both should return zero rows. Cheap — both joins use `id`, the primary key.
- `fitness_daily.raw_ref_ids_json` (JSON array): requires `json_each()` to expand the array
  before joining, e.g. `SELECT fd.id FROM fitness_daily fd, json_each(fd.raw_ref_ids_json) j
  LEFT JOIN fitness_raw_garmin r ON r.id = j.value WHERE r.id IS NULL`. Slower per row but
  acceptable nightly at personal scale (≤365 daily rows per user-year).

---

## 7. Migration sequencing

Three migrations, applied in order. Verified against `db/migrations/` — `0022_*` is the most
recent, so `0023` is the next free slot.

1. **`0023_fitness_auth_and_sync.sql`** — `fitness_auth_state`, `fitness_sync_runs`, indexes.
   Lands first because raw rows reference `sync_run_id` and the CLI re-auth flow needs auth
   state before the first fetch can run.
2. **`0024_fitness_raw.sql`** — `fitness_raw_strava`, `fitness_raw_garmin`, indexes. Depends
   on `0023` for the `sync_run_id` FK target.
3. **`0025_fitness_normalized.sql`** — `fitness_activities`, `fitness_daily`, indexes.
   Independent of raw at the schema level (no FK), but sequenced last so a partial install
   can never leave normalized tables waiting for raw machinery that doesn't exist.

Each file uses `CREATE TABLE IF NOT EXISTS` and `CREATE INDEX IF NOT EXISTS`, matching every
existing migration. No table rebuilds, no triggers, no FTS — fitness data is queried by
typed columns and date ranges, not full-text.

**Why three files instead of one bundled `0023_fitness.sql`.** The bundle is atomic (one
migration either applies fully or not at all); three files give a smaller blast radius per
revert and make it possible to ship the auth/sync surface ahead of the data tables if a
phased rollout becomes useful. At three files the cost is one extra entry in the migration
runner and three header comments — small enough to prefer the granular version. If a future
session prefers the bundle, the merge is mechanical.

---

## 8. Correlation queries (proves schema supports them)

Mood dimension keys come from `config/mood-dimensions.toml`: `joy_sadness`, `energy_fatigue`,
`frustration` (the closest proxy to "stress" — confirm at integrate time whether to add a
dedicated `stress` dimension). All examples are for `user_id = :uid` between `:start` and
`:end`.

### Q1. Sleep quality × energy & joy (daily grain)

```sql
SELECT
    fd.local_date,
    fd.sleep_score,
    fd.sleep_efficiency_pct,
    AVG(CASE WHEN ms.dimension = 'energy_fatigue' THEN ms.score END) AS energy,
    AVG(CASE WHEN ms.dimension = 'joy_sadness'    THEN ms.score END) AS joy
FROM fitness_daily fd
LEFT JOIN entries e
    ON e.user_id = fd.user_id AND e.entry_date = fd.local_date
LEFT JOIN mood_scores ms ON ms.entry_id = e.id
WHERE fd.user_id = :uid AND fd.local_date BETWEEN :start AND :end
GROUP BY fd.local_date, fd.sleep_score, fd.sleep_efficiency_pct
ORDER BY fd.local_date;
```

### Q2. Weekly running distance × stress

Bucket by Monday-of-week (Mon-Sun calendar weeks). Do **not** use
`strftime('%Y-%W', d)` — it fragments any week spanning Dec/Jan into a partial
`%Y-52`/`%Y-53` bucket plus a `%Y-00` bucket, silently halving the weekly
distance for that week. The arithmetic below shifts each date back to the
Monday of its week using SQLite's `%w` (0=Sun..6=Sat); both sides of the join
use the same shift so they always agree.

```sql
WITH weekly_runs AS (
    SELECT
        date(local_date,
             '-' || ((strftime('%w', local_date) + 6) % 7) || ' days') AS week_start,
        SUM(distance_m) / 1000.0                                       AS distance_km
    FROM fitness_activities
    WHERE user_id = :uid AND activity_type = 'run'
      AND local_date BETWEEN :start AND :end
    GROUP BY week_start
),
weekly_stress AS (
    SELECT
        date(e.entry_date,
             '-' || ((strftime('%w', e.entry_date) + 6) % 7) || ' days') AS week_start,
        AVG(ms.score)                                                    AS stress_proxy
    FROM entries e
    JOIN mood_scores ms ON ms.entry_id = e.id
    WHERE e.user_id = :uid AND ms.dimension = 'frustration'
      AND e.entry_date BETWEEN :start AND :end
    GROUP BY week_start
)
SELECT r.week_start, r.distance_km, s.stress_proxy
FROM weekly_runs r LEFT JOIN weekly_stress s USING (week_start)
ORDER BY r.week_start;
```

### Q3. HRV trend × mood trends (rolling **calendar-day** window)

SQLite's `ROWS BETWEEN N PRECEDING` rolls over preceding rows, which silently widens the
window when there are sync gaps in `fitness_daily`. To get a true 7-day or 14-day calendar
window even when a day is missing, materialize a date series first and left-join. Pick
`:window` = 7 or 14.

```sql
WITH RECURSIVE date_series(d) AS (
    SELECT :start
    UNION ALL
    SELECT date(d, '+1 day') FROM date_series WHERE d < :end
),
daily_mood AS (
    SELECT
        e.entry_date AS d,
        AVG(CASE WHEN ms.dimension = 'joy_sadness'    THEN ms.score END) AS joy,
        AVG(CASE WHEN ms.dimension = 'energy_fatigue' THEN ms.score END) AS energy
    FROM entries e
    JOIN mood_scores ms ON ms.entry_id = e.id
    WHERE e.user_id = :uid AND e.entry_date BETWEEN :start AND :end
    GROUP BY e.entry_date
),
joined AS (
    SELECT
        ds.d,
        fd.hrv_overnight_ms,
        dm.joy,
        dm.energy
    FROM date_series ds
    LEFT JOIN fitness_daily fd ON fd.user_id = :uid AND fd.local_date = ds.d
    LEFT JOIN daily_mood    dm ON dm.d = ds.d
)
SELECT
    d,
    AVG(hrv_overnight_ms) OVER w AS hrv_roll,
    AVG(joy)              OVER w AS joy_roll,
    AVG(energy)           OVER w AS energy_roll
FROM joined
WINDOW w AS (
    ORDER BY d
    ROWS BETWEEN (:window - 1) PRECEDING AND CURRENT ROW
)
ORDER BY d;
```

The recursive date series guarantees one row per calendar day in `[:start, :end]`. With that
fixed grid, `ROWS BETWEEN N PRECEDING` and "N calendar days" coincide. `AVG()` ignores NULLs,
so missing days neither corrupt the rolling mean nor shorten the window.

All three queries execute against indexes already declared: `idx_fit_daily_user_date`,
`idx_fit_act_user_type_date`, and the existing `idx_entries_user_date` / `idx_mood_entry`.

---

## 9. Out of scope (explicit)

- `fitness_workouts` (S3 — defer until a query needs it).
- Wide source-native activity-type enum on `activity_type`. The coarse 7-value bucketing is
  deliberate (correlation queries care about "all runs" not "trail runs"). `source_subtype`
  preserves the verbatim source string for any future query that needs the distinction;
  promoting commonly-queried subtypes into the coarse enum is a future migration if a use
  case emerges.
- FTS5 on activity names / notes (fitness data isn't searched by free text).
- Cross-source deduplication of activities (a Strava run uploaded from a Garmin watch will
  appear in both raw tables and normalize to two `fitness_activities` rows; deduping is an
  integrate-layer concern, not a storage one).
- ChromaDB embeddings for activities (no semantic search planned).
- Encryption-at-rest for `fitness_auth_state.access_token` — handled at the SQLite layer if
  needed (master plan Q2), not in this schema.
- Retention policy on `fitness_sync_runs`. At 2 sources × daily cadence ≈ 730 rows/year,
  the table is intentionally unbounded — well within personal-scale limits and useful as
  long-term operational history. Add a pruning policy if it ever becomes load-bearing
  (e.g. keep last 90 days + all non-success rows).
