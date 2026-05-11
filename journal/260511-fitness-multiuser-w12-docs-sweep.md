# W12 — Fitness multi-user plan docs sweep (server)

Date: 2026-05-11
Plan: `docs/fitness-multiuser-plan.md` §5 W12
Branch: `worktree-eng-fitness-w12-docs-sweep`

## What shipped

Docs-only unit. No source changes. Six docs updated to reflect the
post-multi-user-plan reality (webapp connect flow is the primary path,
CLI is operator fallback, Garmin has no global env vars, every fitness
endpoint is documented):

- `docs/fitness-operations.md` — §2 restructured. Webapp paths
  promoted to primary (new §2a Garmin, §2b Strava — the former "preview"
  sections, with "preview" labels and the W8/W9 "lands later" phrasing
  removed). CLI paths demoted to operator fallback (§2c Garmin,
  §2d Strava laptop, §2e Strava headless). The §2 intro and TOC
  renamed from "Initial re-auth" to "Initial connection" to match.
  §6 troubleshooting cross-reference updated from §2b → §2e to match
  the renumbering.
- `docs/api.md` — intro paragraph for "Fitness endpoints" rewritten:
  was "Five endpoints expose the W4–W13 fitness pipeline" (stale; the
  doc already had 12), now names the 12 and points readers at the
  multi-user plan for posture. POST `/api/fitness/sync/{source}` 503
  error description corrected: was "credential vars are unset … for
  Strava + GARMIN_USERNAME/GARMIN_PASSWORD for Garmin," now
  Strava-only with an explicit note that Garmin never 503s here
  post-W6 because Garmin is always wired (per-user credentials, no
  global env vars).
- `docs/jobs.md` — equivalent 503 fix at line 141. The wording about
  "fail-loud at submit time" survives; the GARMIN env-var reference
  is gone.
- `docs/configuration.md` — `STRAVA_REDIRECT_URI` description
  updated to clarify the prod value is the webapp callback URL
  (per multi-user plan D4 / W13 operator step) and the default value
  still drives the CLI listener used by §2e. Cross-reference anchor
  in the description updated from §2b → §2e.
- `docs/fitness-integration-plan.md` — added Q7 "Multi-user pivot
  (2026-05-10)" in §6 Resolved questions. §2 Scope item that
  previously listed "Multi-user fitness data" as out of scope is
  struck through and redirected to the multi-user plan.
- `docs/roadmap.md` — `fitness-multiuser-plan.md` added to "Active
  planning docs" alongside `fitness-integration-plan.md`. Tier 1 #1's
  deferred list has "in-app re-auth flow" struck through with a note
  pointing at the multi-user plan units that delivered it.

## Open questions resolved before editing

The brief flagged three things to read before designing the changes:

1. **Does `docs/api.md` already document fitness endpoints, or is the
   whole section absent?** Already documented — all 12 endpoints are
   present and the response shapes match the code. W12's job here is
   accuracy fixups, not a structural add. Two real fixes: the intro
   paragraph said "Five endpoints" and the sync-route 503 still named
   GARMIN_USERNAME/PASSWORD.
2. **Does `docs/roadmap.md` still have the "in-app re-auth flow"
   deferred item under Tier 1 #1?** Yes — it sits in the deferred
   follow-ups paragraph alongside the `--code` CLI flag, watermark
   fix, etc. Crossing it out with a "shipped, see plan" annotation is
   the cleanest move (keeps the historical list intact).
3. **Is `docs/fitness-integration-plan.md` §6 Q2 the right anchor for
   the multi-user pivot amendment?** No — Q2 in that doc is about
   token storage (SQLite vs file), not single-user posture. The
   single-user posture is in §2 Scope. Adding Q7 in §6 with a
   strike-through update to §2 Scope is the right shape; that keeps
   the Resolved-questions section as a chronological narrative of the
   plan's evolution.

## Decisions

### 1. The webapp paths get the §2a/§2b slots, not the previous "tucked at the end" §2d/§2e.

Reordering matters. A reader hitting §2 "Initial connection" today
should land on the webapp flow first — that's what every user does.
The CLI fallback is for operators who already know what they're doing.
Keeping the old ordering (CLI first, webapp last) would be coherent
with the plan's "ship the change, then update the docs" cadence but
would mis-prime new readers.

### 2. Q7 in fitness-integration-plan, not amend Q2.

Q2 is about token storage. Hijacking it for an unrelated pivot would
muddle both topics. Adding Q7 chronologically (2026-05-10) preserves
both the original decisions and the later evolution as a clean
narrative. The §2 Scope strike-through is the cross-reference that
makes the pivot visible from the doc's executive context, not just
buried at the end.

### 3. configuration.md gets a small touch, not a rewrite.

The W6 commit already reshaped the "wired on this server" paragraph
and dropped the GARMIN rows. W12's job there is sanity check.
Found one consistency issue worth fixing: the `STRAVA_REDIRECT_URI`
description still framed the env var purely as a CLI-listener bind
when the multi-user plan D4 elevates it to a webapp callback URL in
prod. Updated the description to name both purposes (webapp callback
in prod, CLI listener for dev) and refreshed the §2b → §2e anchor.

### 4. Keep the operator note in fitness-operations.md §1.

The §1 "Operator note (prod env hygiene)" still mentions
`GARMIN_USERNAME` / `GARMIN_PASSWORD` — telling operators these are
no longer read and may be removed from prod `.env`. The acceptance
criterion (`grep -r GARMIN_USERNAME docs/`) returns this hit, but the
mention is intentional: removing it would force operators to figure
out on their own why their old env vars stopped working. The
remaining `GARMIN_USERNAME` references in `docs/fitness-multiuser-plan.md`
and `docs/fitness-integration-plan.md` Q7 are similar — historical
references to what was removed, not configuration instructions.

## Local verification

- `uv run ruff check src/ tests/` → All checks passed.
- `uv run pytest -m "not integration"` → 2250 passed, 8 deselected
  (matches W11 baseline — no code changes in this unit).
- `grep -rn GARMIN_USERNAME docs/ | grep -v archive/` → only the
  explanatory references documented in decision #4. No active
  configuration mentions.
- All inter-doc cross-references checked: `configuration.md` →
  `fitness-operations.md` §2e anchor, `roadmap.md` →
  `fitness-multiuser-plan.md`, integration plan §2 Scope → Q7.

## Plan-vs-reality drift

The brief said "document the new endpoints" — they were already
documented (W2/W3/W5 commits dropped the doc blocks alongside the
code). The actual W12 task on api.md was narrower than the brief
suggested: two accuracy fixes in the existing fitness section, not a
new section. Worth recording so the W12 acceptance criterion ("Roadmap
and integration plan link this doc") reads accurately when the
multi-user plan is updated next.

The brief also called the change to fitness-integration-plan.md as
"amend the resolved Q2 (single-user posture) or add a Q7." Q2 in that
doc is *not* about single-user posture — it's about token storage.
The actual single-user posture lives in §2 Scope. Picked the cleaner
shape (Q7 + §2 strike-through) and noted this in decision #2.

## Next unit

W13 — Strava developer-app callback URL update. Operator step (no
code, no test suite). After deploy of the W3/W10 changes, operator
updates the Strava app's Authorization Callback Domain at
developers.strava.com to the prod webapp hostname and flips
`STRAVA_REDIRECT_URI` in the prod `.env`. Then W14 is the staging-gated
end-to-end verification with user 2.
