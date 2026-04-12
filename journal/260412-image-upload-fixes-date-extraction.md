# Image Upload Fixes and Date Extraction

**Date:** 2026-04-12

## Context

First real-world test of the image upload feature from a phone. Uploaded a
handwritten journal page photo via the webapp deployed at
journal-insights.itsa-pizza.com. Multiple issues surfaced.

## Issues Found and Fixed

### 1. Nginx 413 — body too large

The webapp's nginx reverse proxy had no `client_max_body_size` directive,
defaulting to 1MB. A 3.2MB phone photo was rejected before reaching the
server. Fixed by adding `client_max_body_size 20m` to the `/api/` location
block.

### 2. Multipart Content-Type bug (400 error)

The webapp's `apiFetch` always set `Content-Type: application/json`, even for
`FormData` bodies. This caused the server to receive a multipart body with a
JSON content type, failing to parse it. Fixed by skipping the Content-Type
header when `body instanceof FormData`, letting the browser set the correct
`multipart/form-data; boundary=...` header.

### 3. Date extraction from OCR text

Journal entries defaulted to today's date even when the handwritten page
clearly contained a date (e.g., "TUES 17 FEB 2026"). Added a
`date_extraction` module that parses dates from the first 500 characters of
OCR text. Supports multiple formats: named months (DMY/MDY), ISO 8601,
numeric DD/MM/YYYY. The extracted date overrides the caller-provided default.

### 4. Date editing via API

The PATCH endpoint previously only accepted `final_text`. Extended it to also
accept `entry_date` (at least one must be provided). Added `update_entry_date`
to the repository layer.

## New Files

- `src/journal/services/date_extraction.py` — date parsing from OCR text
- `tests/test_services/test_date_extraction.py` — 25 tests covering all
  supported date formats, edge cases, and invalid dates

## Tests

- 28 new tests total (25 date extraction + 3 PATCH date endpoint)
- Full suite: 682 passed
