# 2026-05-09 — Fitness integration: tier plan + foundation phase

A long session that produced the execution plan for the fitness integration, then
shipped the first three of its fifteen work units (the "foundation phase" — schema,
repository, config). It also picked up two unrelated infrastructure improvements
along the way: a fix for the dev-compose ChromaDB healthcheck and a probe-based
auto-skip for the integration test suite when Chroma isn't running.

## What shipped

### Tier plan (commit `96683ef`)

`docs/fitness-tier-plan.md` — execution sequencing for the fitness integration
described by `docs/fitness-integration-plan.md` (decisions) and
`docs/fitness-schema.md` (schema). 15 work units across 5 phases, TDD-ordered,
with files-to-modify, public surfaces, test plans, dependencies, and acceptance
criteria for each. The plan stays narrow: it doesn't relitigate the upstream
decisions, it sequences "what to build, in what order, with what tests."

The draft went through **two parallel review passes** before landing —
`feature-dev:code-explorer` to verify every code-grounded claim against the actual
repo state, and `feature-dev:code-reviewer` to stress-test ordering and TDD
adequacy. Combined they surfaced ~80 findings; the high-impact ones were folded
back into the plan in the same commit:

1. **CLI framework was wrong.** Plan said Typer; project actually uses argparse
   with flat subcommands. W11 rewritten end-to-end. Future-agent footgun avoided.
2. **Routing rule violation.** Plan put `POST /api/fitness/sync/{source}` in
   `api/fitness.py` despite `api/ingestion.py`'s docstring saying *all*
   job-creation routes live there "regardless of URL prefix." Moved it.
3. **Missing JobType / SUCCESS_TOPIC_MAP / JOB_TYPE_LABELS updates.** Plan added
   notification topics but didn't update the routing maps, so success
   notifications would have ignored the user's opt-in default in production.
4. **`is_transient` extension required for fitness errors.** Plan assumed the
   existing function would catch Strava 429 / Garmin transient errors; it only
   recognises Gemini/Anthropic/OpenAI patterns. The W6/W8 work units now own the
   extension explicitly.
5. **Test directory layout.** Plan used `tests/<area>/` paths everywhere; actual
   layout is `tests/test_<area>/`.
6. **MCP tool registration site.** Plan pointed at `mcp_server/tools/__init__.py`;
   actual registration goes in `mcp_server/__init__.py` via side-effect imports.
7. **Strava rate-limit figures.** Plan and the upstream master plan disagreed.
   Looked it up against `developers.strava.com/docs/rate-limits/`: actual is
   200/15min + 2000/day overall, 100/15min + 1000/day for non-upload reads. The
   master plan's "600/30k" was wrong; corrected in the same commit.

There were also ~25 medium-severity findings (resume predicate exact form,
authoritativeness assertion in tests, FK partial-install hazard documentation,
empty-DB API tests, etc.) all folded in. The plan that landed is meaningfully
sturdier than the draft.

### W1 — schema migrations + integrity helper (`1da1ab4`)

Three SQL migrations carrying the schema verbatim from `docs/fitness-schema.md`:

- `0023_fitness_auth_and_sync.sql` — `fitness_auth_state`, `fitness_sync_runs`.
- `0024_fitness_raw.sql` — `fitness_raw_strava`, `fitness_raw_garmin`.
- `0025_fitness_normalized.sql` — `fitness_activities`, `fitness_daily`.

Plus `src/journal/db/fitness_integrity.py` running the soft-pointer integrity
check from §6 of the schema doc (per-source filtering on the join, since the
two raw tables have independent `AUTOINCREMENT` sequences and a source-blind
join would silently match wrong rows).

43 tests covering schema/columns/indexes, every CHECK constraint, UNIQUE
constraints (including the `user_id`-inclusive raw UNIQUE), idempotent re-run
from the pre-fitness baseline, the cross-file partial-install hazard
(0023-only state must be coherent), and the integrity checker against a
deliberately-dirty fixture DB.

### W2 — repository + dataclasses (`74c8b9d`)

`FitnessRepository` (one class, four namespaces — auth state, sync runs, raw
archive, normalized) mirroring the single-file `SQLiteJobRepository` pattern.
Five dataclasses in `models.py` (`FitnessAuthState`, `FitnessSyncRun`,
`FitnessRawRow`, `FitnessActivity`, `FitnessDaily`) plus typed `Literal`s for
the enum columns.

Discipline points the schema doc explicitly called out, all enforced in the
code:

1. `payload_sha256` computed inside `insert_raw`, never by callers.
2. Every `UPDATE` on `fitness_auth_state` sets `updated_at = ?` in the same
   statement (schema §4: app-managed, no SQLite ON UPDATE trigger).
3. `transition_auth` returns `True` iff status actually changed; clears
   `auth_broken_since` on transition to `ok`. Drives D5's fire-once alerting
   and the webapp banner-clear behaviour.
