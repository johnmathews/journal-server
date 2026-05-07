# 2026-05-08 — Item-6 exceptions batch 1: `api/entities.py` split + `entity_extraction` reclassification

First two of the three items from
`docs/refactor-item-6-exceptions-plan.md`. Item 3 (`auth_api.py`
split) deferred to a focused session — the security-sensitive
surface deserves clean attention rather than tail-end-of-batch
attention.

## What landed

1. **`api/entities.py` split — Item 1** (`deceb1e`). The 717-line
   file is carved into:

   | File | Lines | Routes | Owns |
   |---|---:|---:|---|
   | `api/entities.py` | 425 | 9 | core CRUD + read sub-resources: list, detail, mentions, relationships, update, delete, alias lookup/add/delete |
   | `api/entity_merge.py` | 326 | 7 | merge / candidates / quarantine / merge-history |

   `api/__init__.py` registers both functions in
   `register_api_routes` (entities first, entity_merge second). The
   public surface (`from journal.api import register_api_routes`)
   is unchanged. Verified pre-flight that no test does
   `patch("journal.api.entities.X")`, so the second commit the
   plan reserved for retargets was dropped.

2. **`services/entity_extraction/service.py` reclassification —
   Item 2** (this commit). The umbrella plan independently
   confirmed the prior journal sketch's verdict: the 808-line
   orchestrator is the design, not a backlog item.
   `extract_from_entry` is ~300 lines of inherent integration
   glue, and `_resolve_entity` is a 132-line decision tree where
   extraction would need a 14-arg free function or an
   `ExtractionContext` dataclass that "moves lines, not
   eliminates them". Reclassified in `refactor-round-3.md` from
   "item-6 exception" (which implied "future split candidate")
   to "acknowledged-permanent" with an explicit trigger criterion:
   if the file ever crosses ~1000 lines, redesign the
   `_resolve_entity` decision tree as a state machine — do not
   propose another mechanical split.

## Notable decisions exercised

1. **Aliases stay with core entity metadata** (Item 1 plan
   decision 4). The alias-as-side-effect-of-merge implementation
   detail does not bleed into the file layout. All three alias
   routes (lookup, add, delete) live in `entities.py` next to
   the rest of the entity introspection surface.

2. **Merge-history goes with merge ops** (Item 1 plan decision 3).
   `GET /api/entities/{id}/merge-history` is shaped like a read
   on an entity but the data it returns is merge-audit only. Lives
   in `entity_merge.py` so the merge story stays in one file.

3. **Two-commit shape collapsed to one.** The plan reserved a
   second commit for test patch retargets if any existed. Pre-
   flight grep for `patch("journal.api.entities.X")` returned
   zero hits, so the retarget commit was dropped — same call as
   the repository split.

4. **Reclassification documents the trigger to revisit, not just
   the current verdict.** The "acknowledged-permanent" entry for
   `entity_extraction/service.py` includes a specific reopen
   criterion (file crosses ~1000 lines) and a specific approach
   (state machine refactor of the decision tree). This is the
   information that prevents a future planning round from
   re-walking the same path.

## How the extraction was done

A throwaway Python script (`_extract_entities.py`, deleted after
the commit) used `ast.parse` to walk into `register_entities_routes`,
collect the line ranges of every nested `@mcp.custom_route(...)`
async function, classify each by a hardcoded set of names matching
the plan's cluster mapping, and slice the source bytes into the
two new module bodies. Same AST-extraction technique that worked
for `mcp_server/`, `db/repository/`, and now `api/entity_merge.py`.

Two minor fixes after the script ran:

1. `typing.Any` was unused in `entity_merge.py` (the original
   file imported it for `update_entity` body type-hints, which
   stayed in `entities.py`). Removed.
2. Module docstrings were rewritten — the original file's
   docstring described all 16 routes, neither cluster owns all of
   them, and the navigational comments inside the registration
   function (`# ---- entity management: update / delete / merge ----`,
   etc.) were removed since they no longer demarcate anything.

## Acceptance criteria — all met

| # | Criterion | Result |
|---|---|---|
| 1 | Both new files under 500 lines | ✓ (425 + 326) |
| 2 | Unit tests pass | ✓ 1796 |
| 3 | Integration tests pass | ✓ 8 |
| 4 | ruff clean | ✓ |
| 5 | `from journal.api import register_api_routes` succeeds | ✓ |
| 6 | Reach-in gates unchanged | ✓ (api 0, tests 37) |
| 7 | Both new files carry their own module docstring | ✓ |
| 8 | Both functions registered in `register_api_routes` | ✓ |

## Where round 3 + the item-6 batch now stands

After this batch, the item-6 "exceptions still in place" list has:

1. `auth_api.py` (840) — split planned, deferred to a focused
   session.
2. `api/dashboard.py` (609) — marginally over-cap, leave alone
   unless it grows.

The new "acknowledged-permanent" sub-table has:

1. `services/entity_extraction/service.py` (808) — the
   orchestrator IS the design.

The size top-10 has shifted: `mcp_server/bootstrap.py` (475) is
gone (it dropped out of the top-10 after the api/entities split
made room for `api/ingestion.py` at 591 and
`providers/extraction.py` at 560 to take its place). The largest
file in the repo remains `auth_api.py` at 840.

## Files touched

- `src/journal/api/entities.py` — shrunk from 717 to 425.
- `src/journal/api/entity_merge.py` — new, 326 lines.
- `src/journal/api/__init__.py` — one new import + one new
  `register_*_routes` call.
- `docs/refactor-round-3.md` — Item 1 marked RESOLVED in the
  exceptions table; new acknowledged-permanent sub-table created
  with `entity_extraction/service.py`; top-10 size table refreshed;
  "what next" guidance updated.
- `journal/260508-item-6-exceptions-batch-1.md` — this entry.

## Next session

`auth_api.py` split per `docs/refactor-item-6-exceptions-plan.md`
§ Item 3. Three-commit shape (package shell + `_legacy.py`, then
carve, then optional patch retargets). Estimated 3 hours
including the security-sensitive code review at commit B and the
manual login → me → logout smoke test before considering the
session done.
