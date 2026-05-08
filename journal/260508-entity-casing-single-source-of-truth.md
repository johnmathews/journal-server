# Entity casing — single source of truth

**Date:** 2026-05-08

## What I noticed

Two screenshots from the dashboard and the entities admin disagreed:

- Dashboard "What I Write About" / Activities: `running` (lowercase), 16 mentions.
- Entities admin: `Running (Night Run, Run, Runs)`, 16 mentions, same row.

Both views read `entities.canonical_name` from the same row, so any disagreement must
mean we are normalising in two places and the two have drifted.

## Root cause

We had three overlapping casing systems:

1. Server `services/entity_naming.py::smart_title_case` — write-time, knows
   articles / Dutch particles / hyphens / exceptions TOML.
2. `config/entity-casing-exceptions.toml` — operator-managed override map.
3. Webapp `src/utils/entityName.ts::displayName()` — render-time title-caser
   with its own `ACRONYMS` set and `SPECIAL_WORDS` map. Replaced hyphens with
   spaces (so `pull-ups → Pull Ups`, conflicting with the server's `Pull-Ups`).

(3) was the original fix when the LLM was emitting raw lowercase names. (1) and
(2) were added two days ago (commit `54f28ba`, 2026-05-06). (3) was never
removed, and it has been masking the underlying DB state ever since — most
"activities" in the DB are still pre-feature lowercase strings, which the
admin view title-cased on the client and the dashboard chart did not.

## What changed

1. **Algorithm refinement** (`services/entity_naming.py`): replaced the
   whole-string `_has_midword_uppercase` short-circuit with a per-word check.
   Per word: preserve verbatim if fully uppercase length>1 (acronyms) or if
   uppercase appears after lowercase in the same word (`iOS`-style); otherwise
   title-case. Fixes `iOS app → iOS App` and the all-caps acronym case.
2. **Extraction prompt** (`providers/extraction.py`): activity examples are
   now Title Case (`Squash, Climbing, Morning Pages, Frisbee, Bible Study`),
   plus an explicit Title Case rule in the `Rules:` block.
3. **TOML expansion**: ported the union of webapp `ACRONYMS` and
   `SPECIAL_WORDS` into `config/entity-casing-exceptions.toml`. The TOML now
   has 103 entries and is the single config surface for casing exceptions.
4. **Backfill CLI**: new `journal renormalise-entity-casing` subcommand. Walks
   every row in `entities`, applies `smart_title_case` + the loaded
   exceptions, prints proposed renames in dry-run, writes them with
   `--apply`. Surfaces `(user_id, entity_type, canonical_name)` collisions
   without auto-merging — those need the merge UI.
5. **`update_entity` normalisation**: the admin edit path now also runs
   `smart_title_case`, so a manual rename can't reintroduce drift.
6. **Webapp**: deleted `src/utils/entityName.ts` and its tests. Replaced every
   `displayName(x)` call site with the raw `x` and `displayAliases(arr)` with
   `arr.join(', ')`. The webapp now renders whatever the server stores.

## After this lands, you must

Run `journal renormalise-entity-casing --apply` against any environment with
existing data so the DB reflects the new rules. Until you do, the dashboard
will keep showing the raw legacy names.

## Tests

- 9 new cases in `test_services/test_entity_naming.py` covering `iOS app →
  iOS App`, `eBay listing → eBay Listing`, `FOO bar → FOO Bar` and friends.
- 4 new cases in `test_cli.py` covering the backfill (dry-run, apply,
  exceptions, collision skip).
- 2 new cases in `test_services/test_entity_store.py` covering
  `update_entity` normalisation.
- 1 new prompt test asserting Title Case appears in the extraction prompt.

Server full unit suite: 1812 passed (was 1796). Webapp: 1323 passed (was
1354 — minus the deleted `entityName.test.ts` cases, plus the new
canonical-name-verbatim test).
