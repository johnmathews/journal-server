# `db/repository.py` split — planning round

Round-3 Recommendation 3 from `docs/refactor-round-3.md`. Read-only
planning output. The intent is to bring the proposed split back for
sign-off, then carry out the extraction in a follow-up session — one
mixin per commit, full suite after each. This doc mirrors the shape of
`docs/refactor-mcp-server-plan.md` so the two are easy to compare.

---

## Standing facts (verified 2026-05-07)

- `src/journal/db/repository.py`: **1603 lines**.
- Tests on main: 1800 unit + 8 integration = 1808 (per round-3 doc).
- Reach-in residual: api `0`, tests `37`. Both unchanged.
- Top-of-file size leaders: `db/repository.py` 1603 is the single
  largest source file in the repo. `mcp_server.py` (1513) was split
  into the `mcp_server/` package on 2026-05-07 and no longer appears
  in the top-10 — `bootstrap.py` at 475 is now the largest mcp_server
  file.
- Last commit before this doc: `1d57a2a` ("Docs: round-3 status update
  + journal entry").
- Zero `unittest.mock.patch("journal.db.repository.X")` call sites in
  the test suite (verified by grep). Test imports are all of the form
  `from journal.db.repository import SQLiteEntryRepository` /
  `EntryRepository`. Implication: re-exports in the new
  `repository/__init__.py` cover every existing import — no test
  patch retargets at all.

---

## What the file is doing

The 1603 lines fall into the structural pieces below. Per-method line
ranges are accurate as of 2026-05-07 — re-verify before extraction.

| Lines | Cluster | What it owns |
|---:|---|---|
| 1 – 27 | Header | Module docstring, imports, logger. |
| 29 – 87 | Module helpers + constants | `_SUPPORTED_BINS`, `_SUPPORTED_MOOD_BINS`, `_bin_start_sql(granularity, column)` — SQL expression generator for time-bucket starts (day / week / month / quarter / year). Used by mood + stats clusters. |
| 90 – 282 | `EntryRepository` Protocol | 41 method signatures — every public method on the class. |
| 284 – 298 | `_row_to_entry` | Sole row-converter helper. Builds `Entry` from `sqlite3.Row`. Used by 7 methods across core / search / stats. |
| 301 – 314 | Class header + `connection` property | `class SQLiteEntryRepository`, `__init__(self, conn)`, single instance field `self._conn`, public `connection` property. |
| 316 – 1603 | Methods | 41 instance methods (zero private helpers — every method is public). |
| 1127 – 1137 | Class constant | `_HEALTH_ROW_COUNT_TABLES` — fixed table list for the `/health` endpoint, used only by `get_ingestion_stats`. |

### Method-to-cluster mapping (41 methods)

| Cluster | Methods | Method count | Sum of method lines |
|---|---|---:|---:|
| **core** | `create_entry`, `get_entry`, `get_entries_by_date`, `list_entries`, `update_final_text`, `update_entry_date`, `delete_entry` | 7 | ~111 |
| **pages** | `add_entry_page`, `get_entry_pages`, `add_uncertain_spans`, `get_uncertain_spans`, `get_uncertain_span_count`, `verify_doubts`, `get_page_count` | 7 | ~107 |
| **chunks** | `update_chunk_count`, `replace_chunks`, `get_chunks` | 3 | ~62 |
| **search** | `search_text`, `search_text_with_snippets`, `count_text_matches` | 3 | ~90 |
| **mood** | `add_mood_score`, `replace_mood_scores`, `get_mood_scores`, `get_entries_missing_mood_scores`, `prune_retired_mood_scores`, `get_mood_trends`, `get_mood_drilldown` | 7 | ~241 |
| **stats** | `get_statistics`, `count_entries`, `get_calendar_heatmap`, `get_word_count_distribution`, `get_ingestion_stats`, `get_entity_mention_count` | 6 | ~303 |
| **analytics** | `get_entity_distribution`, `get_entity_trends`, `get_topic_frequency`, `get_writing_frequency`, `get_mood_entity_correlation` | 5 | ~277 |
| **legacy entities** | `add_people`, `add_places`, `add_tags` | 3 | ~41 |
| **Total** | | 41 | ~1232 |

The remaining ~370 lines are imports, the Protocol body, the two
helpers, blank lines, docstrings, and per-method type signatures.

### Cross-cluster method calls (the dependency graph)

| Caller (cluster) | → | Callee (cluster) | Notes |
|---|---|---|---|
| `create_entry` (core) | → | `get_entry` (core) | Refetch after INSERT. |
| `update_final_text` (core) | → | `get_entry` (core) | Refetch after UPDATE. |
| `update_entry_date` (core) | → | `get_entry` (core) | Refetch after UPDATE. |
| `get_topic_frequency` (analytics) | → | `search_text` (search) | **Only cross-cluster method call.** Resolves via `self` because both mixins compose into `SQLiteEntryRepository`. |
| `get_mood_trends` (mood) | → | `_bin_start_sql` (helper) | Module-level helper, cluster-agnostic. |
| `get_writing_frequency` (analytics) | → | `_bin_start_sql` (helper) | Same. |
| `get_entity_trends` (analytics) | → | `_bin_start_sql` (helper) | Same. |
| 7 methods across core / search / analytics | → | `_row_to_entry` (helper) | Same. |

There are **no private (`self._foo`) methods** on the class. The only
shared instance state is `self._conn`, touched by every method. Mixin
composition on `self` is therefore strictly sufficient — no cross-mixin
state threading needed.

---

## Proposed package shape

Convert `repository.py` to a package. Final layout:

```
src/journal/db/repository/
  __init__.py         ~25  facade; re-exports EntryRepository,
                           SQLiteEntryRepository (compat surface)
  protocol.py        ~250  EntryRepository Protocol
                           + _row_to_entry helper
                           + _bin_start_sql helper
                           + _SUPPORTED_BINS, _SUPPORTED_MOOD_BINS
  store.py           ~120  class SQLiteEntryRepository(_CoreMixin,
                             _PagesMixin, _ChunksMixin, _SearchMixin,
                             _MoodMixin, _StatsMixin, _AnalyticsMixin)
                           — module docstring, __init__, connection
                           property; nothing else
  core.py            ~190  _CoreMixin — entry CRUD + listing
                           + add_people / add_places / add_tags
                           (legacy, see decision 6)
  pages.py           ~140  _PagesMixin — pages, uncertain spans,
                           verify_doubts, get_page_count
  chunks.py           ~85  _ChunksMixin
  search.py          ~110  _SearchMixin — FTS5 search / snippets /
                           count_text_matches
  mood.py            ~275  _MoodMixin — scores + trends + drilldown
                           + prune + missing
  stats.py           ~340  _StatsMixin — corpus stats:
                           get_statistics, count_entries,
                           get_calendar_heatmap,
                           get_word_count_distribution,
                           get_ingestion_stats
                           (+ module-level _HEALTH_ROW_COUNT_TABLES),
                           get_entity_mention_count
  analytics.py       ~310  _AnalyticsMixin — entity / topic / mood
                           cross-axis analytics:
                           get_entity_distribution,
                           get_entity_trends,
                           get_topic_frequency
                           (calls self.search_text via MRO),
                           get_writing_frequency,
                           get_mood_entity_correlation
```

Eight per-cluster files. Largest is `stats.py` at ~340 lines, the
second largest is `analytics.py` at ~310, the third largest is
`mood.py` at ~275 — all well under the 500-line "comfortable" target
called out in `docs/code-quality-principles.md`. No file approaches
the 800-line soft cap.

This pattern mirrors `entitystore/` (Protocol + helpers in
`protocol.py`, shell class in `store.py`, mixins in topic-named
sibling modules) almost exactly. Methods stay bound to `self` for the
same reason as `IngestionService` and `SQLiteEntityStore` in round-2
item 4: every method touches `self._conn`, threading it through a
context dataclass would only duplicate the constructor surface.

### Why split `stats` from `analytics` (eight buckets, not seven)

The round-3 doc proposed six clusters and put everything aggregate
into a single `stats.py`. Counting actual method bodies, that single
file lands at ~620 lines — over the 500-line target and the largest
file in the post-split repo. Splitting on the natural seam between
"corpus-level descriptive stats" (counts, distributions, calendar,
ingestion health) and "cross-axis analytics" (entity / topic / mood
joins, time-bucketed trends) drops both modules below 350 lines. It
also localises the only cross-cluster method call
(`get_topic_frequency → search_text`) inside a single mixin.

### Why one `_row_to_*` helper deserves its own module slot

`_row_to_entry` is reached by 7 methods spanning 3 clusters. It must
live somewhere all clusters can import without a circular path through
`store`. Co-locating it with the Protocol in `protocol.py` matches
`entitystore/protocol.py` (which holds `_row_to_entity`,
`_row_to_mention`, `_row_to_relationship`, `_normalise`).

---

## Decisions to surface for sign-off

### 1. Mixins, not free functions

Confirmed by the round-3 process notes: "free functions worked
beautifully for the worker extraction in item 2 (clean dependency
boundary via `WorkerContext`). They didn't work for `IngestionService`
/ `SQLiteEntityStore` because each method reaches 5+ instance fields
and threading those through a context dataclass duplicates the
constructor surface." The repository is the same shape — every method
needs `self._conn` — so every mixin gets `self`, not a context.

### 2. `EntryRepository` Protocol + helpers live in `protocol.py`

```python
# db/repository/protocol.py
@runtime_checkable
class EntryRepository(Protocol):
    ...  # 41 method signatures, unchanged

def _row_to_entry(row: sqlite3.Row) -> Entry: ...
def _bin_start_sql(granularity: str, column: str = "entry_date") -> str: ...

_SUPPORTED_BINS = ("week", "month", "quarter", "year")
_SUPPORTED_MOOD_BINS = ("day", "week", "month", "quarter", "year")
```

`_SUPPORTED_MOOD_BINS` is currently used only by `_bin_start_sql`'s
internal branch and by `get_mood_trends`'s validation (`granularity
not in _SUPPORTED_MOOD_BINS`). It moves with the helper. Same for
`_SUPPORTED_BINS` (used by `get_writing_frequency` and
`get_entity_trends` for validation).

### 3. The shell `store.py` is tiny

```python
# db/repository/store.py
"""SQLite implementation of EntryRepository.

The Protocol and shared helpers live in ``protocol.py``. Per-resource
methods live in topic mixins (``core``, ``pages``, ``chunks``,
``search``, ``mood``, ``stats``, ``analytics``) and are pulled into
``SQLiteEntryRepository`` here.
"""
from journal.db.repository.analytics import _AnalyticsMixin
from journal.db.repository.chunks import _ChunksMixin
from journal.db.repository.core import _CoreMixin
from journal.db.repository.mood import _MoodMixin
from journal.db.repository.pages import _PagesMixin
from journal.db.repository.protocol import EntryRepository
from journal.db.repository.search import _SearchMixin
from journal.db.repository.stats import _StatsMixin


class SQLiteEntryRepository(
    _CoreMixin,
    _PagesMixin,
    _ChunksMixin,
    _SearchMixin,
    _MoodMixin,
    _StatsMixin,
    _AnalyticsMixin,
):
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn
```

About 25 lines of actual code. Mirrors `entitystore/store.py` exactly.

### 4. The cross-mixin call (`get_topic_frequency → search_text`) stays as `self.search_text(...)`

There is one and only one cross-cluster method call. Two options:

- **(a) Keep `get_topic_frequency` in `analytics.py`** and let it call
  `self.search_text(...)`. Both `_AnalyticsMixin` and `_SearchMixin`
  compose into `SQLiteEntryRepository`, so MRO resolves the call
  correctly. The implicit dependency is documented in the
  `analytics.py` module docstring.
- **(b) Move `get_topic_frequency` into `search.py`** to avoid any
  cross-mixin call.

**Recommendation: (a).** `get_topic_frequency` is shaped like other
analytics methods — it returns a `TopicFrequency` summary dataclass,
takes the same `(start_date, end_date, user_id)` parameters as the
other dashboard analytics, and is called from the same dashboard
endpoints. Moving it into `search.py` would push it next to the FTS
primitives, which is the wrong cohesion. The MRO-resolved call is
cheap and the dependency direction is monotone (analytics depends on
search; never the reverse). Document with a one-line comment at the
call site.

### 5. `__init__.py` re-export surface — required, not optional

Unlike `entitystore/__init__.py` (empty, because callers always
import from `journal.entitystore.store`), `db/repository.py` is
imported via the package root throughout the codebase. Verified
caller sites:

- 13 `from journal.db.repository import SQLiteEntryRepository` (in
  `cli/`, `mcp_server/bootstrap.py`, 12 test modules).
- 9 `from journal.db.repository import EntryRepository` (in
  `services/`).

After the split, `db/repository/__init__.py` MUST re-export both
names, or all 22 import sites need updating. Re-export is much cheaper
and keeps the existing import surface intact:

```python
# db/repository/__init__.py
"""Compatibility facade for the repository package.

Historical import path: ``from journal.db.repository import X``.
Continues to work after the split — see store.py and protocol.py
for the actual definitions.
"""
from journal.db.repository.protocol import EntryRepository
from journal.db.repository.store import SQLiteEntryRepository

__all__ = ["EntryRepository", "SQLiteEntryRepository"]
```

The two names are the entire compat surface. `_row_to_entry`,
`_bin_start_sql`, and the `_SUPPORTED_*` constants are not currently
imported from outside `repository.py` (verified by grep) and do not
need re-exporting.

### 6. Legacy `add_people` / `add_places` / `add_tags` — placement

Verified by grep: these three methods (~41 lines total) are declared
on the Protocol, implemented on the class, exercised by their own unit
tests in `tests/test_db/test_repository.py`, and **never called from
production code**. They predate the modern `entitystore/` package,
which now owns all entity persistence.

Three options:

1. **Delete them in a separate pre-split commit.** Lowest line-count
   cost, simplest end state. Requires verifying no integration test
   or downstream tool uses them — current grep is clean for `src/`
   but doesn't cover dynamic dispatch (none expected on a Protocol
   method, but worth a final once-over).
2. **Fold into `core.py`** as a small "legacy entity attach" section
   with a comment pointing at `entitystore/` for the modern path.
3. **Their own `legacy.py` module** — 41 lines is too small to deserve
   a module of its own.

**Recommendation:** option 2 for this split (keep behaviour
unchanged), with a follow-up item filed against round-3 to verify and
delete in a separate session. That keeps the split a pure structural
move with no behaviour delta. Option 1 is acceptable if the user
prefers to bundle the deletion — it's a small, isolated change.

### 7. `_HEALTH_ROW_COUNT_TABLES` becomes a module constant

Currently a class-level tuple at lines 1127–1137. Used by exactly one
method (`get_ingestion_stats`). After the split it lives as a
module-level constant in `stats.py`, alongside the method that reads
it. No reason to keep it on the class — there's no subclassing or
override pattern.

### 8. Transaction-pattern cleanup is OUT OF SCOPE

The current file mixes two transaction patterns: explicit
`self._conn.commit()` after an `execute(...)` (most write methods),
and `with self._conn:` context-manager blocks (`replace_chunks`,
`add_uncertain_spans`, `replace_mood_scores`, `verify_doubts`). The
context-manager form is safer (auto-rollback on exception); the
explicit form is more common.

Standardising on `with self._conn:` everywhere is a worthwhile
follow-up but **not part of this split**. Mixing the two refactors
makes both harder to bisect. File the standardisation as a separate
round-3 follow-up if it surfaces during extraction.

Same applies to the round-2 item 1.1 residual (cross-call
shared-connection race). The split moves code; it does not address
the race. Reopen criteria for item 1.1 are unchanged.

### 9. Test patch retargets — none required

Verified by grep: no `unittest.mock.patch("journal.db.repository.X")`
call sites in tests. Every test imports are `from
journal.db.repository import …`, and the re-export from the new
`__init__.py` keeps the binding alive at the same path. Net retarget
cost: **zero.**

### 10. Commit shape (3 commits, mirroring `mcp_server`)

To keep each commit bisectable:

1. **Commit A — package shell, no behavior change.** Move
   `repository.py` to `repository/_legacy.py`. Add
   `repository/__init__.py` re-exporting `SQLiteEntryRepository` and
   `EntryRepository` from `_legacy`. Run full suite — must be green.
2. **Commit B — split `_legacy`.** Carve `_legacy.py` into
   `protocol.py`, `store.py`, `core.py`, `pages.py`, `chunks.py`,
   `search.py`, `mood.py`, `stats.py`, `analytics.py`. Update
   `__init__.py` to import from `protocol` and `store`. Delete
   `_legacy.py`. Run full suite.
3. **Commit C — retarget test patches.** Expected to be a no-op,
   since there are no `patch("journal.db.repository.X")` sites
   today. If the suite passes after commit B, drop commit C.

Use AST-based deletion (`ast.parse` + `node.lineno` /
`node.end_lineno`) for the per-method extracts in commit B — same
technique that worked for `entitystore/` and `services/ingestion/`
in round-2 item 4. Hand-rolled regexes for "find a method's end"
miss multi-line signatures.

Run `uv run pytest -q -m 'not integration'` after **each** commit.
Do not bundle multiple mixin extractions into a single commit; the
size + risk profile of a clean per-mixin commit is what makes commit
B bisectable on a regression.

---

## What this plan does NOT do

- **Does not standardise transaction patterns** (decision 8). The
  `commit()` / `with self._conn:` mix carries forward unchanged.
- **Does not address the cross-call SQLite-connection race** (round-2
  item 1.1 residual). Reopen criteria from
  `docs/refactor-follow-ups.md` § 1.1 are unchanged.
- **Does not delete or refactor** `add_people` / `add_places` /
  `add_tags` (decision 6). Filed as a follow-up.
- **Does not change method signatures, return types, or SQL.** Every
  edit is structural (file moves, mixin class wrappers, import
  rewrites). Method bodies move byte-for-byte — verify by `git diff
  --stat` showing line counts that approximately balance.
- **Does not split `auth_api.py`** (840 lines, item-6 exception),
  `api/entities.py` (717), `services/entity_extraction/service.py`
  (808), or `providers/transcription.py` (778) — out of scope for
  this round.

---

## Acceptance criteria for the extraction (next phase)

1. `find src/journal/db/repository -name '*.py' -exec wc -l {} + |
   sort -rn` shows every file under 500 lines (target) — and every
   file under the 800-line soft cap (hard requirement).
2. `uv run pytest -q -m 'not integration'` passes on commit A, B,
   and C. Expected count: 1800 unit (no test additions in this
   refactor).
3. `CHROMA_HOST=localhost CHROMA_PORT=8401 uv run pytest -m
   integration -q` passes. Expected: 8.
4. `uv run ruff check src/ tests/` passes (no new lint findings).
5. `python -c "from journal.db.repository import EntryRepository,
   SQLiteEntryRepository"` succeeds (re-export surface intact).
6. The reach-in grep gates from `docs/refactor-round-3.md` are
   unchanged: api `0`, tests `37`.
7. `git diff --stat` for commit B shows the expected pattern: the
   new per-cluster modules sum to roughly the deleted `_legacy.py`
   line count (allow ~5% growth for per-module imports + class
   shells + docstrings).
8. `wc -l src/journal/db/repository/*.py` largest file is `stats.py`
   or `analytics.py`, both under 350 lines. No file is the new
   "biggest file in the repo" — that title returns to one of the
   item-6 exceptions (`auth_api.py` at 840).

If all eight pass, land the three commits in order, update
`docs/refactor-round-3.md` Recommendation 3 to "RESOLVED" with a
journal entry pointer (`journal/YYMMDD-repository-split.md`), and
remove `db/repository.py 1603` from the round-3 doc's "Two newly-
largest files" table.

---

## Sessions

Per the round-3 doc's process note ("Plan first, then extract"):

1. **This planning round** (commit on its own as a docs change). No
   code touched. Brings the proposed split back for sign-off.
2. **Extraction session(s).** Land commit A, then commit B (and
   commit C if needed). Single session is feasible — none of the
   per-cluster extractions in commit B require deep judgement once
   the boundaries from this doc are agreed. Estimate: 1.5–2 hours
   of focused work, dominated by commit B's per-mixin AST extract +
   suite-run loop.
