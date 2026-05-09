# 2026-05-09 — Fitness W11: CLI re-auth + first-run flow

W11 from `docs/fitness-tier-plan.md`. Adds the four flat argparse subcommands
in `cli/fitness.py` (`fitness-reauth-strava`, `fitness-reauth-garmin`,
`fitness-sync`, `fitness-status`) that close the bootstrap gap — until W11,
`fitness_auth_state` rows could only be inserted via tests or direct SQL,
which meant every REST/MCP `trigger_sync` call recorded `auth_broken` with
`error_class="MissingAuthState"`. With W11 shipped the operator can run
through the OAuth flow once and the W6 fetch service has a live row to
read.

## What shipped

1. **`src/journal/cli/fitness.py`** *(new)* — four `cmd_*` functions plus
   the `_oauth_listener` helper, the Strava authorize-URL builder, the
   `_stdin_mfa_prompt` callback, and per-source `_provider_factory`
   closures matching the `bootstrap.py` read-then-merge persist semantics
   for in-flight token refreshes. ~470 lines including module docstring
   and inline rationale comments.
2. **`src/journal/cli/__init__.py`** — registered the four subcommands
   in the existing flat-argparse subparsers list, parallel to
   `extract-entities` / `backfill-mood`. `--user-id` (default 1, admin),
   `--source {strava,garmin,both}`, `--since YYYY-MM-DD` flags wired
   per the plan.
3. **`src/journal/providers/strava.py`** — added a free function
   `exchange_code(*, client_id, client_secret, code) -> Tokens`. Wraps
   `stravalib.Client.exchange_code_for_token`, converts the epoch
   `expires_at` into the ISO 8601 UTC string the rest of the codebase
   expects, returns the same `Tokens` `TypedDict` the `persist_tokens`
   callbacks consume.
4. **`tests/test_cli_fitness.py`** *(new)* — 14 tests:
   - 4 parametrised help-text smoke tests (one per subcommand).
   - 3 Strava OAuth tests: happy path, `KeyboardInterrupt` cancellation
     (no DB write, exit 130), and `extra_state`-preserving re-auth on a
     pre-existing `auth_broken` row.
   - 2 Garmin login tests: non-MFA happy path and MFA happy path with
     stdin `input()` returning `123456`.
   - 3 `fitness-sync` dispatcher tests: `--source both` runs both,
     default is `both`, `--source strava` runs only Strava — all via a
     patched `_run_one_source_sync` seam so we don't stand up real
     fetch services.
   - 2 `fitness-status` tests: empty DB friendly message, configured
     sources show auth_status + last-runs.

Total: 2090 passing (2076 prior + 14 W11). Lint clean.

## Plan corrections (W11 plan was four-in-a-row drifted)

W8/W9/W10 plans each named at least one wrong file path; W11 made it
four. Recording the corrections here so the section can be updated
inline.

1. **Test path: `tests/test_cli/test_fitness.py` → `tests/test_cli_fitness.py`.**
   The repo's CLI test convention is a single flat `tests/test_cli.py`
   plus topical flat-files like `tests/test_api_fitness.py`. There is
   no `tests/test_cli/` directory. The new file follows the
   `test_api_fitness.py` precedent.
2. **JobRunner usage: `fitness-sync` runs inline, not via JobRunner.**
   The plan claimed "consistency with every other CLI command —
   `extract-entities`, `backfill-mood` all submit via `JobRunner`."
   Both of those actually call services synchronously with no
   JobRunner construction (see `cli/entities.py` and `cli/mood.py`).
   `fitness-sync` mirrors that shape — build `FitnessRepository`, call
   `StravaFetchService.run_sync` then `normalize_strava` (or the
   Garmin pair), print the result. The long-running server still
   routes its scheduled / on-demand syncs through JobRunner via REST
   (W9) and MCP (W10) — that path is unchanged. The
   `fitness_sync_runs` row is recorded by the fetch service whether
   or not JobRunner is involved.
3. **`StravalibStravaProvider.exchange_code(code)` did not exist.**
   The W4 provider exposes `list_activities`,
   `get_activity_detail`, and `refresh_token_if_needed` — no initial
   code-exchange helper. Added `exchange_code` as a free function in
   `providers/strava.py`. Free function rather than a provider method
   because exchange-code happens *before* a provider can be
   constructed (no access token yet); pinning it to the provider
   class would force an awkward sentinel-value constructor call.
