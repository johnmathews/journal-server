# Search answer synthesis (opt-in RAG) — design

**Date:** 2026-06-16
**Status:** approved, pending implementation plan
**Scope:** cross-cutting — `server/` (REST + provider + service) and `webapp/` (store + view).

## Problem

Today `GET /api/search` runs a fixed hybrid pipeline (BM25 + dense → RRF → Haiku
listwise rerank) and returns a **ranked list of entries**. It never synthesizes an
answer. A natural-language question like *"when did my back start hurting?"* can at
best surface entries that mention back pain; the user has to read them and work out
the answer themselves.

Separately, punctuation in the query used to 400 (FTS5 syntax error) — fixed in a
prior commit (`db/repository/search.py::_to_fts_match_query`); not part of this design
but a prerequisite for questions to reach retrieval at all.

## Goal

Let the user ask a question and get a **synthesized, grounded, cited answer** —
**opt-in** (only generated when the user clicks), shown **above** the existing
results list, which keeps working exactly as today.

Decisions locked during brainstorming:

- **Trigger:** an always-visible "Answer this" button. The user decides when to pay
  the LLM cost; no auto-detection, no auto-answer.
- **Model:** `claude-sonnet-4-6` (adaptive thinking) — stronger temporal/synthesis
  reasoning than Haiku, which this feature lives or dies on.
- **Grounding:** strict. Answer only from retrieved entries; if they don't cover the
  question, say so plainly and never guess.

## Architecture

```
        webapp SearchView
   ┌──────────────┬───────────────────┐
   │ Search (GET) │ Answer this (POST) │
   └──────┬───────┴─────────┬──────────┘
          ▼                 ▼
 GET /api/search   POST /api/search/answer
   (unchanged)             │
                           ▼
                services/answer.py
                 ├─ reuse hybrid search (top-N entries, rides result cache)
                 ├─ build dated passages (entry_date + truncated text)
                 └─ providers/answerer.py  (Anthropic, Sonnet 4.6, structured output)
                           ▼
        {question, answer, answered, citations[], model}
```

Answer synthesis is a **separate POST endpoint**, not a flag on `/api/search`:
isolates per-query LLM cost to an explicit action, keeps the list endpoint fast and
cacheable, and gives a clean response shape.

## Server components

### 1. `providers/answerer.py` (mirrors `providers/reranker.py`)

- `@dataclass AnswerResult`: `answer: str`, `answered: bool`, `cited_entry_ids: list[int]`.
- `Answerer` Protocol: `answer(question: str, passages: list[AnswerPassage]) -> AnswerResult`.
  `AnswerPassage` carries `entry_id: int`, `entry_date: str`, `text: str`.
- `AnthropicAnswerer`:
  - `claude-sonnet-4-6`, `thinking={"type": "adaptive"}`.
  - **Structured output** via `output_config={"format": {"type": "json_schema", "schema": ...}}`
    returning `{answer, answered, cited_entry_ids}` — guarantees parseable, cited output
    without brittle text parsing. (Sonnet 4.6 supports structured outputs.)
  - System prompt: strict grounding — answer only from the numbered passages; cite the
    `entry_id`s used; for temporal questions identify the earliest relevant date; if the
    passages don't cover the question, set `answered=false` and return the fixed message
    *"I couldn't find anything about that in your journal."* Never invent memories.
  - Passage text truncated to a bounded length (≈600–800 chars) like the reranker, so the
    prompt stays bounded at N passages.
  - On `anthropic.APIError` / malformed output → raise `AnswerUnavailable`; the route maps
    it to `502 answer_unavailable`. (Unlike the reranker, we do **not** silently degrade —
    a fabricated or empty answer is worse than telling the user to read the list.)
- `NoopAnswerer`: returns `answered=false` with a "synthesis disabled" message; used when
  `ANSWER_PROVIDER=none` and in unit tests that don't mock the LLM.
- `build_answerer(name, *, anthropic_api_key, model)` — `none`|`anthropic`, fail-fast on
  unknown / missing key (matches `build_reranker`).

### 2. Config (`config.py`, surfaced under `/api/settings`)

