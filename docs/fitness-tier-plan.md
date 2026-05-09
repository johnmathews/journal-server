# Fitness Integration — Execution Plan

**Status:** active. **Last updated:** 2026-05-09 (W9 shipped). **Supersedes:** none.
**Created:** 2026-05-09. **Owner of decisions:** [`fitness-integration-plan.md`](./fitness-integration-plan.md).
**Owner of schema:** [`fitness-schema.md`](./fitness-schema.md).
**Code-grounded:** yes — `src/journal/{providers,services,api,mcp_server,db,cli,config.py}` reviewed in
worktree before writing; existing patterns (jobs runner, notifications topics, migration style) referenced
inline.

This document is the execution sequencing for the fitness integration. Decisions and rationale are upstream
in [`fitness-integration-plan.md`](./fitness-integration-plan.md); concrete schema is in
[`fitness-schema.md`](./fitness-schema.md). **This doc only covers "what to build, in what order, with
what tests."** It does not relitigate decisions.

## Contents

1. [Preparation — credential acquisition (blocking P0)](#1-preparation--credential-acquisition-blocking-p0)
2. [Library + dependency pins](#2-library--dependency-pins)
3. [Work-unit overview](#3-work-unit-overview)
4. [Work units (TDD-ordered)](#4-work-units-tdd-ordered)
5. [Out of scope](#5-out-of-scope-this-document)
6. [Stopping points & checkpoints](#6-stopping-points--checkpoints)
7. [Kill criteria (reference)](#7-kill-criteria-reference)

---

## 1. Preparation — credential acquisition (blocking P0)

These are user-action items, not code. Listed here because work units **W11–W14** cannot be verified
end-to-end until both are done. **W1–W10 can ship without either** (fixture-based testing).

### P0.1 — Strava API application registration

1. Sign in to [strava.com](https://www.strava.com) on the account whose data we want.
2. Visit **<https://www.strava.com/settings/api>** and click *Create & Manage Your App*.
3. Fields the form requires (anything not listed here is optional):
   - **Application Name:** `journal-fitness` (or any short identifier — only the user sees it).
   - **Category:** *Data Importer* (closest fit for "ingest activities into a private analytics tool").
   - **Club:** leave blank.
   - **Website:** any URL — `http://localhost:8400` is fine for personal use; Strava just needs *something*.
   - **Authorization Callback Domain:** `localhost`. This restricts where the OAuth redirect can land. We
     will host the callback at `http://localhost:<port>/strava/callback` during the one-time auth dance.
4. Strava issues a **Client ID** and **Client Secret**. Store both — they go into `.env` as
   `STRAVA_CLIENT_ID` and `STRAVA_CLIENT_SECRET` (W3 adds the config fields).
5. Default rate limits per app (verified 2026-05-09 against
   <https://developers.strava.com/docs/rate-limits/>):
   - **Overall:** 200 requests / 15 min, 2000 / day.
   - **Non-upload:** 100 requests / 15 min, 1000 / day. Most read endpoints (including
     `getLoggedInAthleteActivities`) fall under non-upload.
   - These are well within the daily-cadence single-user budget. No request to raise limits is
     needed.

> **Cross-doc fix needed.** [`fitness-integration-plan.md`](./fitness-integration-plan.md) §6 Q1
> currently says "600 / 15 min, 30k / day" — that's incorrect. The accurate figures are above. The
> master plan should be updated in the same commit that lands this tier plan.

**One-time OAuth code exchange** (runs once, then refresh tokens carry us forward):

1. Browse to a hand-built authorization URL with `scope=activity:read_all`. The CLI re-auth command
   built in W11 prints this URL; for the very first run before W11 is shipped, paste-build it manually:
   ```
   https://www.strava.com/oauth/authorize?client_id=<id>&response_type=code&redirect_uri=http://localhost:8400/strava/callback&approval_prompt=auto&scope=activity:read_all
   ```
2. Approve the prompt; Strava redirects to `localhost:8400/strava/callback?code=<...>` (W11 will run a
   tiny one-shot HTTP listener on that port; for a manual first run, copy the `code` param out of the
   browser URL bar before the redirect 404s).
3. POST `code`, `client_id`, `client_secret`, `grant_type=authorization_code` to
   `https://www.strava.com/oauth/token`. Response yields `access_token` (6h life), `refresh_token`
   (long-lived), `expires_at`. Persist both into `fitness_auth_state` (W2 schema; W6 fetch service uses).
4. From here on `stravalib` auto-refreshes access tokens using the refresh token.

> **API Agreement note** (from research, Nov 2024 update). Strava's API agreement restricts third-party
> apps to displaying a user's data back to *that user only* and prohibits AI/ML training on the data.
> Fine for our single-user personal pipeline; flag if scope ever expands to multi-user.

### P0.2 — Garmin Connect credentials

No application registration. The unofficial library uses the same login as the Garmin Connect web/mobile UI.

1. Confirm the user's Garmin Connect username (email) and password.
2. Confirm whether MFA is enabled on the account. If yes, the first login in `python-garminconnect`
   triggers a `prompt_mfa` callback that needs the user to type the 6-digit code from email/SMS/auth
   app. W11's CLI re-auth command wires this prompt into the terminal.
3. After first successful login, `garth` writes OAuth1 + OAuth2 tokens to `~/.garminconnect` (or whatever
   path we configure). OAuth1 tokens persist ~1 year; we cache them in `fitness_auth_state.extra_state_json`
   instead of (or in addition to) the filesystem so the backup story stays single-source — see W2 for the
   trade-off. Subsequent logins are silent — no MFA prompt — until the OAuth1 token expires or Garmin
   forces a re-auth (the March/April 2026 SSO incident is the canonical example).

> **Reliability note** (research-confirmed). `python-garminconnect` had ~3.5 weeks of total breakage in
> March–April 2026 when Garmin changed SSO and the upstream `garth` library was deprecated. The alerting
> taxonomy (D5) plus the manual re-auth path (W11) is the mitigation; this is not a fix-once problem.

### P0.3 — Local secrets layout

Add to `.env` (server):

```bash
STRAVA_CLIENT_ID=<from P0.1>
STRAVA_CLIENT_SECRET=<from P0.1>
GARMIN_USERNAME=<email>
GARMIN_PASSWORD=<password>
# Optional — only if running re-auth from a non-default port
STRAVA_REDIRECT_URI=http://localhost:8400/strava/callback
```

Per existing convention (`.env` is gitignored; repo provides `.env.example`). W3 updates `.env.example`
with the same keys and dummy values.

---

## 2. Library + dependency pins

Both confirmed via web research 2026-05-09:

| Package | Pin | Why |
|---|---|---|
| `stravalib` | `~=2.2` (latest 2.x; last update 2026-01-23) | Actively maintained, Pydantic 2.x, Python 3.13 support, OAuth refresh + rate limiting built in. Stravaio is unmaintained-adjacent and not a viable alternative. |
| `garminconnect` | exact pin (e.g. `==0.2.x`) | Wraps `garth`; reliability research mandates an exact pin so an upstream change cannot break prod silently. Upgrades go through dev-environment dry run before pinning forward. |
| (transitive) `garth` | follow `garminconnect`'s pin | Don't pin separately — `garminconnect` controls the version that matches its expectations. |

Add via `uv add stravalib==<resolved>` and `uv add garminconnect==<resolved>` during W4 / W5 respectively;
record the resolved versions in the W4/W5 commit messages.

---

## 3. Work-unit overview

15 work units across 5 phases. **W1–W10 unblock without live credentials** (fixture-based tests against
recorded JSON). **W11–W14 require P0.1 / P0.2** to run end-to-end.

| # | Title | Phase | Priority | Needs creds? | Blocks |
|---|---|---|---|---|---|
| W1 | Schema migrations 0023/0024/0025 + integrity-check helper | A. Foundation | Critical | No | W2, W6, W7 |
| W2 | Repository layer for fitness tables | A. Foundation | Critical | No | W6, W7 |
| W3 | Config + notification topics + .env.example | A. Foundation | High | No | W4, W5, W6 |
| W4 | Strava provider (Protocol + `stravalib` adapter) | B. Provider seam | High | No (fixtures) | W6 |
| W5 | Garmin provider (Protocol + `garminconnect` adapter) | B. Provider seam | High | No (fixtures) | W6 |
| W6 | Fetch service + sync-run state machine + alerting taxonomy | C. Pipeline | High | No | W8, W12 |
| W7 | Normalize service (raw → activities/daily, idempotent) | C. Pipeline | High | No | W9, W10 |
| W8 | Job workers + JobRunner wiring (`fitness_sync_*`) | C. Pipeline | High | No | W11 |
| W9 | REST endpoints (`api/fitness.py` reads + `api/ingestion.py` sync POST) | D. Surface | Medium | No | W14 |
| W10 | MCP tools (`mcp_server/tools/fitness.py`) | D. Surface | Medium | No | — |
| W11 | CLI re-auth + first-run flow (`uv run journal fitness-* ...`) | E. Operational | High | **Yes** | W13 |
| W12 | Health endpoint extension (last-success per source, auth status) | E. Operational | Medium | No | — |
| W13 | Backfill from 2026-01-01 + first live smoke test | E. Operational | Medium | **Yes** | — |
| W14 | Documentation + roadmap index update + journal entry | F. Polish | Medium | No | — |
| W15 | Webapp sync-status panel + auth-broken banner *(webapp repo)* | F. Polish | Low | **Yes** for verification | — |

Critical-path: W1 → W2 → W6 → W8 → W11. Everything else parallelizes off that spine.

---

## 4. Work units (TDD-ordered)

Each unit follows the doc → tests → code order from the engineering-team skill, except W1 (migrations) and
W14 (docs) where the doc/tests/code split is degenerate. **All unit tests use an in-memory SQLite DB
created via `db.connection.connect()` then `db.migrations.run_migrations()` — same pattern the existing
repository tests use under `tests/test_db/`.**

> **Test directory naming.** Verified against the actual layout: tests live under `tests/test_<area>/`,
> not `tests/<area>/` (e.g. `tests/test_db/`, `tests/test_providers/`, `tests/test_services/`,
> `tests/test_services/test_jobs/`). All test paths in the work units below follow this convention. New
> directories required by these work units (`tests/test_services/test_fitness/`,
> `tests/test_db/test_migrations/` if not already present, `tests/test_mcp_server/`, `tests/test_cli/`)
> must be created with empty `__init__.py` files matching the existing style.

### W1 — Schema migrations 0023/0024/0025 + integrity-check helper

**Priority:** Critical. **Files:**

- `src/journal/db/migrations/0023_fitness_auth_and_sync.sql` *(new)*
- `src/journal/db/migrations/0024_fitness_raw.sql` *(new)*
- `src/journal/db/migrations/0025_fitness_normalized.sql` *(new)*
- `src/journal/db/fitness_integrity.py` *(new)* — runs the §6 sketch from `fitness-schema.md`.
- `tests/test_db/test_fitness_migrations.py` *(new)*
- `tests/test_db/test_fitness_integrity.py` *(new)*

**SQL content:** copy verbatim from [`fitness-schema.md`](./fitness-schema.md) §2, §3, §4. Use
`CREATE TABLE IF NOT EXISTS` and `CREATE INDEX IF NOT EXISTS` exactly as the existing
migrations do. Add a header comment block to each file mirroring the style of
`migrations/0022_*.sql`.

**Tests:**

1. **Schema present.** After `run_migrations()`, assert each table exists with the expected columns
   (`PRAGMA table_info(fitness_activities)` etc.) and indexes (`PRAGMA index_list`).
2. **CHECK constraints fire.** Insert a `fitness_activities` row with `avg_hr_bpm = 300` (out of range
   20–250) — expect `IntegrityError`. Repeat for `activity_type='unknown'`, `sleep_score=120`, etc. One
   parameterised test covers the lot.
3. **UNIQUE constraints fire.** Insert two `fitness_activities` rows with the same
   `(user_id, source, source_id)` — second insert raises.
4. **Idempotent re-run.** Set `PRAGMA user_version = 22` (the pre-fitness baseline), run
   `run_migrations()`, assert `user_version` advances to 25 and post-state is correct. Set back to 22,
   re-run — `IF NOT EXISTS` clauses must let it succeed without raising on the existing tables.
5. **Cross-file FK partial-install hazard.** `0024_fitness_raw.sql` references `fitness_sync_runs(id)`
   from `0023_fitness_auth_and_sync.sql`. Python's `sqlite3.executescript` is autocommit per
   statement, so a crash mid-`0024` leaves the user at version 23 with `fitness_sync_runs` present
   but raw tables missing. This is acceptable — the system stays coherent at version 23 — but the
   test should explicitly verify: apply only `0023`, query `fitness_sync_runs`, confirm it works
   standalone.
5. **Integrity check on synthetic prod-shaped data.** Build a fixture DB containing:
   - One `fitness_activities` (Strava) with a `raw_ref_id` pointing at a real `fitness_raw_strava.id`.
   - One `fitness_activities` (Strava) with `raw_ref_id = 99999` (orphan).
   - One `fitness_daily` whose `raw_ref_ids_json` contains a valid id + an invalid id.
   Run `check_fitness_integrity(conn)` and assert the orphans are reported.

**Acceptance criteria:**

- `uv run pytest tests/test_db/test_fitness_migrations.py tests/test_db/test_fitness_integrity.py` green.
- Full suite still green (no regression in the existing migration-runner tests).
- `PRAGMA user_version` advances to 25 after running migrations on a fresh DB.

---

### W2 — Repository layer for fitness tables

**Priority:** Critical. **Files:**

- `src/journal/db/fitness_repository.py` *(new — single file, follow the `jobs_repository.py` pattern;
  this is small enough not to warrant a sub-package yet, per the round-3 refactor convention of
  splitting only when growth demands it)*
- `tests/test_db/test_fitness_repository.py` *(new)*

**Public surface (proposed — finalise during code, but tests are the contract):**

```python
class FitnessRepository:
    def __init__(self, conn: sqlite3.Connection) -> None: ...

    # Auth state. All mutating methods MUST set `updated_at` to now in the same UPDATE —
    # `fitness-schema.md` §4 explicitly warns this column is app-managed (no SQLite ON UPDATE).
    def get_auth_state(self, *, user_id: int, source: str) -> FitnessAuthState | None: ...
    def upsert_auth_state(self, state: FitnessAuthState) -> None: ...   # sets updated_at
    def transition_auth(self, *, user_id: int, source: str,
                        status: Literal["ok", "broken"], at: str) -> bool:
        """Returns True iff status actually changed (drives 'fire-once' alert).
        Also sets `auth_broken_since` to `at` on transition to broken, and to NULL
        on transition to ok. Updates `updated_at` to now."""

    # Sync runs
    def start_sync_run(self, *, user_id: int, source: str) -> int: ...   # returns id
    def finish_sync_run(self, run_id: int, *, status: str,
                        error_class: str | None = None,
                        error_message: str | None = None,
                        rows_fetched: int = 0,
                        rows_normalized: int = 0,
                        notes: dict | None = None) -> None: ...

    # Raw archive
    def insert_raw(self, *, source: Literal["strava", "garmin"],
                   user_id: int, endpoint: str, source_id: str,
                   payload_json: str, sync_run_id: int) -> int | None:
        """INSERT OR IGNORE on UNIQUE(user_id, source_id, endpoint, payload_sha256)
        for Strava / UNIQUE(user_id, endpoint, source_id, payload_sha256) for Garmin
        — both include user_id explicitly. Computes payload_sha256 internally;
        callers do not pass it. Returns the new row id, or None if a row with
        identical sha256 already exists. A *changed* payload (different sha256
        for the same logical key) inserts a new row — the old row is preserved
        per the append-only rule (D3)."""
    def list_raw_for_normalize(self, *, source: str, user_id: int,
                               since: str | None = None) -> Iterator[RawRow]: ...

    # Normalized
    def upsert_activity(self, activity: FitnessActivity) -> None: ...   # INSERT OR REPLACE
    def upsert_daily(self, daily: FitnessDaily) -> None: ...            # INSERT OR REPLACE
    def list_activities(self, *, user_id: int, start: str, end: str,
                        activity_type: str | None = None) -> list[FitnessActivity]: ...
    def list_daily(self, *, user_id: int, start: str, end: str) -> list[FitnessDaily]: ...

    # Read-only — last-success per source (for /health)
    def last_successful_sync_at(self, *, user_id: int, source: str) -> str | None: ...
```

Dataclasses for `FitnessAuthState`, `FitnessActivity`, `FitnessDaily`, `RawRow` go in
`src/journal/models.py` next to the existing `Entry`/`Job`/`Entity` dataclasses.

**Tests** (in-memory DB + migrations):

1. Round-trip auth state — upsert, get back identical bytes, including `extra_state_json` survives JSON.
2. `transition_auth` returns True only on status change. First call with `"broken"` returns `True`;
   second consecutive call with `"broken"` returns `False`. After transition to broken,
   `auth_broken_since` is set; after transition back to ok, `auth_broken_since` is NULL.
   `updated_at` advances on every call.
3. `start_sync_run` + `finish_sync_run` with each terminal status (success / auth_broken /
   transient_failure / normalize_drift). Querying `last_successful_sync_at` returns max(started_at)
   across only the success rows.
4. `insert_raw` returns id on first insert, None on duplicate (same `payload_sha256`), and a *new*
   id (new row, not an update) on changed payload. Verify the prior row still exists in raw after
   the changed-payload insert (raw is append-only — D3). Computed `payload_sha256` is deterministic.
5. `upsert_activity` and `upsert_daily` are idempotent — calling twice with the same
   `(user_id, source, source_id)` / `(user_id, source, local_date)` leaves one row, with the latest
   field values.
6. `list_activities` filters by date range and activity_type correctly. Boundary check: insert four
   activities at `local_date` = `[start - 1 day, start, end, end + 1 day]`; assert
   `list_activities(start=..., end=...)` returns exactly the two with `local_date` ∈ {start, end}.
   This pins down the inclusive-on-both-sides semantic concretely.

**Acceptance criteria:**

- `uv run pytest tests/test_db/test_fitness_repository.py` green.
- `payload_sha256` computed inside `insert_raw` (callers don't need to compute it themselves).
- All time fields stored as ISO 8601 UTC strings (project house style — see existing repository).

---

### W3 — Config + notification topics + .env.example

**Priority:** High. **Files:**

- `src/journal/config.py` *(modify — add fields per below)*
- `src/journal/services/notifications.py` *(modify — append 4 entries to `TOPICS`, per
  [`fitness-schema.md`](./fitness-schema.md) §5)*
- `.env.example` *(modify — add P0.3 keys)*
- `tests/test_config.py` *(modify — add cases for new fields)*
- `tests/test_services/test_notifications.py` *(modify — assert new topic keys present)*

**New `Config` fields (additive; defaults preserve current behaviour):**

```python
strava_client_id: str = field(default_factory=lambda: os.environ.get("STRAVA_CLIENT_ID", ""))
strava_client_secret: str = field(default_factory=lambda: os.environ.get("STRAVA_CLIENT_SECRET", ""))
strava_redirect_uri: str = field(default_factory=lambda: os.environ.get(
    "STRAVA_REDIRECT_URI", "http://localhost:8400/strava/callback"))
garmin_username: str = field(default_factory=lambda: os.environ.get("GARMIN_USERNAME", ""))
garmin_password: str = field(default_factory=lambda: os.environ.get("GARMIN_PASSWORD", ""))

# How many consecutive transient failures before Pushover fires (D5):
fitness_transient_failure_threshold: int = field(default_factory=lambda: int(
    os.environ.get("FITNESS_TRANSIENT_FAILURE_THRESHOLD", "3")))
# Backfill cutoff. ISO date.
fitness_backfill_start: str = field(default_factory=lambda: os.environ.get(
    "FITNESS_BACKFILL_START", "2026-01-01"))
```

**Notification topics:** copy the four entries from
[`fitness-schema.md`](./fitness-schema.md) §5 verbatim into the `TOPICS` list. Do **not** create a parallel
notification mechanism (master plan D5).

**Tests:**

1. `Config()` with no env vars: all new fields have the documented defaults. Empty strings for unset
   creds are tolerated (the code that needs them errors at use-site, not at construct-time — matches
   how `anthropic_api_key` already works).
2. New env vars override defaults.
3. The four new `TOPICS` keys exist with `group` / `default` / `admin_only` matching
   [`fitness-schema.md`](./fitness-schema.md) §5 table.

**Acceptance criteria:**

- `uv run pytest tests/test_config.py tests/test_services/test_notifications.py` green.
- `.env.example` lists the new keys with placeholder values + a one-line comment per key explaining
  what to put there. Reference P0.1 / P0.2 in a single header comment.

---

### W4 — Strava provider (Protocol + `stravalib` adapter)

**Priority:** High. **Files:**

- `src/journal/providers/strava.py` *(new — Protocol + adapter, single-file pattern from `providers/ocr.py`)*
- `tests/test_providers/test_strava.py` *(new)*
- `tests/test_providers/fixtures/strava/` *(new — recorded JSON for `get_activities`, `get_activity` covering
  one of each: run, ride, swim, walk, hike, weight training, "other")*
- `pyproject.toml` *(modify — add `stravalib` dep)*

**Protocol surface (data-shape only — stable across adapters):**

```python
@dataclass(frozen=True)
class StravaActivitySummary:
    source_id: str          # str(strava_id)
    sport_type: str         # verbatim Strava enum
    start_time: str         # ISO 8601 UTC
    local_date: str         # YYYY-MM-DD athlete-local
    duration_s: int
    moving_time_s: int | None
    distance_m: float | None
    elevation_gain_m: float | None
    avg_hr_bpm: int | None
    max_hr_bpm: int | None
    calories_kcal: int | None
    extras: dict[str, Any]   # source-specific spillover for normalize → extras_json
    raw_payload: dict        # the verbatim Strava JSON, for raw archive

@runtime_checkable
class StravaProvider(Protocol):
    def list_activities(self, *, after: datetime, before: datetime) -> Iterator[StravaActivitySummary]: ...
    def get_activity_detail(self, source_id: str) -> StravaActivitySummary: ...
    def refresh_token_if_needed(self) -> None: ...
```

The `stravalib` adapter wraps a `stravalib.Client` constructed with the access token from
`fitness_auth_state`; on `refresh_token_if_needed` it calls `Client.refresh_access_token` and writes
the new access/refresh/expires triple back via the repository. **The adapter does NOT touch the DB
directly** — it takes a callable `persist_tokens: Callable[[Tokens], None]` injected by the fetch
service (W6). Same seam as `ocr.py` injects an Anthropic client rather than constructing it.

**Tests** (no live API):

1. **Replay-driven happy path.** Fixture: `tests/test_providers/fixtures/strava/list_activities_response.json` (a
   minimal recorded response from a real account, hand-anonymised). Stub `stravalib.Client.get_activities`
   to yield Pydantic-modelled objects from that fixture. Assert the adapter produces
   `StravaActivitySummary` objects with the right shape.
2. **Token refresh path.** Stub `Client.refresh_access_token` to return a known triple; call
   `refresh_token_if_needed` after setting `expires_at` in the past; assert the persist callback was
   invoked with the new triple.
3. **Units stay metric.** Strava's API delivers metric by default — the adapter just passes through.
   Assert `distance_m` is a float in metres (the value matches the raw fixture's metric field, not a
   yards-converted value). Sentinel test for a future regression where someone enables
   `units='imperial'` on the `stravalib.Client`.
4. **`sport_type` collapsing happens in normalize, not provider.** The adapter exposes `sport_type`
   verbatim. Test that `Run`, `TrailRun`, `WeightTraining` all flow through unchanged.

**Acceptance criteria:**

- `uv run pytest tests/test_providers/test_strava.py` green.
- `stravalib` resolved version recorded in commit message.
- Adapter has zero direct SQLite or HTTP-library imports beyond `stravalib` itself.

**Fixture sourcing:** until P0.1 is done we cannot record a real response. **Workaround:**
hand-craft a JSON fixture matching `stravalib`'s Pydantic model shape, documented inline with a
`# FIXTURE SOURCE: hand-crafted; replace at W13` marker. **At W13** (live smoke test), record a real
response from one anonymised activity and replace the fixture. Treat any test that fails after the
replacement as a real bug, not a flaky test — the hand-crafted fixture diverging from the real API
shape is exactly the failure mode this discipline catches. Same convention applies to W5 Garmin
fixtures.

---

### W5 — Garmin provider (Protocol + `garminconnect` adapter)

**Priority:** High. **Files:**

- `src/journal/providers/garmin.py` *(new)*
- `tests/test_providers/test_garmin.py` *(new)*
- `tests/test_providers/fixtures/garmin/` *(new — recorded JSON for sleep, hrv, body_battery, training_load,
  training_readiness, stress, activities)*
- `pyproject.toml` *(modify — add `garminconnect` dep, exact pin)*

**Protocol surface (data-shape only):**

```python
@dataclass(frozen=True)
class GarminDailyMetrics:
    local_date: str  # YYYY-MM-DD
    sleep_score: int | None
    sleep_duration_s: int | None
    sleep_efficiency_pct: float | None
    hrv_overnight_ms: float | None
    resting_hr_bpm: int | None
    body_battery_high: int | None
    body_battery_low: int | None
    stress_avg: int | None
    training_load_acute: float | None
    training_load_chronic: float | None
    training_readiness: int | None
    extras: dict[str, Any]
    raw_payloads_per_endpoint: dict[str, dict]  # 'sleep' -> {...}, 'hrv' -> {...}, etc.

@dataclass(frozen=True)
class GarminActivitySummary:
    source_id: str
    activity_type_str: str   # verbatim Garmin
    start_time: str
    local_date: str
    duration_s: int
    moving_time_s: int | None
    distance_m: float | None
    elevation_gain_m: float | None
    avg_hr_bpm: int | None
    max_hr_bpm: int | None
    calories_kcal: int | None
    extras: dict[str, Any]
    raw_payload: dict

@runtime_checkable
class GarminProvider(Protocol):
    def login(self, *, mfa_callback: Callable[[], str] | None = None) -> None: ...
    def get_daily(self, date: str) -> GarminDailyMetrics: ...
    def list_activities(self, *, after: datetime, before: datetime) -> Iterator[GarminActivitySummary]: ...
```

The `garminconnect` adapter takes a `tokens_path: Path | None` (defaults to a per-user path under
`<state_dir>/garmin_tokens/`).

**Token-loading sequence (D4 enforcement — "one login per token lifetime").** On `login()`:

1. **First**, attempt to load tokens from the DB row `fitness_auth_state.extra_state_json` (the
   source of truth). If valid, hand them to `garth` and skip the network roundtrip.
2. **Second**, fall through to the filesystem cache at `tokens_path` (belt-and-braces; useful for
   running the bare provider outside the journal-server process, e.g. in a debugging notebook).
3. **Last**, fall through to username/password with the optional `mfa_callback`.

After any successful network login, serialise the resulting OAuth1 + OAuth2 tokens into a JSON blob
that the fetch service mirrors into `fitness_auth_state.extra_state_json`. The filesystem cache is
*not* the source of truth — code paths that construct a fresh provider on every sync must read from
the DB to satisfy D4.

**Tests:**

1. **Replay-driven daily aggregation.** Fixtures: one JSON file per Garmin endpoint. Stub
   `Garmin.get_sleep_data`, `get_hrv_data`, `get_body_battery`, `get_training_status`,
   `get_training_readiness`, `get_stress_data` to read from fixtures. Assert `get_daily('2026-04-15')`
   returns a fully-populated `GarminDailyMetrics`.
2. **Partial-data resilience.** A day with no HRV reading (e.g. user took the watch off): the HRV
   endpoint returns an empty payload. Adapter returns `hrv_overnight_ms = None`, all other fields
   populated, `raw_payloads_per_endpoint` still records the (empty) HRV response.
3. **MFA callback wiring.** Stub `garminconnect.Garmin.login` (the adapter's published seam — NOT
   `garth.login`, which is a transitive internal that can shift between `garminconnect` releases) to
   invoke a 2FA-required path; pass a callback that returns `'123456'`; assert the callback was
   called and login succeeded.
4. **`activity_type_str` is verbatim.** Garmin's strings vary (`running`, `treadmill_running`,
   `cycling`, `mountain_biking`); the provider passes them through, normalize maps to coarse buckets.

**Acceptance criteria:**

- `uv run pytest tests/test_providers/test_garmin.py` green.
- `garminconnect` exact version pinned in `pyproject.toml`.
- The adapter never silently swallows a 401/403 — those propagate as a typed `GarminAuthError`
  (defined in this module) so the fetch service (W6) can classify them as `auth_broken`.

---

### W6 — Fetch service + sync-run state machine + alerting taxonomy

**Priority:** High. **Files:**

- `src/journal/services/fitness/__init__.py` *(new)*
- `src/journal/services/fitness/fetch.py` *(new)*
- `src/journal/services/fitness/errors.py` *(new — `FitnessAuthError`, `FitnessTransientError`,
  `FitnessNormalizeDrift`)*
- `tests/test_services/test_fitness/test_fetch.py` *(new)*

**Behaviour:**

`FitnessSyncResult` is a frozen dataclass defined in `services/fitness/fetch.py` (alongside the
service): `{ rows_fetched: int, rows_normalized: int, run_id: int, status: str }`. Workers (W8)
serialise it via `dataclasses.asdict`.

**Error hierarchy.** `services/fitness/errors.py` defines:

```python
class FitnessError(Exception): ...
class FitnessAuthError(FitnessError): ...
class FitnessTransientError(FitnessError): ...
class FitnessNormalizeDrift(FitnessError): ...
```

Provider-level exceptions (`StravaAuthError` from W4, `GarminAuthError` from W5, plus transient
`stravalib.exc.RateLimitExceeded`/`Fault` and Garmin HTTP errors) are caught by the fetch service
and **re-raised as the corresponding `Fitness*Error` subtype** so downstream code (worker, API)
depends only on `services/fitness/errors.py`, not on provider modules. This keeps the worker import
graph clean and means swapping a provider library never reaches into worker code.

**Concurrency / single-run guard.** Before `start_sync_run`, the fetch service queries
`SELECT id FROM fitness_sync_runs WHERE user_id=:uid AND source=:source AND status='running'` —
if a row exists, return that run's id rather than starting a duplicate. Combined with the
single-worker `JobRunner` constraint, this guards token-refresh races: only one fetch service per
source can write to `fitness_auth_state` at a time, so the read-old-token / write-new-token race
in the runner header's hazard list cannot happen for fitness syncs in practice.

`FetchService` per source. Public method
`run_sync(*, user_id: int, since: datetime | None = None, until: datetime | None = None) -> FitnessSyncResult`:

**Datetime range derivation.** When `since`/`until` are not provided (the routine daily case):
`since = max(last_successful_sync_at, fitness_backfill_start)` and `until = now`. The CLI
backfill (W13) passes explicit values; routine `fitness-sync` lets the service pick. This is the
single source of truth for the daily window — providers don't make it up.

1. Load `fitness_auth_state`. Construct provider with cached tokens. If load fails or auth-state row
   doesn't exist, return early with status `auth_broken` and a sync-run row recording why.
2. `repo.start_sync_run(...)` — get a `sync_run_id`.
3. Try the fetch (provider call list). On `*AuthError`: classify as `auth_broken`, transition auth
   state, fire `notif_fitness_auth_broken` *only if* the transition was new (use the bool returned
   from `transition_auth`). Finish sync run with `auth_broken`.
4. On `*TransientError` (network, 5xx, 429): increment a counter on `extras_json` of the auth state
   row (or join across last-N `fitness_sync_runs` — pick during code, prefer the latter since it's
   query-able). If the threshold (`fitness_transient_failure_threshold`, default 3) is crossed, fire
   `notif_fitness_sync_failure`. Finish run with `transient_failure`.
5. On success: write each fetched payload via `repo.insert_raw(...)` keyed on `(source, endpoint,
   source_id, payload_sha256)`. Increment `rows_fetched`. Transition auth back to `ok` (which fires
   no alert — recovery is silent per D5; the webapp banner clears as a side-effect of the status
   flipping). Finish run with `success`.

The fetch service does **not** normalize. Normalization runs separately (W7) so that a fetch can succeed
even if normalization has a bug — the raw is captured first, derivatives second.

**Tests** (FakeStrava / FakeGarmin providers + in-memory DB):

1. **Happy path Strava** — fake provider yields 3 activities; assert 3 rows in `fitness_raw_strava`,
   one row in `fitness_sync_runs` with `status='success'`, `auth_status` flips to `'ok'`.
2. **Happy path Garmin** — fake provider yields one daily metrics object covering 6 endpoints; assert
   6 rows in `fitness_raw_garmin` (one per endpoint) sharing the same `(source_id, fetched_at)` only
   if intended — actually each endpoint has its own `source_id` (per [`fitness-schema.md`](./fitness-schema.md) §2 table), so it's
   `endpoint='sleep', source_id='2026-04-15'`, etc.
3. **Auth-broken on first failure** — fake raises `StravaAuthError`. Auth state transitions to
   `'broken'`, Pushover fired exactly once. Run again with same fake — auth still broken, Pushover NOT
   re-fired (transition returned False).
4. **Transient threshold** — three consecutive transient failures fire Pushover on the third. Verify
   no fire on attempts 1 and 2.
5. **Idempotent re-run on identical payload** — fake provider returns the same activity twice across
   two `run_sync()` calls. Second insert is a no-op (same `payload_sha256`), `rows_fetched` increments
   only on the first call.
6. **Re-run after an auth recovery** — auth was `broken` with `auth_broken_since` set. Fake is now
   happy. After the success run: `auth_status='ok'`, `auth_broken_since IS NULL` (asserted directly
   against the DB row, not just the dataclass field — the webapp banner clear depends on this
   column being NULL). No Pushover fires (D5: recovery is silent).
7. **Normalize drift not raised here** — drift is W7's concern. The fetch service classifies only
   auth_broken / transient_failure / success / unknown; if it sees an exception class it doesn't
   recognise, the sync-run row records it as `transient_failure` with `error_class` set, and the run
   logs LOUDLY but returns. Test ensures unknown errors don't crash the worker.

**Acceptance criteria:**

- `uv run pytest tests/test_services/test_fitness/` green.
- The state machine matches D5 exactly (`auth_broken` is fire-once, transient is fire-after-N,
  normalize-drift is W7's, success is silent unless the user opted into `notif_fitness_sync_success`).
- The service has zero references to `stravalib`/`garminconnect` types — only the Protocols from W4/W5.

---

### W7 — Normalize service (raw → activities/daily, idempotent)

**Priority:** High. **Files:**

- `src/journal/services/fitness/normalize.py` *(new)*
- `src/journal/services/fitness/_activity_type_map.py` *(new — Strava + Garmin → coarse enum tables)*
- `tests/test_services/test_fitness/test_normalize.py` *(new)*

**Behaviour:**

Two entry points:

```python
def normalize_strava(repo: FitnessRepository, *, user_id: int,
                     since: str | None = None) -> NormalizeResult: ...
def normalize_garmin(repo: FitnessRepository, *, user_id: int,
                     since: str | None = None) -> NormalizeResult: ...
```

**Resume predicate (precise — no hand-waving).** The default `since` is computed as a watermark over
*raw* rows, not normalized rows: `since = (SELECT MAX(fetched_at) FROM <normalized_table> WHERE
user_id=:uid AND source=:source)`. Read all raw rows where `fetched_at > since` (or `>= since` on the
first run when the normalized table is empty — handle the NULL watermark case explicitly). This
predicate compares two `fetched_at` columns from the same clock (`strftime('%Y-%m-%dT%H:%M:%SZ',
'now')` UTC, by DEFAULT in both raw tables) so the comparison is well-defined even after a mid-batch
crash. Both raw and normalized tables write their timestamps at INSERT time using the same SQLite
clock, so a partial batch leaves a coherent watermark for the next run.

Each entry point reads raw rows since the watermark, groups them by `(source_id, endpoint)` for
Garmin (so all 6 daily endpoints for a given day fan in to one `fitness_daily` row), maps fields,
and `INSERT OR REPLACE`s. Activity-type collapsing uses the table from
[`fitness-schema.md`](./fitness-schema.md) §3 — implemented once in
`_activity_type_map.py` and the docstring explicitly notes that the source of truth is the schema
doc; if they ever diverge, the doc wins (the test in W7-#6 will fail).

**Authoritativeness rule** (from §3 of the schema doc): when multiple raw rows exist for the same
`(endpoint, source_id)` due to a Garmin re-publish, pick the one with the largest `fetched_at` and
use *its* id as the contributing entry in `raw_ref_ids_json`. Older rows stay in raw.

**Drift handling:** if a raw row cannot be normalized (missing required field, schema change), do
**not** crash the whole batch. Log loudly, write a `fitness_sync_runs` row with status
`normalize_drift` referencing the drift count, fire `notif_fitness_normalize_drift` (admin-only). The
batch continues for everything that *can* be normalized. Drift is a code bug to fix, not a page.

**Tests** (built on top of W2 fixture builders):

1. **Strava activity normalize.** Insert one raw row per coarse type (run/ride/swim/walk/hike/strength/
   other — 7 fixtures). Run normalize. Assert 7 `fitness_activities` rows with the right
   `activity_type` mapping and `source_subtype` preserving the verbatim `sport_type`.
2. **Garmin daily fan-in.** Insert 6 raw rows for `2026-04-15` (one per endpoint). Run normalize.
   Assert one `fitness_daily` row with all six metric columns populated and
   `raw_ref_ids_json` listing all 6 raw row ids.
3. **Garmin re-publish authoritativeness.** Insert two `fitness_raw_garmin` rows for
   `endpoint='sleep', source_id='2026-04-15'` with different `payload_sha256` and `fetched_at`. Run
   normalize. The newer row's id is in `raw_ref_ids_json`; the older row is NOT but is still in raw.
4. **Idempotent re-run.** Run normalize twice on the same raw set. Same number of normalized rows;
   `normalized_at` advances on the second pass; nothing duplicates.
5. **Drift on missing required field.** Insert a `fitness_raw_strava` whose payload is missing
   `start_date_local`. Run normalize. The row is skipped, the rest of the batch succeeds, a
   `normalize_drift` sync-run is recorded, the Pushover topic is fired once for the batch (not per
   row).
6. **Activity-type mapping edge cases.** `Rowing` → `other`, `WeightTraining` → `strength`,
   `MountainBikeRide` → `ride`, `VirtualRun` → `run`. One parameterised test from the §3 table.
7. **Tests cover prod-shaped data.** Per the engineering-team skill's state-transforming-work-unit
   rules: include at least one fixture in a "dirty" state (mix of valid + drift + duplicates) and
   assert the normalize pass completes and produces the expected normalized rows.

**Acceptance criteria:**

- `uv run pytest tests/test_services/test_fitness/test_normalize.py` green.
- The activity-type map is a single source of truth used both by code and exported back to
  [`fitness-schema.md`](./fitness-schema.md) §3 (no drift between docs and code).
- A drift event never raises out of the function — it's recorded, alerted, and the loop continues.

---

### W8 — Job workers + JobRunner wiring (`fitness_sync_*`)

**Priority:** High. **Files:**

- `src/journal/services/jobs/workers/fitness_sync_strava.py` *(new — follow the `entity_reembed.py`
  shape: no `parent_job_id`, no blob queue, no pipeline coordinator)*
- `src/journal/services/jobs/workers/fitness_sync_garmin.py` *(new)*
- `src/journal/services/jobs/runner.py` *(modify — `submit_fitness_sync_strava`,
  `submit_fitness_sync_garmin`, plumb `FetchService` + normalize callables through `WorkerContext`)*
- `src/journal/services/jobs/workers/__init__.py` *(modify — extend `WorkerContext` with four new
  callables: `fetch_strava`, `fetch_garmin`, `normalize_strava`, `normalize_garmin`. All four are
  required because the worker runs fetch THEN normalize inline — see worker body below)*
- `src/journal/services/jobs/validation.py` *(modify — add
  `FITNESS_SYNC_KEYS: dict[str, type] = {"user_id": int}`. The `source` is encoded in the job type
  name, not in params, matching how `mood_score_entry` doesn't carry a `dimension` param.)*
- `src/journal/models.py` *(modify — extend the `JobType` `Literal` with `"fitness_sync_strava"` and
  `"fitness_sync_garmin"`)*
- `tests/test_services/test_jobs/test_worker_fitness_sync.py` *(new — test files for workers live
  flat in `tests/test_services/test_jobs/test_worker_<name>.py` per the W6/W7 convention)*

**Plan correction (2026-05-09, W8 implementation).** Two earlier sketch assumptions did not survive
contact with the W6/W7 code:

1. The plan body sketch caught `FitnessAuthError` and a generic `Exception` (with `is_transient(e)`
   re-raise). But W6 decision #4 swallows every non-auth exception inside `_FetchServiceBase.run_sync`
   and returns `FitnessSyncResult(status="transient_failure")`; auth failures are also caught and
   converted to `status="auth_broken"` (with the auth transition + Pushover handled inside the fetch
   service). So the worker never sees `FitnessAuthError` from `run_sync` — it must branch on
   `fetch_result.status`. This also means the planned `is_transient` / `friendly_error` extension for
   stravalib / garminconnect errors is unnecessary at the worker layer — that classification already
   happens inside the fetch service. (The errors-module file edit is dropped from W8.)
2. W7 decision #2 keeps `_Drift` as an internal sentinel — `normalize_*` never raises
   `FitnessNormalizeDrift` out. Drift is reported via `NormalizeResult.drift_count` and an admin-only
   Pushover fire-once inside normalize itself. So the worker has no `FitnessNormalizeDrift` catch; it
   simply records `drift_count` in the job's result JSON.
3. W3 already populated `_SUCCESS_TOPIC_MAP["fitness_sync_strava" / "fitness_sync_garmin"]` and the
   matching `_JOB_TYPE_LABELS` entries (verified at `notifications.py:150`+`172`). Notifications-file
   edits are dropped from W8.

The corrected worker body:

```python
def run_fitness_sync_strava(ctx, job_id, params):
    user_id = int(params["user_id"])
    try:
        ctx.jobs.mark_running(job_id)
        fetch_result = ctx.fetch_strava(user_id=user_id)
        if fetch_result.status == "auth_broken":
            ctx.jobs.mark_failed(
                job_id,
                "Strava authorization is broken — please re-authorize",
            )
            return  # fetch already fired notif_fitness_auth_broken
        if fetch_result.status == "transient_failure":
            ctx.jobs.mark_failed(
                job_id,
                "Strava sync failed transiently — will retry on next run",
            )
            return  # fetch already advanced threshold counter
        if fetch_result.status == "running":
            ctx.jobs.mark_succeeded(
                job_id,
                {"skipped": True, "reason": "already_running",
                 "fetch": asdict(fetch_result)},
            )
            return
        # status == "success"
        normalize_result = ctx.normalize_strava(user_id=user_id)
        result = {
            "fetch": asdict(fetch_result),
            "normalize": asdict(normalize_result),
        }
        ctx.jobs.mark_succeeded(job_id, result)
        ctx.notifier.notify_success(user_id, "fitness_sync_strava", result)
    except Exception as exc:  # terminal-state guard
        # ... mark_failed + notify_failed, exactly like run_entity_reembed
```

Fetch and normalize run in the same job so the operator sees one unified "fitness sync" lifecycle in
the jobs UI. **This is committed, not optional** — the earlier draft hedged on splitting; we're not
going to split unless normalize becomes a measurable bottleneck.

**Tests** (unit-test the worker function directly with a minimal `WorkerContext` built from fakes,
same pattern as `tests/test_services/test_jobs/test_worker_entity_reembed.py`):

1. **Happy path success.** Fake fetch returns `FitnessSyncResult(status="success", ...)`, fake
   normalize returns `NormalizeResult(rows_normalized=3, drift_count=0)`. Worker calls
   `ctx.jobs.mark_succeeded` once with both results in `result`.
2. **Auth-broken short-circuit.** Fake fetch returns `status="auth_broken"`. Worker marks failed,
   normalize is **not** called.
3. **Transient-failure short-circuit.** Fake fetch returns `status="transient_failure"`. Worker marks
   failed, normalize is **not** called.
4. **Already-running short-circuit.** Fake fetch returns `status="running"`. Worker marks succeeded
   with a `skipped` flag, normalize is not called.
5. **Drift recorded in result JSON.** Fake normalize returns `drift_count=2`. Worker still marks
   succeeded; `result["normalize"]["drift_count"] == 2`.
6. **Terminal-state guard.** Fake fetch raises a bare `RuntimeError`. Worker calls `mark_failed`
   without re-raising (matches `run_entity_reembed`'s guard).
7. **Notification on success.** Fake fetch + normalize both succeed; `notifier.notify_success` is
   called with `"fitness_sync_strava"` so the `_SUCCESS_TOPIC_MAP` lookup gates the Pushover.
8. Same coverage for the Garmin worker (parametrized or symmetric).

**Acceptance criteria:**

- `uv run pytest tests/test_services/test_jobs/test_workers/test_fitness_sync.py` green.
- Full suite still green (`JobRunner` constructor changes are additive; `JobType` literal additive).
- A daily APScheduler / cron entry is **out of scope** for this work unit — operators trigger via the
  CLI command in W11 or the REST endpoint in W9. The "scheduler that fires daily at 04:00" lands as a
  future work unit on top of the existing job machinery; until then, the recommended invocation is
  an OS-level cron entry: `0 4 * * * cd /path/to/server && uv run journal fitness-sync --source both`.

---

### W9 — REST endpoints (`api/fitness.py` + `api/ingestion.py`)

**Priority:** Medium. **Files:**

- `src/journal/api/fitness.py` *(new — read-side routes only)*
- `src/journal/api/ingestion.py` *(modify — add the `POST /api/fitness/sync/{source}` job-creation
  route. The project's documented routing rule (read the docstring at the top of
  `api/ingestion.py`) is: "Routes whose primary effect is to create a job or perform a long-running
  write live in `api/ingestion.py`, regardless of URL prefix." `POST /sync/{source}` creates a job,
  so it goes here. The earlier draft tried to bundle it into `api/fitness.py`; that violates the
  rule. The only way to bundle it elsewhere would be to add a new override category to
  `code-quality-principles.md`, which is not warranted for one route.)*
- `src/journal/api/__init__.py` *(modify — register fitness routes following the existing pattern;
  see how `entities.py` and `dashboard.py` are wired in `register_api_routes`)*
- `tests/test_api/test_fitness.py` *(new)*

**Endpoints:**

| Method | Path | Module | Returns |
|---|---|---|---|
| `GET` | `/api/fitness/activities?start=&end=&type=` | `api/fitness.py` | List `FitnessActivity` in window |
| `GET` | `/api/fitness/daily?start=&end=` | `api/fitness.py` | List `FitnessDaily` in window |
| `GET` | `/api/fitness/sync/status` | `api/fitness.py` | Per-source dict: `last_success_at`, `auth_status`, `auth_broken_since`, `last_runs` (last 10) |
| `POST` | `/api/fitness/sync/{source}` | `api/ingestion.py` | 202 with `job_id` (new job) or 202 with the existing `job_id` if a sync is already running for that source — match the existing-job posture in `api/ingestion.py` for `POST /entries/ingest/images` |
| `GET` | `/api/fitness/integrity` | `api/fitness.py` | `{"orphans": [...]}` |

All auth-required, `user_id` from session.

**Response shapes (these are the contract for W15 webapp work):**

```jsonc
// GET /api/fitness/sync/status — empty DB
{ "strava": null, "garmin": null }

// GET /api/fitness/sync/status — populated
{
  "strava": {
    "auth_status": "ok",
    "last_success_at": "2026-05-09T04:01:23Z",
    "auth_broken_since": null,
    "last_runs": [
      { "id": 412, "started_at": "...", "finished_at": "...", "status": "success",
        "rows_fetched": 3, "rows_normalized": 3, "error_class": null, "error_message": null },
      ...
    ]
  },
  "garmin": { ... or null if never configured ... }
}

// GET /api/fitness/integrity — orphan record shape
{
  "activities": [
    { "activity_id": 17, "source": "strava", "raw_ref_id": 99999, "issue": "raw_row_missing" }
  ],
  "daily": [
    { "daily_id": 42, "source": "garmin", "missing_raw_ids": [99998, 99999] }
  ]
}
```

**Tests** (Starlette TestClient + in-memory DB):

1. Each GET returns 200 with the expected shape; out-of-range dates return empty arrays not 404.
2. `GET /api/fitness/sync/status` on an empty DB (no `fitness_auth_state` rows) returns
   `{"strava": null, "garmin": null}` with status 200 — not 500, not a KeyError. This is the
   most-likely-real first-use state.
3. `POST /api/fitness/sync/strava` returns 202 with a `job_id`. Calling again while the previous
   `running` job exists returns 202 with that job's id (not a new id, not 409 — match
   `api/ingestion.py`'s existing posture for duplicate ingest jobs).
4. `GET /api/fitness/integrity` returns empty arrays on a clean DB; populated arrays on a fixture DB
   seeded with deliberately-orphaned references.
5. Auth check: anonymous request returns 401 on every endpoint.

**Acceptance criteria:**

- `uv run pytest tests/test_api/test_fitness.py` green.
- The exact JSON shapes above appear in `docs/api.md` after W14 ships, so W15 (webapp) has a contract
  to build against without reading server source.

---

### W10 — MCP tools (`mcp_server/tools/fitness.py`)

**Priority:** Medium. **Files:**

- `src/journal/mcp_server/tools/fitness.py` *(new — module that registers `@mcp.tool()`s as
  side effects of import, exactly like `tools/entities.py`, `tools/jobs.py`, `tools/queries.py`)*
- `src/journal/mcp_server/__init__.py` *(modify — add the side-effect import and re-export the
  fitness tool symbols. **Note:** registration happens via the package facade `__init__.py`, NOT via
  `tools/__init__.py` (which is currently a docstring-only file with no imports). The earlier draft
  pointed at the wrong file.)*
- `tests/test_mcp_server/test_tools/test_fitness.py` *(new)*

Tools to expose (master plan D6: every meaningful query and operational lever is an MCP tool):

| Tool | Purpose |
|---|---|
| `fitness_list_activities(start, end, activity_type=None)` | Window-listing of activities |
| `fitness_list_daily(start, end)` | Window-listing of daily metrics |
| `fitness_sync_status()` | Per-source sync status snapshot (matches `GET /api/fitness/sync/status` shape) |
| `fitness_integrity_check()` | Run `check_fitness_integrity()` and return the orphan list. Operational tool — exposed via MCP so an external agent can detect drift without polling the REST API. |
| `fitness_trigger_sync(source)` | Submit a `fitness_sync_*` job. Operational tool — D6 says every meaningful operation is also MCP-callable; an external agent that wants to refresh data and then query it should be able to do both via MCP. |
| `fitness_correlate_sleep_mood(start, end)` | Q1 — sleep score × energy/joy (the §8 query) |
| `fitness_correlate_weekly_runs_stress(start, end)` | Q2 — weekly running × stress |
| `fitness_correlate_hrv_mood(start, end, window=7)` | Q3 — rolling HRV × mood |

Each tool is a thin wrapper around the same repository methods and SQL queries documented in
[`fitness-schema.md`](./fitness-schema.md) §8. All return JSON-serialisable dicts/lists.

**Tests:** invoke each tool against a seeded in-memory DB; assert returned shape and basic content.
Use the existing tool-test pattern from `tests/test_mcp_server/`.

**Acceptance criteria:**

- `uv run pytest tests/test_mcp_server/test_tools/test_fitness.py` green.
- The eight tools above are listed in the running MCP server's tool registry (verify by introspecting
  `mcp` after import, the existing pattern other tool-tests use).

---

### W11 — CLI re-auth + first-run flow

**Priority:** High. **Needs creds.** **Files:**

- `src/journal/cli/fitness.py` *(new — `cmd_*` functions in the same shape as `cli/entities.py` and
  `cli/mood.py`)*
- `src/journal/cli/__init__.py` *(modify — register flat argparse subcommands; the existing CLI uses
  argparse, **not Typer**, with flat subcommands like `journal extract-entities`)*
- `tests/test_cli/test_fitness.py` *(new — invoke `cli.main()` with constructed `argv` lists, the
  same shape `tests/test_cli/test_*.py` use today)*

**Subcommands (flat, argparse):**

- `uv run journal fitness-reauth-strava` — prints the authorize URL, runs a one-shot
  `http.server.HTTPServer` on the host/port parsed from `STRAVA_REDIRECT_URI`, blocks until the
  callback fires, extracts the `code` query param, exchanges it for tokens via the W4 provider's
  `exchange_code(code)` helper, persists to `fitness_auth_state` via the W2 repository. Idempotent —
  upserts existing rows. If the user cancels (Ctrl-C before callback), the listener shuts down
  cleanly and no rows are written.
- `uv run journal fitness-reauth-garmin` — reads `GARMIN_USERNAME` / `GARMIN_PASSWORD` from env (or
  prompts via `getpass`), calls `GarminProvider.login(mfa_callback=_stdin_mfa_prompt)`, persists the
  resulting token blob into `fitness_auth_state.extra_state_json`. The `_stdin_mfa_prompt` reads a
  6-digit code from stdin via `input()` so a normal terminal can drive it.
- `uv run journal fitness-sync [--source strava|garmin|both] [--since YYYY-MM-DD]` — submits a
  `fitness_sync_*` job through the existing `JobRunner` (consistency with every other CLI
  command — `extract-entities`, `backfill-mood` all submit via `JobRunner`). Synchronous "wait for
  the job to finish" flag (`--wait`) is optional polish.
- `uv run journal fitness-status` — prints the per-source status table the webapp will render
  (last_success_at, auth_status, last 10 sync runs in a tabulated form).

**Bootstrap sequencing — important.** The §1 P0.1 manual OAuth flow is *not* a substitute for this CLI
command. The first time we have a working bootstrap is when `fitness-reauth-strava` and
`fitness-reauth-garmin` ship. Until then, `fitness_auth_state` rows can only be inserted via tests or
direct SQL. **Every checkpoint that mentions "live smoke test" implicitly depends on W11 having shipped
first.** Listed under W6 already; restated here for the operator.

**Tests:**

1. **Strava OAuth — happy path.** Patch `http.server.HTTPServer` with a stand-in that synthesises a
   GET request with `?code=TEST_CODE` to the registered handler, assert the handler shuts the server
   down, calls the provider's `exchange_code` once with `TEST_CODE`, and the W2 repository
   `upsert_auth_state` was invoked with the resulting tokens.
2. **Strava OAuth — user cancellation.** Send `KeyboardInterrupt` mid-wait; assert no DB write
   occurred and the process exits cleanly.
3. **Garmin login — non-MFA happy path.** Stub `GarminProvider.login` to succeed without invoking the
   MFA callback; assert tokens persisted.
4. **Garmin login — MFA happy path.** Stub `GarminProvider.login` to invoke the MFA callback once;
   patch `input` to return `'123456'`; assert that string was passed back to the callback.
5. **`fitness-sync --source both`** submits two jobs (one per source) and prints both job ids.
6. **`fitness-status` empty DB.** With no `fitness_auth_state` rows, the command prints a message
   indicating no fitness sources are configured and exits 0 (not an error).

**Acceptance criteria:**

- `uv run journal fitness-reauth-strava --help` etc. each show sensible help text.
- After `fitness-reauth-strava` against a real account (P0.1 done), a subsequent `fitness-sync
  --source strava` fetches one batch of activities into raw and normalized tables.
- After `fitness-reauth-garmin` against a real account (P0.2 done), a subsequent `fitness-sync
  --source garmin` populates `fitness_daily` for at least the previous day.
- All four commands wired into `cli/__init__.py`'s argparse subparsers list, parallel to existing
  ones like `extract-entities`, `backfill-mood`, `seed`.

---

### W12 — Health endpoint extension

**Priority:** Medium. **Files:**

- `src/journal/api/health.py` *(modify — extend the existing payload)*
- `src/journal/services/liveness.py` *(modify — add `check_fitness_freshness` if it fits the
  existing pattern; otherwise inline)*
- `tests/test_api/test_health.py` *(modify)*

**Wiring note.** `FitnessRepository` reaches the health handler via the existing services-getter
pattern: register it in `mcp_server/bootstrap.py`'s `_init_services` (alongside `entry_repo`, etc.),
expose via `services.get("fitness_repo")` from inside `health.py`. No new threading required.

**Add to `/health` and `/api/health`:**

```json
{
  "fitness": {
    "strava": {
      "auth_status": "ok",
      "last_success_at": "2026-05-09T04:01:23Z",
      "auth_broken_since": null
    },
    "garmin": { "auth_status": "broken", "last_success_at": "2026-04-30T04:01:14Z",
                "auth_broken_since": "2026-05-01T04:01:09Z" }
  }
}
```

Health *status* (`overall_status`) downgrades to `degraded` if any source has been broken for
>48h. Tunable via config; default 48h (long enough for Garmin's typical SSO-incident weekend break,
short enough to surface a real outage).

**Tests:**

1. Empty DB (no `fitness_auth_state`) → fitness section omitted, overall status unchanged.
2. One source `ok`, one source `broken` recently → fitness present, overall status not yet
   `degraded`.
3. One source broken for >48h → overall status drops to `degraded`.

**Acceptance criteria:**

- `uv run pytest tests/test_api/test_health.py` green.
- `/health` (unauthenticated) and `/api/health` (authenticated) both surface the new payload.

---

### W13 — Backfill from 2026-01-01 + first live smoke test

**Priority:** Medium. **Needs creds.** **Files:**

- `src/journal/services/fitness/backfill.py` *(new)*
- `src/journal/cli/fitness.py` *(modify — add `backfill` subcommand)*
- `tests/test_services/test_fitness/test_backfill.py` *(new)*
- `journal/<YYMMDD>-fitness-first-fetch.md` *(new — smoke-test journal entry)*

**Behaviour:**

`backfill_strava(*, user_id, start='2026-01-01', end=today)` paginates through Strava's
`get_activities(after=..., before=...)` in 30-day windows, writes raw rows, normalizes incrementally.

**Resume predicate (per source).** If interrupted, re-running picks up from
`(SELECT MAX(local_date) FROM fitness_activities WHERE user_id=:uid AND source='strava')` for the
Strava backfill, and the analogous `source='garmin'` filter for the Garmin backfill, plus
`fitness_daily WHERE source='garmin'` for the daily endpoints. The earlier draft used
source-agnostic `MAX(local_date)`, which would silently skip Garmin days if Strava had progressed
further. **Per-source filtering is the load-bearing detail.**

**Tests** (all executable unit tests, not manual smoke runs):

1. **Resumable mid-window crash.** Inject a `raise RuntimeError` after the 5th window via a
   side-effect callable on the fake provider. Run `backfill_strava`; expect it to fail. Snapshot
   `fitness_activities` row count → N1. Remove the side effect, re-run; expect success. Final row
   count is the total for the window. Verify no duplicate `(user_id, source, source_id)` rows.
2. **Empty start window** (fake provider returns empty for the requested `after`/`before`):
   completes in zero pagination rounds.
3. **Rate-limit retry.** Fake provider raises `stravalib.exc.RateLimitExceeded` once on the second
   page, then succeeds on retry. Test depends on the `is_transient` extension shipped in W6/W8 — if
   that extension hasn't landed, this test will fail because the error is mis-classified as
   permanent. Document the dependency in the test docstring.

**Smoke test (manual, with creds):**

1. Run `uv run journal fitness backfill --source strava --start 2026-01-01`.
2. `SELECT COUNT(*) FROM fitness_activities WHERE source='strava'` matches the user's Strava count
   for the year.
3. Run `uv run journal fitness backfill --source garmin --start 2026-01-01`.
4. `SELECT COUNT(*) FROM fitness_daily WHERE source='garmin'` ≈ days since 2026-01-01.
5. Spot-check three random rows against the Strava/Garmin web UI.
6. Capture observations (any drift events, any rate-limit hits, any unexpected schema fields) in the
   journal entry.

**Acceptance criteria:**

- The smoke test runs to completion without errors AFTER both creds are in place.
- The journal entry documents what worked and what surprised us.

---

### W14 — Documentation + roadmap index update + journal entry

**Priority:** Medium. **Files:**

- `docs/architecture.md` *(modify — add a "Fitness pipeline" section describing the four layers and
  pointing at the schema doc)*
- `docs/external-services.md` *(modify — add Strava and Garmin entries with rate-limit, auth, and
  reliability notes)*
- `docs/jobs.md` *(modify — add `fitness_sync_*` job types)*
- `docs/api.md` *(modify — add the W9 endpoints)*
- `docs/configuration.md` *(modify — document new env vars)*
- `docs/roadmap.md` *(modify — flip Tier 1 #1 from "implementation has not yet started" to
  "in progress" with a link to this doc; on completion of all work units, mark closed and archive
  this doc per the documentation-lifecycle rules)*
- `journal/<YYMMDD>-fitness-implementation-summary.md` *(new — wraps W1–W13)*

**Per-doc content requirements** (avoid stub-quality updates — the global rule says "no stubs"):

- `architecture.md` — "Fitness pipeline" subsection: 4-layer diagram (fetch → raw → normalize →
  integrate), pointer to schema doc, pointer to this tier plan.
- `external-services.md` — Strava entry (rate limits, OAuth flow, app-registration steps, API
  Agreement note) and Garmin entry (no app registration, MFA flow, reliability caveats with the
  March 2026 incident, version-pinning policy).
- `jobs.md` — `fitness_sync_strava` and `fitness_sync_garmin` job types: params shape
  (`FITNESS_SYNC_KEYS`), success/failure semantics, drift handling.
- `api.md` — all five W9 endpoints with method, path, auth requirement, query params, and the
  exact response JSON shapes from W9. This is the API-shape contract W15 (webapp) will build against.
- `configuration.md` — every new env var from W3 with a one-line description and default.
- `roadmap.md` — flip Tier 1 #1 to "in progress" with a link to this tier plan; on completion of
  all work units, mark the entry closed and `git mv` this doc into `docs/archive/` per the
  documentation-lifecycle rules in the parent `CLAUDE.md`.

**Acceptance criteria:**

- All affected docs link bidirectionally where it makes sense.
- `roadmap.md` references this tier plan during execution; archive on completion.
- The journal entry records what shipped, what surprised us, deferred items, and the resolved
  versions of `stravalib` and `garminconnect` (the W2 / W4 / W5 commit messages already capture
  these — the journal entry consolidates).

---

### W15 — Webapp sync-status panel + auth-broken banner *(webapp repo, separate commit)*

**Priority:** Low. **Lives in `webapp/`, not `server/`.** Listed here for completeness, not driven by
this plan beyond an API-shape contract. Adds:

- A "Fitness" sidebar entry.
- A status panel showing per-source `last_success_at`, `auth_status`, last 10 sync runs.
- A persistent banner when any source is `broken` with a "Re-auth" CTA pointing at the CLI
  command (or, later, an in-app re-auth flow).

Owner: webapp side, future session.

---

## 5. Out of scope (this document)

- The official Garmin Health API (master plan §2 — not available).
- `.fit` file ingestion.
- Real-time / sub-daily polling.
- Cross-source activity dedup (a Strava run uploaded from a Garmin watch will appear in both raw
  tables — dedup is an integrate-layer concern, future).
- Encryption-at-rest for `fitness_auth_state.access_token` (handled at SQLite layer if needed —
  master plan Q2).
- Daily scheduler (`cron`/APScheduler trigger). The pipeline runs via job submission today; adding a
  scheduled trigger is a small future work unit on top of W8.
- ChromaDB embeddings of activities (no semantic search planned for fitness).
- Webapp implementation (W15 — separate repo, separate commit).

---

## 6. Stopping points & checkpoints

If the session runs out of budget mid-execution, these are good stopping points where the system is
**still in a coherent state**:

1. **After W1.** Schema lands, no other code depends on it yet — safe to merge as a no-op user-facing
   change.
2. **After W3.** Foundation complete; safe to merge — adds env-var slots and notification topics
   that simply default off without creds.
3. **After W7.** Pipeline is functionally complete in dry mode — fetch and normalize can be
   exercised directly from a Python REPL using the W6 `FetchService` and W7 normalize functions
   with fake providers and an in-memory DB. The worker wiring (W8) is not yet shipped at this
   point. Useful as a coherent end-of-day milestone if W8/W9/W10 don't fit.
4. **After W10.** Internal API surface is complete — only operational/CLI/UX wiring remains. This
   is the last "fully tested without creds" milestone.
5. **After W12.** Server-side feature-complete except for the live smoke test (W13). Useful merge
   if creds aren't available.
6. **After W14.** Done.

Per `<commit-pattern>`: each work unit is one commit (or one cluster of related commits if a unit
naturally splits — e.g. W14's six file edits are one commit). Push after each unit. Watch CI green
before starting the next.

---

## 7. Kill criteria (reference)

The master plan ([`fitness-integration-plan.md`](./fitness-integration-plan.md) §7) holds the
cross-cutting kill criteria. This document does not duplicate them — review them at any natural
checkpoint.
