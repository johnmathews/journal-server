# Refactor round 3 — kickoff doc for the next session

This document is the entry point for the next refactor session. The
v2 plan (`docs/code-quality-refactor-plan.md`) and the round-2 living
punch list (`docs/refactor-follow-ups.md`) are both **fully closed**;
this doc captures the current state, the new candidates that surfaced
along the way, and a recommendation for what to pick up next.

**Load this doc first.** It is self-contained for a cold start. Open
files referenced by name only when you actually need them.

---

## How to use this doc

Three companion documents make up the canonical reference:

1. **`docs/code-quality-principles.md`** — standing rules. The "agent
   test", anti-patterns, and the api/ routing rules (default = primary
   URL resource; override = `ingestion.py` for write/job-creation
   routes). Read on every session that touches the api/ layer or
   designs a new split.
2. **`docs/code-quality-refactor-plan.md`** — historical sequence (v2).
   Units 1a → 7. Useful for understanding *why* an early split is
   shaped the way it is. Don't re-execute units from this doc.
3. **`docs/refactor-follow-ups.md`** — open items from round 2. Items
   1–7 are now all marked RESOLVED, ACCEPTED+DOCUMENTED, or LARGELY
   RESOLVED. The doc still has value as a record of decisions and
   standing-fact verification commands.
4. **This doc (`docs/refactor-round-3.md`)** — successor punch list
   for round 3.

Per-session journal entries under `journal/260507-*.md` record the
decisions and exceptions for each landed item. They are the source of
truth for "why did we do X this way" — link back to them from new
commits when relevant.

### Per-session bootstrap

For any new session continuing the refactor, start with:

1. Open *only* this doc plus the target file(s) for the chosen item.
2. Load `docs/code-quality-principles.md` if the work touches public
   API shape (services, api/, package layout).
3. Skim the relevant `journal/260507-*.md` entry for the unit that
   originally produced the file you're editing.
4. **Before recommending or doing anything**, run the standing
   verifications listed in the "Standing facts" section to make sure
   the snapshot is still accurate.

---

## What round 2 closed

Every item from `docs/refactor-follow-ups.md` is closed as of
2026-05-07. Brief summaries — full detail lives in the journal
entries listed.

| Item | Result | Journal |
|---|---|---|
| 1 — Flake `test_patch_text_queues_mood_scoring` | RESOLVED. Within-call shared-connection race in `submit_save_entry_pipeline` — each child's `executor.submit` happened before the API thread finished its writes. Fix: stage every child row up front, `mark_succeeded` the parent, then dispatch all children. 1000/1000 green post-fix. | `260507-item-1-save-pipeline-race-fix.md` |
| 1.1 — Cross-call connection-sharing race | ACCEPTED + DOCUMENTED. Tried `LockedConnection` wrapper + `with self._conn:` blocks; ran into `cursor.lastrowid` / `cursor.fetchone()` reads happening outside per-method locks. Closing every gap requires per-thread connections (architectural rewrite) or connection-wide locks held across full multi-step transactions. Decision: accept the residual risk, rewrite docstrings honestly, document reopen criteria. | (Inline in `docs/refactor-follow-ups.md` § 1.1) |
| 2 — Worker-class extraction from `runner.py` | RESOLVED. `runner.py` 1214 → 423 lines. Each `_run_*` is a free function under `services/jobs/workers/<name>.py` taking a `WorkerContext`. New `JobNotifier` (208), `save_pipeline.py` (186), `retry.py` (96). 4 new direct unit tests for `run_entity_reembed`. | `260507-item-2-worker-extraction.md` |
| 3 — Test private-state cleanup, round 2 | LARGELY RESOLVED + further reduced by item 7. Test reach-in count **254 → 37**. Final residual is in 4 justified buckets — see "Standing facts" below for the breakdown. | `260507-item-3-test-private-state-cleanup.md` |
| 4 — Parked oversized files | RESOLVED. Three packages: `services/ingestion/` (5 files, max 375), `entitystore/` (4 files, max 408), `cli/` (5 files, max 603). Mixin classes for the first two; per-command modules for cli. Cmd_seed's literal sample data extracted to `cli/_seed_samples.py` (679 lines, data only). | `260507-item-4-parked-files-split.md` |
| 5 — Unit 1b carryover | RESOLVED. `update_entry_date` and `verify_doubts` moved from `QueryService` to `IngestionService` to align with the read/write split. | `260507-item-5-write-method-ownership.md` |
| 6 — Acknowledged size-cap exceptions | Status quo, "no action unless forced". Three remaining files (table below). | n/a |
| 7 — Production reach-in pattern in `services/reload.py` | RESOLVED. New named methods on `IngestionService` (`replace_ocr`, `replace_transcription`, `replace_mood_scoring`, `replace_formatter`, `replace_heading_detector`, `set_preprocess_images` plus `repository` and `mood_scoring` accessors) and `JobRunner` (`mood_scoring` property + `replace_mood_scoring`). Also fixed a latent bug from item 2 — `services["job_runner"]._mood_scoring = new` had silently become a phantom-attribute write after the WorkerContext refactor. | (Inline in `docs/refactor-follow-ups.md` § 7) |

