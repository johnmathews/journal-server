# 2026-05-07 — Round 3 Recommendation 3: db/repository.py split

The single biggest source file in the repo (1603 lines) was carved
into a 10-file package using the same mixin pattern as
`entitystore/`. Three commits on a worktree branch
(`eng-repository-split`), then merged to main.

## What landed

1. **Docs: planning round for db/repository.py** (`d06ac0e`). New
   `docs/refactor-repository-plan.md` (446 lines). Mirrors the shape
   of `refactor-mcp-server-plan.md`: standing facts, cluster table
   with per-method line ranges, proposed package layout, ten
   sign-off decisions, acceptance criteria. Read-only — no code
   touched.

2. **db/repository: convert to package — commit A** (`f690336`).
   Pure relocation: `repository.py` → `repository/_legacy.py` (git
   rename), new `__init__.py` re-exporting `EntryRepository` and
   `SQLiteEntryRepository` from `_legacy`. 1799 unit tests pass;
   ruff clean.

3. **db/repository: split package into real modules — commit B**
   (`98ee348`). `_legacy.py` carved into nine modules:

   | File | Lines | Owns |
   |---|---:|---|
   | `protocol.py` | 300 | EntryRepository Protocol + `_row_to_entry` + `_bin_start_sql` + `_SUPPORTED_*BINS` |
   | `store.py` | 56 | `SQLiteEntryRepository` class shell — `__init__`, `connection` property, MRO of seven mixins |
   | `core.py` | 185 | `_CoreMixin` — entry CRUD + listing + legacy `add_people`/`add_places`/`add_tags` |
   | `pages.py` | 134 | `_PagesMixin` — pages + uncertain spans + verify_doubts + get_page_count |
   | `chunks.py` | 82 | `_ChunksMixin` |
   | `search.py` | 109 | `_SearchMixin` — FTS5 |
   | `mood.py` | 272 | `_MoodMixin` |
   | `stats.py` | 357 | `_StatsMixin` — corpus stats |
   | `analytics.py` | 312 | `_AnalyticsMixin` — entity / topic / mood cross-axis analytics |

   `_legacy.py` deleted. `__init__.py` rewritten to import from
   `protocol` and `store`. Largest file is `stats.py` at 357,
   well under the 500-line target.

4. **Docs: round-3 doc + ocr-context corrections.** Recommendation 3
   marked RESOLVED, "two newly-largest files" both struck, top-10
   size table refreshed (largest in repo is now `auth_api.py` at
   840), "next session" guidance rewritten now that all five
   recommendations are closed. `ocr-context.md` repository pointer
   updated from `db/repository.py` to `db/repository/pages.py`.

## Decisions and notable departures from the round-3 sketch

1. **Eight clusters, not six.** The round-3 doc proposed a single
   `stats` module. Counting actual method bodies, that lands at
   ~620 lines — over the comfortable target and the largest file
   in the repo post-split. Split on the natural seam between
   "corpus-level descriptive stats" (counts, distributions,
   calendar, ingestion health) and "cross-axis analytics" (entity /
   topic / mood joins, time-bucketed trends). Both end up under
   360 lines, and the only cross-mixin call
   (`get_topic_frequency` → `search_text`) localises inside
   `analytics`.

2. **Re-export from `__init__.py` is required, not optional.** 22
   caller sites import `from journal.db.repository import …`
   (unlike `entitystore/`, where callers use
   `from journal.entitystore.store import …` and the package
   `__init__.py` is empty). Re-export keeps every existing import
   path working untouched.

3. **Zero test patch retargets.** Verified upfront by grep that
   no test does `patch("journal.db.repository.X")`. The third
   commit from the mcp_server pattern was therefore predicted to
   be a no-op and skipped — landed clean in two commits + the
   planning doc.

4. **Cross-mixin call kept as `self.search_text(...)`.**
   `get_topic_frequency` belongs in `analytics` (it returns a
   `TopicFrequency` summary, not a search primitive) but calls
   `search_text` through MRO. Documented at the call site.

