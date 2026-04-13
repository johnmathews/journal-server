# Verify Doubts Endpoint

Added the ability to mark all OCR doubts on an entry as verified, so
users can confirm that uncertain words are actually correct without
having to edit them.

## What changed

- **Migration 0009**: Added `doubts_verified` boolean column (default 0)
  to the `entries` table.
- **Repository**: New `verify_doubts(entry_id)` method sets the flag.
  `get_uncertain_span_count()` returns 0 when verified, preserving the
  underlying span rows for future analysis.
- **API**: New `POST /api/entries/{id}/verify-doubts` endpoint. Both
  `_entry_to_dict` and `_entry_summary` now include `doubts_verified`
  and suppress spans/count when the flag is set.
- **Tests**: 5 repository tests + 4 API tests covering the new behavior.

## Design decision

Used a single boolean on the `entries` table rather than per-span
resolution or deleting span rows. Reasons:

1. The user's workflow is all-or-nothing ("all remaining doubts verified")
2. Preserving span rows enables future use: glossary enrichment from
   corrected uncertain words, accuracy tracking across model versions,
   and identifying entries to re-OCR if switching to a better model
3. Simpler than a `resolved` column on `entry_uncertain_spans` with
   filtering in every query

## Context

Research during this session found that Gemini 3 Pro significantly
outperforms Claude Opus at handwriting OCR (1.67% vs 4.28% CER) and
is cheaper. The preserved uncertainty data will be valuable if/when
the project switches OCR providers — it can identify which entries
had the most errors for re-processing.
