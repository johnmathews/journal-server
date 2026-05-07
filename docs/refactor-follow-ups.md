# Refactor follow-ups — open items after the v2 plan landed

A single punch list for sessions after the original `code-quality-refactor-plan.md`
(v2) closed out on 2026-05-07. **Load this doc first** in any new session that
plans to continue the refactor — it has the work list, pointers to the
historical context, and the per-session bootstrap.

This is a *living* document. Mark items resolved as you land them; add new
follow-ups here rather than scattering them across journal entries.

---

## How to use this doc

Three companion documents make up the canonical reference:

1. **`docs/code-quality-principles.md`** — standing rules. The "agent test",
   anti-patterns, and the api/ routing rules pair (default = primary URL
   resource; override = `ingestion.py` for write/job-creation routes).
   Read on every session that touches the api/ layer or designs a new split.
2. **`docs/code-quality-refactor-plan.md`** — historical sequence (v2). Units
   1a → 7. Useful for understanding *why* a given split is shaped the way it
   is. Don't re-execute units from this doc — they're done.
3. **This doc (`docs/refactor-follow-ups.md`)** — open work. The punch list.

Per-session journal entries under `journal/260507-*.md` record the decisions
and exceptions for each landed unit. They are the source of truth for "why
did we do X this way" — link back to them from new commits when relevant.

### Per-session bootstrap

For any new session continuing this refactor, start with:

1. Open *only* this doc plus the target file(s) for the chosen item.
2. Load `docs/code-quality-principles.md` if the work touches public API
   shape (services, api/, package layout).
3. Skim the relevant `journal/260507-*.md` entry for the unit that
   originally produced the file you're editing.
4. **Before recommending or doing anything**, run the standing
   verifications listed in this doc's "Standing facts" section to make
   sure the snapshot is still accurate.

---

## Open items

### 1. ~~Flake: `test_patch_text_queues_mood_scoring`~~ — RESOLVED 2026-05-07

Root cause was a within-call race in `submit_save_entry_pipeline`: each
child's `executor.submit` happened before the API thread finished its
own SQLite writes (additional `_jobs.create` calls + `mark_succeeded`
on the parent), so the worker thread started writing through the
shared `check_same_thread=False` connection while the request thread
was still mid-write. Fix: stage every child row up front, call
`mark_succeeded` on the parent, then dispatch all children to the
executor as a final step. Verified with
`pytest --count=1000` (was ~5/100 failures pre-fix). New
deterministic regression test asserts pipeline `mark_succeeded`
precedes every `executor.submit`. See
`journal/260507-item-1-save-pipeline-race-fix.md`.

### 1.1 Cross-call connection-sharing race (newly surfaced)

While reproducing item 1, a 20× PATCH loop test exposed a separate
issue: iteration N's API-thread writes can race iteration N-1's
worker-thread writes on the shared connection (the worker is still
draining when the next PATCH lands). The within-call fix does not
address this — it is a property of "one SQLite connection shared by
two writer threads", which the runner module's docstring claims is
safe but isn't.

**Goal:** Decide whether to fix structurally (write lock around the
connection, or connection-per-thread) or to accept the residual risk
and update the runner docstring to stop claiming safety it doesn't
provide.

**Symptom:** `sqlite3.OperationalError: not an error` raised from any
write that interleaves with another thread's write. Not currently
observed in production logs but not implausible under load.

**Session size:** Half-day if structural fix; 15 minutes if the
decision is "accept and document".

**Pointer:** `journal/260507-item-1-save-pipeline-race-fix.md` § "Out of scope".

---

### 2. `services/jobs/runner.py` — worker-class extraction

**Where:** `src/journal/services/jobs/runner.py` (1163 lines as of
2026-05-07).

**What landed in Unit 4:** File split into a package; `errors.py` and
`validation.py` extracted; `_friendly_error` / `_is_transient` /
`_validate_params` / `*_KEYS` constants moved out and renamed without
the underscore prefix. `JobRunner` itself stayed monolithic.

**What did NOT land in Unit 4:** The plan's DoD called for "no file in
`services/jobs/` over ~500 lines" and "worker functions individually
unit-testable (called directly without going through `JobRunner`)".
Both bullets are unmet. Reasons recorded in
`journal/260507-units-3-and-4.md` § "Acknowledged exception".

**Goal:** Extract each `_run_*` worker as a free function under
`services/jobs/workers/<name>.py` so workers are independently testable
and `runner.py` shrinks to submission + dispatch + notification glue.

**Worker inventory (audit done in Unit 4):**

