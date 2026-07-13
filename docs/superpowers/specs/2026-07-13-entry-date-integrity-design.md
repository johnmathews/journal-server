# Entry Date Integrity — Design

**Date:** 2026-07-13
**Status:** approved (design), awaiting implementation plan
**Origin:** two real incidents (entries 112 and 116, both handwritten with the
previous year in the heading — "Monday 29 June 2025", "Thursday 9 July 2025" —
during June/July 2026). Each landed as the earliest entry in the corpus and was
pinned by the storyline judge as a spurious opening chapter in three storylines.
Cleanup required manual date edits plus per-storyline re-bootstraps because a
date edit propagates nowhere.

## Requirements (from John)

1. It must be **impossible** to create or set an entry date earlier than
   2026-01-01 (the journal's true start era).
2. A recurrence of the year-off mistake must **fix itself** without special
   intervention.
3. When an impossible date cannot be confidently auto-repaired, the entry is
   ingested but **quarantined** — held out of downstream pipelines until the
   date is confirmed (decision: "Ingest, but hold from pipelines").
4. Editing an entry's date after it is already in storylines must
   **automatically re-bootstrap** the affected storylines (decision: fully
   automatic, not notify-only).

## Design

Four bounded components. 1–3 act at ingest time; 4 acts at edit time.

### 1. Date bounds (config + single validator)

- New config: `MIN_ENTRY_DATE` (env var, ISO date, default `2026-01-01`),
  parsed in `config.py` alongside the existing settings.
- Ceiling: `today + 1 day`, evaluated at validation time using the server
  clock (prod VM runs CEST; the +1 buffer absorbs timezone skew).
- One validation helper (new `services/entry_dates.py`) used by **every**
  write path: `validate_entry_date(date) -> None` (raises a typed error with
  an actionable message naming the bounds).
- Hard-rejecting call sites (explicit, user-supplied dates):
  - `PATCH /api/entries/{id}` date edits → 400 with the validator's message.
  - Text / voice / URL ingestion where a date is supplied explicitly.
  - MCP ingestion tools (same service path).
  - CLI ingestion paths.
- OCR-*detected* dates are not hard-rejected; they flow into component 2.
- Test/sample fixtures set their own `MIN_ENTRY_DATE` where they need
  historical dates.

### 2. Weekday cross-check + auto-repair at ingest

Both incidents shared a signature: the handwritten heading's weekday did not
match the written date, and did match the same day/month in an adjacent year.

- The heading detector additionally captures the weekday token when present
  (it already finds the date).
- At ingest, when a heading date has a weekday: verify weekday-vs-date
  consistency. When the date is **out of range** (component 1 bounds) or the
  weekday **mismatches**:
  - Search candidate years from `MIN_ENTRY_DATE.year` through `current year
    + 1`, same day/month.
  - If **exactly one** candidate makes the weekday match *and* lands in
    range → auto-correct to it, log the correction, and record a reviewable
    doubt on the entry ("date auto-corrected from YYYY-MM-DD") using the
    existing doubts/verify mechanism, so the UI shows an audit marker.
  - If the original date is in range but no unique repair exists (weekday
    mismatch only) → keep the date, record a doubt (plausible date, flagged).
  - If the date is out of range and no unique repair exists → component 3.
- Dates with no weekday in the heading: in range → accept unchanged
  (current behavior); out of range → component 3.

### 3. Quarantine for unrepairable dates

- New `entries` column `date_confirmed INTEGER NOT NULL DEFAULT 1`
  (quarantined entries get 0). New migration — number assigned at
  implementation time; the reserved drop of `storyline_panels_legacy`
  remains a **separate, later** migration per the 0036 rollout runbook.
- A quarantined entry is created with its OCR text intact and the detected
  (invalid) date stored verbatim as a **provisional display value** — this
  is the one deliberate exception to requirement 1's hard bound, and it is
  what the user corrects from. The invariant the system guarantees is:
  **no `date_confirmed` entry ever carries an out-of-range date**, and only
  confirmed entries reach any pipeline. The save pipeline stops before
  derived data: **no chunking/embedding, no entity extraction, no mood
  scoring** — which transitively keeps it out of FTS, vector search, and
  storyline candidacy (candidate queries also filter `date_confirmed = 1`
  for defense in depth).
- Entries list/detail show a "confirm date" badge. Confirming (or editing)
  the date via the existing PATCH validates bounds, sets
  `date_confirmed = 1`, and queues the standard save pipeline
  (chunk/embed → extraction → storyline extension checks). Release is the
  single gate through which a quarantined entry enters the pipelines.

### 4. Date-edit propagation

`update_entry_date` becomes a real service operation instead of a bare SQL
UPDATE:

1. Validate bounds (component 1).
2. Update the row (as today).
3. Refresh the entry's ChromaDB chunk metadata (`entry_date` is stamped into
   chunk metadata at embed time and currently goes stale on date edits; only
   a full text edit incidentally heals it today).
4. Find every storyline whose chapters contain the entry (via
   `storyline_chapter_entries`) and queue one re-bootstrap job per affected
   storyline on Pool B (existing `storyline_update` job with
   `bootstrap=True`), coalescing with any already-queued job for the same
   storyline.
5. Quarantine release with an unchanged-or-new date queues nothing here —
   a quarantined entry was never in any chapter.

Known consequence (accepted): a re-bootstrap redraws the whole storyline —
titles and narratives are rewritten and published chapters arrive unread
(observed on the 2026-07-13 Atlas/Fitness/Family re-sweeps).

## Out of scope

- The SPA stale-chunk navigation bug found the same day (router lazy-import
  404s after a deploy) — separate fix in the webapp repo.
- Backfill: prod data is already clean (both incidents hand-fixed
  2026-07-13; verified range 2026-02-15 → 2026-07-10).
- Multi-user nuances beyond passing `user_id` through the existing paths.

## Testing

Bug-fix rule applies — failing tests first for the two latent defects:

- `update_entry_date` leaves Chroma metadata stale and triggers no storyline
  reconciliation (component 4's motivating bug).
- Out-of-range dates are accepted verbatim at every write path (component
  1's motivating bug).

Then: unit tables for the weekday-repair search (both real incidents as
fixtures, ambiguous-candidate cases, no-weekday cases); quarantine exclusion
tests (candidate queries, save-pipeline gating, release flow); API 400
contract tests; migration test on prod-shaped data. Webapp: badge + confirm
flow component/store tests, coverage held ≥ 85%.

## Rollout

Server first (migration + validator + repair + quarantine + propagation),
webapp second (badge/confirm UI). No data backfill. `MIN_ENTRY_DATE` needs
no prod env change (default is correct). Update `server/docs/` +
`webapp/docs/` living references and both journals per house rules.
