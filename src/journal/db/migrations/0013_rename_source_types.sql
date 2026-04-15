-- Migration 0013: Rename source_type values to a clearer taxonomy.
--
--   ocr    -> photo               (uploaded images of handwritten pages)
--   manual -> text_entry           (typed directly in the webapp)
--   import -> imported_text_file   (uploaded .md/.txt files)
--   voice  -> voice                (unchanged — mic recording in webapp)
--   (new)    imported_audio_file   (uploaded audio files, distinct from live recording)
--
-- The column itself stays TEXT NOT NULL with no CHECK constraint
-- (relaxed in migration 0007).  Only the stored values change.

UPDATE entries SET source_type = 'photo'              WHERE source_type = 'ocr';
UPDATE entries SET source_type = 'text_entry'         WHERE source_type = 'manual';
UPDATE entries SET source_type = 'imported_text_file' WHERE source_type = 'import';