| Worker | Lines | Notes |
|---|---:|---|
| `_run_entity_extraction` | ~88 | Single-entry + batch paths in one method |
| `_run_entity_reembed` | ~34 | Smallest; uses the `EntityReembedder` Protocol from Unit 3 |
| `_run_image_ingestion` | ~126 | Has retry loop + pipeline-notify logic |
| `_run_mood_score_entry` | ~71 | |
| `_run_reprocess_embeddings` | ~55 | |
| `_run_mood_backfill` | ~46 | |
| `_run_audio_ingestion` | ~115 | Same retry/pipeline shape as image |

**Approach:**

1. Decide the dependency-passing shape. Two options:
   1. **Free functions with explicit kwargs.** Each worker takes the
      specific deps it needs. Verbose at the dispatch site but minimal
      at the worker site.
   2. **`WorkerContext` dataclass.** A single bundle holding `jobs_repo`,
      `extraction`, `reembedder`, `mood_scoring`, `mood_backfill`,
      `entries`, `ingestion`, `notify_success`, `notify_failed`,
      `notify_retrying`, `try_pipeline_notification`. Workers take one
      argument; tests build a minimal context.
   - Recommend option 2 — it scales better as workers gain dependencies
     and reads cleaner at the dispatch site.
2. Extract `JobNotifier` first (the four `_notify_*` methods plus
   `_get_notify_strategy` and `_try_pipeline_notification`) into
   `services/jobs/notifier.py`. Most workers need it.
3. Start with the smallest worker (`reembed`, 34 lines) to validate the
   pattern. Add a direct unit test that constructs the context with
   fakes and calls `run_entity_reembed(ctx, job_id, params)`.
4. Move the others in size order. Image and audio share the retry
   helper — extract that to `services/jobs/retry.py` and have both
   workers call it.
5. `runner.py`'s `_run_*` methods become 3-line dispatchers that hand
   off to the workers. Or remove them entirely and have
   `submit_*` use `self._executor.submit(run_<name>, ctx, job_id, params)`.

**Acceptance:**
- Every worker has a dedicated module under `services/jobs/workers/`.
- At least one worker has a direct unit test instantiated without
  `JobRunner`.
- `runner.py` ≤ 500 lines.
- All existing tests pass; the `max_workers=1` invariant docstring
  is preserved verbatim.

**Session size:** Probably one full session. Image / audio retry
extraction is the trickiest part.

**Pointer:** `journal/260507-units-3-and-4.md` § "Acknowledged exception".

---

### 3. Test private-state cleanup, round 2 — LARGELY RESOLVED 2026-05-07

Sweep complete for every category in the original snapshot. Total
reach-in count 254 → 66 (-188 sites). Per-category outcomes:

| Pattern | Result |
|---|---|
| `provider._client` (~69) | resolved — `_make_provider()` helpers in each provider test file now return `(provider, fake_client)` and tests configure the SDK mock through the test's own `client` variable |
| `ingestion_service._repo` (~27) | resolved — `tests/test_services/test_ingestion.py` fixture split into `repo` + `ingestion_service`; one site survives in `tests/test_auth_api.py` (mirrors a real production reach-in — see item 7 below) |
| `scorer._client` (~13) | resolved — `_make_scorer(client)` helper patches `anthropic.Anthropic` |
| `svc._build_success_message` (~11) | resolved — extracted to module-level `build_success_message`; tests import the function directly |
| `provider._primary` / `_fallback` / `_secondary` / `_shadow` (~25) | resolved — promoted to read-only accessors on `RetryingTranscriptionProvider`, `ShadowTranscriptionProvider`, and `DualPassOCRProvider` |
| `runner._ingestion` (~10) | resolved — `runner_factory` accepts `ingestion=` kwarg |
| `rr._client` (~8) | resolved — `_make()` helper returns `(rr, fake_client)` |
| `provider._model` (~7) | resolved — `.model` property added to all 9 provider/service classes that hold a model name; `__new__` write-pattern tests rebuilt to use real constructors with patched SDKs |
| `svc._is_topic_enabled` (~5) | resolved — rewrote tests against the public `notify_*` surface (Pushover urlopen side effect) |
| `svc._post_to_pushover` / `svc._resolve_credentials` | resolved — extracted to module-level `post_to_pushover` / `resolve_credentials`; tests import them directly |
| `_vector_store` (~5) | resolved — `IngestionService.vector_store` property added (mirrors `QueryService.vector_store`); test sites switched |
| `poller._thread` (4) | resolved — added `is_running()` and `wait(timeout=...)` to `HealthPoller` |

