# W7 — CLI `--user-id` required (server)

Date: 2026-05-10
Plan: `docs/fitness-multiuser-plan.md` §5 W7
Branch: `worktree-eng-fitness-w7-cli-user-id-required`

## What shipped

Every `fitness-*` subcommand now requires `--user-id`. argparse exits 2 with
`error: the following arguments are required: --user-id` if the flag is
omitted. The implicit "default to user 1" behaviour is gone.

## Open question resolved before coding

The brief flagged "are the CLI subcommands defined in Typer (decorators) or
argparse?" — read `cli/__init__.py` first. The fitness commands are added via
`subparsers.add_parser(...)` with `.add_argument("--user-id", type=int, default=1,
...)`. argparse, not Typer. The fix is mechanical: swap `default=1` for
`required=True` on each of the five subcommands.

`fitness-audit` does not take `--user-id` (it audits all users) — explicitly
excluded from both the code change and the new parameterized test.

## Changes

1. `src/journal/cli/__init__.py` — five subparsers updated:
   `fitness-reauth-strava`, `fitness-reauth-garmin`, `fitness-sync`,
   `fitness-backfill`, `fitness-status`. Each `--user-id` argument switched
   from `default=1, help="...(default: 1 = admin)"` to `required=True,
   help="...(required — no default)."`.
2. `src/journal/cli/fitness.py` — dropped the dead `_DEFAULT_USER_ID = 1`
   module constant. A repo-wide grep confirms no other references.
3. `tests/test_cli_fitness.py` — 10 existing `sys.argv` invocations updated
   to include `--user-id 1`. New parametrized test
   `test_fitness_subcommand_requires_user_id` covers all five subcommands
   and asserts they exit non-zero with `--user-id` in stderr when the flag
   is omitted. `fitness-audit` excluded.
4. `docs/fitness-operations.md` — added a single-blockquote note at the top
   of §2 stating that `--user-id` is required and there is no implicit
   default. All existing CLI examples already showed `--user-id 1`, so the
   rest of the doc needed no edits.

## Decisions

### 1. argparse `required=True` over a manual sentinel check.

Could have left `default=None` and surfaced the error in each `cmd_fitness_*`
function. Rejected — argparse's built-in handling produces a clean
`error: the following arguments are required: --user-id` and exits 2 before
the command function runs. Less code, identical UX, and matches the rest of
the CLI (no other subcommand does manual flag validation).

### 2. Did not touch the help text on the broader subparser block.

The `fitness-reauth-garmin` subparser help still mentions
`GARMIN_USERNAME/GARMIN_PASSWORD from env`. That's W6's edit, not W7's — kept
the diff surface scoped.

### 3. One doc note, no example rewrites.

The plan-text says "Update docs/fitness-operations.md examples: every command
shows `--user-id N` with no implicit default." Every example in the doc
already showed `--user-id 1`. Adding the up-front blockquote about the
required-flag behaviour was the minimal honest edit; rewriting examples that
already comply would have been busywork.

## Local verification

- `uv run ruff check src/ tests/` → All checks passed.
- `uv run pytest tests/test_cli_fitness.py` → 24 passed (was 19; 5 new
  parametrized cases).
- `uv run pytest -m "not integration" -q` → 2247 passed (was 2242 after W4).

## Plan-vs-code drift

None. The plan called for argparse `required=True` on all five subcommands,
removal of `_DEFAULT_USER_ID`, an updated operations doc example block, and
tests for the missing-flag exit. All four shipped.