| Env var | Default | Controls |
|---|---|---|
| `ANSWER_PROVIDER` | `anthropic` | `anthropic` enables synthesis; `none` disables (button still shows, returns the disabled message). |
| `ANSWER_MODEL` | `claude-sonnet-4-6` | Model used by `AnthropicAnswerer`. |
| `ANSWER_CONTEXT_ENTRIES` | `8` | Top-N entries fed to the answerer as context. |

### 3. `services/answer.py`

`answer_question(question, start_date, end_date, user_id) -> AnswerResponse`:
1. Call the existing hybrid search (`query_svc.search_entries` / hybrid) with
   `limit=ANSWER_CONTEXT_ENTRIES` — reuses the same retrieval + result cache.
2. If no entries: return `answered=false` with the "couldn't find" message; skip the LLM.
3. Build `AnswerPassage`s (entry_date + truncated text), call the answerer.
4. Resolve `cited_entry_ids` back to entries (drop ids the model invented that aren't in
   the candidate set) → `citations: [{entry_id, entry_date, snippet}]`.

### 4. Route (`api/search.py`)

`POST /api/search/answer` — body `{q, start_date?, end_date?}`, same bearer auth as
`/api/search`. Validation mirrors `/api/search` (`missing_query`, etc.).
Returns:

```json
{
  "question": "when did my back start hurting?",
  "answer": "Your back pain first appears on 2026-02-14, where you wrote …",
  "answered": true,
  "citations": [
    {"entry_id": 42, "entry_date": "2026-02-14", "snippet": "woke up with a stiff lower back …"}
  ],
  "model": "claude-sonnet-4-6"
}
```

Errors: `400 missing_query`; `502 answer_unavailable` (LLM error/malformed). The list
endpoint is unaffected by answer failures.

## Webapp components

- `types/search.ts`: `AnswerCitation` (`entry_id`, `entry_date`, `snippet`),
  `AnswerResponse` (`question`, `answer`, `answered`, `citations`, `model`).
- `api/search.ts`: `answerQuestion(params)` → `POST /api/search/answer` (JSON body).
- `stores/search.ts`: add `answer`, `answered`, `answerCitations`, `answerLoading`,
  `answerError`; action `runAnswer()` (uses current `query` + date filters). Any change to
  the query/filters or a new `runSearch` **clears** the answer so a stale answer never sits
  above fresh results.
- `SearchView.vue`:
  - Always-visible **"Answer this"** button beside Search (`data-testid="search-answer"`),
    disabled while answering or when the query is empty.
  - Answer panel **above** the results list:
    - loading → spinner + "Thinking…"
    - `answered=true` → prose answer + citation chips linking to `/entries/:id`
    - `answered=false` → the plain "couldn't find" message
    - `answerError` → "Answer unavailable — see results below."
  - 16px-on-mobile inputs already fixed; new controls reuse `text-base sm:text-sm`.

## Testing

- **Server:** `tests/test_providers/test_answerer.py` (structured-output parse, grounding
  `answered=false`, citation filtering of invented ids, API-error → `AnswerUnavailable`);
  `tests/test_services/test_answer.py` (no-results short-circuit, passage building,
  citation resolution) with a fake answerer; `tests/test_api.py::TestSearchAnswer` (200
  shape, `missing_query` 400, `answer_unavailable` 502). Answerer is faked/Noop in unit
  tests — no live LLM calls.
- **Webapp:** store test (mock `answerQuestion`: success, `answered=false`, error, cleared
  on new search); `SearchView.test.ts` (button renders + disabled states, click → action,
  renders answer + citation links, `answered=false` message, error state). Hold the 85%
  coverage gate.

## Docs & journal

- `server/docs/search.md`: new "Answer synthesis" section (endpoint, params, response,
  errors, config, grounding contract). `webapp/docs`: brief note on the Answer panel.
- Dated journal entries in both repos.

## Cost & latency

≈5–8¢ and ≈2–4s per click (Sonnet 4.6, ~8 entries of context). Paid only on click.

## Out of scope (YAGNI)

- Streaming the answer (full answer after a spinner is fine for v1).
- Auto-detecting questions / auto-answering.
- Conversational follow-ups / multi-turn.
- A `chunks_fts` index or retrieval changes beyond reusing the existing hybrid pipeline.