**Residual: ~66 reach-ins** — all in one of these tolerated buckets:

1. **Docstring text** (~7 sites in `provider._client`): the helper
   docstrings explicitly mention the old reach-in pattern they
   replaced. Counts in the grep but is not actually a code reach-in.
2. **Production reach-in mirrors** (~22 sites): `services/reload.py`
   directly writes `services["ingestion"]._ocr`,
   `_transcription`, `_mood_scoring`, `_repo` and
   `services["job_runner"]._mood_scoring`. Tests for the reload
   endpoints assert those side effects via the same attributes (and
   `.._system_text`, `.._context_prompt` on the OCR/transcription
   providers). Cleaning these up requires promoting the swap surface
   to a public API on the affected services first — see item 7 below.
3. **Tests of legitimately internal state** (~6 sites):
   `job_runner._jobs` / `_executor` mid-test inspection,
   `mcp_module._services` for the MCP bootstrap, `mcp_server._init_services`
   for the same. Promoting these would add tests-only public API.
4. **Singleton residual** (~5 sites in `runner._extraction`,
   `store._client`, etc.): one-off accesses where the surrounding test
   does mostly OK but pokes one private attribute for an assertion.
   Worth cleaning up if those tests are revisited for other reasons,
   not a category-sized investment.

**Grep gate** (CI / manual): the count should not rise above 70 in
casual development; a meaningful jump means a new private-state
reach-in slipped in.

```bash
grep -rE '\._[a-z]' tests/ --include='*.py' | grep -v 'self\._' | grep -v 'import \|from ' | wc -l
```

**Pointers:** `journal/260507-item-3-test-private-state-cleanup.md`
for the full breakdown of decisions per category.

---

### 4. Parked oversized files (planning round needed)

The original review flagged three files over the 800-line smell
threshold that were intentionally out of scope for the v2 plan. Each
needs a planning round (read the file, propose a split, surface
decisions) **before** any extraction work.

**The plan's "Out of scope" section** in
`docs/code-quality-refactor-plan.md` § "Out of scope — parked for a
future round" remains the canonical entry point. Current sizes (verify
before planning):

| File | Lines | Notes |
|---|---:|---|
| `services/ingestion.py` | 985 | Over the smell threshold. Already gained one method in Unit 1b (`store_source_file`, `get_page_count`). |
| `entitystore/store.py` | 1074 | Over the smell threshold. |
| `cli.py` | 1621 | Over the *problem* threshold (1500). CLI files are conventionally inline-style, but this is past comfort for agent context windows. |

**Approach for each:**

1. **Planning session first.** Read the whole file, propose a
   resource-by-resource (or command-by-command for cli.py) split with
   line-count estimates, surface decision points to the user, get
   sign-off. Same shape as the conversation that opened Unit 1a.
2. **Extraction sessions.** Once the split shape is agreed, execute
   one resource per commit, full test suite after each. Same playbook
   as Unit 1a.

**Recommended order:** `services/ingestion.py` first — it has the
clearest sub-responsibilities (image vs voice vs text vs URL paths
each have their own ingest method) and the most existing test
coverage to lean on. `cli.py` last — biggest, command-shaped, and
the one most likely to need cross-cutting design decisions.

**Acceptance per file:** No file in the resulting package over ~600
lines (matching the api/ cap from Unit 1a). All tests pass.
`docs/code-quality-refactor-plan.md`'s "Out of scope" section gets
the corresponding bullet struck through and a pointer added to the
new journal entry.

---

### 5. ~~Unit 1b carryover — write-method ownership~~ — RESOLVED 2026-05-07

`update_entry_date` and `verify_doubts` moved from `QueryService` to
`IngestionService`. API call sites in `api/entries.py` (PATCH date
update; `POST /verify-doubts`) now use `ingestion_svc.<method>`. The
two delegate-style tests were removed from
`tests/test_services/test_query_service_public_api.py`; equivalent
behavioural tests added under
`tests/test_services/test_ingestion.py::TestIngestionPublicAPI`. All
1797 unit tests pass. See
`journal/260507-item-5-write-method-ownership.md`.

---

### 6. Acknowledged size-cap exceptions (no action unless forced)

These four files are documented over the soft cap but were left as-is
because the resource cohesion outweighs the split benefit at current
size. Re-evaluate only if growth pressure forces it; do not refactor
speculatively.

