# 260616 — search answer synthesis: opt-in grounded synthesis from hybrid results

Adds a new `POST /api/search/answer` endpoint that synthesizes a short, grounded, cited
answer from the hybrid-search top-N (default 8) using `claude-sonnet-4-6` with adaptive
thinking. Strict grounding: answers only from retrieved passages; returns `answered:false`
with "I couldn't find anything about that in your journal." when not covered; never
guesses. On LLM error it raises and the route returns 502.

## Why a separate POST endpoint (not a flag on GET /api/search)

- **Opt-in design:** synthesis is expensive (~5-8¢, ~2-4s per click) and not always wanted.
  A separate POST makes the cost/latency explicit to the user and the frontend. A
  search-flag approach would require the UI to show loading spinners and explain why
  results are slow when the user didn't ask for synthesis.
- **API decoupling:** search GET is cached and stateless; synthesis POST is stateful and
  expensive. Separate routes clarify the contract and allow independent rate-limiting or
  feature flags.
- **Error handling:** synthesis failure (API down, quota hit, malformed answer) is a 502
  and stops the synthesis panel. Results are still visible below. A flag on GET would
  taint the whole response.

## Strict grounding: contract and enforcement

Answers are constructed **only** from the top-N retrieved passages via the hybrid pipeline
(BM25 + dense + RRF + listwise rerank). The prompt:

1. Instructs Claude to synthesize an answer only using the given passages.
2. Explicitly forbids guessing or using background knowledge outside the passages.
3. Returns `answered: false` (not an error, not a guess) when the passages don't cover
   the query.

The response shape is `{ answered: bool, answer?: str, context_entries: list[id, title] }`.
If `answered: false`, the answer field is omitted and the UI shows the fallback message.

## Implementation: Answerer provider + AnswerService

Like the reranker, the Answerer is a Protocol interface (`providers/answerer.py`) with a
single implementation using Anthropic's SDK. It mirrors the reranker's JSON-in-text
parsing pattern: Claude returns a JSON object with `answered`, `answer`, and `context`
keys, delimited by triple backticks. The service (`services/answerer/`) wraps the
Answerer and orchestrates:

1. Call hybrid search with the user's query to get top-N passages.
2. Format passages into the prompt (entry title, text snippet, entry ID for linking).
3. Call the Answerer (Claude with adaptive thinking).
4. Parse the JSON response, validate fields, and return typed result.
5. On parse/validation failure, raise. The route catches it and returns 502.

The endpoint hands the answer + top-N entries to the frontend for rendering.

## Config knobs

Three env vars control behavior:

- `ANSWER_PROVIDER` — which provider to use (default `anthropic`, could be `disabled`
  for feature-flag opt-out).
- `ANSWER_MODEL` — which model to call (default `claude-sonnet-4-6`).
- `ANSWER_CONTEXT_ENTRIES` — how many top-N results to use for synthesis (default 8).

The `claude-sonnet-4-6` with adaptive thinking balances cost, latency, and reasoning quality
for journal queries.

## Cost and latency profile

- Cost: ~5–8¢ per synthesis call (Sonnet at ~0.0015¢/1k input tokens, 0.006¢/1k output,
  plus 20% thinking overhead).
- Latency: ~2–4s at peak Anthropic load (adaptive thinking adds ~1s).
- Per 100 journal searches, ~10–20 synthesis calls expected (user clicks "Answer this"
  ~10–20% of the time based on early feedback). Cost per month: ~$1–3 for typical use.

## Files touched

- `src/journal/providers/answerer.py` — Answerer Protocol + Anthropic implementation.
- `src/journal/services/answerer/` — AnswerService, orchestration, JSON parsing, validation.
- `src/journal/api/search.py` — `POST /api/search/answer` route (calls AnswerService,
  passes result + entries to frontend).
- Tests: `tests/test_providers_answerer.py`, `tests/test_services_answerer.py`,
  `tests/test_api_search.py` (new route tests).

## What's next

Frontend (webapp): Search page gets an always-visible "Answer this" button that calls
`POST /api/search/answer` and renders the synthesized answer + citation chips above the
results list, degrading to the results list on error.
