# `mcp_server.py` split — planning round

Round-3 Recommendation 2 from `docs/refactor-round-3.md`. Read-only
planning output. The extraction is intended to land next, in this same
session, if the decisions below are approved.

---

## Standing facts (verified 2026-05-07)

- `src/journal/mcp_server.py`: **1513 lines**.
- Tests on main: 1800 unit + 8 integration = 1808.
- Reach-in residual (tests → private state): **37**.
- Top-of-file size leaders unchanged from round 3 doc.

---

## What the file is doing

The 1513 lines fall into six clusters:

| Lines | Cluster | What it owns |
|---:|---|---|
| 1 – 42 | Header | Module docstring, imports, `_services` global. |
| 44 – 461 | `_init_services()` | The full constructor: db, vector store, providers, chunker, entity store, mood scoring, runtime settings + on-change callback, ingestion, jobs, health poller, atexit, auth, email, services dict assembly. |
| 463 – 475 | App wiring | `lifespan`, `mcp = FastMCP(...)`, three `register_*_routes` calls. |
| 477 – 503 | Ctx helpers | `_get_query`, `_get_ingestion`, `_get_entity_extraction`, `_get_entity_store`, `_get_job_runner`, `_get_job_repository`, `_user_id`. |
| 506 – 1428 | `@mcp.tool()` registrations | 18 tool functions + two job helpers (`_job_to_tool_dict`, `_poll_job_until_terminal`). |
| 1430 – 1513 | `main()` + `__main__` | uvicorn config, middleware stack, `anyio.run`. |

Within `_init_services`, the **runtime-settings on-change callback** (lines 199 – 313, ~115 lines) is by far the largest sub-block — it owns OCR / mood / formatter / heading-detector swap logic and is a closure over four locals.

---

## Proposed package shape

Convert `mcp_server.py` to a package. Final layout:

```
src/journal/mcp_server/
  __init__.py        ~50  facade; re-exports public symbols (compat)
  __main__.py         ~5  `from .runserver import main; main()`
  app.py             ~10  mcp = FastMCP(...); register_*_routes(mcp, ...)
  bootstrap.py      ~440  _init_services, lifespan, _services global,
                          on-change callback (kept as a closure for now)
  runserver.py       ~80  main() — config gate, middleware, uvicorn
  tools/
    __init__.py       ~5  empty (or import side-effects only)
    _ctx.py          ~30  the seven ctx-helper functions
    queries.py      ~270  search, get_by_date, list_entries,
                          get_statistics, mood_trends, topic_frequency
    ingestion.py    ~330  ingest_media{,_from_url}, ingest_text,
                          ingest_multi_page{,_from_url}, update_entry_text
    entities.py     ~140  extract_entities, list_entities,
                          get_entity_mentions, get_entity_relationships
    jobs.py         ~220  _job_to_tool_dict, _poll_job_until_terminal,
                          extract_entities_batch,
                          backfill_mood_scores_batch, get_job_status
```

Every per-file estimate above is comfortably under the 800-line soft
cap. The biggest is `bootstrap.py` at ~440 — well below the cap and
within the same range as today's `services/entity_extraction/service.py` (808). The on-change callback sits inside `_init_services` as a closure (no extraction this round — see decision 4).

Why per-resource tools instead of one tools.py:

- A single `tools.py` would land at ~860 lines — over the 800 cap.
- The api/ layer already follows a per-resource pattern; mirroring it
  keeps mental load low.
- Bisecting a tool regression is easier when each cluster is its own
  file.

---

## Decisions to surface for sign-off

### 1. `mcp = FastMCP(...)` lives in `app.py`

The `mcp` instance is created once and decorated by every `@mcp.tool()`. Tools in `tools/` import it from `journal.mcp_server.app`. `bootstrap.py` also imports it (for the `lifespan` reference). No factory pattern — `mcp` is a module-level singleton, same as today.

`app.py` also makes the three `register_*_routes(mcp, lambda: bootstrap._services)` calls. The lambda closes over `bootstrap._services` (currently `mcp_server._services` — a module global). After the split, `app.py` imports `bootstrap` and uses `bootstrap._services` lazily through the lambda; the dict is populated when `_init_services()` runs.

**Risk:** import order. `app.py` must import `bootstrap` (for the
lambda); `bootstrap.py` must import `app` (for the `mcp` reference in
`lifespan`). Resolve via:

```python
# bootstrap.py
from journal.mcp_server.app import mcp  # for lifespan attachment
```

`lifespan` is registered when `mcp = FastMCP(...)` is constructed, but
the `_init_services` body it calls is in bootstrap. The cleanest order:

1. `app.py` defines `mcp` with a `lifespan` parameter that does
   `yield bootstrap._init_services()`. Importing `bootstrap` at the
   top of `app.py` is fine — bootstrap.py doesn't need anything from
   app.py at import time (only at lifespan-call time, which happens
   after both modules have finished importing).

Order this carefully during extraction. Verify by import-time-only
runs (`python -c "import journal.mcp_server"`) before running tests.

### 2. Test patch retargets — known surface

Tests currently patch six paths against `journal.mcp_server`:

| Path | Pattern | New target after split |
|---|---|---|
| `journal.mcp_server.ChromaVectorStore` | `patch(...)` | `journal.mcp_server.bootstrap.ChromaVectorStore` |
| `journal.mcp_server.load_config` | `monkeypatch.setattr` (×5) | `journal.mcp_server.bootstrap.load_config` |
| `journal.mcp_server._init_services` | imported / called | re-exported from `__init__.py` — no retarget |
| `journal.mcp_server._services` | read in `services/reload.py` docstring (only) | re-exported from `__init__.py` — no retarget |
| `journal.mcp_server.lifespan` | `from ... import` | re-exported from `__init__.py` — no retarget |
| `journal.mcp_server.<journal_*>` | 8 tool functions, `from ... import` | re-exported from `__init__.py` — no retarget |

