# Code Quality Refactor Plan (v2)

Implementation plan for the 2026-05-07 codebase review's recommendations, revised after a
code-grounded review of v1. v1 was written without reading the actual files; v2 incorporates
structural realities (e.g. `api.py`'s closure-over-`services_getter` shape, additional reach-ins,
`entity_extraction.py`'s actual line count and dependency graph).

See [`code-quality-principles.md`](code-quality-principles.md) for the standards each unit is
moving the codebase toward.

## Sequencing overview

| # | Unit | Session budget | Depends on |
|---|---|---|---|
| 1a | Mechanical `api.py` split into `register_*` chain | 2 sessions | — |
| 1b | Expose public accessors on `QueryService`/`IngestionService`; remove `api.py` private reach-ins | 1 session | 1a |
| 2 | `EntityExtractionService` scratch-state + file split | 2 sessions | — |
| 3 | Decouple `JobRunner` from `reembed_entity_for_description` | 0.5 session | 2 |
| 4 | Split `services/jobs.py` along worker types | 1.5 sessions | 3 |
| 5 | Chroma integration test in CI | 1 session | — (parallelisable) |
| 6 | Test private-state cleanup | 0.5 session | opportunistic |
| 7 | `.venv` shebang doc fix | 15 min | tack onto any |

Total: ~8.5 focused sessions plus tack-ons. v1 was 4.5 sessions and missed several issues; the
budget grew because the code is more entangled than v1 assumed, not because scope expanded.

---

## Pre-flight: implicit-state grep

Before starting Unit 2, run a 5-minute grep across `src/journal/services/` for sibling instances
of per-call scratch attributes (anything matching `self\._current_` or similar transient state
set on `self` between calls). Catalog any hits and address them in Unit 2 alongside
`_current_candidate_*`. This gates Unit 2's scope; do not skip.

---

## Unit 1a — Mechanical api.py split

**Goal.** Turn `src/journal/api.py` (3171 lines) into a package `src/journal/api/` with one file
per resource, preserving behaviour exactly.

**Structural reality.** All routes are nested closures inside one
`register_api_routes(mcp, services_getter)` function (line 258). They capture `services_getter`
and an inner `_require_services` helper. They cannot just be moved to sibling files and re-exported
— they need access to the captured services.

**Approach.**

1. Create `src/journal/api/` package with `__init__.py` exposing one top-level
   `register_api_routes(mcp, services_getter)`.
2. For each resource, create a `register_<resource>_routes(mcp, services_getter)` function in its
   own module under `src/journal/api/`. Resource modules — grounded in the actual route inventory,
   not the speculative list from v2's first draft (which named `auth.py`/`media.py`/`query.py`
   without checking the file): `_shared.py`, `entries.py`, `ingestion.py`, `settings.py`,
   `users.py`, `notifications.py`, `health.py`, `dashboard.py`, `search.py`, `jobs.py`,
   `entities.py`. Eleven modules total.
3. The top-level `register_api_routes` calls each `register_*_routes` in sequence.
4. Hoist genuinely shared helpers (`_entry_to_dict`, `_entity_summary`, `_entity_detail`,
   `_mention_dict`, `_relationship_dict`, `_job_to_dict`, `_entry_summary`, `_chunk_match_dict`,
   `_search_result_dict`, `_pricing_to_dict`, `_runtime_get`, `_convert_heic_to_jpeg`, the cached
   `_token_encoder`) into `src/journal/api/_shared.py`. Each helper is a free function — no
   closure capture inside `_shared.py`. The legacy `_require_services()` wrapper in `api.py` is
   a no-op around `services_getter()` and is dropped during the split; resource modules call
   `services_getter()` directly.
5. **Routing rule — primary resource (URL prefix root).** Default placement: a route under
   `/api/<resource>/...` belongs in `<resource>.py`. The handler can still call across services.
6. **Routing rule — responsibility override (deviation, documented).** Write/job-creation routes
   live in `ingestion.py` regardless of URL prefix. Concretely: `/api/entries/ingest/*`,
   `/api/entities/extract`, and `/api/mood/backfill` all sit in `ingestion.py`. Rationale: strict
   URL-prefix routing pushes ~530 lines of ingestion handlers into `entries.py`, ballooning it past
   the 600-line cap and mixing read/CRUD handlers with long-running, job-spawning workflows that
   share dependencies (`IngestionService`, `JobRunner`, OCR/transcription providers) the read
   side never touches. The rule going forward: **if a new route's primary effect is "create a
   job that does work", it goes in `ingestion.py`** — even if its URL nests under another
   resource. Encode this rule as a module docstring at the top of `ingestion.py` and reference
   it from `_shared.py` so future agents see it on first read.
