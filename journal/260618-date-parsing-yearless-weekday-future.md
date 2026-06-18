# Date parsing — year-less headings, weekday disambiguation, no-future rule

**Date:** 2026-06-18
**Branch:** `<current server branch>`
**Reference doc:** [`docs/architecture.md`](../docs/architecture.md) ("Entry date resolution" section)

## Context

Handwritten entries start with a date heading in many shapes: with or
without the year, with or without a weekday, with or without a trailing
time. The deterministic parser `extract_date_from_text()` only matched
dates that included a **4-digit year** — every pattern had `\d{4}`. So
the user's common year-less forms ("9 June", "10 June 23:35",
"Thursday 18 June 22:55") fell through to the LLM heading detector,
which is explicitly told to refuse year-less dates ("return
is_heading=false instead of guessing"). Net result: those entries got
no parsed date.

The user gave two facts to exploit: a journal entry can never be dated
in the future, and the heading may carry a weekday that pins the year.

## Decisions (confirmed with user)

- **Explicit year → kept as written, even if future.** The no-future
  rule only governs *inferred* (missing) years; a written year is
  assumed intentional, not an OCR misread. (`"20 June 2026"` stays
  2026-06-20 even though today is the 18th.)
- **Weekday → disambiguate + graceful fallback.** For a year-less date,
  pick the most recent past year whose weekday matches; if none matches
  in the search window, fall back to the plain most-recent-past year.

## Change

All in `services/date_extraction.py` (central — feeds image, voice, and
text ingestion):

- `_PAT_DMY_NAMED` rewritten: named groups, **year now optional**,
  optional leading weekday captured, trailing time left unmatched
  (ignored).
- New `_infer_missing_year(month, day, weekday_idx, today)` — walks back
  from the current year, skips future dates, returns the most recent
  match (weekday-aware when present). Search window `_YEAR_SEARCH_RANGE
  = 28` (one full weekday cycle, so a match always exists for real
  dates; we still return the *nearest* past match).
- `extract_date_from_text(text, today=None)` — added the `today` param
  for deterministic testing; defaults to `datetime.date.today()`.
  Callers are unchanged (they pass only `text`).

MDY-named, ISO, and numeric patterns are untouched (they always carry a
year). The LLM heading detector is unchanged — it still covers
spelled-out and relative phrasings the regex can't.

## Notes

- **Ingestion ordering is unchanged.** The regex result is passed to the
  heading detector as its `entry_date` hint, so the LLM resolves
  year-less/relative phrases against the same inferred year rather than
  fighting it.
- **Weekday reality check:** weekdays repeat every ~5–6 years, so
  "most recent matching weekday" still biases toward recent years — it
  corrects the *nearest* wrong-weekday guess (e.g. Thu→Wed bumps 2026→
  2025) rather than reaching arbitrarily far back. Documented honestly
  in the function docstring.
- **Forward-only.** Sets `entry_date` on new ingestions; existing
  entries unaffected (editable via `PATCH /api/entries/{id}`).

## Tests

TDD: 15 new failing tests first (RED was `TypeError: unexpected kwarg
'today'`), then implemented.

- `TestYearlessDate`, `TestTimeSuffixIgnored`, `TestWeekdayDisambiguation`,
  `TestExplicitYearKeptEvenIfFuture` in
  `tests/test_services/test_date_extraction.py`, all pinned to a fixed
  `today = 2026-06-18` (a Thursday).
- Covers every example from the request: "7 June 2016", "9 June",
  "10 June 23:35", "11 June 2026 22:15", "Thursday 18 June 22:55",
  "Thursday 18 June 2026 14:30".

Full unit suite: 2939 passed (+15). `ruff` clean. The 40 pre-existing
date tests still pass unchanged.
