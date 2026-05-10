# W6 — Drop global Garmin env vars (server)

Date: 2026-05-10
Plan: `docs/fitness-multiuser-plan.md` §5 W6
Branch: `worktree-eng-fitness-w6-drop-garmin-env`

## What shipped

`config.py` no longer carries `garmin_username` / `garmin_password`. The
`GARMIN_USERNAME` and `GARMIN_PASSWORD` env vars are dead — set them in prod
or not, no code path reads them.

The CLI operator-fallback (`journal fitness-reauth-garmin`) now requires
`--username` and reads the password from `getpass()` only. The webapp connect
endpoints (W2, shipped earlier today) are the per-user primary path.

## Open question resolved before coding

The brief flagged "does `cmd_fitness_reauth_garmin` already have a `--username`
flag, or does it only read from config?" — read the code first. It read
`config.garmin_username` with an `input()` fallback and `config.garmin_password`
with a `getpass()` fallback. So this unit had real code work to do, not just
arg-rename work.

While reading the code I also found **two further references** the brief did
not explicitly call out, both load-bearing:

1. `cli/fitness.py:_garmin_provider_factory` (lines 418-419) passed
   `username=config.garmin_username, password=config.garmin_password` to the
   provider for `fitness-sync` / `fitness-backfill`.
2. `mcp_server/bootstrap.py` (lines 174-211) had a `garmin_configured` env-var
   gate that controlled whether the long-running server registered the Garmin
   sync/backfill callables at all, and then passed the same config fields to
   the provider.

Removing the config fields without touching those two would have produced an
`AttributeError` at startup. Both got the same treatment: pass empty strings
to the provider; let `tokens_blob` (from `fitness_auth_state.extra_state`) be
the real credential.

## Decisions

### 1. Always wire the Garmin path in bootstrap.

The pre-W6 `garmin_configured = bool(config.garmin_username and config.garmin_password)`
gate was a proxy for "does this server have any Garmin user?" From W6 forward
that proxy is meaningless — every user can connect their own Garmin via the
webapp, and the server has no idea up front. So I dropped the gate entirely:
Garmin is registered unconditionally. A user without a `fitness_auth_state`
row produces a clean `auth_broken` sync run rather than a 503 — that's the
correct semantic, and matches how the W5 backfill workers already treat
missing auth state.

The bootstrap log message was updated accordingly: only `STRAVA_CLIENT_ID`
controls whether Strava is wired; Garmin is always wired.

### 2. Pass `username=""`, `password=""` to the provider, don't change its signature.

`GarminConnectGarminProvider.__init__` takes `username: str, password: str`
as keyword-only positionals. Could have made them optional with `str = ""`
defaults, but that change rippled through every call site and tests. Passing
empty strings at the two call sites is a one-line edit per site and matches
the provider's existing behavior: when `tokens_blob` is set, the provider
calls `client.client.loads(self._tokens_blob)` and returns before touching
`self._username` / `self._password`. When no `tokens_blob` exists, the
provider falls through to `client.login(tokenstore)` with empty credentials,
fails with `GarminConnectAuthenticationError`, and the fetch service writes
`auth_status='broken'`. That's the correct user-visible behavior for "no
connection yet" — the W11 banner picks it up.

### 3. Operator note in `docs/fitness-operations.md`, not a full doc sweep.

The brief's gotcha was explicit: "W6 should NOT preempt W12. Don't expand
into other docs." I touched `docs/configuration.md` and
`docs/fitness-operations.md` only, and only the sections that *named*
`GARMIN_USERNAME` / `GARMIN_PASSWORD`. The §2c Garmin re-auth section got
a small heading change ("operator fallback") and a `--username` example;
the §6 troubleshooting and env-grep recipe got their references trimmed.
`docs/api.md`, `docs/jobs.md`, and the broader doc structure are W12's job.

### 4. Vestigial env-var flag for the operator.

Per the brief: prod env has a leftover `STRAVA_REFRESH_TOKEN` that no code
reads (predates the `fitness_auth_state` table). I noted both
`GARMIN_USERNAME` / `GARMIN_PASSWORD` and `STRAVA_REFRESH_TOKEN` as safe to
remove on the next prod deploy in the operator note inside
`docs/fitness-operations.md` §1.

## Tests

1. `tests/test_config.py::TestFitnessConfig`:
   - Updated `test_defaults_when_unset` and `test_env_overrides` to drop
     assertions on the removed fields; both now assert `not hasattr(config,
     "garmin_username")` / `garmin_password`.
   - New `test_garmin_env_vars_ignored` — sets both env vars, constructs
     Config, asserts no attributes appear. This is the regression guard
     against accidentally re-adding the fields under the same env names.
2. `tests/test_cli_fitness.py`:
   - Dropped `GARMIN_USERNAME` / `GARMIN_PASSWORD` from the `fitness_env`
     fixture.
   - Updated both Garmin re-auth happy-path tests to pass `--username
     test_user@example.com` and patch `journal.cli.fitness.getpass` to return
     `"test_password"`.
   - Added `test_fitness_reauth_garmin_requires_username` — argparse must
     refuse to run without `--username`.
   - Added `test_fitness_reauth_garmin_no_env_var_fallback` — setting the
     env vars does not satisfy the requirement; argparse still errors and
     stderr names `--username`.

## Local verification

- `uv run ruff check src/ tests/` → All checks passed.
- `uv run pytest tests/test_cli_fitness.py tests/test_config.py::TestFitnessConfig`
  → 30 passed.
- `uv run pytest -m "not integration"` → 2250 passed (was 2247 after W7;
  3 new W6 tests).
- `grep -r GARMIN_USERNAME src/` → no hits in active code (config.py has a
  comment naming the removed fields for grep discoverability).

## Plan-vs-code drift

The plan-text listed only `config.py` and `cli/fitness.py:cmd_fitness_reauth_garmin`
as the code surface. Reality required touching:

- `src/journal/cli/__init__.py` — argparse `--username` flag for
  `fitness-reauth-garmin`.
- `src/journal/cli/fitness.py:_garmin_provider_factory` — drop the config
  reads.
- `src/journal/mcp_server/bootstrap.py` — drop the `garmin_configured` env
  gate and the username/password passthrough; update the wired-services log.
- `src/journal/api/ingestion.py` — one-paragraph docstring update on the
  `POST /api/fitness/sync/{source}` route to remove the GARMIN-env mention.

Each of those touches is the *minimum* needed to keep the build compiling
once the config fields disappear. Drift recorded so a future reader can map
the broader change to the plan's narrower description.

The plan's Code-Surface table at §4 already lists `cli/fitness.py` for the
Garmin-cred CLI work and `config.py` for the field removal, which is
correct as far as it goes — but it doesn't enumerate the bootstrap /
provider-factory ripple. Worth noting in W12 if the table gets a refresh.
