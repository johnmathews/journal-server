# W4 — Per-user integrity check (server)

Date: 2026-05-10
Plan: `docs/fitness-multiuser-plan.md` §5 W4
Branch: `worktree-eng-fitness-w4-user-integrity`

## What shipped

`GET /api/fitness/integrity` and the `fitness_integrity_check` MCP tool now scope
the orphan report to the calling user. The `check_fitness_integrity` function
gains a required `user_id: int` keyword argument and threads it through all three
of its queries.

## Open question resolved before coding

The brief flagged "does the integrity endpoint *currently* return a global view
or is it already user-scoped?" — I read `api/fitness.py` first. The endpoint was
explicitly scope-less, with a six-line comment noting "the integrity check itself
is global … there is no per-user filter on raw orphans, so an un-authed read
would leak the existence of orphans across tenants." The W11-style "verify and
add tests only" collapse did not apply; the unit needed real code changes.

The plan-text said "Add user_id to `db.fitness_integrity` (or wherever the orphan
query lives)." The query did live in `db/fitness_integrity.py`, no relocation
needed.

## Decisions

### 1. Scope the inner `fitness_raw_garmin` lookup by user_id too.

The daily-rollup path expands `raw_ref_ids_json` into placeholders and queries
`SELECT id FROM fitness_raw_garmin WHERE id IN (…)`. Without a user_id filter
on this inner query, a Garmin raw row owned by user B with `id=N` would silently
satisfy user A's daily soft pointer to `id=N` — a cross-user join is data
corruption, not a valid resolution.

Added `WHERE user_id = ? AND id IN (…)` to the inner query and a regression test
(`test_cross_user_garmin_raw_does_not_satisfy_daily_pointer`).

### 2. Add `AND r.user_id = fa.user_id` to the activity-side joins.

The same concern applies to the scalar `raw_ref_id` joins: a raw row owned by
user B at id=N would satisfy user A's activity-side LEFT JOIN, making the orphan
disappear. The fix is symmetric to (1) — add `r.user_id = fa.user_id` to each
ON clause. Caught by `test_cross_user_raw_row_does_not_satisfy_soft_pointer`.

Neither correctness gap was *new* with W4 — they existed in the single-user
posture too, but were latent because every row carried `user_id=1`. W4 is the
right moment to fix them because the multi-user surface is what makes them
exploitable.

### 3. Removed the stale comment in `api/fitness.py:integrity`.

The previous comment said "Auth required even though the integrity check itself
is global — there is no per-user filter on raw orphans". With W4 that's false,
so I replaced it with a one-paragraph note describing the per-user scoping
(matches the cadence on other endpoints in the file).

## Tests added

1. `test_orphans_are_user_scoped` — seeds dangling pointers under both user 1
   and user 2, asserts each report contains only the calling user's orphans.
   This is the explicit W4 acceptance criterion.
2. `test_cross_user_raw_row_does_not_satisfy_soft_pointer` — Alice's activity
   points at a raw_strava row owned by Bob; orphan must still be reported.
3. `test_cross_user_garmin_raw_does_not_satisfy_daily_pointer` — same shape
   for the JSON-array daily path.

The existing seven tests in `tests/test_db/test_fitness_integrity.py` were
updated mechanically (`check_fitness_integrity(db_conn)` → `check_fitness_integrity(db_conn, user_id=1)`)
and the seed helpers gained optional `user_id` kwargs (default 1) to let the
new tests reuse them.

The endpoint and MCP-tool tests (`test_api_fitness.py`, `test_mcp_tools_fitness.py`)
needed no changes — they already authenticate as `_TEST_USER_ID`, so the new
scoping is transparent.

## Spanning posture inherited from W5

The W5 "spanning idempotency" pattern doesn't apply here — integrity is a
read-only check, not a fetch/normalize job. Mentioning it for the record:
W5's posture is that fetch workers re-read auth state at each step; W4 is
pure-read with no state changes, so the only invariant is "user A never sees
user B's rows."

## Local verification

- `uv run ruff check src/ tests/` → All checks passed.
- `uv run pytest -m "not integration" -q` → 2242 passed (3 new tests on top
  of the 2239 baseline).

## Plan-vs-code drift

None. The plan called for adding `user_id` to the function signature, the
endpoint, and the MCP tool; that is exactly what shipped. The two extra
correctness-tightening edits (inner-query filter, activity-side ON clauses)
are within W4's spirit — "no global orphan list ever returned to a non-admin
user" is the acceptance criterion, and a cross-user FK accident would
silently violate it.
