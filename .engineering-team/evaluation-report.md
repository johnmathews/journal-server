# Evaluation report — UI changes (mood-trend defaults, entity casing, stale entities)

Date: 2026-05-06. **Cross-cutting evaluation**. Canonical copy lives in the matching
worktree on the webapp side at
`/Users/john/projects/journal/webapp/.claude/worktrees/eng-ui-changes/.engineering-team/evaluation-report.md`
— this file is a server-side mirror so anyone working in this worktree can read it without
hopping repos.

---

# Evaluation report — UI changes (mood-trend defaults, entity casing, stale entities)

Date: 2026-05-06. Cross-cutting: webapp + server. Scope is the three asks; this is **not** a
project-wide audit.

## Executive summary

1. **Ask #1 (mood-trends default)** is a one-line constant change in the dashboard store
   plus test updates. Webapp-only — no server change.
2. **Ask #2 (smart entity capitalization)** has a single chokepoint to hook
   (`SQLiteEntityStore.create_entity()` at `entitystore/store.py:260`). No backfill required,
   but a `(user_id, entity_type, canonical_name)` UNIQUE constraint exists with case-sensitive
   collation — `running` and `Running` can coexist as separate rows today. After the fix,
   new writes will be consistent; pre-existing duplicates stay as-is until manually merged.
3. **Ask #3 is misdiagnosed.** The orphan-cleanup logic *already works*. The `Zij Kanaal C
   Zuid` entity is **not** orphaned — it has one live mention (id 2742) on entry 83 with the
   *current* extraction-run UUID and the *current* text. The real bug is upstream: an LLM
   hallucination on the initial extraction was preserved by a warn-but-keep policy at
   `extraction.py:379`, and on re-extraction the embedding matcher re-bound the corrected
   quote to the same hallucinated entity. User has approved (b) auto-rename to longest
   in-quote substring, falling back to (c) soft quarantine when no substring works.
4. The **merge feature is fully implemented** (REST + UI) but **missing from
   `docs/entity-tracking.md`** — that gap is what made it look like the feature didn't exist.

## Server-side findings

### Smart entity capitalization

**Chokepoint.** `SQLiteEntityStore.create_entity()` at
`src/journal/entitystore/store.py:260–282`. Currently applies only `canonical_name.strip()`.
Called only from `EntityExtractionService._resolve_entity()` at
`src/journal/services/entity_extraction.py:487`.

**Schema risk.** Migration `0011` defines `UNIQUE(user_id, entity_type, canonical_name)`
under SQLite default `BINARY` collation. Lookups use `LOWER(canonical_name)` (lines 235,
252) so reads are case-insensitive, but the unique constraint isn't.

**Plan.** New `services/entity_naming.py` with `smart_title_case()`. New
`config/entity-casing-exceptions.toml` (operator-managed, hot-reloadable via
`services/reload.py`). Hook into `create_entity()` before the insert.

**Tests:** `tests/test_entitystore/`, `tests/test_services/test_entity_extraction.py`,
`tests/test_api.py` will need fixture updates. New `tests/test_services/test_entity_naming.py`.

### Stale-entity bug — root cause

`extraction.py:379` warns when canonical_name isn't a substring of its quote, but creates
the entity anyway. On re-extraction after entry edit, the embedding matcher re-binds the
corrected quote to the existing hallucinated entity. The orphan GC correctly leaves the
entity alone because it now has 1 valid mention. **Three defects:**

1. `extraction.py:379` should reject (or rename to longest-in-quote substring) instead of
   warning.
2. No post-extraction sanity check on touched entities.
3. Merge-candidate detector too strict for short multi-word place names — three
   `Zij Kanaal C *` rows in prod that should be flagged.

User approved fixes:
- Rename to longest-in-quote substring; fall back to soft quarantine.
- Soft quarantine = hidden from charts but kept in DB (preserves descriptions, aliases,
  manual edits).

### Merge feature — undocumented but works

`POST /api/entities/merge` at `api.py:2691–2750`, `merge_entities()` at
`entitystore/store.py:664–750`. Reassigns mentions, relationships, aliases; absorbed
canonical names become aliases on the survivor; `entity_merge_history` for audit. **Missing
from `docs/entity-tracking.md`.** Plan: add a "Merging entities" section.

### Production environment — undocumented

- Compose root: `/srv/media/docker-compose.yml` on the `media` VM.
- Containers: `journal-server` (port 8400, `ghcr.io/johnmathews/journal-server:latest`,
  restart `always`), `journal-webapp` (port 8402), `journal-chromadb` (port 8401, custom
  image not upstream).
- Bind mounts: `/srv/media/config/journal/{data,context,chromadb}` and
  `mood-dimensions.toml`.
- DB at `/srv/media/config/journal/data/journal.db` (12.6 MB). Query via
  `docker exec journal-server python3 -c '...'` (no sqlite3 binary in container).
- No auto-update — manual `docker compose pull && up -d`.
- Public exposure via separate Cloudflare Tunnel host on tailnet.

Plan: new `docs/production-deployment.md`.

## Bug candidates (server-side scope)

1. **[VERIFIED]** `extraction.py:379` warns but doesn't reject hallucinated canonical names.
2. **[VERIFIED]** No post-extraction sanity sweep.
3. **[SUSPECTED]** Merge-candidate detector misses near-duplicate place variants.
4. **[VERIFIED]** Entity casing inconsistent at write time.
5. **[VERIFIED]** Merge feature & prod env undocumented.

## Out-of-scope but flagged

Running on `:latest` with no auto-update is fragile. Pinning to SHAs or running Watchtower
with a label allowlist would be a meaningful robustness improvement. Not part of this work.
