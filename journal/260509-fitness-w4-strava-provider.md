# W4 — Strava provider (Protocol + stravalib adapter)

**Date:** 2026-05-09. **Plan:** [docs/fitness-tier-plan.md](../docs/fitness-tier-plan.md) §W4.

## What shipped

1. **`src/journal/providers/strava.py`** — `StravaActivitySummary` dataclass, `StravaProvider`
   Protocol, `StravalibStravaProvider` adapter wrapping `stravalib.Client` with an injected
   `persist_tokens: Callable[[Tokens], None]` callback. Zero direct DB or HTTP-library imports
   beyond `stravalib` itself, per acceptance criteria.
2. **Hand-crafted fixture** — `tests/test_providers/fixtures/strava/list_activities_response.json`
   (8 activities: run, trail run, ride, swim, walk, hike, weight training, yoga). Sibling
   `README.md` carries the `FIXTURE SOURCE: hand-crafted; replace at W13` marker since JSON
   has no comment syntax.
3. **`tests/test_providers/test_strava.py`** — 13 tests covering all four scenarios from the
   plan (replay happy path, token refresh + persist callback, metric units sentinel,
   `sport_type` verbatim passthrough) plus a JSON-safety check on `raw_payload`, a
   Protocol-conformance check, the `get_activity_detail` path, and a fail-safe edge case
   on unparseable `token_expires_at`.
4. **`pyproject.toml` / `uv.lock`** — `stravalib==2.4` (resolved within `~=2.2`).

## Decisions worth recording

1. **`Tokens` field names match `fitness_auth_state` columns** (`access_token`,
   `refresh_token`, `token_expires_at`) so the W6 fetch service can wire `persist_tokens`
   directly to a repository upsert without a rename layer. ISO 8601 UTC strings; the
   adapter converts to/from epoch seconds at the `stravalib.Client` boundary.
2. **`token_expires_at` parse failures fail-safe to "expired"**, not crash. The constructor
   passes the parsed value into `stravalib.Client(token_expires=...)`, so a corrupt persisted
   timestamp would otherwise prevent the adapter from booting. Unparseable → epoch 0 → next
   API call triggers `stravalib`'s auto-refresh, same outcome the explicit
   `refresh_token_if_needed` path produces. Caught by `test_refresh_treats_unparseable_expiry_as_expired`
   on first run.
3. **`sport_type` is passed through verbatim** (`Run`, `TrailRun`, `WeightTraining`, etc.).
   Collapsing to the seven `FitnessActivityType` literals (`run`, `ride`, `swim`, `walk`,
   `hike`, `strength`, `other`) is normalize's job (W7), not the provider's. The plan
   called this out and the test parametrises four examples to lock it in.
4. **`raw_payload` uses `model_dump(mode="json")`**, not the original JSON dict. `stravalib`
   parses JSON → Pydantic at the SDK boundary, so the original dict is unrecoverable in the
   production code path; `model_dump(mode="json")` round-trips to a JSON-safe dict suitable
   for the raw-archive table.

## Pinned

- `stravalib==2.4` (built on Pydantic 2, requires Python 3.11+, `pint` + `arrow` transitive).
- `~=2.2` in `pyproject.toml` allows future 2.x patch/minor bumps without manual lock churn.

## What's not done yet

1. **Fixture is hand-crafted** — flagged in the sibling `README.md` and will be replaced at
   W13 (first live smoke test) with a real anonymised response. Tests that fail after that
   swap are real bugs, not flakiness.
2. **OAuth code-exchange (one-time bootstrap)** lives in W11, not here. This adapter only
   handles refresh.
3. **Fetch service / sync runs / DB writes** are W6+. The provider is consumed by, but does
   not contain, that orchestration.

## Tests

- 1946 passed, 0 failed (1933 prior baseline + 13 new).
- Lint clean (ruff).
- `stravalib` API shape verified live before writing the adapter (`SummaryActivity.model_fields`
  inspection): `Duration` is an `int` subclass, `Distance` is a `float` subclass,
  `start_date` is timezone-aware. No timedelta/pint conversion needed at the boundary.
