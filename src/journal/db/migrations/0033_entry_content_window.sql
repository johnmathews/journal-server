-- Content window: half-open [content_start_char, content_end_char) into
-- entries.raw_text. NULL = whole text. Same convention as
-- entry_uncertain_spans (0005). Marks neighbour-entry text on the first/
-- last photographed page so it is kept verbatim but excluded from derived
-- artifacts. Re-runnable: a partial failure leaves user_version unchanged,
-- and a half-applied ALTER is recovered by re-running (the migration
-- runner guards on PRAGMA user_version).
ALTER TABLE entries ADD COLUMN content_start_char INTEGER;
ALTER TABLE entries ADD COLUMN content_end_char INTEGER;
