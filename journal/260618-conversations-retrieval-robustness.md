# 260618 — conversations retrieval robustness: classify → dispatch → fallback

Replaces the single-mechanism reply path in `/conversations` with a four-way
intent router that picks the right retrieval shape per message, while preserving
strict grounding and bounded cost on every path.

## Six weaknesses in the old reply path

1. **Aggregate questions answered from a sample.** Fixed top-8 hybrid search
   means "how many times did I mention my back?" was answered from ≤8 entries.
   The grounding contract prevents fabrication but cannot detect undercounting —
   the model answered confidently from an incomplete view.
2. **Temporal-origin questions unreliable.** Retrieval sorted by relevance, not
   date. "When did the back pain start?" had no mechanism to surface the earliest
   entry — the system prompt's "lead with the earliest passage" instruction cannot
   fix a recall gap.
3. **Fixed top-8** is a recall ceiling for broad questions and occasional noise
   (padding with marginal entries) for narrow ones.
4. **Naïve head truncation.** Each entry was cropped to its first 800 chars.
   A long entry retrieved by relevance could correctly match but feed the model
   the wrong 800 chars.
5. **No cross-turn carry-forward.** Each turn re-retrieved fresh; passages cited
   in the previous answer were not guaranteed to return.
6. **Crude multi-turn query.** Retrieval used `original_question + "\n" + latest`;
   the middle of the conversation was invisible to retrieval and a drifted thread
   still anchored on turn 1.

## Why approach C (hybrid router + bounded tool-use)

Three approaches were on the table:

- **A — pure agentic.** Give the answerer unrestricted tool access. Unbounded
  cost and latency; harder to reason about grounding guarantees.
- **B — pure router, separate specialist.** Full independent pipelines per intent.
  High duplication; lookup regression risk if the lookup path is forked.
- **C — hybrid router + bounded tool-use.** A single intent-classification call
  dispatches to thin handler wrappers over existing `QueryService` methods. Lookup
  (the common path) is unchanged; novel paths add exactly one structured query each.
  One bounded `search_again` re-retrieval hop on the answerer covers the carry-
  forward gap without free-range agentic loops.

Approach C preserves bounded cost and latency (a Haiku JSON call + at most one
extra hybrid search per reply), keeps the strict grounding contract intact across
all intents, and makes the floor the previous behavior — any handler error falls
back to the lookup path.

## New components

- `src/journal/providers/intent_classifier.py` — `IntentClassifier` Protocol,
  `HeuristicIntentClassifier` (offline regex, the fallback), `AnthropicIntentClassifier`
  (Haiku, primary), `build_intent_classifier` factory. Mirrors `query_classifier.py`.
- `src/journal/services/conversations/handlers.py` — `LookupHandler`,
  `AggregateHandler`, `TemporalHandler`, `TrendHandler`, `ReplyOutcome`. Handlers
  depend only on `QueryService` + `Answerer` for testability.
- `src/journal/services/conversations/passages.py` — pure functions:
  `window_passage` (matched-chunk truncation), `select_passages` (adaptive band
  selection), `build_citations` (id → citation dict).

`ConversationService` (already a package) now imports the handlers and classifier.
`mcp_server/bootstrap.py` constructs all four handlers and passes them to the
service alongside the classifier.

## Tunable knobs

Compile-time constants in `services/conversations/handlers.py`:

| Constant | Value | Meaning |
|---|---|---|
| `_CANDIDATE_POOL` | 20 | Hybrid-search candidates before adaptive trim |
| `_PASSAGE_FLOOR` | 3 | Minimum passages after selection |
| `_PASSAGE_CEILING` | 15 | Maximum passages after selection |
| band | 0.5 | Score band relative to top score |
| re-retrieval hops | 1 | Maximum `search_again` calls the answerer may make |

These are starting values — the band and floor in particular are likely
candidates for tuning once real-traffic evals are available.

## Classifier notes

The `HeuristicIntentClassifier` (offline regex) is the **fallback only**. The
primary is `AnthropicIntentClassifier` (Haiku), which also emits a context-aware
`search_query` — fixing weakness #6 (crude multi-turn query) in the same single
call with no extra round-trip.

## Temporal simplification

The temporal handler hardcodes `sort="date_asc"` to bring the earliest evidencing
entry to the front. A `date_desc` variant for "when did X stop?" is not yet
implemented — the ascending sort is the dominant case and the simplification is
acceptable for v1. This is a known gap.

## Spec / docs

- Spec: `docs/superpowers/specs/2026-06-17-conversations-retrieval-robustness-design.md`
  — status flipped to `implemented 2026-06-18`.
- `docs/conversations.md` — Reply flow section replaced with classify→dispatch→fallback
  model; config table updated with handler knobs.
