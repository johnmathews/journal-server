# 260510 — fitness-multiuser-plan review and revision

Lead-engineer review of `docs/fitness-multiuser-plan.md` (the draft that takes the fitness
pipeline from single-user to per-user). Today's pass produced ten substantive revisions
to the plan before it gets executed. The doc is now marked **ready for execution**.

## Review approach

Two parallel agents:

1. **Code-claim verifier.** Walked every line-numbered citation in §2 ("Current state
   verified") and confirmed the load-bearing facts about repository, schema, providers,
   workers, and MCP tools. Result: claims hold up *mostly* — two minor drifts (config.py
   line range off by one, "all seven GET endpoints" should be "all four"), no structural
   surprises. The schema really is multi-user-ready, the repository really does take
   `user_id` everywhere, the providers really are user-agnostic.

2. **Design stress-tester.** Took the seven decisions and the work-unit scoping under a
   skeptical lens, with web-fetched docs for `python-garminconnect`, `garth`, and the
   Strava OAuth flow. Result: several real gaps that would have surfaced painfully during
   W2/W3 implementation.

## Findings that drove revisions

The biggest single finding was that **the original D2 was based on a stale mental model
of `python-garminconnect`**. The library (current 0.3.x) has a first-class non-blocking
two-step API: `Garmin(..., return_on_mfa=True).login()` returns synchronously, and
`client.resume_login(state, code)` resumes after MFA. The original D2 specified a
"park a thread on a Queue + Event" pattern based on the assumption that `prompt_mfa` was
the only entry point. That whole machinery is unnecessary — the new D2/W2 have no
threading at all.

The second-biggest finding was that **neither pending-session token (Garmin MFA, Strava
OAuth state) was specified as user-bound in the original draft.** Without binding, a
leaked token (logs, screenshot, tab handoff) lets any other authenticated user complete
someone else's connect/MFA challenge. The fix is one line at issue (store user_id in the
pending entry) and one line at consume (assert match), but it had to be in the spec
explicitly. Now it is, and tests in W2/W3 explicitly cover the cross-user rejection path.

The third finding — and the one most likely to bite at runtime — was that the original
plan had **no concept of upstream account identity**. If a user disconnects Strava and
reconnects with a *different* Strava account, the new tokens upsert cleanly onto the
existing `fitness_auth_state` row, but the historical `fitness_activities` rows now
silently mix two real Strava users under one journal `user_id`. We can't detect this
after the fact unless we capture the upstream id at every connect from W2/W3 forward —
hence the new D8 and the "must be done at the *first* connect" framing.

The Strava-related rejection rationale in D4 had a separate subtle problem: it claimed a
backend-route callback wouldn't carry the user's session cookie cross-origin. That's not
generally true (`SameSite=Lax` cookies *are* sent on top-level GETs from OAuth
redirects). The decision to use a webapp-route callback is still right, but the *reason*
in the doc was unsound. Replaced with the actual load-bearing reason: the SPA owns its
error UX (denied authorization, expired state, exchange failure) and the alternative
needs a backend-to-frontend redirect dance. Cookie behavior is not what makes the
choice.

D3's "tokens are valid 365 days" is correct in the abstract but materially understates
the re-auth cadence in 2026. `garth` (the predecessor library) was deprecated 2026-03-28
because Garmin changed their auth flow, and Garmin has flagged **OAuth1 retirement on
2026-12-31** with a migration to OAuth2 PKCE in their developer portal. Every connected
user will need to reconnect before that date, possibly via a `python-garminconnect`
library bump that invalidates existing token blobs. The plan now says so, and the kill-
criteria section flags it as a forcing event on the calendar.

W5 had no idempotency or sync-conflict policy — double-clicking the Backfill button
would have enqueued two parallel orchestrator runs writing the same date window.
Existing `fitness_raw_*` upserts probably protect against duplicate rows, but "probably"
is not a spec. New rule, written explicitly: only one fetch job per `(user_id, source)`
runs at a time across both sync and backfill workers; colliding submits return the
existing `job_id` rather than enqueueing a fresh job.

Two operational hardening items needed adding because they cut across multiple workers:

- **Mid-run auth removal.** A user can disconnect a source while a sync or backfill is
  running. Workers must re-fetch `fitness_auth_state` at each provider call and fail the
  run cleanly with `error="auth removed during run"` rather than leaving rows stuck in
  `running`. This applies retroactively to the existing `fitness_sync_*` workers, not
  just the new backfill workers.
- **Auth-status flip verification.** D3's reliance on the "auth banner directs users to
  reconnect" flow is only useful if sync workers actually write
  `auth_status='broken'` on Garmin/Strava 401s. Verifying that path — and adding it if
  missing — is now part of W11.

Smaller fixes folded in:

- §2 "all seven GET endpoints" → "all four"
- §2 `config.py 412–429` → `412–430`
- W11 priority bumped Low → Medium (because the auth_status verification work makes it
  load-bearing, not cosmetic)

## What didn't change

The strategic shape was right: per-user Garmin (forced by Garmin's auth model) +
operator-global Strava app (one OAuth app per server) is the standard multi-tenant
pattern for this combo. The non-goals (no encryption-at-rest, no admin impersonation, no
token persistence durability) are sensibly scoped for homelab scale. The work-unit
ordering and the W14 end-to-end gate are unchanged.

## Plan status

`docs/fitness-multiuser-plan.md` is now **ready for execution** (revised 2026-05-10).
Subsequent unit-by-unit implementation can proceed against the revised D-sections and
W-units. The next session that picks this up should start with W1 (read-only audit) and
treat W2/W3/W5 as the highest-density units — each carries spec changes from this review
that will not be obvious from a casual re-read of the original draft.