Net retarget cost: **one `patch` call + five `monkeypatch.setattr`
strings**, all in `tests/test_lifespan.py`. Trivial mechanical edit.
Re-exports in `__init__.py` cover everything else.

### 3. `__init__.py` re-export surface (for back-compat)

```python
# journal/mcp_server/__init__.py
from journal.mcp_server.app import mcp
from journal.mcp_server.bootstrap import (
    _init_services,
    _services,
    lifespan,
)
from journal.mcp_server.runserver import main
from journal.mcp_server.tools.entities import (
    journal_get_entity_mentions,
    journal_get_entity_relationships,
    journal_list_entities,
)
from journal.mcp_server.tools.ingestion import (
    journal_ingest_media,
    journal_ingest_media_from_url,
    journal_ingest_multi_page,
    journal_ingest_multi_page_from_url,
    journal_ingest_text,
    journal_update_entry_text,
)
from journal.mcp_server.tools.jobs import (
    journal_extract_entities_batch,
    journal_backfill_mood_scores_batch,
    journal_get_job_status,
)
from journal.mcp_server.tools.queries import (
    journal_search_entries,
    journal_get_entries_by_date,
    journal_list_entries,
    journal_get_statistics,
    journal_get_mood_trends,
    journal_get_topic_frequency,
)
```

`__init__.py` is purely a facade. No logic, no module-level side
effects beyond imports. Note: importing the tools modules has the
side effect of running every `@mcp.tool()` decorator, which is what
registers the tools with FastMCP. **This is required behaviour** —
removing or deferring those imports would silently break tools.

### 4. The on-change callback stays inside `_init_services`

The 115-line callback closes over four locals (`config`,
`runtime_settings`, `ingestion_service`, `job_runner`). Extracting it
to a separate `runtime_settings_callback.py` requires either:

- Passing a mutable services-dict to a factory and populating it after
  construction, or
- Threading every captured local through as parameters (signature is
  `Callable[[str, Any], None]` — fixed).

Both add complexity that doesn't pay back unless the callback is
independently tested. Today there are no direct tests of the
callback; it is only exercised end-to-end via runtime-settings PATCH
endpoints. **Recommendation:** keep it inline in `_init_services` for
this split. Revisit if/when we add direct callback unit tests.

### 5. `python -m journal.mcp_server` keeps working via `__main__.py`

CLAUDE.md uses `python -m journal.mcp_server` to start the dev server.
Once the file becomes a package, `python -m journal.mcp_server` runs
`journal/mcp_server/__main__.py`, which we add as:

```python
# journal/mcp_server/__main__.py
from journal.mcp_server.runserver import main

if __name__ == "__main__":
    main()
```

`pyproject.toml` `[project.scripts]` only references `journal.cli:main`
— there is no entry-point referencing `mcp_server:main`, so no
pyproject change is needed.

### 6. Commit shape (3 commits)

To keep each commit bisectable:

1. **Commit A — package shell, no behavior change.** Move `mcp_server.py` to `mcp_server/_legacy.py`, add `mcp_server/__init__.py` that re-exports everything from `_legacy`. Add `__main__.py`. Run full suite — must be green.
2. **Commit B — split _legacy.** Carve `_legacy.py` into `bootstrap.py`, `app.py`, `runserver.py`, and `tools/{_ctx,queries,ingestion,entities,jobs}.py`. Delete `_legacy.py`. Update `__init__.py` re-exports to point at the real modules. Run full suite.
3. **Commit C — retarget test patches.** Update the 6 patch paths in `tests/test_lifespan.py` from `journal.mcp_server.{load_config,ChromaVectorStore}` to `journal.mcp_server.bootstrap.{load_config,ChromaVectorStore}`. Run full suite.

After commit B the test suite may have intermittent retargeting
failures (since the patches now bind to a re-export, not the symbol
that `_init_services` actually reads at runtime). Watch for them and
roll the fix into commit C. If commit B passes the suite as-is —
because re-export keeps the binding alive at the test path — commit C
is a no-op and can be dropped.

---

## What this plan does NOT do

- **Does not extract the on-change callback** (decision 4).
- **Does not split `db/repository.py`** — that is round-3
  Recommendation 3, deliberately deferred per the round-3 doc to its
  own session.
- **Does not change tool behavior, signatures, or docstrings.** The
  only edits to the 18 tool functions are the import path of the
  decorator (`from journal.mcp_server.app import mcp` instead of using
  the local `mcp`).

---

## Acceptance criteria for the extraction (next phase)

1. `find src/journal/mcp_server -name '*.py' -exec wc -l {} +` shows every file under 500 lines.
2. `uv run pytest -q -m 'not integration'` passes (1799 unit; 1800 if any new tests are added).
3. `uv run ruff check src/ tests/` passes.
4. `python -c "import journal.mcp_server; from journal.mcp_server import main, lifespan, journal_ingest_text"` succeeds (re-export surface intact).
5. `python -m journal.mcp_server` boots the server (smoke test — interrupt after the route-list log).
6. The reach-in grep gates from `docs/refactor-round-3.md` show: tests `~37`, api `0`.

If all six pass, land the three commits in order and update
`docs/refactor-round-3.md` Recommendation 2 to “RESOLVED” with a
journal entry pointer.
