# 2026-06-10 — Quality round (engineering-team run)

One-day evaluate → plan → implement cycle over both repos, driven by the
engineering-team run at
`.engineering-team/runs/manual-20260610T160013Z/` (evaluation report +
improvement plan). This entry is the server-side round summary; the
matching webapp entry is `webapp/journal/260610-quality-round.md`.

## What the evaluation found

- The **fitness multi-user final mile** (PR #21) had sat green and
  unreviewed since 2026-06-04 — the only Critical-priority open item.
  Prod lacked the watermark race fix and Rowing still bucketed as
  `other`.
- Two **date-pinned webapp tests** failed on wall-clock alone; the
  server suite was green (2,491 passed) but with a HealthPoller
  shutdown blemish.
- Security gaps: redirect hops unvalidated in URL ingestion,
  reusable password-reset tokens, no auth rate limiting, register
  enumeration.
- Deploy config was broken-on-arrival (webapp compose stack had
  nothing listening on the mapped port; retired env vars lingering).
- Test-debt: MCP media-tool wrappers existence-only, zero
  `api/notifications.py` route tests, ChromaVectorStore only covered
  by auto-skipping integration tests.
- Docs drifted: CLAUDE.md structure map missing fitness/storylines
  entirely, stale test counts, stale shared-connection threading
  claims, roadmap header five weeks old.

## What merged today (server)

Fifteen PRs (#21, #23–#36) plus two dependabot PRs (#13 urllib3
security, #22 minor/patch group):

| PR | What |
|---|---|
| #21 | Fitness multi-user final mile W1–W6 (integrity tests, `--code` reauth, watermark race fix, vestigial audit, `row` canonical type + migration 0029, plan archival) |
| #23 | Drain the job queue on test shutdown instead of racing it |
| #24 | Hybrid search degrades to BM25-only when dense retrieval or rerank fails |
| #25 | Deterministic HealthPoller shutdown; barrier-based parallelism assertion |
| #26 | SSRF: validate every redirect hop in URL ingestion; exact-host Slack token |
| #27 | Auth hardening trio: single-use reset tokens, per-IP rate limiting, register/health hardening |
| #28 | Behavioral tests for MCP media ingestion tools + consistent error mapping |
| #29 | Cover notifications routes and remaining non-admin gates |
| #30 | Hybrid pipeline integration tests against real Chroma; unify `CHROMADB_*` env vars |
| #31 | Deploy config: fail-fast secret key, purge retired env vars, correct dev Chroma mount |
| #32 | Queue mood/entity follow-ups for every entry of a multi-entry image |
| #33 | Version the prod journal compose services in-repo (compose mirror) |
| #34 | Dep majors: pillow-heif 1.x, google-genai 2.x; pip-audit CI gate |
| #35 | Uniform API handler decorator — 503/JSON boilerplate + `to_thread` boundary |
| #36 | Split `api/ingestion.py` and `api/fitness.py` per the size rule |

## Key decisions

- **Reset-token hash binding.** Password-reset tokens are single-use
  by embedding a SHA-256 fingerprint of the user's *current* password
  hash in the token payload; validation compares against the live
  hash, so a successful reset (which changes the hash)
  self-invalidates every outstanding token. No token store, no
  migration; unknown emails get a random fingerprint so the failure
  mode is enumeration-safe too (PR #27).
- **In-process rate limiter.** Per-IP auth rate limiting is an
  in-process thread-safe fixed-window limiter keyed by `(ip, path)`
  (default 10 req / 5 min), applied as pure-ASGI middleware to the
  four auth POST routes only — not Redis/nginx, because
  single-instance deployment makes shared state unnecessary (PR #27).
- **`to_thread` boundary in the handler decorator.** The uniform
  `api/_handler.py` decorator centralises the 503/JSON boilerplate
  and runs each sync route body via `asyncio.to_thread`, keeping the
  event loop free; this is safe by construction because repositories
  take per-thread connections from `ConnectionFactory` (PR #35).
- **Compose-mirror provenance.** The prod compose file is versioned
  in-repo at `deploy/docker-compose.prod.yml` as a *mirror* with a
  sync-provenance header (fetched 2026-06-10, secret-scanned) — the
  VM remains the runtime source of truth, and observed drift was
  carried verbatim to be fixed VM-first (PR #33).

## Numbers

- Server test count **2,491 → 2,594** (2,583 unit + 11 integration;
  full suite green with dev Chroma up, 2026-06-10).
- Coverage held at ~86% through the round.
- Migrations now 0001 → 0029.
- `api/ingestion.py` 591→541 and `api/fitness.py` split (largest new
  fragment `api/fitness_garmin.py` at 525 lines).

## Docs sweep (W26, this branch)

CLAUDE.md structure map + test counts refreshed from real runs;
`docs/refactor-round-3.md` standing facts re-measured (test reach-ins
37 → 61, flagged for re-bucketing); roadmap reordered and globally
renumbered (Tier 2 → items 3–4, Tier 3 → items 5–12) with the
storylines anchor-edit gap marked CLOSED; both stale shared-connection
docstrings in `services/jobs/runner.py` rewritten (plus
`save_pipeline.py` comment and `docs/jobs.md`); the
`STRAVA_REFRESH_TOKEN` operator note removed after the prod `.env`
cleanup landed.

## Remaining follow-ups

- **W18 — fitness multi-user end-to-end verification with user 2**
  (the archived multiuser plan's W7/W14): never executed; the
  acceptance gate for the whole multi-user initiative. Human-gated,
  needs a prod session.
- **Gemini live smoke at next deploy** — google-genai 2.x major (PR
  #34) is covered by unit tests only; exercise OCR against the live
  API once deployed.
- **Strava token revocation** — the removed `STRAVA_REFRESH_TOKEN`'s
  old token should still be revoked in the Strava settings UI.
- **auth_api thread-shift** — the auth routes still do sync DB work on
  the event loop; shift them behind the PR #35 `to_thread` boundary
  when next touched.