**Test count:** 1800 unit + 8 integration = 1808 (was 1794 + 8 at the
start of round 2).

**Test reach-in count:** 254 → 37 (-217 sites). All 37 remaining are
in 4 documented buckets — verify before acting.

**`runner.py` line count:** 1214 → 423.

**Acknowledged item-6 exceptions still in place:**

| File | Lines | Reason |
|---|---:|---|
| `api/entities.py` | 717 | Resource cohesion outweighs split benefit at current size. Planned split: `entities.py` CRUD + `entity_merge.py` for merge / candidates / quarantine / aliases. Source: `journal/260507-api-py-split-unit-1a.md`. |
| `api/dashboard.py` | 609 | Marginally over-cap; leave as one module unless it grows further. |
| `services/entity_extraction/service.py` | 808 | Orchestrator stays large by design; further trim would need an `ExtractionContext` refactor or a 10-arg free-function `_resolve_entity`. Source: `journal/260507-unit-2-entity-extraction-split.md`. |

---

## Outstanding points (round 3)

### A. Two newly-largest files

While planning item 4, two files surfaced as the new top-of-list size
outliers. Neither was on the original parked list. They were noted in
`docs/refactor-follow-ups.md`'s standing-facts table but never scoped
into an item.

| File | Lines | Notes |
|---|---:|---|
| `db/repository.py` | 1603 | Now the single largest source file. Mostly the `SQLiteEntryRepository` class plus a sibling Protocol and `_row_to_*` helpers. The class spans many query families: entry CRUD, chunks, entry pages, uncertain spans, mood scores, mood trends, mood drilldowns, entity distributions, ingestion stats, writing frequency, calendar days, statistics, FTS5 search. Each of those is a coherent group. |
| `mcp_server.py` | 1513 | Bootstrap (`_init_services`) + route registration + the runtime-settings on-change callback. Item 7 made the on-change callback considerably tidier (5 toggleable hooks now go through public methods) but the file is still ~80 lines of callback embedded in 1500 lines of bootstrap. |

Both are above the original 800-line "smell" threshold and at/near the
1500-line "problem" threshold (`mcp_server.py` is right on the edge).

### B. Item 3 residual

37 test reach-ins remain. All four buckets are documented in
`docs/refactor-follow-ups.md` § 3 and the journal entry. Verify the
breakdown is unchanged with the grep gate (see Standing facts).
Worth working only if a specific cluster turns out to bite during
unrelated work — not category-sized investment material.

### C. Item 1.1 residual

The cross-call shared-connection race is still theoretically present.
Reopen criteria are documented in
`docs/refactor-follow-ups.md` § 1.1: production
`sqlite3.OperationalError: not an error` reports, OR a future change
that materially increases worker-thread SQLite write rate. Do not
revisit speculatively.

---

## Recommendations

In rough order of value vs. effort:

### 1. Tidy round 2 docs (15 min, recommended regardless)

