# Doc cleanup and plan-hygiene conventions

Two threads of doc work in this session: targeted cleanup on two reference docs, then a
broader process change for how planning docs are written and maintained going forward —
applied retroactively to the existing planning docs in `server/docs/`.

## 1. Reference doc cleanup

### `docs/entity-tracking.md`

Three categories of fix:

**Stale code references.** The doc named `merge_entities()` as living in
`entitystore/store.py` around lines 664–757; it has since moved into `entitystore/merge.py`
(`_MergeMixin`). The post-extraction sanity-sweep matcher was renamed from
`_canonical_name_supported` (in `entity_extraction.py`) to `is_canonical_name_supported`
(in `entity_extraction/sanity.py` — module split into a package). Both fixed; line
ranges removed since they rot fast.

**Changelog leakage.** All three `WU4` references stripped — work-unit IDs are
internal-sprint jargon and meaningless after the sprint closes. The "previous near-miss
band was removed in WU4 because..." block rewritten as plain current-state description
(the Hermione/Neville rationale survived; the changelog framing didn't). The "older
revision short-circuited..." paragraph in the casing section replaced with forward-looking
design rationale.

**Awkward sentences.** Numbered list `1./2./3./4.` for the dedup stages converted to bullets
because the items are labeled `Stage 0/a/b/c` (which align with DB `match_source` values),
and double-numbering was confusing. "Two paths" header for quarantine fixed to "Three paths"
(it lists three). Several long compound sentences split. Typo "Re- running" → "Re-running".

### `docs/mood-scoring.md`

**Stale path.** `mcp_server.py` is now a package; the runtime callback for mood scoring
lives in `mcp_server/bootstrap.py`.

**Cost claim corrected via web verification.** The doc claimed switching to Haiku 4.5
"drops cost by ~5×" to ~$0.03/month. Verified current Anthropic pricing:
Sonnet 4.5 = $3/$15 per MTok (input/output), Haiku 4.5 = $1/$5. The actual multiplier is
**3×, not 5×**, and the monthly cost on Haiku is **~$0.06, not $0.03**. Fixed; also
dropped the dated alias `claude-haiku-4-5-20251001` for the unversioned `claude-haiku-4-5`
that the codebase uses everywhere else.

**Changelog leakage.** "Two **new** endpoints" → "two endpoints". The MCP-tool section
was three sentences of release-note framing ("still accepts", "is now... instead of",
"Existing LLM consumers... should still render correctly") — rewritten as plain reference
content. Misuse of "load-bearing" (used to mean its opposite) replaced.

The lesson on both docs: timeless reference content gets contaminated when changelog
phrasing leaks in. Words to grep for during future cleanup: `now`, `still`, `as before`,
`previous`, `unchanged`, `new` (in adjective position), and any work-unit/sprint ID.

## 2. Plan-hygiene conventions added to `/engineering-team` and `/done`

Acted on a piece of documentation-maintenance research that diagnosed real failure modes
in this repo's planning docs: shadow inventory (six refactor plans, none indexed from the
roadmap), missing status headers (most refactor plans started with just `# Title`), the
v1→v2 churn pattern (plans drafted without reading the code get rewritten as "v2" once
implementation reveals structural realities), and decisions buried under execution
sequencing.

I pushed back on three specifics — a cited example was wrong (`phase-2-brief.md` *did*
have a supersession header, just in a different format than the new convention); the 12k
character cap was arbitrary; and the "kill criteria for every plan" recommendation was
over-prescriptive. Implemented the core findings with those guardrails:

**`/engineering-team` (`~/.claude/skills/engineering-team/SKILL.md`):**

- Phase 2 Step 2 reinforced: read the code before specifying any change. *"If a subagent
  is proposing changes to a file, that subagent must have read the file."* Targets the
  v1→v2 churn directly.
- Phase 2 Step 3 gained a new "Plan hygiene and persistence" section covering status
  headers, roadmap indexing, decisions-first structure, length-as-warning-signal (not a
  hard cap), kill criteria (only for multi-week initiatives), and the supersession
  protocol. Scoped to **persistent** plans, not the one-shot
  `.engineering-team/improvement-plan.md` that Phase 3 consumes.

**`/done` (`~/.claude/commands/done.md`):**

- Phase 2 (Documentation) gained a "Planning-doc hygiene" sub-step: when a session creates
  or modifies a planning doc in `docs/`, ensure it has a status header, mark superseded
  plans (don't delete), index from `roadmap.md`, flag length issues without auto-reflowing.
  Explicit carve-out: reference docs (API guides, runbooks, architecture explainers) are
  exempt — they live by different rules.

## 3. Retroactive application to existing planning docs

Ten docs in `server/docs/` got status headers normalized to the new convention:

| Doc | Status |
|---|---|
| `roadmap.md` | active (header normalized + new "Active planning docs" index added near the top) |
| `tier-1-plan.md` | active (Items 2/3a/3b/4 shipped; 1 and 3c remain) |
| `security-roadmap.md` | active (Tier 1 complete; later tiers remain) |
| `refactor-round-3.md` | active (supersedes v2 and follow-ups) |
| `refactor-repository-plan.md` | active |
| `refactor-item-6-exceptions-plan.md` | active |
| `refactor-mcp-server-plan.md` | **closed** — split landed 2026-05-07 |
| `refactor-follow-ups.md` | **closed** — superseded by round-3 |
| `code-quality-refactor-plan.md` (v2) | **closed** — superseded by round-3 |
| `phase-2-brief.md` | **superseded** by roadmap (existing notice reformatted) |

The new "Active planning docs" section in `roadmap.md` is the index entry point —
follow a link, then read the linked doc's `Status:` header to see whether it's live,
closed, or superseded. Closed/superseded docs no longer require grep to discover.

## 4. Length flags surfaced (not acted on)

Six docs exceed the ~12k char or ~300 line warning threshold the new convention sets.
Surfaced rather than reflowed (per convention — don't reflow unprompted):

1. `roadmap.md` — 32k chars, 595 lines.
2. `refactor-item-6-exceptions-plan.md` — 27k chars, 632 lines (natural split: per-item).
3. `tier-1-plan.md` — 27k chars, 426 lines (mostly historical now; shipped detail could move to journal).
4. `refactor-repository-plan.md` — 21k chars, 449 lines.
5. `refactor-follow-ups.md` — 19k chars, 402 lines (closed; less urgent).
6. `refactor-round-3.md` — 18k chars, 349 lines.
7. `code-quality-refactor-plan.md` — 18k chars, 360 lines (closed).

The most actionable are #2 (clean split points along the three items) and #3 (a lot of
shipped-and-documented detail that could move to journal entries). Left for a future
pass — not chasing them down now.

## What I didn't change

- Reference docs (`docs/api.md`, `docs/development.md`, etc.) — convention is explicitly
  scoped to planning docs.
- The two untracked files in the working tree (`docs/fitness-integration-plan.md`,
  `journal/260508-fitness-integration-planning.md`) — pre-existing work I didn't touch
  this session. Confirmed with the user before commit.
