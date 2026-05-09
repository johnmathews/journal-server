# 260509 — docs archive + accuracy audit

End-to-end pass over `docs/`: archive superseded plans into `docs/archive/`, then re-audit every
surviving active doc against source code and prod state on `media`. Driven by user request to
make the active `docs/` listing easier to scan, and to verify the surviving docs are still
accurate after the round-1 → round-3 refactors and the recent entity / fitness work.

## Archive

Eight superseded planning docs moved into `docs/archive/`:

| File | Status header (already self-declared) |
|---|---|
| `code-quality-refactor-plan.md` | closed 2026-05-07, superseded by `refactor-round-3.md` |
| `refactor-follow-ups.md` | closed 2026-05-07, superseded by `refactor-round-3.md` |
| `refactor-repository-plan.md` | closed 2026-05-07 (split landed) |
| `refactor-item-6-exceptions-plan.md` | closed 2026-05-08 (all three items shipped) |
| `refactor-mcp-server-plan.md` | closed 2026-05-07 (split landed) |
| `phase-2-brief.md` | superseded 2026-04-11 by `roadmap.md` |
| `tier-1-plan.md` | closed 2026-05-09 (all four Tier 1 items done) |
| `audit-2026-05-09.md` | self-declared "should be archived once read" |

`docs/archive/README.md` indexes the archive and reiterates that nothing in there is load-bearing.

Inbound links in `roadmap.md`, `refactor-round-3.md`, and `code-quality-principles.md` were
rewritten from `./foo.md` to `./archive/foo.md`. Links inside archived docs that reference
active siblings (`roadmap.md`, `refactor-round-3.md`, `code-quality-principles.md`) were
rewritten from `./` to `../`.

## Audit (parallel reviewer subagents, code-grounded against `src/journal/` + prod on `media`)

Prod ground truth captured once via `ssh media`: container revision `1edb55e`, schema version
22, runtime settings (`ocr_provider=gemini`, `ocr_dual_pass=true`, mood scoring on, registration
on), 31 tables, /health output, 12-row pricing table.

Material findings fixed across active docs:

- **`ocr-context.md`** — major inversion: doc claimed Gemini was the dual-pass primary and
  Anthropic Opus the secondary. `_build_dual_pass_provider` in source actually hard-wires
  Anthropic Claude Opus 4.6 as primary and Gemini 2.5 Pro as secondary whenever
  `OCR_DUAL_PASS=true`, and ignores the runtime `ocr_provider` setting in dual-pass mode.
  Rewrote with the correct primary/secondary and cited source.
- **`external-services.md`** — same OCR inversion propagated through the production-stack
  table, the per-page walkthrough (3-page entry: 8 → 11 upload calls; 13 → 16 lifecycle calls),
  the cost estimate (~$0.125 for OCR alone in dual-pass), and the ASCII pipeline diagram.
- **`api.md`** — `sessions` → `user_sessions` table name; `/api/stats` response shape (single
  float not dict, per `Statistics` dataclass); broken link `entity-extraction.md` →
  `entity-tracking.md`; mood-dashboard route count (5 listed, not 4).