7. Move handlers in groups, one resource per commit. Run the full test suite after each.

**Definition of done.**

- All existing tests pass without modification.
- No file in `src/journal/api/` exceeds ~600 lines, with one acknowledged exception:
  `entities.py` may sit at ~660 lines after Unit 1a; a follow-up split into
  `entities.py` + `entity_merge.py` (or similar) is parked until growth pressure forces it.
  Recorded explicitly so it is not silently dropped.
- `mcp_server.py` still calls `register_api_routes(mcp, services_getter)` — public interface
  unchanged.
- `src/journal/api.py` is gone (or reduced to a thin re-export if downstream imports demand it,
  in which case file a follow-up to clean up callers).
- The responsibility-override rule (§6 of Approach) is encoded as a docstring on `ingestion.py`
  and referenced from `_shared.py`.

**Estimate.** 2 sessions. Closure-chain wiring takes most of session 1; resource moves take
session 2.

**Note on v1.** v1 framed this as "create `__init__.py` re-exporting routes." That framing was
wrong — there's nothing to re-export at the route level. The routes exist only as side effects
of calling `register_*` against a `mcp` instance.

---

## Unit 1b — Remove api.py reach-ins into private service state

**Goal.** Eliminate the ~47 instances of `api.py` reaching into `QueryService._repo`,
`QueryService._vector_store`, `IngestionService._repo`, `IngestionService._store_source_file`,
etc. This is the same anti-pattern Unit 3 targets for `JobRunner`, applied at scale — and the
principles doc names it as a top-priority agent-friendliness issue.

**Approach.**

1. After Unit 1a completes, catalog every `_`-prefixed attribute access from `src/journal/api/`
   modules into service objects. Group by intent (read entry by id, store file, query vector
   store, …).
2. For each access pattern, add a corresponding public method on the relevant service. Prefer
   named methods that describe the operation; avoid exposing repos/clients wholesale via a
   `repo` property unless there's no cleaner option.
3. Update `api/` callers to use the public methods.
4. Add direct unit tests for the new public methods (some are likely covered transitively
   already; this lifts coverage and pins behaviour).

**Definition of done.**

- Zero `_`-prefixed attribute access from `api/` modules into service objects.
- All tests pass.
- Each new public method has direct test coverage.

**Estimate.** 1 session. Mechanical once Unit 1a's resource split makes the call sites visible
and naturally clusters them.

---

## Unit 2 — EntityExtractionService: scratch state + file split

**Goal.** Eliminate `_current_candidate_ids` / `_current_candidate_embeddings`; split the
1189-line file along its responsibilities.

**Approach.**

1. **Run the pre-flight implicit-state grep first.** Address any siblings inside this unit.
2. **Scratch state.** Thread `candidates` and `embeddings_by_id` as explicit parameters through
   the call chain: `extract_from_entry` (line 405) → `_resolve_entity` → `_try_llm_asserted_match`
   (line 956). The params thread **two levels deep** — both signatures change. Delete
   `self._current_candidate_*` (lines 272–273, 430–435). Run tests.
3. **File split.** Create `src/journal/services/entity_extraction/` package with modules along
   responsibilities:
   - `extraction.py` — LLM extraction call
   - `dedup.py` — three-stage dedup
   - `signature.py` — signature heuristic
   - `sanity.py` — post-extraction sanity sweep
   - `matching.py` — stage-0 LLM-asserted matching
4. **Helpers needing multiple service deps.** `_canonical_name_supported` (line 1130, called by
   the sanity sweep at line 682) needs both `self._store` and `self._repo`. Two viable shapes:
   (a) keep it as a method on `EntityExtractionService` and have `sanity.py` call it; or
   (b) pass a small `ExtractionContext` dataclass holding `store` and `repo` to extracted
   helpers. Pick whichever yields fewer lines of glue. Default to (a) unless multiple helpers
   would benefit.
5. **Orchestrator.** `EntityExtractionService` stays in the package's `__init__.py` (or
   `service.py`) and orchestrates the helpers. `extract_from_entry` remains the orchestrator
   and stays relatively large; bulk savings come from helpers moving out.

