# Entry Date Integrity

How the server keeps entry dates inside a sane window, self-heals the
common "wrote last year's date in the heading" mistake, and propagates
date edits everywhere they matter. Shipped 2026-07-13; design rationale in
[the spec](superpowers/specs/2026-07-13-entry-date-integrity-design.md)
(origin: entries 112/116, both handwritten with the previous year and
pinned as spurious opening chapters in three storylines).

## Bounds

Every entry date must lie in `[MIN_ENTRY_DATE, today + 1 day]`.

| Env var          | Default      | Purpose                                    |
| ---------------- | ------------ | ------------------------------------------ |
| `MIN_ENTRY_DATE` | `2026-01-01` | Hard floor — start of real journal data. |

The ceiling is evaluated against the server clock at write time (the
prod VM runs CEST; the +1 day buffer absorbs timezone skew). One
validator (`services/entry_dates.py::validate_entry_date`) is used by
every explicit-date write path: `PATCH /api/entries/{id}`, text/URL
ingestion, and (transitively) the MCP/CLI tools. Violations are a 400
at the API and a typed `EntryDateError` in services.

## Weekday auto-repair (detected dates)

OCR/voice *detected* dates aren't hard-rejected — they go through
`repair_entry_date`, which cross-checks the heading's weekday word
against the written date:

- date in range, weekday consistent (or absent) → **ok**.
- weekday contradicts the date and exactly one candidate year in the
  window makes it consistent → **repaired** silently (logged, and the
  heading region is recorded as a reviewable uncertain span so the UI
  highlights it).
- in-range date, contradicting weekday, no unique repair → **doubtful**:
  date kept, span recorded.
- out-of-range date, no unique repair → **unrepairable** → quarantine.

Both real incidents ("Thursday 9 July 2025", "Monday 29 June 2025" —
each a weekday that only fits 2026) repair silently under this rule.
The gate runs in all ingest orchestrators via
`IngestionService._apply_date_repair` (image single/multi-page, voice
single/multi-recording).

## Quarantine

An unrepairable date still creates the entry (OCR text intact, the bad
date stored verbatim as a provisional display value, `date_confirmed = 0`)
but **nothing derived is produced**: no chunks, no embeddings, no FTS,
no mood score, and the ingestion workers skip the post-ingestion
follow-ups (entity extraction → storyline checks). Storyline candidate
queries additionally filter `date_confirmed = 1` as defense in depth.
The system invariant: **no confirmed entry ever carries an out-of-range
date, and only confirmed entries reach any pipeline.**

Release: editing the entry's date (webapp date editor → the normal
PATCH) validates bounds, sets `date_confirmed = 1`, and queues the
standard save-entry pipeline (chunk/embed → extraction → storyline
extension checks). The job result for a quarantined ingest carries
`"quarantined": true`.

## Date-edit propagation

`IngestionService.update_entry_date` returns `(entry, released)` and is
no longer a bare SQL UPDATE:

1. Bounds validation (400 on failure — nothing written).
2. Row update.
3. Per-chunk `entry_date` metadata refreshed in ChromaDB in place
   (`VectorStore.update_entry_metadata` — no re-embedding).
4. The PATCH handler queues one `storyline_update(bootstrap=True)` job
   per storyline whose chapters contain the entry (Pool B). Queueing
   failures never fail the request — the date write has already
   committed. Response carries `storyline_bootstrap_job_ids`.

Consequence (accepted in the spec): a re-bootstrap redraws the whole
storyline — fresh titles/narratives, published chapters arrive unread.

## Operational notes

- Migration `0037_entry_date_confirmed.sql`; existing rows default to
  confirmed. The `storyline_panels_legacy` drop remains a separate,
  later migration.
- The webapp shows an "Unconfirmed date" badge (list) and a clickable
  pill (detail) for quarantined entries — see
  `journal-webapp/docs/` for the UI side.
