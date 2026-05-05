# 2026-05-05 — Mood dimensions: `[meta]` block + version surfacing

The mood-dimensions config now carries operator-managed metadata (a `version`
date and an optional `description`) in a top-level `[meta]` table. The webapp
surfaces both on its new admin **Moods** tab so operators can see at a glance
which definitions are live without SSH'ing into the server.

## Versioning convention

Bump `version` to today's date in `YYYY-MM-DD` format whenever the toml is
edited in a way users would care about (added/removed/renamed a facet, changed
a scoring criterion, changed the order). For multiple edits on the same day,
append `.2`, `.3`, … to disambiguate (e.g. `2026-05-05.2`). Free-form string
on the server side — no parsing, no validation beyond non-emptiness.

## Server surface area

Cross-cutting changes (matched commit on the webapp side):

- `config/mood-dimensions.toml` — new `[meta]` table at the top with
  `version = "2026-05-05"` + a description blurb.
- `src/journal/services/mood_dimensions.py` — new `MoodDimensionsMeta`
  dataclass (frozen) and `load_mood_meta(path)` loader. Unknown keys in
  `[meta]` are silently dropped for forward-compat. Missing `[meta]` block
  returns the default empty meta — callers should treat empty `version` as
  "unknown" rather than as an error.
- `src/journal/mcp_server.py` — also load meta at startup, stash in services
  dict as `mood_dimensions_meta`.
- `src/journal/services/reload.py` — also reload meta on the `reload-mood-
  dimensions` admin endpoint, return version in the reload response, and
  refresh `services["mood_dimensions"]` (which had been a latent staleness
  bug — the API endpoint reads from the services dict but the reload only
  rebuilt the scoring service's internal copy).
- `src/journal/api.py` — extend `/api/dashboard/mood-dimensions` response to
  include a `meta: { version, description }` object alongside the existing
  `dimensions` array. Empty strings when scoring is disabled.

Tests cover the loader (5 cases including forward-compat for unknown keys
and whitespace stripping), and the API endpoint (presence of meta, empty
meta when scoring is off).

## What stays at the CLI

The Moods admin tab is **read-only** — no UI for editing the toml. Changing
a facet's `notes` triggers the LLM-rebackfill workflow (`journal backfill-
mood --force`), which lives at the CLI by design. Adding the same flow to
the admin UI is a larger feature worth its own session.

## Result

```
1593 passed, 35 warnings in 31.03s
```

All existing server tests still pass; 5 new tests for the loader, 1 new
test for the API endpoint shape.
