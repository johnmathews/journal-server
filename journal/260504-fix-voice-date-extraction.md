# 2026-05-04 — Voice ingestion: leading date is now preserved as entry_date

## The bug

When a voice (or multi-voice) journal entry began with a date — e.g. dictating
today an entry from three days ago, opening with "Friday 1 January 2026" —
the date was stripped from the body but **lost entirely**. The entry was filed
under the upload day (today), not the dictated day, and the spoken date
disappeared from both the body and the entry record.

## Root cause

`260501-strip-leading-date-from-body.md` changed the heading-detection policy
from "promote leading date to a `# Heading` in the body" to "strip it
entirely." The OCR paths (`ingest_image`, `ingest_multi_page_entry`) call
`extract_date_from_text(raw_text)` *before* heading detection to update the
entry's `date` from the OCR text. The voice paths (`ingest_voice`,
`ingest_multi_voice`) skip that step. The result:

- OCR: leading date stripped from body **and** propagated to `entry_date`.
- Voice: leading date stripped from body, but `entry_date` left at whatever
  the caller passed in (today).

`HeadingDetectionResult` carried only `heading_text` and `body`, so even if
ingestion had consulted it for a date, there was no parsed ISO field — the
canonical heading_text would have needed re-parsing.

## Fix

Two coordinated changes:

1. **Voice paths now mirror OCR.** Both `ingest_voice` and
   `ingest_multi_voice` call `extract_date_from_text(raw_text)` before
   detection and use the result as the entry's `date`. This handles the
   regex-friendly forms (numeric, abbreviated, ISO) the user's reported case
   used.

2. **Heading detector returns an ISO date.** `HeadingDetectionResult` gained
   a `date_iso: str | None` field. The LLM prompt now requests an `iso_date`
   alongside `heading_text` and `source_phrase`. Server-side validation
   (`_validate_iso_date`) requires `YYYY-MM-DD` form within a plausible year
   range (1900–2100); anything else falls back to None. All four ingestion
   paths (image, multi-page, voice, multi-voice) prefer `det.date_iso` over
   the regex result when set — the LLM is more capable for spelled-out and
   relative phrases ("the first of January", "yesterday") that the regex
   can't parse.

Order of preference for the entry date:

1. `det.date_iso` from the heading detector (most capable resolver).
2. `extract_date_from_text(raw_text)` regex match.
3. The caller-provided `date` (defaults to upload day).

## Tests

Six new ingestion tests in `TestHeadingDetection`:

- `test_voice_with_regex_extractable_leading_date_sets_entry_date` —
  reproduces the user's exact reported case with no detector wired in
  (regex-only path, mirrors OCR).
- `test_multi_voice_with_regex_extractable_leading_date_sets_entry_date`.
- `test_voice_with_detector_iso_date_sets_entry_date` — LLM-resolved spelled-out
  phrase routes through `det.date_iso`.
- `test_multi_voice_with_detector_iso_date_sets_entry_date`.
- `test_image_with_detector_iso_date_overrides_caller_date`.
- `test_multi_page_ocr_with_detector_iso_date_overrides_caller_date`.

Five new heading-detector unit tests for `iso_date` parsing:

- `test_iso_date_returned_when_valid` — happy path.
- `test_iso_date_absent_yields_none` — old-shape responses still work.
- `test_iso_date_malformed_yields_none` — `"not-a-date"` rejected.
- `test_iso_date_out_of_plausible_range_yields_none` — `"0001-01-01"` rejected.
- `test_iso_date_with_time_component_yields_none` — datetime strings rejected
  by `date.fromisoformat`.

Full suite: 1587 passed (one pre-existing flake on `tests/test_api_ingest.py`
unrelated to this change — verified by stashing the worktree changes and
reproducing the same flake on `main`).

## Out of scope

Backfilling existing entries that lost their leading date is not addressed
here. Per the prior journal entry, the policy is "new ingestions only";
that still holds.

## Follow-ups landed in the same session

- `ed170b0` — line-length fix in the heading-detector SYSTEM_PROMPT after
  adding `iso_date` pushed the example JSON past the 100-char ruff limit.
  The example is now pretty-printed across multiple lines (the LLM still
  outputs single-line JSON, that instruction is unchanged).
- `d445753` — pre-existing WIP edits to `config/mood-dimensions.toml`
  were committed: `anxiety_eagerness` is replaced by `energy_fatigue`
  (pure arousal, decoupled from valence direction); `comfort_discomfort`
  is split into three unipolar axes (`fulfillment`, `connection`,
  `frustration`) that capture distinct facets without the bipolar
  conflation. Final dimension list: `joy_sadness`, `energy_fatigue`,
  `agency`, `fulfillment`, `connection`, `frustration`,
  `proactive_reactive`. This is a behavioural change for mood scoring
  going forward and worth checking when reviewing dashboards over the
  transition.
