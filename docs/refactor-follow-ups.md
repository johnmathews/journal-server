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

### 1. Flake: `test_patch_text_queues_mood_scoring`

**Where:** `tests/test_api_ingest.py:363`

**What happened:** Failed once on CI run `25489575555` (Unit 3 push) with
`AssertionError: assert None is not None`. The same logs contained
`sqlite3.OperationalError: not an error`. Re-passed on the next CI run
unchanged. The test's own comment flags background-thread SQLite contention:

> Prevent entity extraction from running in a background thread —
> its SQLite writes on the shared connection race with the mood job
> creation that this test is actually checking.

The test mocks `submit_entity_extraction` to dodge the race, which suggests
there is still a window where another job's write can race with the PATCH
handler's read. The single-flake-then-recovery pattern fits a race rather
than a logic bug.

**Goal:** Reproduce reliably, then fix.

**Approach:**

1. Reproduce — likely needs a tight loop or a barrier (`threading.Barrier`)
   to force the worker thread and the PATCH thread to interleave on the
   shared SQLite connection. Run with `pytest --count=200` or a custom
   fixture that holds an `Event` open until both threads are mid-write.
2. Diagnose — the relevant invariant from `services/jobs/runner.py`'s
   docstring is "writes from multiple worker threads on a single
   connection with WAL + NORMAL synchronous is NOT safe". Check whether
   the test is exercising a code path that violates that invariant
   (e.g. spawning a follow-up job whose worker writes while the test
   thread is mid-read).
3. Fix — options, ordered by preference:
   1. Make the test's setup deterministically wait for any in-flight
      jobs to drain before reading `data.get("mood_job_id")`.
   2. Change the API path so the PATCH response is constructed entirely
      inside one transaction.
   3. Document and assert the race window if it is genuinely benign.

**Acceptance:** A `pytest --count=N` run for some reproducibly-large N
goes green; the original test plus any new regression test stay green
in CI for ten consecutive pushes.

**Session size:** 30–90 minutes. Can be done standalone.

**Pointer:** `journal/260507-units-3-and-4.md` for the broader context of
when this test was last touched.

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

### 3. Test private-state cleanup, round 2

**Where:** `tests/` — about 258 reach-ins remaining as of 2026-05-07.

**What landed in Unit 6:** `EntryRepository.connection` and
`JobRepository.connection` properties added; `repo._conn` / `jobs_repo._conn`
renamed to `.connection` everywhere in tests. ~17 sites resolved.

**Remaining categories** (current counts — re-grep before starting,
they change as the codebase evolves):

| Pattern | Sites | Strategy |
|---|---:|---|
| `provider._client` | ~69 | Add `replace_client(...)` per provider class, OR migrate tests to full provider mocks |
| `ingestion_service._repo` | ~27 | Add named pass-throughs on IngestionService for the long-tail repo methods (Unit 1b pattern) |
| `scorer._client` | ~13 | Same as provider._client |
| `svc._build_success_message` | ~11 | Direct test of a private method; either promote to public API or rewrite the test against the public surface |
| `provider._primary` | ~11 | Tests assert factory-built provider's primary/fallback shape; promote to a `.primary` / `.fallback` accessor pair |
| `runner._ingestion` | ~10 | JobRunner internal state poked by tests; add public accessor or refactor the test |
| `rr._client` | ~8 | Same as provider._client |
| `provider._model` | ~7 | Promote to `.model` property (read-only) |
| `svc._is_topic_enabled` | ~5 | Test of private method; same options as `svc._build_*` |

**Goal:** Drive the count to zero (or to a small, justified set with
a comment explaining why). The grep gate that catches new ones:

```bash
grep -rE '\._[a-z]' tests/ | grep -v 'self\._' | grep -v 'import \|from '
```

**Approach:** One session per category, biggest-first. The
`provider._client` block is the largest and warrants a per-provider
design pass — figure out whether the test wants to *replace* the
client entirely (in which case a `replace_client(...)` method on the
provider) or simply *observe* the client config (in which case
read-only accessors). Don't lump them.

**Acceptance:** Per-category PR. Each PR resolves all sites in its
category, leaves the grep count at zero for that pattern, and doesn't
introduce new public API for tests-only consumers.

**Session size:** 30-60 min for the small categories
(`scorer._client`, `provider._model`); a session each for
`provider._client`, `ingestion_service._repo`, `svc._build_*`.

**Pointer:** `journal/260507-units-5-6-7.md` § "Categories deferred
to a future Unit 6.5".

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

### 5. Unit 1b carryover — write-method ownership

**Where:** `src/journal/services/query.py`,
`src/journal/api/entries.py:182, 322`.

**What:** `update_entry_date` and `verify_doubts` are *writes* but
landed on `QueryService` in Unit 1b to match the existing call-site
service handle. Semantically they belong on `IngestionService`.

**Goal:** Move both to `IngestionService` and update the api/
call sites to use `ingestion_svc.update_entry_date` /
`ingestion_svc.verify_doubts`.

**Approach:** Mechanical move. Both methods are pure delegations to
`self._repo.<method>` with no extra logic. No tests should change
behaviour-wise; some test mocks may need to switch from `query_svc`
to `ingestion_svc`.

**Acceptance:** `update_entry_date` and `verify_doubts` removed from
`QueryService`; added to `IngestionService`; api/entries.py call
sites updated; tests pass.

**Session size:** 15-30 minutes. Tack onto another unit's session.

**Pointer:** `journal/260507-unit-1b-remove-api-reach-ins.md` §
"Decisions worth remembering" item 2.

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

Expected: ~258 (declining as item 3 is worked). A *rise* means a new
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
