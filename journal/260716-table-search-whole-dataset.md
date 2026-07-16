# 1. Whole-dataset search params on /api/storylines and /api/jobs

**Date:** 2026-07-16
**Sibling change:** `journal-webapp` `webapp/journal/260716-table-infinite-scroll-and-search.md`
(frontend infinite scroll + search boxes that consume these params).

## 1.1 What changed

Added an optional `search` query param to two list endpoints so the webapp's table
search runs over the **entire table**, not just the current page. The match is
evaluated in SQL before `LIMIT`/`OFFSET`, and the returned `total` reflects the
filtered count, so pagination/infinite-scroll stays correct.

- **`/api/storylines`** — `search` matches (case-insensitive substring) against
  storyline `name` + `description`.
  - `db/storyline_repository.py`: `list_storylines` and `count_storylines` gained a
    `search` param; a module helper `_search_needle()` builds the `%needle%` (lowercased,
    trimmed, `None` when blank). Predicate `AND (LOWER(name) LIKE ? OR LOWER(description) LIKE ?)`
    added identically to both methods so the count matches the page.
  - `api/storylines.py`: reads `search` from query params, passes to both.
- **`/api/jobs`** — `search` matches against `id` + `type` + `error_message`.
  - `db/jobs_repository.py` `list_jobs`: appends
    `(LOWER(id) LIKE ? OR LOWER(type) LIKE ? OR LOWER(COALESCE(error_message,'')) LIKE ?)`
    to the shared `where_clauses`/`params`, so it applies to both the `COUNT(*)` and the
    row query automatically. Composes with the existing `status`/`type` filters.
  - `api/jobs.py`: reads `search` and passes it through.

This mirrors the pattern already used by `/api/entities` (`entitystore/store.py`).

## 1.2 Tests

TDD throughout — failing tests written first. Repository + route tests assert: matches
on each searchable column, case-insensitivity, whitespace-trim/blank-ignored, composition
with status/type filters, user isolation (storylines), and — the important one — that
`total` reflects **all** matches beyond the current page (seed >limit matches, assert
`total > len(page)`). Full unit suite green: **3252 passed**; coverage 88%; ruff clean.

## 1.3 Decisions / notes

- **LIKE wildcards (`%`/`_`) are not escaped** in the needle. This is deliberate — it
  matches the existing entity-search behavior. Escaping only these two endpoints would
  make them inconsistent with entity search. Practical impact is negligible (job ids are
  UUIDs; type identifiers contain literal `_` which still matches). Flagged as a known
  minor item; revisit only if literal-substring semantics are ever needed across all
  three search surfaces at once.
- No schema/migration changes — pure additive, revert-by-commit.
