# Improvement plan — UI changes (mood-trend defaults, entity casing, stale entities)

Date: 2026-05-06. Server-side mirror of the canonical plan at
`/Users/john/projects/journal/webapp/.claude/worktrees/eng-ui-changes/.engineering-team/improvement-plan.md`.
Same content, included here so anyone working in the server worktree has it locally.

---

# Improvement plan — UI changes (mood-trend defaults, entity casing, stale entities)

Date: 2026-05-06. Worktree: `eng-ui-changes` in both repos.

## Approach

Cross-cutting work across two repos. Each work unit is scoped to one repo; cross-repo
dependencies (server API → webapp consumer) are called out explicitly. All work happens in
the matching `eng-ui-changes` worktree in each repo, producing two coordinated commits.

## Work units overview

| # | Repo | Title | Priority | Depends on |
|---|---|---|---|---|
| 1 | webapp | Mood-trends default = affect-axes group | High | — |
| 2 | server | Smart entity capitalization (write-time normalization) | Medium | — |
| 3 | server | Entity `is_quarantined` flag + repo + filter | High | — |
| 4 | server | Reject/rename hallucinated names + post-save sanity sweep | High | WU3 |
| 5 | server | Loosen merge-candidate detection for near-duplicate places | Medium | — |
| 6 | server | Docs: existing merge feature + production deployment | Medium | — |
| 7 | webapp | Surface quarantined entities in admin/list UI | Medium | WU3, WU4 |

WU1, WU2, WU3, WU5, WU6 are independent — implement in parallel. WU4 follows WU3. WU7
follows WU3 + WU4.

## WU1 — Mood-trends default selection = affect-axes group (webapp)

Replace the `agency` default (`stores/dashboard.ts:127, 240–248`) with the affect group's
members from `MOOD_GROUPS` (`utils/mood-groups.ts`). Update tests in
`stores/__tests__/dashboard.test.ts`. Visual-verify via Playwright.

Acceptance: only `joy_sadness` and `energy_fatigue` series visible on first load; affect
group label shows full state; user can still toggle freely; tests + lint green.

## WU2 — Smart entity capitalization (server)

New `services/entity_naming.py` with `smart_title_case()` (algorithm in eval report). New
`config/entity-casing-exceptions.toml` with operator-managed preserved-case overrides
(`iOS`, `IKEA`, `FC Barcelona`, etc.). Hook into `entitystore/store.py:create_entity()`.
Add `reload_entity_casing_exceptions()` in `services/reload.py` and an admin reload route.
Comprehensive test module + fixture updates across existing tests.

Acceptance: new entities normalized at write time; mid-word uppercase preserved; exceptions
honored; reload endpoint works; pytest + ruff green.

## WU3 — Entity quarantine flag (server)

New migration `0012_entity_quarantine.sql` adding `is_quarantined`, `quarantine_reason`,
`quarantined_at`. Update model + repository. Default-filter quarantined out of entity-list
and chart endpoints. New endpoints: `/api/entities/quarantined`,
`/api/entities/{id}/quarantine`, `/api/entities/{id}/release-quarantine`. Tests cover
migration + repository + endpoints.

Acceptance: migration applies; default lists exclude quarantined; release/quarantine
round-trip persists; tests green.

## WU4 — Reject/rename hallucinated names + post-save sanity sweep (server)

`providers/extraction.py:379` — replace warn-and-keep with longest-substring repair, fall
back to `pending_quarantine_reason` flag. `services/entity_extraction.py:extract_from_entry`
— add a sweep over touched entities; for any entity whose canonical_name is not a substring
of any mention quote or any mentioned entry's text, soft-quarantine via the WU3 helper.
Reproduction test pinned to the `Zij Kanaal C Zuid` flow.

Acceptance: hallucinated names auto-renamed when possible, quarantined otherwise; no row
deletion; reproduction test passes; logs at INFO clearly state the action taken.

## WU5 — Loosen merge-candidate detection (server)

Add a normalized-signature heuristic alongside the existing embedding-distance threshold:
whitespace-stripped, lowercased; entities of the same type whose signatures are equal or
short-substring-of-each-other become merge candidates. New tests cover the prod
`Zij Kanaal C *` triple.

Acceptance: new tests pass; existing tests pass; manually verifiable on prod DB.

## WU6 — Docs: merge feature + production deployment (server)

Add "Merging entities", "Quarantine", and "Casing normalization" sections to
`docs/entity-tracking.md`. New file `docs/production-deployment.md` covering compose layout,
containers, bind mounts, image source, update workflow, public exposure, operational
commands. Explicitly call out `:latest` pinning and manual update workflow as a known
fragility.

Acceptance: docs match the actual code and prod state; render cleanly.

## WU7 — Webapp: surface quarantined entities (webapp)

Add quarantine badge + filter + release action in `views/EntityListView.vue`. Show
quarantine reason in `views/EntityDetailView.vue`. New API client functions in
`api/entities.ts`. Tests for both views.

Acceptance: quarantined entities hidden from default view; visible under Quarantined
filter; release works round-trip; coverage thresholds met; visual verification via
Playwright.

## Risk summary

WU4 is highest-risk (LLM repair path). Soft-quarantine instead of delete; comprehensive
test coverage including the prod-incident reproduction. WU2's risk is test-fixture churn.
Others are low-risk.

## Out-of-scope (flagged)

1. Backfilling pre-existing duplicate-case entity rows — future writes consistent post-WU2;
   user cleans up via merge UI.
2. Production image pinning / Watchtower — `:latest` everywhere; documented but unchanged.
3. Embedding-matcher improvements — long-term; WU4 sweep is a pragmatic compensating
   control.