4. `find_running_sync_run` gives the W6 fetch service a single-run guard so
   concurrent token-refresh races (the JobRunner header's hazard list) cannot
   happen for fitness syncs in practice.
5. `max_normalized_fetched_at` is the watermark predicate W7 uses to resume
   normalize from the last point. Both raw and normalized tables write
   `fetched_at` via the same SQLite clock, so the comparison is well-defined
   even after a mid-batch crash.

19 tests against an in-memory migrated DB.

### W3 — config + notification topics + .env.example (`cf428ae`)

Six new `Config` fields (Strava OAuth + Garmin login + transient-failure
threshold + backfill cutoff) with env-var defaults; `__post_init__` extended
to reject a threshold of 0.

Four notification topics appended per D5:

| Key                              | Group   | Default | Notes                          |
| -------------------------------- | ------- | ------- | ------------------------------ |
| `notif_fitness_auth_broken`      | failure | on      | Fire-once on transition        |
| `notif_fitness_sync_failure`     | failure | on      | Fire-after-N (threshold)       |
| `notif_fitness_normalize_drift`  | admin   | on      | Code bug — admin-only          |
| `notif_fitness_sync_success`     | success | off     | Opt-in, mirrors entity_reembed |

`_SUCCESS_TOPIC_MAP` and `_JOB_TYPE_LABELS` extended with both fitness job
types so `notify_job_success` routes the success notification through the
user's preference instead of the always-notify fallback. The reviewer caught
that this would otherwise have ignored the opt-in default in production.

`.env.example` documents all six new vars with one-line comments and pointers
to the credential-acquisition steps in the tier plan.

12 new tests (6 in `test_config.py`, 6 in `test_services/test_notifications.py`).

### Two infrastructure fixes that came up along the way

#### ChromaDB integration test probe (`2b5a7df`)

Plain `uv run pytest` from a cold dev box was producing "1897 passed, 8 errors"
because the integration suite was getting *collected* and attempting Chroma
connections at fixture-setup time when no Chroma was running. Fixed by:

1. New `tests/integration/conftest.py` with a TCP probe at collection time. If
   Chroma is unreachable, every item carrying the `integration` marker gets a
   `pytest.mark.skip` with an actionable reason naming the
   `docker compose -f docker-compose.dev.yml up -d` command.
2. Local default for `CHROMA_PORT` flipped from `8000` to `8401` — the port
   the dev compose actually exposes. CI sets `CHROMA_PORT=8000` explicitly to
   hit its service container, so CI is unaffected. The previous `8000` default
   was a silent footgun that made local integration runs fail even when Chroma
   was up via the dev compose.
3. Server `CLAUDE.md` gains a "Running tests locally" section listing the three
   modes (default unit-only with auto-skip, all-with-Chroma, CI-style
   force-skip) so future agent sessions discover the right command without
   poking around.

After the fix: cold box → "8 skipped" with a clear reason; Chroma up via dev
compose → "1933 passed".

#### Dev compose healthcheck (this `/done` wrap-up)

`docker-compose.dev.yml` had `healthcheck: ["CMD", "curl", "-f", ...]` against
`chromadb/chroma:latest`. The base image has no curl, no wget, no python, no
nc — only bash + standard utils. Container was running but flagged
`unhealthy`. Replaced with `bash -c 'exec 3<>/dev/tcp/localhost/8000'` (bash's
built-in `/dev/tcp` virtual file). Production isn't affected: production uses
a custom `journal-chromadb` image that installs curl on top of the base, per
`Dockerfile.chromadb`. The `/done` skill's "Docker healthcheck validation"
rule caught this exactly as designed.

## Decisions worth remembering

1. **Stop at the foundation milestone (after W3) rather than push through
   W4–W10 with hand-crafted fixtures.** W4–W10 are doable without credentials
   in principle, but the `stravalib`/`garminconnect` Pydantic shapes are
   moving targets without a real account to record from. Better to do W4+
   once the fixtures can be real recordings rather than guesses that'll need
   replacement at W13 anyway. The plan's stopping-point #2 ("Foundation
   complete; safe to merge — adds env-var slots and notification topics that
   simply default off without creds") was the explicit checkpoint we hit.
2. **No-op user-visible posture.** Everything that landed today defaults off
   without credentials. Notification topics are opt-in or fire-on-event.
   `Config` accepts empty creds at construct time and errors at use site
   (matching the existing posture for `anthropic_api_key`). Migrations are
   strict-additive — no rebuilds, no triggers, no cross-table churn.
3. **Engineering-team skill update — worktree convention codified.** The skill
   now documents that worktrees live at `<repo>/.claude/worktrees/<name>/`
   with an `eng-` prefix and a kebab-case descriptive name. This is what the
   `EnterWorktree` tool does anyway, but the convention wasn't written down
   anywhere — making it explicit avoids ad-hoc sibling paths in future
   sessions.

## Numbers

- 5 commits on branch `worktree-eng-fitness-tier-plan`.
- 1933 tests pass (unit + integration). 86% coverage.
- 0 lint errors.
- ~3500 lines added (1184 docs + ~2300 code/tests).
- 3 of 15 work units shipped. Critical-path remaining: W4 → W5 → W6 → W7 → W8.

## What's blocked

W4–W13 need Strava + Garmin credentials before they can land with real
fixtures. Credential acquisition steps are documented at `docs/fitness-tier-plan.md`
§1 P0.1 and P0.2. Once those are in place, W4 picks up the Strava provider
seam, then W5 the Garmin one, etc.