- **`configuration.md`** — removed `LOG_LEVEL` row (not read anywhere in source).
- **`development.md`** — flagged that `JOURNAL_SECRET_KEY` must be set (server fail-closes per
  `mcp_server/runserver.py:27-32`); corrected the `.env.example` ships-`REGISTRATION_ENABLED`
  claim (it doesn't).
- **`refactor-round-3.md`** — `runner.py` 423 → 471 lines (after-landing edits);
  `api/entity_merge.py` 326 → 406; re-measured Top-10 sizes to 2026-05-09 numbers; refreshed
  "snapshots accurate as of" date.
- **`security-roadmap.md`** — full rewrite (user explicitly asked for thorough). Recorded
  Argon2id parameters (m=65536, t=3, p=4), corrected Traefik claim (Cloudflare Tunnel + nginx
  is the actual edge now), added prod `MCP_ALLOWED_HOSTS` corroboration with file/line
  citations, added new Tier 3 item 15 (backup integrity / restore drill), added Out-of-Scope
  section to head off enterprise scope creep, renumbered everything.
- **`security.md`** — added status header, tightened scope sentence to call out single-VM home
  server + Cloudflare Tunnel posture, noted ChromaDB does not need backup, dated the
  `auth_api/` split.
- **`auth.md`** — corrected `registration_enabled` env-var claim (it's a runtime setting now,
  not env-only); expanded password reset flow (always-200 enumeration mask, session revocation
  on success); cited `mcp_server/runserver.py:27` for the secret-key fail-closed behavior.
- **`roadmap.md`** — collapsed Tier 1 (all four items shipped, content was duplicating Closed
  list entries 13–15 and 28); replaced verbose shipped-detail blocks in Tier 2 / Tier 3 with
  forward-pointers to Closed items; renumbered active items; fixed internal cross-refs that
  shifted. File ~42k → ~33k without losing roadmap content (deduplication only).
- **`architecture.md`** — added status header; corrected the false "each `cli/<command>.py`
  registers from `__init__.py`" claim (actual layout is single-file argparse with two handler
  modules); replaced subcommand list with the verified 16 names from `cli/__init__.py` (e.g.
  `reembed-entity` → `backfill-entity-embeddings`); fixed schema typo `entry_extraction_stale`
  → `entity_extraction_stale`.
- **`entity-tracking.md`** — added status header + TOC (file is 31k); fixed stale source-file
  reference for `extract_from_entry()` (now `services/entity_extraction/service.py`).
- **`fitness-integration-plan.md`** — replaced non-canonical table-style status header with
  canonical `**Status:** ... **Last updated:** ...` form; removed broken reference to
  not-yet-written `fitness-tier-plan.md`; added missing `fitness-schema.md` to Related docs;
  added TOC.
- **`fitness-schema.md`** — same canonical status header conversion; added TOC (file is 29k).
- **`jobs.md`** — corrected package-split file list; replaced `mcp_server.py` references with
  `mcp_server/bootstrap.py` (correct location of `reconcile_stuck_jobs` call); rewrote the
  retry-out-of-scope bullet to disclose the in-flight retry behavior (3/6/12/24/48-min
  exponential backoff, first-retry-only Pushover).
- **`sqlite-threading.md`** — added status header; pointed at the now-archived
  `refactor-follow-ups.md` for the historical context the connection.py docstring still
  references.
- **`code-quality-principles.md`**, **`mood-scoring.md`**, **`search.md`**,
  **`context-files.md`**, **`transcription-providers.md`**, **`production-deployment.md`** —
  verified accurate, no edits.

## Guidance

Updated `~/.claude/CLAUDE.md`, `~/.claude/skills/engineering-team/SKILL.md`,
`~/.claude/commands/done.md`, `journal/CLAUDE.md`, `server/CLAUDE.md`, `webapp/CLAUDE.md`:

- Removed the `~12k character / ~300 line` plan length cap. Replaced with "prefer shorter docs
  but no hard cap — let scope and detail required dictate length; if a doc is hard to re-read,
  prefer splitting (decisions doc + execution doc) or trimming restated background over
  truncating for length."
- Added an explicit archive lifecycle rule: when a doc is closed or superseded, add a status
  header to the top, `git mv` it into `docs/archive/` in the same commit, and update inbound
  links from active docs. The active `docs/` listing should only contain currently load-bearing
  material.

Also stripped `fitness-integration-plan.md`'s self-imposed "Length cap: ~12k characters"
discipline note to match the new global rule.

## Why this took the shape it did

The user's premise was right: the active `docs/` listing was full of closed plans and the
self-declared status headers made archival a mechanical decision. The accuracy audit on top of
that caught real bugs in the docs (OCR primary/secondary inversion in two places, table-name
drift in `api.md`, Tier 1 content duplicated across roadmap and tier-1-plan, `LOG_LEVEL`
documented but not read by code, `runner.py` line counts stale by 50). These would have rotted
into reader confusion without the second pass.

