# Unit 2 — `EntityExtractionService` scratch state + file split

Date: 2026-05-07

Two-commit landing:

1. **Drop `_current_candidate_*` scratch state.** Threaded `candidate_ids`
   and `candidate_embeddings` as explicit parameters from
   `extract_from_entry` → `_resolve_entity` → `_try_llm_asserted_match`.
   Two-level threading; both signatures changed.
2. **Split the file into a package.** `services/entity_extraction.py`
   (1187 lines) → `services/entity_extraction/` with `signature.py`,
   `matching.py`, `sanity.py`, `service.py`, `__init__.py`.

## Pre-flight grep — implicit-state siblings

The plan called for a 5-minute grep across `services/` for sibling
patterns. Found:

- `services/jobs.py:237-239`: `_pending_images` and `_pending_audio`
  on `JobRunner`. **Verdict:** *not* implicit per-call state. They are
  job-id-keyed dicts that span the lifetime of a queued job — populated
  on submit, consumed on run, popped on terminal state. Different from
  `_current_candidate_*`: the access is keyed by an explicit parameter
  (`job_id`), not implicit. Recorded for Unit 4 to address naturally
  during the jobs split, but not Unit 2's concern.

No other matches.

## Why the split shape

The five-module split follows the responsibilities the original 1187-line
file already had — they were just visually adjacent rather than physically
separated. After the move:

| Module | Lines | Responsibility |
|---|---:|---|
| `signature.py` | 224 | String-signature heuristic (pure functions) + `find_signature_matches` (one EntityStore dep) |
| `matching.py` | 136 | `try_llm_asserted_match` (four-guard WU4-D) + `cosine_similarity` |
| `sanity.py` | 123 | Post-extraction sanity sweep + canonical-name support check |
| `service.py` | 808 | `EntityExtractionService` orchestrator |
| `__init__.py` | 25 | Public re-exports |

`service.py` at 808 lines is acknowledged-over-cap: the orchestrator
glues all the pieces together and `extract_from_entry` is genuinely
~300 lines of integration logic. The plan explicitly anticipated this:

> The orchestrator stays in the package's `__init__.py` (or
> `service.py`) and orchestrates the helpers. `extract_from_entry`
> remains the orchestrator and stays relatively large; bulk savings
> come from helpers moving out.

Further trimming would require either threading an `ExtractionContext`
dataclass through `_resolve_entity` (option (b) from the plan, more
glue than savings) or extracting `_resolve_entity` as a free function
with a 10-argument signature. Neither buys enough to justify the churn.

Down from 1187 lines on the original single file — ~32% smaller — and
each helper module sits under 250 lines.

## Approach choices

1. **Free functions with explicit deps (option (a) from the plan).**
   Each extracted helper takes the deps it needs as parameters
   (`store: EntityStore`, `embeddings: EmbeddingsProvider`, etc.). No
   `ExtractionContext` dataclass — the helpers already had small,
   stable dep sets.
2. **`__init__.py` re-exports for back-compat.**
   `tests/test_services/test_entity_extraction.py` imports
   `_normalized_signature`, `_is_short_difference`, `_is_signature_match`
   directly from the module. To keep that test green during Unit 2,
   the `__init__.py` re-exports them. Unit 6 will clean up the test
   reach-ins (the underscore prefix shouldn't be crossing the package
   boundary at all).
3. **Tests transitively cover the new modules.** The existing
   `test_entity_extraction.py` already exercises every signature-match
   case, the sanity sweep behaviour, and the four-guard match through
   integration. Adding focused per-module tests would duplicate that
   coverage. 1793 tests pass — same number as before the split.

## Decisions worth remembering

1. **Two-level param threading is tractable.** The plan warned the
   threading was "two levels deep" and would change two signatures.
   It did. Both `_resolve_entity` and `_try_llm_asserted_match`
   gained `candidate_ids` and `candidate_embeddings` parameters. No
   external callers (no test reaches `_resolve_entity` directly) so
   no migration cost there. The underscore-prefixed methods kept the
   underscore — they're internal.
2. **`run_sanity_sweep` swallowed an inline 38-line block.** The
   sanity-sweep loop was inlined in `extract_from_entry` (lines
   ~654-691). Pulling it into `sanity.py` shrinks `extract_from_entry`
   and gives the sweep a name — a future agent grep'ing for
   "sanity sweep" lands directly on `sanity.py:run_sanity_sweep`.
3. **`min_cosine` is now a parameter of `try_llm_asserted_match`,
   not a self-attribute access.** The orchestrator passes
   `self._llm_match_min_cosine` at the call site. Future callers
   (e.g. a CLI debug tool that wants to test the guards with a
   custom threshold) can vary it without touching the service.
