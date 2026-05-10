# Fitness tier plan — archived

**Date:** 2026-05-10. **Scope:** server repo, docs only. **Worktree:**
`eng-fitness-tier-plan-archive`.

With W15 (webapp views) shipped at journal-webapp `352145e` earlier today,
all 15 work units of the fitness integration tier plan are complete:
server-side W1–W14 at journal-server `c1422fc`, webapp W15 at
journal-webapp `352145e`. Per the project's docs lifecycle convention, the
tier plan is now archived.

## What changed

1. `docs/fitness-tier-plan.md` → `docs/archive/fitness-tier-plan.md`
   (`git mv` in this commit, history preserved). Top-of-file header
   replaced with a `**Status:** closed 2026-05-10.` block plus a
   per-unit closeout summary (W1–W15) and the deferred follow-ups list.
2. Internal `./fitness-*.md` links inside the moved file rewritten to
   `../fitness-*.md` so they continue to resolve from
   `docs/archive/`.
3. Inbound links updated in:
   - `docs/fitness-integration-plan.md` — header status line and the
     "execution sequencing" pointer now reference
     `archive/fitness-tier-plan.md`.
   - `docs/fitness-operations.md` — top-of-doc related-docs block and
     the §1 Strava-app-registration pointer.
   - `docs/fitness-pipeline.md` — top-of-doc related-docs block.
   - `docs/external-services.md` — Strava §App registration pointer.
   - `docs/roadmap.md` — index entry and Tier 1 Item 1 (renamed to
     "shipped 2026-05-10", status block rewritten to reflect the full
     W1–W15 completion, deferred follow-ups inlined). The Tier 1
     preamble note tweaked so the "ready to start now" framing
     doesn't contradict the shipped Item 1 sitting under it.
4. `docs/archive/README.md` — added the index row for the archived
   tier plan.

## Archive decisions for the other fitness docs

`fitness-integration-plan.md` and `fitness-schema.md` **stay active** —
the W14 journal entry already made this call (decisions doc + schema
reference are load-bearing as long as the running pipeline depends on
them) and W15's post-audit didn't change the calculus. They will be
revisited only if the schema is materially redesigned or the
decisions are superseded.

`fitness-operations.md` and `fitness-pipeline.md` also stay active —
operator runbook and engineer-facing data flow, both load-bearing
references for the production system.

The active `docs/` listing for fitness is now: `fitness-integration-plan.md`,
`fitness-schema.md`, `fitness-pipeline.md`, `fitness-operations.md`. Four
docs covering decisions, schema, data flow, operations — no execution
plan in the active set, which is the right shape for shipped work.

## Deferred (independent of this archive)

Five items remain for ad-hoc pickup, none blocking:

1. `fitness-reauth-strava --code <code>` flag for headless Strava
   re-auth.
2. W7 dense-backfill watermark fix (`normalized < fetched`
   under-projection).
3. Explicit `Rowing → other` activity-type map entry.
4. In-app re-auth flow (webapp-hosted OAuth roundtrip, removes the
   CLI dependency).
5. Mood × fitness correlation views (webapp, several work units).

## Test results

- `uv run ruff check src/ tests/`: **All checks passed** (smoke check
  to confirm no code files were edited — none were).
- No test changes; no test run needed (pure docs work).
