-- Flag indicating the user has reviewed all OCR uncertain spans for
-- an entry and confirmed they are correct. When set to 1, the API
-- returns uncertain_span_count = 0 and uncertain_spans = [] so the
-- webapp treats the entry as "no remaining doubts". The underlying
-- span rows in entry_uncertain_spans are preserved for future use
-- (e.g. glossary enrichment, accuracy tracking, re-OCR prioritisation).
--
-- Default is 0 (not verified). Only set to 1 via
-- POST /api/entries/:id/verify-doubts.

ALTER TABLE entries ADD COLUMN doubts_verified INTEGER NOT NULL DEFAULT 0;