**Definition of done.**

- No file in `services/entity_extraction/` over ~500 lines.
- No implicit `self.` state between method calls (no per-call scratch attributes).
- All tests pass.
- Implicit-state grep results documented in commit message; siblings (if any) addressed.

**Estimate.** 2 sessions. Session 1: scratch state + grep. Session 2: file split.

**Note on v1.** v1 said "convert `_resolve_entity` to take params" — true but incomplete; the
threading goes one more level. v1 also said "~970 lines" — actual is 1189.

---

## Unit 3 — Decouple JobRunner from reembed_entity_for_description

**Goal.** Remove `JobRunner`'s direct dependency on
`EntityExtractionService.reembed_entity_for_description` (the reach-in flagged in the original
review). Do **not** attempt to remove the entire `EntityExtractionService` import — `JobRunner`
also calls `extract_from_entry` (line 709) and `extract_batch` (line 717), which are normal
service interactions, not reach-ins. Decoupling those would add boilerplate without meaningful
benefit.

**Approach.**

1. Define `EntityReembedder` Protocol with one method: `reembed_entity_for_description(...)`
   matching the existing signature.
2. `JobRunner` constructor accepts an `EntityReembedder` parameter alongside the existing
   `EntityExtractionService` dependency.
3. Production wiring in `mcp_server.py`: pass `entity_extraction_service` for both — it satisfies
   both interfaces.
4. Tests for `_run_entity_reembed` inject a fake `EntityReembedder`.

**Definition of done.**

- `JobRunner._run_entity_reembed` (line 788) calls the injected `EntityReembedder`, not
  `self._extraction.reembed_entity_for_description`.
- All tests pass.

**Note on v1.** v1's DoD said "JobRunner has no import from `services.entity_extraction`." That
DoD is unachievable without three Protocols; the import legitimately stays for the other two
calls. v2 narrows the DoD to the actual reach-in.

**Estimate.** 0.5 session.

---

## Unit 4 — Split services/jobs.py along worker types

**Goal.** `services/jobs.py` is ~1261 lines — named in the original review as a monolith
alongside `api.py` and `entity_extraction.py`, but missed in v1 of the plan. Split it.

**Approach.**

1. Audit the file to enumerate worker types (entity extraction, entity batch extraction, entity
   reembed, ingestion, mood scoring, etc.). The audit is the first ~30 minutes; do not skip it.
2. Create `src/journal/services/jobs/` package:
   - `runner.py` — `JobRunner` class + job dispatch
   - one `<type>_worker.py` per worker (e.g. `extraction_worker.py`, `reembed_worker.py`,
     `ingestion_worker.py`)
   - `terminal_state.py` — shared "terminal state guarantee" helper if extracted (the
     `mark_succeeded` / `mark_failed` invariant the review praised)
3. Workers receive their dependencies (services, repo) via constructor or via `JobRunner`
   injection — no implicit `self._extraction` reach inside worker bodies.
4. `JobRunner` orchestrator stays in `runner.py` and dispatches to worker functions/classes.
5. Preserve the documented `max_workers=1` invariant; carry the docstring to `runner.py`.

**Definition of done.**

- No file in `services/jobs/` over ~500 lines.
- Worker functions are individually unit-testable (called directly without going through
  `JobRunner`).
- All tests pass.
- The `max_workers=1` rationale docstring is preserved verbatim.

**Estimate.** 1.5 sessions. Session 0.5: audit + pattern decision. Session 1: mechanical moves.

---

## Unit 5 — Chroma integration test in CI

**Goal.** Lift `vectorstore/store.py` `ChromaVectorStore` coverage from 68% → 90%+ via a
real-container integration test. The class has five public methods (`add_entry`, `search`,
`delete_entry`, `count`, `get_chunks_for_entry`) — all five must be exercised.

**Approach.**

1. **Healthcheck the container.** `ChromaVectorStore.__init__` connects synchronously (line 56–59
   of `store.py`); there is no lazy-connect path. CI must use `healthcheck:` on the ChromaDB
   service container, and the pytest fixture must wait for healthy state before constructing
   `ChromaVectorStore`.
2. **Marker registration.** Check whether pytest is run with `--strict-markers` (look in
   `pyproject.toml [tool.pytest.ini_options]`). If yes, register `integration` in the markers
   list before adding `@pytest.mark.integration`.