## Verification rounds 3 and 4 (same day)

After the initial commit landed, the user pushed for higher confidence. Three more
verification passes were run:

### Round 3 — link checker, typo checker, re-read of substantive rewrites

- **lychee** (offline link checker) on `docs/`: caught one stale cross-repo link in
  `development.md` that referenced the GitHub repo name `journal-webapp` instead of the
  local sibling-dir name `webapp`. Fixed.
- **codespell**: one typo fix (`unparseable` → `unparsable` in `search.md`). Other matches
  were either project terminology ("master plan") or false positives (`collections.deque`).
- **Re-read of three substantive rewrites** (`security-roadmap.md`, the `external-services.md`
  OCR walkthrough, the `roadmap.md` Tier 1 collapse) caught the dual-pass-OCR primary/
  secondary inversion bug repeated in **two more places** the original audit missed:
  - `external-services.md` "Vision LLM available providers" table had Gemini 2.5 Pro
    labelled "Current primary" and Anthropic Opus 4.6 labelled "Switchable alternative".
    Inverted: Anthropic is now "Current dual-pass primary", Gemini is "Current dual-pass
    secondary". The pyproject.toml dependency comment was also wrong.
  - `roadmap.md` Tier 3 #9 (OCR context priming evaluation) and #11 (grow glossary) both
    described prod as "Gemini primary, Anthropic shadow". Rewrote with the actual dual-pass
    posture.
  - `security-roadmap.md` item 7 (ZDR) had wildcard paths (`services/transcription_*`) that
    don't match the real module layout — replaced with `providers/transcription.py`,
    `providers/extraction.py`, `providers/ocr.py`.
  - `security-roadmap.md` item 16 (TOTP) overstated "Cloudflare Tunnel + Cloudflare Access
    posture" — only Tunnel is visible in the repo. Softened to "any Cloudflare Access
    policy that may be configured — verify before relying on it".

### Round 4 — cost-figure verification + line-citation sweep

- **Cost figures** cross-checked against the live prod `pricing` table (12 model rows
  on `media`, `last_verified=2026-04-23`). All per-token / per-minute rates verified.
  Per-call estimates and walkthrough math (per-page OCR, per-entry walkthrough, mood-
  scoring monthly, dual-pass $0.18/entry, ocr-context cache table, lifecycle ASCII)
  recomputed and confirmed. **One fix:** `search.md` reranker estimate `~$0.001 per
  search` → `~$0.015 per search` (was off by ~15× because Haiku 4.5 input alone over
  ~15K tokens at $1/MTok = $0.015). Math now shown inline so future readers can sanity-
  check.
- **Line-number citations** (every `file.ext:NNN` pattern in `docs/*.md`) verified
  against actual source. **One fix:** `roadmap.md` Closed item 24 cited `config.py:263`
  for the `JOURNAL_ENABLE_MOOD_SCORING` default; the literal `"true"` is on line 262.
  All other citations (in `security-roadmap.md`, `api.md`, `fitness-schema.md`,
  `sqlite-threading.md`) verified at exact line.

### Residual uncertainty (acknowledged, not fixed)

Items the verifiers flagged but couldn't resolve from the repo alone:

1. Provider data-retention / policy claims (Anthropic ZDR, OpenAI training opt-out) —
   not code-verifiable; need external policy review.
2. Handwriting benchmark scores (~91%/95%/100%) cited from "AIMultiple" — not
   independently verified.
3. The `gpt-5.4` row in the prod `pricing` table is unexplained provenance — could be
   a stub or test row.
4. `entity-tracking.md` "$0.0001 per entity" backfill cost looks ~25× high based on
   plausible token counts but hand-wavy enough that it wasn't worth fixing without
   knowing the input-size assumption.
