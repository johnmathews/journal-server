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

### 1.1 ~~Cross-call connection-sharing race~~ — ACCEPTED + DOCUMENTED 2026-05-07

Investigated; landed on "accept and document" after the structural
fix proved bigger than the bug warranted.

**What was tried:** A `LockedConnection` wrapper that serialised
``execute`` / ``executemany`` / ``commit`` calls behind an RLock,
plus ``with self._conn:`` blocks around every multi-step write
(``create_entry``, ``create_entity``, ``create_mention``,
``create_relationship``, ``store_source_file``). All 1800 unit
tests pass with that wrapper, BUT a 20× PATCH-loop regression test
still failed intermittently. Diagnostic logging showed
``cursor.lastrowid`` pointed at a real rowid and the row existed
inside the locked ``with`` block, but a subsequent SELECT (via
``get_entry()``) returned ``None`` — the in-flight cursor / fetch
state on the shared connection was being clobbered by the worker
thread's concurrent operations during the unlocked window between
``execute()`` returning a Cursor and the caller's
``cursor.fetchone()`` / ``lastrowid`` read.

Closing every such window requires either:

1. Per-thread connections (each thread opens its own
   ``sqlite3.Connection``; SQLite's WAL handles cross-connection
   coordination correctly). Substantial architectural change —
   services and repos currently share a single connection
   instance, so this rewires every constructor and every test
   fixture.
2. A connection-wide write lock held across the *entire* multi-step
   operation (each repo method explicitly acquires/releases). Many
   sites, deep changes.

**Decision:** accept the residual risk. The within-call race
fixed in item 1 was the only one we have actually observed in
practice (test or production). Cross-call races are theoretical
— workers spend almost all their time in LLM calls, not SQLite,
so the concurrent-write window is small and short.

**What changed in this commit:**
- ``db/connection.py`` docstring rewritten to honestly describe
  the threading hazard, the two real fix options, and the explicit
  decision to accept the risk.
- ``services/jobs/runner.py`` docstrings (module + ``JobRunner``
  class) updated to stop claiming the single-worker executor makes
  the connection safe — it doesn't, because the API thread is also
  a writer.

**Reopen if:** ``sqlite3.OperationalError: not an error`` shows up
in production logs, OR a future change increases the rate of
worker-thread DB writes (anything pulling more SQLite work into
the workers, especially in the hot ingestion paths).

**Pointer:** `journal/260507-item-1-save-pipeline-race-fix.md` § "Out of scope" for the within-call fix.

---

### 2. ~~`services/jobs/runner.py` — worker-class extraction~~ — RESOLVED 2026-05-07

`services/jobs/runner.py` 1214 → **423 lines** (target was ≤ 500).

`WorkerContext` dataclass (option 2 from the design table) bundles
the dependencies. Each worker is now a free function under
`services/jobs/workers/<name>.py` taking
`(ctx, job_id, params)`. New supporting modules:

- `services/jobs/notifier.py` (208 lines) — `JobNotifier` wraps
  `notify_success`/`notify_failed`/`notify_retrying`/`get_notify_strategy`/
  `try_pipeline_notification`. Dropped underscore prefixes; the
  notifier is constructed once by `JobRunner.__init__`.
- `services/jobs/retry.py` (96 lines) — `run_with_retry[T]` shared by
  the image + audio workers. Same exponential-backoff loop with
  status-detail updates and "notify on first retry only" rule.
- `services/jobs/save_pipeline.py` (186 lines) — the
  `submit_save_entry_pipeline` orchestrator (parent + 3 children +
  deferred dispatch + defensive sweep). `JobRunner.submit_save_entry_pipeline`
  is now a thin shim over the free function.

Direct unit test for the smallest worker added at
`tests/test_services/test_jobs/test_worker_entity_reembed.py` — 4
tests building a minimal `WorkerContext` and calling
`run_entity_reembed` without instantiating `JobRunner`.

Verified: 1800 unit tests pass (was 1796; +4 new). The
`max_workers=1` invariant docstring is preserved verbatim on both
the module header and the `JobRunner` class.

See `journal/260507-item-2-worker-extraction.md`.

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

### 4. ~~Parked oversized files~~ — RESOLVED 2026-05-07

All three files split into per-resource packages with mixin classes
(ingestion, entitystore) or per-command modules (cli). Every
resulting file under the 600-line soft cap except
``cli/_seed_samples.py`` (679 lines, all literal data — no edits
expected).

| File | Was | Now |
|---|---:|---|
| ``services/ingestion.py`` | 985 | ``ingestion/`` package: service.py 375, image.py 280, voice.py 270, text.py 70, url_sources.py 206 |
| ``entitystore/store.py`` | 1074 | ``entitystore/`` package: protocol.py 263, store.py 408, mentions.py 195, merge.py 325 |
| ``cli.py`` | 1621 | ``cli/`` package: __init__.py 603, _seed_samples.py 679 (data), _services.py 96, entities.py 251, mood.py 108 |

The originally-proposed free-function shape from Decision 2 was
reversed for ingestion and entitystore once the dependency surface
was visible — methods on those classes reach 5+ instance fields
each, and threading those through a context dataclass would have
duplicated the constructor signature for no real test-isolation
gain. Mixin classes keep the methods bound to ``self`` and only
move the file-organisation needle, which was the actual goal.
CLI commands kept the free-function shape (they take
``(args, config)`` and have no shared state).

See:
- ``journal/260507-item-4-ingestion-split.md``
- ``journal/260507-item-4-entitystore-split.md``
- ``journal/260507-item-4-cli-split.md``

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
| ~~`services/jobs/runner.py`~~ | ~~1163~~ → 423 | Resolved by item 2 | `journal/260507-item-2-worker-extraction.md` |

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

Top-10 sizes after item 4 (2026-05-07):

| File | Lines | Status |
|---|---:|---|
| `db/repository.py` | 1603 | New: not previously flagged. Worth a planning round if it grows further. |
| `mcp_server.py` | 1509 | New: bootstrap + route wiring; on the cusp of the problem threshold. |
| `auth_api.py` | 840 | Unchanged from item 6's exception list. |
| `services/entity_extraction/service.py` | 808 | Item 6 exception. |
| `providers/transcription.py` | 778 | Within range; multiple wrapper classes. |
| `providers/ocr.py` | 753 | Within range; dual-pass + per-provider classes. |
| `services/notifications.py` | 744 | Grown by item 3 part E (module helpers). |
| `api/entities.py` | 717 | Item 6 exception. |
| `cli/_seed_samples.py` | 679 | Pure data — no edits expected. |

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
