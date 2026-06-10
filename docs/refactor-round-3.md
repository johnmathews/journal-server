# Refactor round 3 — kickoff doc for the next session

**Status:** active. **Last updated:** 2026-06-10 (standing facts re-measured after the
2026-06-10 quality round; see `journal/260610-quality-round.md`). **Supersedes:**
[`archive/code-quality-refactor-plan.md`](./archive/code-quality-refactor-plan.md) and
[`archive/refactor-follow-ups.md`](./archive/refactor-follow-ups.md).
Both child plans ([`archive/refactor-repository-plan.md`](./archive/refactor-repository-plan.md) and
[`archive/refactor-item-6-exceptions-plan.md`](./archive/refactor-item-6-exceptions-plan.md)) are now closed.

This document is the entry point for the next refactor session. The
v2 plan (`docs/archive/code-quality-refactor-plan.md`) and the round-2 living
punch list (`docs/archive/refactor-follow-ups.md`) are both **fully closed**;
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
2. **`docs/archive/code-quality-refactor-plan.md`** — historical sequence (v2).
   Units 1a → 7. Useful for understanding *why* an early split is
   shaped the way it is. Don't re-execute units from this doc.
3. **`docs/archive/refactor-follow-ups.md`** — open items from round 2. Items
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

Every item from `docs/archive/refactor-follow-ups.md` is closed as of
2026-05-07. Brief summaries — full detail lives in the journal
entries listed.

| Item | Result | Journal |
|---|---|---|
| 1 — Flake `test_patch_text_queues_mood_scoring` | RESOLVED. Within-call shared-connection race in `submit_save_entry_pipeline` — each child's `executor.submit` happened before the API thread finished its writes. Fix: stage every child row up front, `mark_succeeded` the parent, then dispatch all children. 1000/1000 green post-fix. | `260507-item-1-save-pipeline-race-fix.md` |
| 1.1 — Cross-call connection-sharing race | ACCEPTED + DOCUMENTED. Tried `LockedConnection` wrapper + `with self._conn:` blocks; ran into `cursor.lastrowid` / `cursor.fetchone()` reads happening outside per-method locks. Closing every gap requires per-thread connections (architectural rewrite) or connection-wide locks held across full multi-step transactions. Decision: accept the residual risk, rewrite docstrings honestly, document reopen criteria. | (Inline in `docs/archive/refactor-follow-ups.md` § 1.1) |
| 2 — Worker-class extraction from `runner.py` | RESOLVED. `runner.py` 1214 → 423 at landing (471 as of 2026-05-09 after follow-on edits). Each `_run_*` is a free function under `services/jobs/workers/<name>.py` taking a `WorkerContext`. New `JobNotifier` (208), `save_pipeline.py` (186), `retry.py` (96). 4 new direct unit tests for `run_entity_reembed`. | `260507-item-2-worker-extraction.md` |
| 3 — Test private-state cleanup, round 2 | LARGELY RESOLVED + further reduced by item 7. Test reach-in count **254 → 37**. Final residual is in 4 justified buckets — see "Standing facts" below for the breakdown. | `260507-item-3-test-private-state-cleanup.md` |
| 4 — Parked oversized files | RESOLVED. Three packages: `services/ingestion/` (5 files, max 375), `entitystore/` (4 files, max 408), `cli/` (5 files, max 603). Mixin classes for the first two; per-command modules for cli. Cmd_seed's literal sample data extracted to `cli/_seed_samples.py` (679 lines, data only). | `260507-item-4-parked-files-split.md` |
| 5 — Unit 1b carryover | RESOLVED. `update_entry_date` and `verify_doubts` moved from `QueryService` to `IngestionService` to align with the read/write split. | `260507-item-5-write-method-ownership.md` |
| 6 — Acknowledged size-cap exceptions | Status quo, "no action unless forced". Three remaining files (table below). | n/a |
| 7 — Production reach-in pattern in `services/reload.py` | RESOLVED. New named methods on `IngestionService` (`replace_ocr`, `replace_transcription`, `replace_mood_scoring`, `replace_formatter`, `replace_heading_detector`, `set_preprocess_images` plus `repository` and `mood_scoring` accessors) and `JobRunner` (`mood_scoring` property + `replace_mood_scoring`). Also fixed a latent bug from item 2 — `services["job_runner"]._mood_scoring = new` had silently become a phantom-attribute write after the WorkerContext refactor. | (Inline in `docs/archive/refactor-follow-ups.md` § 7) |

**Test count:** 1800 unit + 8 integration = 1808 (was 1794 + 8 at the
start of round 2).

**Test reach-in count:** 254 → 37 (-217 sites). All 37 remaining are
in 4 documented buckets — verify before acting.

**`runner.py` line count:** 1214 → 423 at landing (471 as of 2026-05-09).

**Acknowledged item-6 exceptions still in place:**