5. **`_HEALTH_ROW_COUNT_TABLES` demoted from class to module
   constant** in `stats.py`. Only `get_ingestion_stats` reads it,
   and there's no subclassing or override pattern — keeping it on
   the class was historical accident.

6. **Legacy `add_people`/`add_places`/`add_tags` left in
   `core.py`.** Tested but never called from production code
   (verified by grep). Filed as a follow-up for separate
   verification + deletion. Kept here unchanged so the split is
   purely a structural move with no behaviour delta.

7. **`from __future__ import annotations` not used.** Matches the
   sibling `db/*.py` modules and dodges most TC001/TC003 ruff
   warnings (which fire when annotations are strings but callers
   want the type at runtime).

8. **Transaction-pattern cleanup deferred.** The original file
   mixes `self._conn.commit()` and `with self._conn:` — both
   patterns carry over unchanged. Standardising on the context-
   manager form is a worthwhile separate refactor.

## How the extraction was done

A throwaway Python script (`_extract.py` in the worktree, deleted
after commit B) used `ast.parse` to map every method to a
`(start_lineno, end_lineno)` range, then sliced raw source bytes
into the per-cluster modules. Same AST-deletion technique that
worked for `entitystore/` and `services/ingestion/` in round 2.

Bugs caught during commit B's first test run, all in the per-mixin
boundary work the script didn't anticipate:

1. Module-level `log = logging.getLogger(__name__)` — needed in
   four cluster modules (core, pages, chunks, mood), not just one.
2. `self._HEALTH_ROW_COUNT_TABLES` reference inside
   `get_ingestion_stats` — needed updating to bare
   `_HEALTH_ROW_COUNT_TABLES` after the demotion to module const.
3. Missing `from datetime import datetime` in `stats.py` (the
   `get_ingestion_stats` signature uses `now: datetime`).
4. Several unused imports flagged by ruff after dropping the
   `from __future__` line (e.g., `_SUPPORTED_MOOD_BINS` only used
   inside `_bin_start_sql` not at the call sites).

All four fixed before commit B was finalised. Total round-trip time
from "first test run after carve" to "1799 passing": about 10
minutes, dominated by re-running the suite after each fix.

## Acceptance criteria — all met

| # | Criterion | Result |
|---|---|---|
| 1 | Every file under 500 lines | ✓ (largest: `stats.py` at 357) |
| 2 | Unit tests pass | ✓ 1799 |
| 3 | Integration tests pass | ✓ 8 |
| 4 | ruff clean | ✓ |
| 5 | `from journal.db.repository import EntryRepository, SQLiteEntryRepository` succeeds | ✓ |
| 6 | api reach-in count | ✓ 0 (unchanged) |
| 7 | tests reach-in count | ✓ 37 (unchanged) |
| 8 | `db/repository.py` removed from top-10 size list | ✓ (largest is now `auth_api.py` at 840) |

Coverage: 85% total (above 80% threshold). The new package files
are 100% on shells, 91-92% on mood/search, and 49-61% on
stats/analytics — that pre-existing gap matches the original
file's coverage, not a regression caused by the split.

## Where round 3 stands now

All five recommendations from `docs/refactor-round-3.md` are
closed: 1, 2, and 3 RESOLVED; 4 and 5 are deliberately-deferred.
The "two newly-largest files" table is empty. The next-session
guidance in the doc was rewritten to reflect this — natural
follow-ups now are item-3 residual cleanup, the item-6 exceptions
(`auth_api.py`, `api/entities.py`, `services/entity_extraction/service.py`),
and the legacy-entity-method deletion filed by this split.

## Files touched

- New: `docs/refactor-repository-plan.md`,
  `journal/260507-round-3-repository-split.md` (this file),
  nine modules under `src/journal/db/repository/`.
- Renamed: `src/journal/db/repository.py` →
  `src/journal/db/repository/_legacy.py` (commit A), then deleted
  (commit B).
- Edited: `docs/refactor-round-3.md`, `docs/ocr-context.md`,
  `src/journal/db/repository/__init__.py` (twice — once at commit
  A, once at commit B).
- Untouched: every caller of `from journal.db.repository import
  …`. The re-export keeps the public path stable.
