# 260510 — fitness multi-user W1: pre-flight data audit

First work unit from `docs/fitness-multiuser-plan.md`. Adds a CLI subcommand
`journal fitness-audit` that walks every fitness table and asserts every row
has a non-NULL `user_id` matching a real user in `users`. Used as the
pre-flight check before W2/W3 ship and as the verification gate at W14.

## Why a CLI subcommand (not an integration test)

The plan offered both shapes — a `tests/integration/test_fitness_data_isolation.py`
or a `journal fitness-audit` CLI subcommand. The CLI shape won because:

1. The acceptance criterion is "run against a copy of the prod DB" — that's
   naturally a one-shot CLI invocation, not a pytest fixture.
2. W14 needs to re-run the same check post-rollout to confirm the row-count
   snapshot still holds. A CLI command is repeatable; an integration test
   couples to the test harness.
3. The unit-test surface (the five new tests in `tests/test_cli_fitness.py`)
   exercises the same audit logic end-to-end through `main()`, so the test
   coverage is comparable either way — no value lost.

## What it audits

Six fitness tables: `fitness_auth_state`, `fitness_sync_runs`,
`fitness_activities`, `fitness_daily`, `fitness_raw_strava`,
`fitness_raw_garmin`. For each, the audit reports:

- Total row count (used as the W14 baseline regression target).
- Per-user breakdown via `LEFT JOIN users ON users.id = t.user_id`, so each
  line shows `user_id=N (email@…)` — operator can read the output without a
  separate DB lookup.
- Violations: rows with `user_id IS NULL` or `user_id` pointing at a
  non-existent user (FK orphan, possible if foreign-keys enforcement was off
  during a delete or if a row was inserted via raw SQL).

Exit 0 on a clean audit, exit 1 if any violations are reported. The
`PASS`/`FAIL` line at the bottom is greppable.

## Output shape

Plain text, two-column. Example for a clean DB with two users:

```
fitness data audit (db: /Users/john/projects/journal/server/journal.db)
============================================================

[fitness_auth_state] rows=2
  user_id=1 (mthwsjc@gmail.com)                                rows=1
  user_id=2 (mthwsjc+demo@gmail.com)                           rows=1

[fitness_sync_runs] rows=12
  user_id=1 (mthwsjc@gmail.com)                                rows=12

...

============================================================
violations: 0
result: PASS
```

A violation line names the table, the bad `user_id`, and what's wrong:
`fitness_auth_state: 1 row(s) with orphan user_id=999 (referenced user no
longer exists)`.

## Tests

Five new tests in `tests/test_cli_fitness.py`:

1. `test_fitness_subcommand_help[fitness-audit]` (parametrized) — `--help`
   exits 0.
2. `test_fitness_audit_clean_empty_db_exits_zero` — migrated DB with no
   fitness rows reports zero rows + `PASS` + violation count of 0.
3. `test_fitness_audit_with_valid_rows_groups_per_user` — two users with one
   `fitness_auth_state` row each; verify both emails appear in the per-user
   breakdown.
4. `test_fitness_audit_orphan_user_id_fails` — insert a `fitness_auth_state`
   row with `user_id=999` (no such user) under `PRAGMA foreign_keys=OFF`;
   expect exit 1 + the orphan id named in the output.
5. `test_fitness_audit_null_user_id_fails` — rebuild `fitness_sync_runs`
   without the `NOT NULL` on `user_id` (SQLite can't `ALTER TABLE` to drop
   `NOT NULL`, so we use the rebuild-and-rename trick), insert a row with
   `user_id=NULL`, expect exit 1 + a `NULL` violation line.

The NULL test is defense-in-depth — the schema enforces `NOT NULL` so this
path is structurally unreachable in normal use. It exists because the plan's
acceptance criterion explicitly says "non-NULL", and a future schema change
or a raw-SQL operator action could re-introduce a NULL. Cheap to test, cheap
to keep.

## Local DB run

Ran against `/Users/john/projects/journal/server/journal.db` — the local
file is currently empty fitness-wise (looks like a clean dev DB rather than
a prod copy). All six tables show `rows=0`, violations: 0, result: PASS.
The plan's §2 reports 80+80 activities + 129 daily rows in actual prod for
user 1, so a real prod-DB copy will produce a more interesting baseline
than this dev-box run.

**Operator follow-up:** before W2 ships, pull a fresh prod-DB copy and
re-run `journal fitness-audit` against it. Capture the row counts as the
W14 baseline and confirm violations: 0. The dev-box clean run captured
above only proves the audit *works*, not that prod is clean.

## Files touched

- `src/journal/cli/fitness.py` — `cmd_fitness_audit` + `_FITNESS_TABLES`
  tuple of the six table names.
- `src/journal/cli/__init__.py` — import, subparser registration, dispatch.
- `tests/test_cli_fitness.py` — five new tests (one parametrized into the
  existing help-text test).
- `docs/fitness-operations.md` — new `### journal fitness-audit` section
  under §5 "Status, health, and integrity".

## Acceptance check vs the plan

> Add `tests/integration/test_fitness_data_isolation.py` (or a CLI
> subcommand `journal fitness-audit`) that asserts every
> `fitness_auth_state`, `fitness_sync_runs`, `fitness_activities`,
> `fitness_daily`, and `fitness_raw_*` row has a non-NULL `user_id`
> matching a user in `users`.

CLI shape, six tables (the two `fitness_raw_*` plus the four named).
NULL + orphan both reported. ✔

> Run against a copy of the prod DB and confirm clean.

Ran against the local `journal.db` (clean). Operator needs to repeat
against a fresh prod-DB copy to capture the actual baseline — noted
above and in the doc.

> Snapshot the row counts as the baseline regression target for W14.

Audit prints per-table row counts. W14's "re-run the audit script" step
will get the same shape and can be diff'd against the W1 capture by eye
or by `grep -E 'rows='`.

## What's next

W2 (Garmin pending-session store + connect/MFA endpoints) and W3 (Strava
OAuth endpoints) are independent and can ship in either order. W2 carries
more spec changes from the lead-engineer review (the `return_on_mfa` /
`resume_login` rewrite, user-bound pending sessions, D8 upstream-id
capture, per-email cool-down) and will be the higher-density unit.

Recommend starting fresh-session for W2 — different module surface
(`api/fitness.py`, `services/fitness/garmin_pending.py`, both new), the
context I have loaded for the CLI audit isn't load-bearing for W2.
