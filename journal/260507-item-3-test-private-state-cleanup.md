# Item 3 — test private-state cleanup, round 2

Date: 2026-05-07

Closed item 3 from `docs/refactor-follow-ups.md` modulo a
documented residual. Total reach-in count: **254 → 66** across five
commits (parts A–E).

## Per-category outcome

Every category in the original snapshot resolved to either zero or a
small justified set. See the doc's item 3 table for the per-category
strategy and the residual classification.

| Pattern | Sites resolved | Strategy used |
|---|---:|---|
| `provider._client` | ~69 | `_make_provider()` returns `(provider, fake_client)` tuple |
| `ingestion_service._repo` | ~26 | Fixture split: separate `repo` fixture + `ingestion_service` that depends on it |
| `provider._primary` / `_fallback` / `_secondary` / `_shadow` | ~25 | Read-only `.primary` / `.fallback` / `.secondary` / `.shadow` properties on the wrapper providers |
| `scorer._client` | ~13 | `_make_scorer(client)` helper patches `anthropic.Anthropic` |
| `svc._build_success_message` | ~11 | Extracted to module-level `build_success_message` |
| `runner._ingestion` | ~10 | `runner_factory(ingestion=...)` kwarg |
| `rr._client` | ~8 | `_make()` returns `(rr, fake_client)` |
| `provider._model` | ~7 | `.model` property on every provider class that holds a model name |
| `_post_to_pushover` / `_resolve_credentials` | ~7 | Extracted to module-level `post_to_pushover` / `resolve_credentials` |
| `_vector_store` | ~5 | `IngestionService.vector_store` property (mirrors `QueryService.vector_store`) |
| `svc._is_topic_enabled` | 5 | Tests rewritten against the public `notify_*` surface |
| `poller._thread` | 4 | `HealthPoller.is_running()` + `wait(timeout=)` |
| `__new__` write-pattern leftovers | 4 | Replaced with patched-SDK construction through real `__init__` |

## Residual (~66 sites)

Four buckets, all justified or out of scope:

1. **Docstring text** (~7 in `provider._client`): the helper docstrings
   explicitly mention the old reach-in pattern they replaced.
2. **Production reach-in mirror** (~22): `services/reload.py` writes
   `services["ingestion"]._ocr` etc. directly to hot-swap providers.
   Tests assert via the same attributes. New follow-up — item 7 in
   `docs/refactor-follow-ups.md` — covers the production-side fix.
3. **Tests of legitimately internal state** (~6): `_jobs`, `_executor`,
   `_services`, `_init_services` mid-test inspection.
4. **One-off singletons** (~5): `runner._extraction`, `store._client`,
   etc. — single accesses surrounded by otherwise-clean tests.

## Decisions worth remembering

- **Pattern: factory returns `(obj, fake_client)` tuple.** Every test
  file that previously did `provider = _make_provider()` then
  `provider._client.X = ...` now does
  `provider, client = _make_provider()` then `client.X = ...`. The
  `_make_provider` body wraps the SDK class patch and explicitly
  passes `return_value=fake_client` so the constructor's
  `self._client = anthropic.Anthropic(...)` line picks up the fake.
  This is the cleanest way to support construction-time DI without
  changing the production constructor signatures.
- **Pattern: extract pure functions to module level.** Helpers that
  don't really need instance state (`build_success_message`,
  `post_to_pushover`, `resolve_credentials`) became module-level
  functions next to the existing `build_pipeline_failure_body`. The
  class methods stayed as thin shims so internal callers don't
  churn. Tests import the module function directly.
- **`__new__` bypass is a smell, not a workaround.** Several tests
  used `Provider.__new__(Provider)` followed by manual attribute
  assignment to dodge real-SDK construction. The fix in every case
  was to patch the SDK class and call the real `__init__` — same
  outcome, no half-initialised objects, no hand-set private state.
- **Don't lump production-mirror reach-ins with the test cleanup.**
  When the pattern under test *is* the production code's pattern
  (`ingestion._ocr`, `ingestion._transcription`, …), the right move is
  to fix production first. Trying to clean up only the tests papers
  over the real shape of the issue. Logged as item 7 in
  `docs/refactor-follow-ups.md`.

## Verification

- `uv run pytest -m 'not integration'` → 1796 passed (1794 baseline +
  2 net from category rewrites).
- `uv run ruff check src/ tests/` → clean.
- Per-category reach-in count is 0 or in the documented residual
  buckets.