`docs/refactor-follow-ups.md` § 3 still says "residual: ~66" (it was
true when item 3 closed, before item 7 cleared the production-mirror
bucket). Update it to 37 and add a one-line pointer to this doc at
the top. Five-minute pass.

### 2. Planning round for `mcp_server.py` (recommended next refactor)

Cleaner shape than `db/repository.py` and the smaller of the two new
candidates. Likely natural splits:

```
mcp_server/
  __init__.py            — main + run loop + serving
  bootstrap.py           — _init_services (the big constructor)
  routes.py              — route registrations (or split per resource
                           if there are enough)
  runtime_settings.py    — the on-change callback that swaps
                           providers via the public methods landed
                           in item 7
```

Sessions:
1. **Planning round** (read-only, ~30 min). Read the full file,
   propose split shapes with line estimates, surface decisions
   (especially around how routes are registered — those are
   side-effecting calls into FastMCP that may not move cleanly).
2. **Extraction sessions** — likely one per module, full test suite
   after each.

### 3. Planning round for `db/repository.py` (bigger, also valuable)

The repository class is the single biggest file in the codebase and
spans many query families. A split by query family follows the same
mixin pattern that worked for `IngestionService` and
`SQLiteEntityStore` in item 4. Suggested clusters (line estimates
need verification):

```
db/repository/
  __init__.py             — re-export
  protocol.py             — EntryRepository Protocol + _row_to_*
                            helpers
  repository.py           — class shell, __init__, entry CRUD,
                            update_final_text, get_entries_by_date,
                            list_entries
  search.py mixin         — search_text, search_text_with_snippets,
                            count_text_matches (FTS5)
  chunks.py mixin         — replace_chunks, get_chunks,
                            update_chunk_count
  pages.py mixin          — add_entry_page, get_entry_pages,
                            uncertain_spans
  mood.py mixin           — mood scores, mood trends, mood drilldown,
                            prune_retired_mood_scores
  stats.py mixin          — get_statistics, get_ingestion_stats,
                            get_writing_frequency,
                            get_entity_distribution, calendar_days,
                            count_entries
```

Same mixin shape as `entitystore/` from item 4. Methods stay bound to
`self`. Per-cluster file size estimates probably 200–300 lines each.

Sessions:
1. **Planning round** — propose the clusters, verify line counts,
   surface any cross-cluster method dependencies.
2. **Extraction sessions** — one mixin per commit, full test suite
   after each. The `protocol.py` step + the move to a package is the
   biggest single commit.

### 4. Item 3 residual cleanup (low priority)

Skip unless the residual surfaces a real friction point during
unrelated work. The four documented buckets are all in tolerated
shape.

### 5. Stop here (also valid)

Everything from the original plan is closed. The two new big files
were never explicitly scoped; deferring them is a reasonable choice if
other work is more pressing. The reach-in grep gate (see Standing
facts) catches regressions in the meantime.

---

## My pick for the next session

**Recommendation 1 (doc tidy) + Recommendation 2 (planning round for
`mcp_server.py`).** That gives the next session:

- A 15-minute warm-up doing the doc tidy.
- A focused read-only planning round on the cleaner of the two big
  files. Bring the proposed split back for sign-off, then start
  extraction in a follow-up session.

Reasons not to combine planning rounds for both files: the planning
output for `db/repository.py` is its own substantial doc (many query
clusters, line-count estimates, dependency analysis) and would
crowd a single session.

---

## Standing facts (verify before acting)

These snapshots are accurate as of 2026-05-07. Each has a
re-verification command. Trust the command output, not the snapshot.

### Reach-in count from `api/` to private service state

```bash
grep -rE 'query_svc\._|ingestion_svc\._|entity_store\._|notif\._|user_repo\._|stats_collector\._|job_runner\._|job_repository\._|runtime\._' src/journal/api/ | grep -v '_shared.py' | grep -v 'from \|import '
```

Expected: zero hits.

### Reach-in count from `tests/` to private state

```bash
grep -rE '\._[a-z]' tests/ --include='*.py' | grep -v 'self\._' | grep -v 'import \|from ' | wc -l
```