| File | Lines | Reason |
|---|---:|---|
| ~~`api/entities.py`~~ | ~~717~~ → split | RESOLVED on 2026-05-08. Split into `api/entities.py` (425, CRUD + read sub-resources) and `api/entity_merge.py` (406, merge/candidates/quarantine/merge-history). See `docs/archive/refactor-item-6-exceptions-plan.md` § Item 1. |
| ~~`auth_api.py`~~ | ~~840~~ → split | RESOLVED on 2026-05-08. Carved into `auth_api/{__init__,_shared,core,account,profile,api_keys,admin}.py`. Largest resulting file is `account.py` at 355 lines (under the 400-line target). Two inline `from journal.api import _runtime_get` imports in `auth_register` and `auth_config` were hoisted to module-level on the way out (no circular-import risk). See `docs/archive/refactor-item-6-exceptions-plan.md` § Item 3. |
| `api/dashboard.py` | 609 | Marginally over-cap; leave as one module unless it grows further. |

**Acknowledged-permanent (no further split planned):**

| File | Lines | Reason |
|---|---:|---|
| `services/entity_extraction/service.py` | 808 | The orchestrator IS the design — already the result of a 1187 → 808 split (round 2 unit 2). `extract_from_entry` is ~300 lines of inherent integration glue; `_resolve_entity` is a 132-line decision tree where extraction would need a 14-arg free function or an `ExtractionContext` dataclass that "moves lines, not eliminates them". Independent re-analysis confirmed this in `docs/archive/refactor-item-6-exceptions-plan.md` § Item 2. **Trigger to revisit:** if the file ever crosses ~1000 lines, redesign the `_resolve_entity` decision tree as a state machine — do not propose another mechanical split. |

---

## Outstanding points (round 3)

### A. Two newly-largest files

While planning item 4, two files surfaced as the new top-of-list size
outliers. Both were resolved on 2026-05-07: `mcp_server.py`
(Recommendation 2) and `db/repository.py` (Recommendation 3).

| File | Lines | Status |
|---|---:|---|
| ~~`db/repository.py`~~ | ~~1603~~ → split | RESOLVED on 2026-05-07. Carved into `db/repository/{__init__,protocol,store,core,pages,chunks,search,mood,stats,analytics}.py`. Largest resulting file is `stats.py` at 357 lines. See `docs/archive/refactor-repository-plan.md` and Recommendation 3 below. |
| ~~`mcp_server.py`~~ | ~~1513~~ → split | RESOLVED on 2026-05-07. Carved into `mcp_server/{bootstrap,app,runserver,__init__,__main__}.py` + `mcp_server/tools/{_ctx,queries,ingestion,entities,jobs}.py`. Largest resulting file is `bootstrap.py` at 475 lines. See `docs/archive/refactor-mcp-server-plan.md` and Recommendation 2 below. |

### B. Item 3 residual

37 test reach-ins remain. All four buckets are documented in
`docs/archive/refactor-follow-ups.md` § 3 and the journal entry. Verify the
breakdown is unchanged with the grep gate (see Standing facts).
Worth working only if a specific cluster turns out to bite during
unrelated work — not category-sized investment material.

### C. Item 1.1 residual

The cross-call shared-connection race is still theoretically present.
Reopen criteria are documented in
`docs/archive/refactor-follow-ups.md` § 1.1: production
`sqlite3.OperationalError: not an error` reports, OR a future change
that materially increases worker-thread SQLite write rate. Do not
revisit speculatively.

---

## Recommendations

In rough order of value vs. effort:

### 1. Tidy round 2 docs (15 min, recommended regardless) — RESOLVED

Landed 2026-05-07. `docs/archive/refactor-follow-ups.md` now carries a
top-of-doc pointer to this round-3 doc and the residual count is
corrected from "~66" to 37 in both the item-3 section and the
standing-facts table.

### 2. Planning round for `mcp_server.py` (recommended next refactor) — RESOLVED

Landed 2026-05-07 in three commits (planning + commit A + commit B):

- Plan: `docs/archive/refactor-mcp-server-plan.md` — proposed package shape
  and surfaced six decisions (mcp instance location, test-patch
  retargets, `__init__.py` re-export surface, on-change callback
  staying inline, `__main__.py`, three-commit shape).
- Commit A: `mcp_server.py` → package shell with `_legacy.py`,
  `__init__.py` re-exports, `__main__.py`, no behavior change.
- Commit B: `_legacy.py` carved into `bootstrap.py` (475),
  `app.py` (26), `runserver.py` (93), `tools/_ctx.py` (46),
  `tools/queries.py` (233), `tools/ingestion.py` (312),
  `tools/entities.py` (186), `tools/jobs.py` (240). Test patches
  retargeted to `journal.mcp_server.bootstrap.X`.

Outcome: `mcp_server.py` no longer appears in the top-10 size list
(largest package file is now `bootstrap.py` at 475 lines, well under
the 500-line target). 1799 unit tests pass; reach-in gates unchanged.

### 3. Planning round for `db/repository.py` (bigger, also valuable) — RESOLVED

Landed 2026-05-07 in three commits (planning + commit A + commit B):

