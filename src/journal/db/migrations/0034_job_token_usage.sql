-- Per-job LLM token usage + dollar cost. Populated by the job runner's
-- usage-collection shim (services/usage.py) after each job reaches a
-- terminal state — tokens are recorded even for FAILED jobs. All three
-- columns are nullable: legacy rows and jobs that made no LLM calls leave
-- them NULL. cost_usd stays NULL for now (W2); W3 wires pricing to fill it.
-- Re-runnable: a half-applied ALTER is recovered by re-running (the runner
-- treats "duplicate column name" as a no-op — see migrations.py).
ALTER TABLE jobs ADD COLUMN input_tokens INTEGER;
ALTER TABLE jobs ADD COLUMN output_tokens INTEGER;
ALTER TABLE jobs ADD COLUMN cost_usd REAL;