Expected: ~37 after item 7. A *rise* means a new test reached into
private state and should be addressed at the source.

Residual breakdown (what makes up the 37):
1. Docstring text in `services/ingestion/service.py` (2 sites) —
   intentional references to the old reach-in pattern.
2. Production reach-in mirrors (~6 sites for `_system_text` /
   `_context_prompt` on OCR/transcription provider internals) —
   tests assert the providers were rebuilt with new context. Could
   be promoted to public read accessors but the value is low.
3. Tests of legitimately internal state (~6 sites on
   `job_runner._jobs` / `_executor`, `mcp_module._services`,
   `mcp_server._init_services`) — promoting these would add
   tests-only public API.
4. One-off singletons on the long tail.

### File sizes vs the soft cap

```bash
find src/journal -name '*.py' -exec wc -l {} + | sort -rn | head -10
```

Top-10 sizes after item 7 (2026-05-07):

| File | Lines | Status |
|---|---:|---|
| `db/repository.py` | 1603 | Round-3 candidate (Recommendation 3). |
| `mcp_server.py` | 1513 | Round-3 candidate (Recommendation 2). |
| `auth_api.py` | 840 | Item 6 exception. |
| `services/entity_extraction/service.py` | 808 | Item 6 exception. |
| `providers/transcription.py` | 778 | Within range. |
| `providers/ocr.py` | 753 | Within range. |
| `services/notifications.py` | 744 | Grown by item 3 part E (module helpers). |
| `api/entities.py` | 717 | Item 6 exception. |
| `cli/_seed_samples.py` | 679 | Pure data — no edits expected. |

### Test counts

```bash
uv run pytest -q -m 'not integration' 2>&1 | grep -E 'passed|failed' | tail -1
CHROMA_HOST=localhost CHROMA_PORT=8401 uv run pytest -m integration -q 2>&1 | grep -E 'passed|failed' | tail -1
```

Expected on a clean main branch: 1800 unit + 8 integration = 1808
total.

### Working tree + branch state

```bash
git -C /Users/john/projects/journal/server status
git -C /Users/john/projects/journal/server log --oneline origin/main..HEAD
```

Expected at session start: clean tree, no unpushed commits. The last
commit before this doc was added is `c95fd62` ("Item 7: replace
reload reach-ins with public methods").

---

## Process notes worth remembering

Carried forward from round 2's journal entries:

- **Per-resource files via mixins, not free functions, for stateful
  services.** Free functions worked beautifully for the worker
  extraction in item 2 (clean dependency boundary via
  `WorkerContext`). They didn't work for `IngestionService` /
  `SQLiteEntityStore` because each method reaches 5+ instance
  fields and threading those through a context dataclass duplicates
  the constructor surface. Mixins keep the methods bound to `self`
  and only move the file-organisation needle, which was the actual
  goal. Apply the same principle to `db/repository.py`.
- **Plan first, then extract.** Every parked-file split started with
  a planning round that surfaced decision points (free functions vs
  mixins, where the Protocol lives, what gets re-exported). Skipping
  the planning round on the assumption "the shape is obvious"
  burned time in item 4 — re-evaluate after reading the actual
  method bodies.
- **AST-based deletion for surgical extracts.** When pulling methods
  out of a file in chunks, `ast.parse` + `node.lineno` /
  `node.end_lineno` is the safe way to find exact boundaries.
  Hand-rolled regexes for "find a method's end" repeatedly missed
  multi-line signatures and decorators.
- **Test patches retarget when classes move.** Every package-shape
  refactor produced a round of `unittest.mock.patch("...")`
  retargets in tests (the old `journal.cli.X` becomes
  `journal.cli._services.X` or `journal.cli.entities.X` depending
  on where the symbol now lives). Plan time for this; it's
  mechanical but always present.
- **Standing-facts table beats "I think this is small enough".**
  Re-running the size + reach-in counts at session start catches
  drift. The "newly-largest files" surfaced for round 3 were
  noticed only because the standing-facts table was up to date.