- Plan: `docs/archive/refactor-repository-plan.md` — proposed package shape
  (8 cluster files instead of the round-3 doc's 6 — `stats` split
  on the natural seam between corpus stats and cross-axis
  analytics) and surfaced 10 decisions including mixin-vs-free-
  function shape, Protocol + helper placement, the cross-mixin
  call (`get_topic_frequency` → `search_text`), `__init__.py`
  re-export surface (required because 22 caller sites import via
  the package root), legacy `add_people`/`add_places`/`add_tags`
  placement, transaction-pattern cleanup deferral, and the three-
  commit shape.
- Commit A: `db/repository.py` → package shell with `_legacy.py`
  + `__init__.py` re-exporting `EntryRepository` and
  `SQLiteEntryRepository`. No behavior change.
- Commit B: `_legacy.py` carved into `protocol.py` (300),
  `store.py` (56), `core.py` (185), `pages.py` (134),
  `chunks.py` (82), `search.py` (109), `mood.py` (272),
  `stats.py` (357), `analytics.py` (312). All under the 500-line
  comfortable target. The expected commit C (test patch retargets)
  was not needed — verified upfront that no test does
  `patch("journal.db.repository.X")`, so the package re-export
  keeps every caller's import path working.

Outcome: `db/repository.py` no longer appears in the top-10 size
list (largest file in the package is now `stats.py` at 357 lines).
1799 unit tests + 8 integration tests pass; reach-in gates
unchanged (api 0, tests 37); ruff clean.

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

All five recommendations from this round are now closed (1, 2, and
3 RESOLVED; 4 and 5 are deliberately-deferred). The "two newly-
largest files" table is empty. Both filed follow-ups from the
repository-split plan have also landed (legacy entity-method
deletion + transaction-pattern standardisation, see
`journal/260507-repository-cleanup-followups.md`).

The standing facts table below is the source of truth for what to
look at next. Remaining natural follow-ups (file by importance,
not urgency):

1. **Recommendation 4 (item-3 residual cleanup)** — only worth
   touching if a specific cluster of the 37 reach-ins surfaces real
   friction during unrelated work.

The `auth_api.py` split landed on 2026-05-08; with it, all three
item-6 exceptions are dispositioned (split, split, or
acknowledged-permanent) and there is no remaining file in the
"acknowledged-but-pending" bucket.

None of the remaining points is urgent. **Stop here is also a fine
choice** — the reach-in grep gate catches regressions in the
meantime.

---

## Standing facts (verify before acting)

These snapshots are accurate as of 2026-06-10 (file sizes, reach-in
counts, and test counts re-verified after the quality round). Each has a
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

Expected: 61 (re-measured 2026-06-10; was 37 after item 7 on
2026-05-09). The rise comes from the fitness, storylines, and
quality-round test waves landed between 2026-05-10 and 2026-06-10 —
the new sites have not been re-bucketed against the four categories
below. Re-bucket (and push back on any avoidable reach-ins) the next
time a refactor session touches the affected suites; a further *rise*
beyond 61 means a new test reached into private state and should be
addressed at the source.

Residual breakdown as of 2026-05-09 (what made up the original 37):
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

Top-10 sizes (re-measured 2026-06-10):

| File | Lines | Status |
|---|---:|---|
| `services/storylines/service.py` | 879 | Acknowledged — generation orchestrator from the storylines cycle (2026-05-12). Same shape-argument as `entity_extraction/service.py`: mostly inherent pipeline glue. Split when next materially touched, or if it crosses ~1000 lines. |
| `services/notifications.py` | 877 | Acknowledged — grew from 744 via the Pushover topic additions. Natural seam exists (toast vs Pushover dispatch); split when next touched. |
| `services/entity_extraction/service.py` | 809 | Acknowledged-permanent (see table above). Unchanged. |
| `db/fitness_repository.py` | 807 | Acknowledged — three table families (activities / daily wellness / auth state) in one repo; the `db/repository/` package pattern applies cleanly. Split when next touched. |
| `cli/fitness.py` | 803 | Acknowledged — per-command bodies, mostly argument plumbing + output formatting. Split per command group (auth / sync / backfill / audit) when next touched. |
| `providers/transcription.py` | 778 | Within range. |
| `cli/__init__.py` | 774 | Within range (grew from 620). |
| `providers/ocr.py` | 773 | Within range. |
| `mcp_server/bootstrap.py` | 768 | Within range (grew from 475 via fitness + storylines wiring). |
| `services/jobs/runner.py` | 742 | Within range (grew from 471 via fitness + storylines job types). |

Two former over-cap entries were split on 2026-06-10 (PR #36):
`api/ingestion.py` and `api/fitness.py` — the largest fragments are
now `api/ingestion.py` at 541 and `api/fitness_garmin.py` at 525
lines, both under the cap. `cli/_seed_samples.py` (679, pure data)
dropped out of the top-10. Largest `auth_api/` file is still
`account.py` (~355 lines).

### Test counts

```bash
uv run pytest -q -m 'not integration' 2>&1 | grep -E 'passed|failed' | tail -1
CHROMADB_HOST=localhost CHROMADB_PORT=8401 uv run pytest -m integration -q 2>&1 | grep -E 'passed|failed' | tail -1
```

Expected on a clean main branch (re-measured 2026-06-10): 2583 unit +
11 integration = 2594 total.

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
