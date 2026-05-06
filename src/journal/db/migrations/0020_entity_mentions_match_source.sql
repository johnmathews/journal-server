-- Migration 0020: Track which resolution stage matched a mention.
--
-- During entity extraction, a mention is linked to an entity via one
-- of several stages: exact-name match (stage_a), alias match
-- (stage_b), embedding similarity (stage_c), or LLM-asserted match
-- (llm_asserted, added in WU4 alongside known-entity prompt
-- injection). For mentions that created a brand-new entity rather
-- than matching an existing one, the column stays NULL.
--
-- Recorded for telemetry / audit. Lets us measure how often each
-- stage fires once the LLM-asserted path is live, and gives us a
-- way to quarantine "all mentions linked via stage X" if a guard
-- bug is later discovered.

ALTER TABLE entity_mentions ADD COLUMN match_source TEXT;
