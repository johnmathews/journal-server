# 2026-07-13 — Entry date integrity: floor, weekday repair, quarantine, edit propagation

## Why

Two handwritten entries (112, 116) carried last year's date in their
headings ("Monday 29 June 2025", "Thursday 9 July 2025" — written in
June/July **2026**). Each became the earliest entry in the corpus and the
storyline judge dutifully pinned each as a spurious opening chapter in
three storylines (Atlas, Fitness, Family). Cleanup was manual: fix the
date in the UI, fix the heading text, then per-storyline
`bootstrap-storylines` re-runs over ssh — because a date edit was a bare
SQL UPDATE that propagated nowhere (stale ChromaDB chunk metadata
included).

## What shipped

Four components (spec:
`docs/superpowers/specs/2026-07-13-entry-date-integrity-design.md`,
living doc: `docs/entry-date-integrity.md`):

1. **Bounds** — `MIN_ENTRY_DATE` (default 2026-01-01) ≤ date ≤ today+1,
   one validator on every explicit-date write path; 400/`EntryDateError`.
2. **Weekday auto-repair** — detected dates are cross-checked against the
   heading's weekday word; a unique year candidate repairs silently with
   a reviewable uncertain span as the audit marker. Both real incidents
   would have self-healed.
3. **Quarantine** — unrepairable dates create the entry with
   `date_confirmed = 0` (migration 0037) and skip ALL derived processing;
   the date edit that fixes it releases the deferred save pipeline.
4. **Propagation** — date edits refresh per-chunk vector metadata in
   place and auto-queue `storyline_update(bootstrap=True)` per affected
   storyline (found via the new `find_storyline_ids_for_entry` reverse
   lookup). The 2026-07-13 manual dance is now automatic.

## Decisions

- **Quarantine over reject** (John's call): an unconfirmable date still
  ingests — the scan is preserved — but is held from every pipeline
  until confirmed. Provisional bad date stored verbatim as the display
  value; the invariant is that no *confirmed* entry is out of range.
- **Fully automatic re-bootstrap on date edit** (John's call): costs a
  few LLM calls per rare edit; rewrites the affected storyline's
  narratives (arrive unread).
- **Cross-request job coalescing dropped as YAGNI** — Pool B is
  single-worker and bootstrap is idempotent.
- Malformed caller dates at ingest (`"not-a-date"`) now quarantine
  instead of embedding with a junk date prefix (contract change; test
  updated).

## Process notes

Hybrid execution of the plan (`docs/superpowers/plans/
2026-07-13-entry-date-integrity-server.md`): inline TDD for mechanical
tasks, reviewer subagents for the risky ones + one final whole-branch
review. The mid-branch reviewer caught two real issues (bootstrap
queueing could 500 a PATCH whose date write already committed; missing
404 guard on a mid-update delete race) — both fixed with tests. A piped
`pytest | tail` masked one failing test long enough to get committed;
lesson: `set -o pipefail` in verification commands.
