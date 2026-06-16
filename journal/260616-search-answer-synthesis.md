# 260616 — search answer synthesis: opt-in grounded synthesis from hybrid results

Adds a new `POST /api/search/answer` endpoint that synthesizes a short, grounded, cited
answer from the hybrid-search top-N (default 8) using `claude-sonnet-4-6` with adaptive
thinking. Strict grounding: answers only from retrieved passages; returns `answered: false`
with "I couldn't find anything about that in your journal." when not covered; never
fabricates. On LLM error or malformed output, raises `AnswerUnavailable` and the route
returns 502.

## Endpoint

`POST /api/search/answer` — JSON body `{q, start_date?, end_date?}`, same bearer auth as
`GET /api/search`. Opt-in and separate from the cached search GET so the LLM cost is only
paid on demand. Response shape:

```json
{
  "question": "...",
  "answer": "...",
  "answered": true,
  "citations": [{"entry_id": 42, "entry_date": "2025-03-01", "snippet": "..."}],
  "model": "claude-sonnet-4-6"
}
```

## Provider: `src/journal/providers/answerer.py`

Mirrors `providers/reranker.py`. Key types:

- `Answerer` — Protocol with a single `answer(question, passages)` method.
- `AnswerPassage(entry_id, entry_date, text)` — one retrieved entry offered as grounding.
- `AnswerResult(answer, answered, cited_entry_ids)` — synthesized answer.
- `AnswerUnavailable` — raised on API error or malformed output; route maps to 502.
- `AnthropicAnswerer` — default model `claude-sonnet-4-6`, `max_tokens=1024`, **adaptive
  thinking** (`thinking={"type": "adaptive"}`), system prompt marked `cache_control`.
  Lenient JSON parse finds first `{` / last `}`. Keys: `answer`, `answered`,
  `cited_entry_ids`. Cited ids are coerced to int and validated against the passage set.
- `NoopAnswerer` — always returns `answered=False`; used when `ANSWER_PROVIDER=none`.
- `build_answerer(name, *, anthropic_api_key, model)` — factory; unknown names raise.

## Service: `src/journal/services/answer.py`

`AnswerService` reuses `QueryService.search_entries` (limit=`ANSWER_CONTEXT_ENTRIES`,
default 8). Short-circuits to a no-match result WITHOUT an LLM call when there are no
search results. Builds dated passages with text truncated to ~800 chars. Resolves cited
ids back to entries and builds `AnswerCitation(entry_id, entry_date, snippet)` where
snippet is entry text truncated to 160 chars.

## Config

- `ANSWER_PROVIDER` — `anthropic` | `none`, default `anthropic`.
- `ANSWER_MODEL` — default `claude-sonnet-4-6`.
- `ANSWER_CONTEXT_ENTRIES` — default 8.

## Tests

- `tests/test_providers/test_answerer.py`
- `tests/test_services/test_answer.py`
- `TestSearchAnswer` in `tests/test_api.py`

## Cost and latency

~5–8¢, ~2–4s per click (adaptive thinking adds ~1s).
