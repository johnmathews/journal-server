# Mood Rationale & Entity Distribution API

## What changed

Added mood score rationale storage and two new API endpoints to support the webapp's new Insights page.

### DB migration 0014
- Added `rationale TEXT` column to `mood_scores` table. NULL for existing rows.

### Mood scorer changes
- Tool schema now requires a `rationale` field per dimension alongside `value` and `confidence`.
- System prompt instructs Claude to write 1-2 concrete sentences (under 30 words each) explaining the key signal in the entry that drove the score.
- Parser extracts rationale from both the tool_use block and the JSON fallback path.
- `RawMoodScore` dataclass gains `rationale: str | None`.

### Mood trends extended
- `get_mood_trends()` now returns `score_min` and `score_max` per bin (SQL `MIN()`/`MAX()` aggregates). These power variance bands on the Insights page chart showing the spread of individual entry scores within each time bin.

### New endpoints
- `GET /api/dashboard/mood-drilldown` — Per-entry scores for a dimension within a date window. Returns entry_id, entry_date, score, confidence, and rationale.
- `GET /api/dashboard/entity-distribution` — Entity mention counts grouped by entity name, filtered by type and date range. Powers the "What I Write About" doughnut chart.

### Backfill note
Existing entries have `rationale = NULL`. Run `journal backfill-mood --force` after deployment to populate rationales for all entries. New entries scored going forward include rationales automatically.

## Tests
29 new server tests covering repository methods, API endpoints, and scorer rationale parsing.
