-- Backfill pricing rows for two models referenced in code but missing
-- from the 0017 seed, so per-job cost estimates don't silently undercount:
--   * claude-opus-4-7  — storyline narrator default model
--   * whisper-1        — transcription fallback model
-- INSERT OR IGNORE mirrors 0017 so re-running is a no-op if a row exists.

INSERT OR IGNORE INTO pricing (model, category, input_cost_per_mtok, output_cost_per_mtok, cost_per_minute, last_verified) VALUES
('claude-opus-4-7', 'llm', 5.0, 25.0, NULL, '2026-07-11'),
('whisper-1', 'transcription', NULL, NULL, 0.006, '2026-07-11');