4. **`GarminProvider.login(*, mfa_callback=...)` is correct as written.**
   No drift on this one.

## Decision log

1. **No JobRunner in CLI.** See plan correction #2. Pragmatic
   tradeoff: the alternative is constructing a CLI-scoped
   `ThreadPoolExecutor` and shutting it down on exit. The user-feedback
   memory ("Always shutdown ThreadPoolExecutors in test fixture
   teardown — missed shutdown causes CI segfault") is the canary —
   we already had to learn that lesson once. Keeping the CLI
   single-threaded sidesteps the failure mode entirely. Trade: no
   `jobs` table row for CLI-driven syncs. Accept: the `fitness_sync_runs`
   row is the durable audit, the operator sees the result on stdout,
   and `--source both` covers the multi-source case via a Python loop
   not a parallel job submission.
2. **Re-auth persist semantics differ from bootstrap-time refresh persist.**
   Bootstrap's `_persist` closures preserve `auth_status` and
   `auth_broken_since` because those fields are managed by the fetch
   service (D5 alerting taxonomy). The CLI re-auth, by contrast, is
   the operator declaring "I just fixed the auth interactively" — so
   it explicitly sets `auth_status="ok"`, clears `auth_broken_since`,
   and stamps `last_successful_login_at=now`. `extra_state`,
   `last_refresh_at`, and `created_at` are still read-then-merged
   from the existing row to preserve fetch-service-owned fields.
3. **`_NoopFitnessNotifier` for CLI runs.** A short-lived CLI
   invocation should not fan out Pushover alerts — the operator is
   already watching the terminal. The fetch service still records
   `fitness_sync_runs` rows on every code path; only the alert
   side-channel is muted. `services/notifications.py`'s
   `PushoverNotificationService` is structurally identical and
   remains in use for the long-running server.
4. **`_oauth_listener` is the test seam, not the lower-level handler.**
   The plan sketch (test #1) suggested patching `http.server.HTTPServer`
   with a stand-in that synthesises a GET request. We landed on the
   simpler approach of patching `_oauth_listener` itself with a
   return value or a `KeyboardInterrupt` side-effect. The handler
   class is exercised in production and the listener function's
   contract is just "return the captured code or raise" — patching
   at that boundary tests the cmd's branching logic without the
   ceremony of a fake `HTTPServer`.
5. **`--user-id` flag default 1.** The CLI is operator-driven; there's
   no request context. Defaulting to admin (`user_id=1`) matches
   how `migrate-chromadb` does it and means the common case ("the
   operator is also the only fitness user") needs no flag.
6. **`fitness-status` empty-DB message returns exit 0.** A user with
   no configured fitness sources is a valid state, not an error —
   the message guides them to the re-auth commands. Same posture as
   `extract-entities` reporting "No entries matched the filter —
   nothing to extract."

## What's deferred

- **No `--wait` flag for `fitness-sync`.** The plan listed this as
  "optional polish." Since the CLI runs syncs inline, every invocation
  already waits — the flag would be vestigial. If a future change
  reintroduces JobRunner for CLI, this can come back.
- **No live HTTP test for `_oauth_listener`.** The plan called for a
  synthetic GET against the registered handler; we landed on the
  simpler approach above. The handler class itself is small (~25
  lines) and is exercised at runtime when an operator runs the
  command for the first time. W13 (first live smoke) will be the
  end-to-end validation.
- **No `fitness backfill --start` subcommand.** That's W13's job.
- **No webapp surface.** That's W15.

## Files

- `src/journal/cli/fitness.py` — new
- `src/journal/cli/__init__.py` — modified (4 subparsers + dispatcher entries)
- `src/journal/providers/strava.py` — modified (added `exchange_code`)
- `tests/test_cli_fitness.py` — new (14 tests)
- `docs/fitness-tier-plan.md` §W11 — annotated with corrections (see below)

## Next

W12 (health endpoint extension): surface per-source `auth_status`,
`last_success_at`, `auth_broken_since` from `/health` and `/api/health`,
downgrade `overall_status` to `degraded` when a source has been broken
for >48h. Files are in different layers (`api/health.py`,
`services/liveness.py`) so this is a fresh-session candidate.
