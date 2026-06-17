# Conversations Retrieval Robustness — Design

**Date:** 2026-06-17
**Status:** draft (design approved, spec under review)
**Scope:** `server/` — conversation/chat reply path and its retrieval.

## Problem

The `/conversations` feature answers natural-language questions about a user's
journal. Today every reply (`ConversationService.reply`,
`services/conversations.py:71`) does one thing: a fixed top-8 hybrid search,
truncates each result to the first 800 chars, and hands those passages to the
multi-turn answerer. That single mechanism is excellent for **lookup** questions
("what did I say about Rome?") but is structurally a poor fit for other question
shapes the chat UI invites:

1. **Aggregate / coverage questions** ("how many times did I mention my back?")
   are answered from an 8-entry *sample*. The grounding prompt prevents
   fabrication of facts but cannot detect "I only saw 8 of 40 relevant entries",
   so the model answers confidently and undercounts. **Highest-impact gap.**
2. **Temporal-origin questions** ("when did the back pain start?") rank by
   *relevance*, not date — the earliest entry has no reason to land in the top 8,
   so "when did X start" is systematically unreliable. The system prompt's
   "lead with the earliest passage" instruction cannot fix a recall problem.
3. **Fixed top-8** is both a recall ceiling (broad questions truncated) and
   occasional noise (narrow questions padded with marginal entries).
4. **Naive truncation** takes the *first* 800 chars (`conversations.py:100`), not
   the chunk that matched — a long entry can be retrieved correctly yet feed the
   model the wrong 800 chars.
5. **No cross-turn passage carry-forward** — each turn re-retrieves fresh;
   passages the previous answer cited are not guaranteed to return.
6. **Crude multi-turn query** — retrieval uses `original_question + "\n" + latest`
   (`conversations.py:85`); the middle of the conversation is invisible to
   retrieval, and a drifted thread still anchors on turn 1.

## Strengths to preserve (non-negotiable)

The redesign must not regress these:

- **Hybrid retrieval quality** — BM25 + dense + RRF + Haiku rerank
  (`services/hybrid.py`). Best-in-class for lookup; untouched.
- **Strict grounding contract** — "answer only from the provided material, cite
  entry IDs, set `answered:false` if you can't" (`providers/answerer.py`).
- **Bounded, predictable cost & latency** — independent of journal size.

## Approach: hybrid router + bounded tool-use (approach "C")

A new intent-classification + handler-routing layer sits in front of the
answerer. The new paths are **strictly additive**: anything that fails or is
uncertain falls back to today's `lookup` behavior, so the floor is the current
system, never worse.

### Correction to prior framing

