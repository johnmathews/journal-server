# 2026-05-09 — Roadmap + tier-1-plan audit and refresh

User flagged `docs/tier-1-plan.md` as stale and asked for the whole roadmap to be reviewed,
and for both docs to be updated. Did the audit, then committed the doc refresh as `28d260f`.

## What was actually stale

`tier-1-plan.md` was dated 2026-04-11 and last verified that day. Almost a month of work had
landed since. Concrete misses:

1. **Item 1 (entity-extraction first run)** — marked pending. In reality the entity tables
   have been populated and worked against in prod since at least 2026-04-13: auto-reextraction
   on save, the entity-distribution / entity-trends dashboard endpoints, the entire
   2026-05-06 → 2026-05-08 entity-quality program (casing, aliases, quarantine, merge
   candidates, dedup rejection memory, past-dismissals UI). The plan's T1.1 ops checklist
   was never explicitly run, but downstream features that depend on populated entity tables
   have been shipping for weeks. T1.1.b dedup-threshold tuning was never executed —
   `ENTITY_DEDUP_SIMILARITY_THRESHOLD` is still `0.88` — but no entry suggests anyone was
   blocked by that, so the implicit decision is "default holds."
2. **Item 3b.vii (frontend mood chart)** — marked ⏳ pending. Was actually shipped on
   2026-04-11 alongside the backend; `journal/260413-mood-scoring-deployment-fix.md`
   explicitly notes the plan was stale on the day it was written. Now lives in
   `webapp/src/views/DashboardView.vue:381` (`renderMoodChart`) with variance bands and
   grouped/ungrouped toggles plus a sibling mood-correlation chart added 2026-04-21.
3. **Item 3c (people + topic charts)** — marked pending, blocked on Item 1. Actually shipped
   2026-04-21 with renamed endpoints (`/api/dashboard/entity-distribution`,
   `entity-trends`, `calendar-heatmap`) and a CSS-grid heatmap rather than the
   `chartjs-chart-matrix` plugin. Open question 6 was decided against the plugin.
4. **Open question 2 (mood-scoring default)** — recommendation was default `False` (opt-in).
   Was reversed at deployment time on 2026-04-13: `JOURNAL_ENABLE_MOOD_SCORING` defaults to
   `true` (`config.py:263`). Toggleable at runtime from the webapp Settings page.

`roadmap.md` had been touched 2026-05-08 but only to update the linked-planning-docs section.
The Tier 1 / Tier 2 / Closed sections were ~6 weeks behind reality. ~30 closed items missing:
multi-user auth + tier-1 data isolation, unified Dashboard expansion, wake lock + voice
confidence, Pushover notification stack, multi-provider transcription, hybrid search, live
reload, settings/admin split, mood-dimension overhaul, the entity-quality program, and
refactor round 3 module splits.

## What got changed

`tier-1-plan.md`:

1. Status header → `closed 2026-05-09`. Added a closeout summary of all four items at the
   top of the doc.
2. In-body sections for Items 1, 3b.vii, and 3c updated to reflect what actually shipped
   (rather than what the original work-unit checklist said). Original work units preserved
   as historical reference so the planning trail is intact.
3. Historical note above Open Questions flagging which recommendations were reversed.

`roadmap.md`:

1. Active planning docs section refreshed; tier-1-plan now closed; `mood-scoring.md`,
   `search.md`, `transcription-providers.md` added with current-state notes.
2. Tier 1 — all four original items marked done; **fitness integration** promoted to Tier 1
   as the next active item that meets the "no upstream dependency" criterion.
3. Tier 1 Item 3 (Dashboard) rewritten with full chart + endpoint inventory (8 charts, 7
   endpoints).
4. Tier 1 Item 4 (Search) — noted the 2026-05-01 hybrid-search overhaul that drops the mode
   toggle.
5. Tier 2 Item 7 (entity extraction trigger UI) — marked superseded by
   auto-reextraction-on-save.
6. Deferred D7 (multi-user auth) marked resolved 2026-04-15.
7. Deferred D2 (entity chip stale cache) — risk note updated given auto-reextraction now
   runs on save.
8. Closed list expanded with items 23–56 covering ~6 weeks of work, grouped by workstream
   rather than per-commit.

Tier 3 polish items (10, 12, 13–18) and Tier 2 items 5/6 (entity graph viz, LadybugDB
experiment) verified still pending and left alone — no `/graph` route in the webapp router
and no `cytoscape` dep in `package.json`.

## How the audit was done

Launched a verification subagent to cross-check tier-1-plan claims against the current code
plus a survey of journal entries since 2026-04-11. The subagent's work surfaced the four
specific stale items above and confirmed the entity-extraction backlog isn't actually a
backlog. Then did the doc edits in the main thread. Total: one large evaluation pass +
~10 targeted Edits across the two docs.

## Notes for future plan-hygiene passes

1. The "Last updated" header on a long-lived planning doc lies easily — `roadmap.md` claimed
   2026-05-08 because someone edited the linked-docs section that day, but the body content
   reflected 2026-04-11. Touching the header should mean "I read the body and it's still
   correct," not "I edited a line." Worth treating as a rule next time.
2. When a plan describes a tier of work and that whole tier ships, the cleanest move is to
   close the plan immediately rather than letting it linger as "active" with stale checkboxes.
   The closeout summary at the top is enough to keep the rationale legible.
3. Promoting fitness integration to Tier 1 leaves a single visible "ready to start" item.
   That's the right shape — Tier 1 should never be empty (otherwise readers get stuck
   wondering what to do next) and it should never have ten things in it (otherwise it's
   indistinguishable from Tier 2).
