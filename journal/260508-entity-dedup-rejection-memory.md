# 2026-05-08 — Entity dedup: persistent rejections, per-pair candidates, signature tightening

## Context

User flagged two pain points in the merge-candidate flow:
1. Same pair (e.g. "John Mathews" vs "John Mathews' mother") appearing 3+ times in a 10-row candidate list.
2. Dismissing the pair didn't stick — every subsequent extraction re-flagged it as pending.

A read of the prod DB (`media:journal-server:/data/journal.db`, 467 entities, 7 pending, 54 dismissed, 0 ever
accepted) showed:

- 85% of dismissals scored exactly 0.95 — the signature heuristic's synthetic score, not embeddings.
- The "John Mathews / mother" pair had been dismissed 4 times across two days.
- The embedding near-miss band (0.73–0.88) had **0 acceptances ever** vs ~14 dismissed + 7 still pending. Currently
  pending: Hermione vs Neville, Mum Weasley vs Arthur Weasley, Remus vs Tonks — the embedding model clusters fictional
  characters together but they are clearly distinct entities.
- The `UNIQUE(a, b, extraction_run_id)` constraint on `entity_merge_candidates` allowed the same pair to insert one
  row per extraction run, so dismissals only blocked that specific run's row.

## What changed

Five backend changes plus a webapp panel:

**WU1 — `entity_pair_decisions` table (migration 0021).** Persistent "not a duplicate" memory keyed on
`(user_id, entity_id_lo, entity_id_hi)` with `entity_id_lo < entity_id_hi` enforced by CHECK so lookup is
order-independent. `resolve_merge_candidate` now writes a rejection in the same transaction when status='dismissed';
`_resolve_entity` consults `is_pair_rejected` before creating any candidate. `merge_entities` calls
`_transfer_pair_rejections_for_merge` before deleting the absorbed entity, re-targeting rejections involving the
absorbed entity onto the survivor (or dropping self-pairs). FK CASCADE is the safety net.

**WU2 — Per-pair UNIQUE on `entity_merge_candidates` (migration 0022).** SQLite forces a table rebuild for UNIQUE
changes. The migration collapses historical rows to one per `(lo, hi)` pair, taking the highest similarity and a
sensible status precedence (accepted > dismissed > pending). `create_merge_candidate` is now an UPSERT with
`WHERE status='pending'` — repeated extraction touches one row instead of inserting many, and dismissed pairs are
never resurrected even if WU1's check is bypassed somehow.

**WU3 — Signature heuristic tightening (`signature.py`).** Added `_is_likely_word_tail(tail, allow_short_words)`
that rejects three classes of tail:
- Starts with `'` or `'` — possessive marker
- Purely numeric — specifier ("Psalms 63")
- Vowel-bearing tails ≥3 chars (≥5 with `allow_short_words=True`) — likely real words

Applied to Case 2 (substring + leftover) at the strict threshold (catches "John Mathews' mother", "Bible study",
"Haarlem Centraal", "Spaarnebuiten", "Interview practice", "RAG pipelines"). Applied to Case 3 prefix branch
(common prefix → divergent suffix tails) at the lenient threshold (preserves Dutch place qualifiers like "Weg",
"Zuid"). Applied to Case 3 suffix branch (common suffix → divergent prefix tails) at the strict threshold (catches
"Chaos Engineering" vs "Data Engineering"). Updated existing test that assumed the old behavior.

**WU4 — Removed embedding near-miss candidate creation.** `_resolve_entity` Stage C still auto-merges at
`>= self._threshold`; below it now creates only a new entity, no candidate. Saved variable `near_miss` is
hard-asserted None at the call site. Reasoning: 0/21 hit rate in prod history, plus user said "I can always
manually merge if necessary."

**WU6 — Past-dismissals API.** Two new endpoints:
- `GET /api/entities/pair-decisions?limit&offset` — list rejections, joined with entity summaries.
- `DELETE /api/entities/pair-decisions/{id}` — undo a rejection (the pair becomes eligible again on next
  extraction).

## Tests

- `test_pair_decisions.py` — full coverage of record/lookup/transfer/cascade for the new repo methods.
- `test_entity_extraction.py::TestSignatureCandidateSkipsWhenRejected` — service-level: signature match between A and B
  produces no candidate when `(A, B)` is rejected.
- `test_entity_extraction.py::TestNoEmbeddingNearMissCandidates` — regression for WU4.
- `test_entity_extraction.py` — added 7 regression cases covering each prod false-positive pattern; preserved
  typo-recall test for "Andrew" vs "Andrews".
- `test_entity_store.py::TestMergeCandidateUpsert` — repeated create keeps one row at max score; dismissed pairs are
  not resurrected; per-pair single row enforced.
- `test_migrations.py` — schema assertions for both new migrations.
- `test_api.py` — dismiss-records-decision, list, undo, 404 paths.

Full server suite: 1852 passed. ruff clean.

## Decisions / tradeoffs

- **"Rejected forever" semantics:** discussed with user. Edge case: when entity A is merged into C, the rejection
  on `(A, B)` transfers to `(C, B)`. Avoids losing the user's intent across merges.
- **Signature heuristic recall:** intentionally reduced for relational/specifier suffixes. The "Zij Kanaal C" vs
  "Zij Kanaal C Weg" case (helper-level test) is no longer matched — small tradeoff the user accepted in exchange
  for eliminating ~85% of prod false positives. Place qualifiers still match via Case 3 prefix branch.
- **Why not just collapse on read:** the read-side group-by would mask the pile-up symptom but leave the underlying
  write amplification — every extraction would still hammer the table. WU2 fixes the source.

## Out of scope (per user)

- Retroactive cleanup of duplicate entities — user already did this manually in prod.
- The 16 same-name entity pairs in prod ("dad", "atlas", etc.) are all cross-`user_id` and correct multi-tenancy
  behavior.
- Auto-merge threshold (`0.88`) is unchanged — no evidence of misbehavior there.
