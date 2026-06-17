# 2026-06-17 — Cheap query classifier gates answer synthesis

So the webapp can auto-answer questions without a second click, the answer flow
now classifies the query first with a cheap model, and only questions reach the
(expensive) Sonnet answer step.

- New `providers/query_classifier.py`: `QueryClassifier` protocol +
  `AnthropicQueryClassifier` (Haiku 4.5, replies QUESTION/SEARCH, ~$0.0001/call)
  + `HeuristicQueryClassifier` (`?`/wh-word, no LLM). The Anthropic classifier
  falls back to the heuristic on any API error or unparseable reply, so a
  classifier hiccup never blocks search. Mirrors the `reranker`/`answerer`
  provider pattern.
- `AnswerService` takes the classifier and gates on it: a non-question returns
  immediately (`is_question=false`, empty answer, **no retrieval, no Sonnet
  call**); a question runs the existing grounded-answer path. `AnswerResponse`
  and the `POST /api/search/answer` payload now carry `is_question`.
- Config: `ANSWER_CLASSIFIER_MODEL` (default `claude-haiku-4-5`). `ANSWER_PROVIDER=none`
  uses the offline heuristic classifier and never synthesizes.

Cost: the classifier is ~$0.0001/search; the only real cost remains the Sonnet
answer, which now fires automatically for questions instead of on a click.
Retrieval inside the answer is a cache hit against the preceding `GET /api/search`.