There is **no existing stats router** in `query.py`. `QueryService`
(`services/query.py`) is a flat facade that *exposes* the structured methods we
need but does not classify questions or pick among them. This work **builds the
classifier**; the handlers it routes to are thin wrappers over methods that
already exist (`get_topic_frequency`, `count_entries`, `get_mood_trends`,
`get_entries_by_date`, `list_entries`, `get_mood_entity_correlation`, and
`search_entries`'s existing `start_date`/`end_date`/`sort` params).

### Intent classifier

One cheap Haiku tool-use call per message (same pattern as `mood_scorer`,
`reranker`, `formatter`). Input: the question plus, for follow-ups, the
conversation context. Output (structured): an `intent`, extracted parameters
(topic/entity, date range, mood dimension), and — for follow-ups — a condensed
standalone `search_query` (fixes weakness #6, rides this same call, no extra
round-trip).

| Intent | Example | Handler uses |
|---|---|---|
| `lookup` | "what did I say about Rome?" | today's hybrid+grounded path, unchanged |
| `aggregate` | "how many times did I mention my back?" | `get_topic_frequency` / `count_entries` |
| `temporal` | "when did the back pain start?" | `search_entries(sort="date_asc")` + date filter |
| `trend` | "have I gotten happier this year?" | `get_mood_trends` / `get_mood_entity_correlation` |

### Handlers

Each handler fetches the *right* structured facts, assembles a grounded context
block (numbers + supporting dated entries), and calls the answerer with the
**same grounding contract**. The grounding strength is preserved; only the
material changes — a count, a date-sorted series, or the earliest entries
instead of 8 relevance samples.

- `aggregate` — returns the count/frequency plus a sample of supporting entries
  as citations. The answer leads with the number; citations let the user verify.
- `temporal` — retrieves with `sort="date_asc"` (and `date_desc` for "when did X
  stop"), optionally date-filtered, so the earliest/latest evidencing entry is
  guaranteed present.
- `trend` — returns the trend series (e.g. weekly mood) plus representative
  entries from the endpoints of the period.

### Lookup-path quality fixes

Apply only to the `lookup` handler (other handlers return targeted sets already):

- **Adaptive passage count (#3).** Retrieve a larger candidate set (20), include
  passages whose rerank score is within a relative band of the top score,
  clamped to **`[3, 15]`**. Narrow questions take 3–4; broad ones take more; the
  clamp bounds cost.
- **Matched-chunk truncation (#4).** Replace `r.text[:800]` with an 800-char
  window *centered on the matched span* — locate via `matching_chunks` (dense)
  or the FTS5 `snippet` offset (BM25); fall back to head-truncation when neither
  is present. Same char budget, correct 800 chars.

### Bounded re-retrieval (#5 carry-forward)

On the `lookup` path, give the answerer a single `search_again(query)` tool. If
mid-answer it finds the passages insufficient (common when a follow-up leans on
earlier context), it may issue **exactly one** reformulated retrieval; we run it,
append results, and let it finalize. Capped at one hop — worst case stays 2 LLM
calls + 2 retrievals. Fixes carry-forward without going open-loop.

## Error handling & cost ceiling

- Classifier failure / low confidence → `lookup` (today's behavior).
- Any handler's structured query erroring → fall back to `lookup`, not an error.
- `search_again` unused or erroring → finalize with passages in hand.
- `answered:false` discipline retained on every path.

**Documented worst case per message:** 1 Haiku classify + 1 answerer call +
(lookup only) ≤1 extra retrieval + ≤1 extra answerer call. Predictable upper
bound.

## Components & boundaries

- **`IntentClassifier`** (new) — question (+context) →
  `(intent, params, search_query)`. A Protocol plus a Haiku tool-use adapter in
  **`providers/`**, alongside the other auxiliary Haiku components
  (`mood_scorer`, `reranker`, `formatter`). Swappable and stubbable like them.
- **Handlers** (new) — one per non-`lookup` intent, in a new
  **`services/conversations/` package** (today's single `conversations.py` module
  becomes that package: `service.py` for `ConversationService`, `handlers.py`,
  `passages.py`). Each handler depends only on `QueryService` + `Answerer`. Pure
  orchestration, independently testable with stubbed `QueryService` returns.
- **`ConversationService.reply`** (modified) — becomes: classify → dispatch to
  handler → persist. The current retrieval body moves into the `lookup` handler.
- **Passage windowing helper** (new, pure function) — `SearchResult` → centered
  800-char passage. No I/O; trivially unit-tested.
- **`continue_conversation`** (modified, `providers/answerer.py`) — gains the
  optional single `search_again` tool on the lookup path.

`QueryService`, `HybridSearchService`, and the rerank/embedding providers are
**unchanged**.

## Testing

Per the repo's write-failing-test-first workflow, each weakness gets a failing
test first, then the fix:

- **Classifier:** question→intent table with the Haiku call stubbed; asserts
  intent + extracted params + condensed `search_query`.
- **Each handler:** stubbed `QueryService` returns; asserts the correct method is
  called with the correct params and the grounded context is assembled correctly.
- **Adaptive count:** synthetic `SearchResult` score distributions → asserts band
  selection and `[3,15]` clamp.
- **Matched-chunk truncation:** synthetic results with dense/BM25/neither →
  asserts the window centers on the match and falls back to head-truncation.
- **Bounded tool-use:** simulate the model calling `search_again` twice → assert
  capped at one.
- **Fallbacks:** classifier error, handler-query error → assert `lookup` path runs
  and a grounded answer still returns.
- **Regression:** `intent=lookup` reproduces current behavior where feasible.

## Out of scope

- Changes to the underlying hybrid/rerank/embedding pipeline.
- Fully-agentic (open-loop, unbounded tool-use) answering — explicitly rejected
  to preserve bounded cost and the grounding contract.
- Persisting retrieved passages across turns in the DB (the one-hop
  `search_again` covers the carry-forward need without new schema).

## Tunable knobs (starting values)

- Adaptive passage band: clamp `[3, 15]`, candidate set 20.
- Re-retrieval cap: 1 extra hop on the lookup path.
- Classifier model: Haiku 4.5 (consistent with existing auxiliary calls).
