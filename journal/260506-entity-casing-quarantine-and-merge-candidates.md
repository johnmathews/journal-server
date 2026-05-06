# 2026-05-06 — Entity casing, quarantine, hallucination guard, merge-candidate detection

Engineering-team session covering five server-side work units. Driven by a UI ask that
turned out to span backend logic + a misdiagnosed prod entity bug.

## Context — the user's three asks

1. Mood-trends chart should default to the affect-axes group (joy + energetic) on page
   load. _(webapp-only — see sibling commit in journal-webapp.)_
2. Entity names should be smart-title-cased — `running` → `Running`, `church` → `Church`,
   but not naïve: `The Netherlands`, `iOS`, `FC Barcelona` should round-trip.
3. A place entity `Zij Kanaal C Zuid` was hanging around in prod after the user fixed the
   OCR that originally produced it. The user expected stale-entity auto-deletion. Also
   asked for manual entity merging — already exists, just undocumented.

## Investigation — what was actually wrong

Investigated prod via `ssh media`. The orphan-cleanup logic is correct and runs on every
entry edit. The reported entity is **not** orphaned — it has 1 valid mention against the
current text with the latest extraction-run UUID. What's wrong is upstream:

- The original LLM extraction hallucinated the canonical name `Zij Kanaal C Zuid` from a
  quote `'"Zij Kanaal C" Zuid is clearly a canal'` (the OCR'd `weg` plus a stray `Zuid`).
- `providers/extraction.py:379` warned about the canonical-not-in-quote mismatch but
  kept the entity anyway.
- After the user fixed the OCR, re-extraction ran. The matcher (embedding-based)
  re-bound the corrected quote to the existing hallucinated entity 745 because the
  embeddings were close. The orphan GC saw a live mention and (correctly) skipped it.
- Three near-duplicate `Zij Kanaal C *` rows existed in prod that the merge-candidate
  detector never flagged.

So the actual fix isn't tightening orphan cleanup — it's stopping hallucinated names
from being kept and adding a sanity sweep that catches zombie-rebound entities after
re-extraction.

## What shipped

### WU2 — Smart title-case at write time

- New `services/entity_naming.py` with `smart_title_case()`.
  - Trim, exception-table lookup, mid-word-uppercase preservation, word-by-word title
    case with non-leading article/preposition/Dutch-particle lowercasing, hyphen-segment
    aware. Idempotent.
- New `config/entity-casing-exceptions.toml` (operator-managed).
- Wired into `entitystore/store.py:create_entity()` — the single chokepoint.
- Hot-reloadable via `POST /api/admin/reload/entity-casing` (matches the pattern of
  `/mood-dimensions`).
- 67 new tests; **zero pre-existing fixtures broke** (existing tests already used
  title-cased canonical names).

### WU3 — Soft entity quarantine

- Migration `0018_entity_quarantine.sql` adding `is_quarantined`, `quarantine_reason`,
  `quarantined_at` plus a partial index on `is_quarantined = 1`.
- Store helpers `quarantine_entity` / `release_quarantine` /
  `list_quarantined_entities`. Idempotent quarantine refreshes timestamp + reason.
- `list_entities` and `list_entities_with_mention_counts` exclude quarantined rows by
  default; chart/aggregation queries (`get_entity_distribution`, `get_entity_trends`,
  `get_mood_entity_correlation`) hardcode the exclusion.
- New routes:
  - `GET /api/entities/quarantined`
  - `POST /api/entities/{id}/quarantine`
  - `POST /api/entities/{id}/release-quarantine`
- Quarantined entities still merge cleanly (as survivor or absorbed) — no special-casing.
- 23 new tests.

### WU4 — Reject hallucinated names + post-extraction sanity sweep

- `providers/extraction.py:_longest_canonical_substring_in_quote()` — token-aligned
  substring search, case-insensitive, whitespace-tolerant. Rejects matches < 3 chars.
- The warn-only path at the old `:379` is now a rename-or-flag path (~lines 434–484).
  When the LLM produces a canonical not in its quote: try to rename to the longest
  in-quote substring; if that fails, set `pending_quarantine_reason` on the parsed
  entity dict.
- `services/entity_extraction.py` — entities flagged with `pending_quarantine_reason`
  are quarantined immediately on creation. After the existing orphan-cleanup, a sweep
  iterates `touched_entity_ids` and quarantines any whose canonical name doesn't
  appear in any of its mention quotes or any mentioned entry's `final_text`
  (case-insensitive, whitespace-tolerant).
- Author exemption — first-person prose creates an author entity whose canonical name
  isn't written verbatim. Without the exemption the existing happy-path tests would
  mass-quarantine the journal author.
- Headline regression test: `test_reextraction_after_entry_edit_quarantines_orphaned_canonical`
  reproduces the prod sequence (seed `Zij Kanaal C Zuid` + mention; update entry text;
  re-extract; assert quarantined with reason).
- 13 new tests.

### WU5 — Loosen merge-candidate detection

- Existing embedding-distance threshold left untouched; added a normalized-signature
  heuristic alongside it in `services/entity_extraction.py`:
  - Lowercase + whitespace-stripped + trivial-punctuation-stripped signatures.
  - Three trigger cases: equal signatures; substring with short leftover (≤6 chars or
    single token); long common prefix or suffix with short divergent tails (common
    region ≥8 chars AND ≥2× max-tail length, both tails ≤6 chars).
- The third case was added beyond the original plan — without it, the user's headline
  case `Zij Kanaal C Weg` vs `Zij Kanaal C Zuid` (neither is a substring of the other)
  wouldn't trigger.
- Synthetic similarity scores (1.0 / 0.95) sort these to the top of the merge-review UI.
- Cross-type filter respected. Self-pairs skipped.
- 15 new tests; existing detector tests still pass.

### WU6 — Documentation

- `docs/entity-tracking.md` extended with three new top-level sections:
  - **Merging entities** — the existing-but-undocumented feature: API, semantics
    (mentions reassigned, absorbed canonical names → aliases, `entity_merge_history`
    audit, accepted candidates), webapp UI flow.
  - **Quarantine** — what soft-quarantine means, when it triggers, how to release.
  - **Casing normalization** — algorithm, exceptions file, reload endpoint.
- New `docs/production-deployment.md` — full operator runbook: media-VM compose layout
  at `/srv/media/docker-compose.yml`, three containers
  (server/webapp/chromadb on `ghcr.io/johnmathews/journal-*:latest`), bind mounts under
  `/srv/media/config/journal/`, manual `pull && up -d` update workflow, Cloudflare
  Tunnel exposure path (separate tailnet host), runbook commands (logs, DB queries via
  `docker exec ... python3 -c '...'` since there's no `sqlite3` binary in the
  container), backup target, known fragilities (`:latest` with no auto-update).
- `docs/api.md` — new endpoint entries for the three quarantine routes.
- `docs/configuration.md` — entity-casing-exceptions.toml entry + curl reload example.

## Test results

- `uv run pytest`: **1711 passing** (was 1608 pre-session); ruff clean.
- Coverage retained on changed modules: `entity_naming.py` 96%, `services/reload.py`
  98%, `entitystore/store.py` 88%.
- The headline regression test for the `Zij Kanaal C Zuid` flow passes.

## Out of scope (flagged for follow-up)

1. Backfilling pre-existing duplicate-case entity rows. Future writes are consistent;
   prod cleanup happens via the merge UI.
2. Production image SHA-pinning / Watchtower. Documented as a known fragility.
3. Embedding-matcher improvements — long-term; the post-extraction sanity sweep is a
   compensating control.

## Sibling commit

The webapp side (mood-trends affect-axes default + quarantine UI surface) ships in
`journal-webapp` in the same eng-ui-changes session.