3. **Test container.** Add `docker-compose.test.yml` with ChromaDB + healthcheck, OR use
   `testcontainers-python`. Pick one; document why.
4. **Integration test file.** Exercise all five public methods end-to-end against a live Chroma:
   write embeddings, query by vector, delete, re-query, count. Cover error paths (empty results,
   missing entries) where reachable.
5. **CI workflow.** Add a job that runs `pytest -m integration` after the unit suite, using a
   service container. Use `gh run watch` to confirm CI green on first run.
6. **Docs.** Document the marker in `docs/development.md` so devs can skip with
   `pytest -m "not integration"`.

**Definition of done.**

- Integration test runs locally and in CI.
- Coverage on `vectorstore/store.py` ≥90%.
- CI green.
- `docs/development.md` documents the marker.

**Estimate.** 1 session (mostly CI plumbing).

**Parallelisable.** Unit 5 has no code-level dependencies on Units 1–4. Can run in parallel if
you have spare context.

---

## Unit 6 — Tidy private-state test reaches

**Goal.** Remove `_client`, `_conn`, and similar underscore-prefixed attribute access in tests.

**Approach.**

1. `grep -rE '\._[a-z]' tests/` to find candidates.
2. For each: refactor to use the existing Protocol-based fake at the seam, OR add a small public
   accessor to the production class.
3. Bundle into a single PR; pure test refactor.

**Definition of done.**

- No `_`-prefixed attribute access from test files into production objects.
- All tests pass.

**Estimate.** 0.5 session.

---

## Unit 7 — Fix .venv shebang foot-gun

**Goal.** Document the `journal-server` → `server` rename foot-gun so future devs and agents
don't burn time on stale `.venv` shebangs after a directory rename.

**Approach.** Add a callout to `docs/development.md` describing the symptom and the fix
(`rm -rf .venv && uv sync`). Optionally a `scripts/bootstrap.sh` that does the same.

**Estimate.** 15 minutes. Tack onto any unit's PR.

---

## Out of scope — parked for a future round

The principles doc threshold is "smell at 800 lines, problem at 1500." These files exceed the
smell threshold but were not in the original review and are excluded from this plan:

- `services/ingestion.py` (~978 lines) — over the smell threshold.
- `entitystore/store.py` (~1074 lines) — over the smell threshold.
- `cli.py` (~1621 lines) — over the problem threshold; CLI files are conventionally inline-style,
  but this is past comfort for agent context windows.

Park, schedule a separate planning round once Units 1–7 land. If any of these are touched
incidentally during the units above, opportunistic splits are fine.

---

## Standing rules per unit

- New session per unit. Fresh context. Each session loads only this plan, the principles doc,
  and the unit's target files.
- Run full test suite locally before committing.
- Push immediately, watch `gh run watch`, do not start the next unit until CI is green.
- For any bug found mid-refactor: write a failing test first, then fix.
- Update `docs/` in the same PR if the change affects documented behaviour.
- After each unit lands, mark the corresponding recommendation as done in the
  `review_codebase_2026-05-07` auto-memory entry.

---

## Provenance

- **v1 (2026-05-07, morning).** Written conversationally without reading the codebase. Captured
  intent but missed several structural realities.
- **v2 (2026-05-07, afternoon, this doc).** Revised after a code-grounded review. Changes from v1:
  1. Unit 1 split into 1a (mechanical) and 1b (remove reach-ins). v1 framed the split as
     "re-export routes" — wrong. Reality is a `register_*(mcp, services_getter)` chain.
  2. Unit 1b is new. v1 missed ~47 reach-ins from `api.py` into private service state.
  3. Unit 2 budget doubled (1 → 2 sessions). v1 underestimated the param-threading depth and
     the multi-dependency helpers.
  4. Unit 3 DoD reworded. v1 said "no import from `services.entity_extraction`" — unachievable
     without three Protocols. v2 narrows to the one actual reach-in.
  5. Unit 4 is new. v1 missed `services/jobs.py` despite the original review naming it.
  6. Unit 5 expanded to cover ChromaDB healthcheck and `--strict-markers` registration.
  7. Pre-flight implicit-state grep added.
  8. Out-of-scope section added explicitly so `ingestion.py`, `entitystore/store.py`, and
     `cli.py` aren't silently dropped.