| File | Lines | Planned shape (if needed) | Source |
|---|---:|---|---|
| `api/entities.py` | 717 | Split into `entities.py` (CRUD) + `entity_merge.py` (merge / merge-candidates / quarantine / aliases) | `journal/260507-api-py-split-unit-1a.md` |
| `api/dashboard.py` | 609 | Already minor over-cap; leave as one module unless growth | Same |
| `services/entity_extraction/service.py` | 808 | Orchestrator stays large by design; further trim would need an `ExtractionContext` refactor or a 10-arg free-function `_resolve_entity` | `journal/260507-unit-2-entity-extraction-split.md` |
| `services/jobs/runner.py` | 1163 | See item 2 in this doc | `journal/260507-units-3-and-4.md` |

---

### 7. Production reach-in pattern: hot-swap of providers via `_attr =`

**Where:** `src/journal/services/reload.py`, `src/journal/cli.py:290`,
plus the matching test fixtures in `tests/test_auth_api.py` and
the reload-endpoint tests in `tests/test_admin_api.py` /
`tests/test_auth_api.py`.

**What:** The reload endpoints rebind `services["ingestion"]._ocr`,
`._transcription`, `._mood_scoring`, `._repo`, and
`services["job_runner"]._mood_scoring` directly via attribute
assignment. The pattern works (Python attribute writes are atomic) but
it is the only place in production where one component reaches across
the public service boundary. Every test that verifies a reload
landed has to mirror the pattern, and the residual ~22 reach-ins from
item 3 all live here.

**Goal:** Promote the swap surface to a public method on each
affected service. Concrete shape (sketch):

```python
class IngestionService:
    def replace_ocr(self, provider: OCRProvider) -> None: ...
    def replace_transcription(self, provider: TranscriptionProvider) -> None: ...
    def replace_mood_scoring(self, scoring: MoodScoringService | None) -> None: ...

class JobRunner:
    def replace_mood_scoring(self, scoring: MoodScoringService) -> None: ...
```

`reload.py` calls those instead of writing `_ocr` etc. The reload
tests assert `services["ingestion"].ocr is not old_ocr` (or call
the same `replace_*` again and observe the new instance). The CLI's
`ingestion._repo` reach-in at `cli.py:290` becomes
`ingestion.repository` (read-only property, mirroring the existing
`QueryService.connection` style).

**Acceptance:** No `services["ingestion"]._*` writes anywhere in
`src/`. The `tests/` reach-in count drops to ≤ ~10 (just the
docstring text and the truly-internal singletons listed in item 3's
residual breakdown).

**Session size:** 30-60 minutes per service.

**Pointer:** Item 3's residual breakdown.

---

## Standing facts (verify before acting)

These snapshots are accurate as of 2026-05-07. Each has a re-verification
command. Trust the command output, not the snapshot.

### Reach-in count from `api/` to private service state

```bash
grep -rE 'query_svc\._|ingestion_svc\._|entity_store\._|notif\._|user_repo\._|stats_collector\._|job_runner\._|job_repository\._|runtime\._' src/journal/api/ | grep -v '_shared.py' | grep -v 'from \|import '
```

Expected: zero hits. If non-zero, a new reach-in landed since Unit 1b
and should be addressed before unrelated work.

### Reach-in count from `tests/` to private state

```bash
grep -rE '\._[a-z]' tests/ --include='*.py' | grep -v 'self\._' | grep -v 'import \|from ' | wc -l
```

Expected: ~66 after item 3 part E (down from 254). A *rise* means a new
test reached into private state and should be addressed at the source.

### File sizes vs the soft cap

```bash
find src/journal -name '*.py' -exec wc -l {} + | sort -rn | head -10
```

Anything new over ~600 lines that is not already on the exception list
warrants attention.

### Test counts

```bash
uv run pytest -q -m 'not integration' 2>&1 | grep -E 'passed|failed' | tail -1
CHROMA_HOST=localhost CHROMA_PORT=8401 uv run pytest -m integration -q 2>&1 | grep -E 'passed|failed' | tail -1
```

Expected on a clean main branch: 1794 unit + 8 integration = 1802 total.

---

## Suggested order

If you want a single happy path:

1. **Item 1 (flake)** first — small, fits any session, removes a CI
   noise source that affects future runs.
2. **Item 5 (write ownership)** — 15-minute mechanical move, tack onto
   item 1's session if context is cheap.
3. **Item 3 (test cleanup)**, smallest categories first — builds muscle
   memory for the rename pattern. Spread across short sessions.
4. **Item 2 (worker-class extraction)** — full session, structural
   change.
5. **Item 4 (parked files)** — planning session first, then per-file
   sessions. Probably the longest-running thread.

Items 6 (size-cap exceptions) — only act if growth forces it.
