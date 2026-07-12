# 1. Storyline recovery: list-timestamp bug + deterministic chaptering

**Date:** 2026-07-12 (continues [[260711-storyline-auto-extension-race-fix]])

Context: after shipping the ingestion-reliability fixes (W1–W6, PR #58), we ran
the recovery commands against the live `media` deployment for user 1's four
storylines (Fitness, Family, Atlas, Simmons & Simmons). Running it for real
surfaced two further problems.

## 1.1 Bug: storylines list showed stale "last generated" dates (PR #59)

**Symptom:** after re-generating a storyline, the list UI *still* showed the old
"last generated" date — the exact original complaint, persisting even with fresh
content.

**Cause:** the chapter-based generation path stamped only
`storyline_chapters.last_generated_at` (`record_chapter_generation_complete`),
never `storylines.last_generated_at` — the column the list reads.

**Fix:** `record_chapter_generation_complete` now also bumps the parent
storyline's `last_generated_at`, so every generation path (regenerate_chapter,
resegment, append) keeps the storyline-level timestamp in sync. Verified live:
all four storylines' list dates went current.

## 1.2 The sectioning narrator won't split — prompt and model both ruled out (PR #60)

**Symptom:** the chapter backfill (`resegment_storyline`) produced a single
1,500–1,700-word chapter per storyline instead of several ~250-word chapters.

**Investigation (all on live data):**
1. **Prompt.** Rebalanced `SECTIONING_SYSTEM_PROMPT` — hard 280-word cap, "must
   be MULTIPLE sections", `N/200` rule-of-thumb, plus a distinct hard-cap log.
   Re-ran: still one ~1,600-word section.
2. **Model.** The narrator ran `claude-opus-4-7`; overrode
   `STORYLINE_NARRATOR_MODEL=claude-opus-4-8` for the CLI exec (4.8 is the latest
   Opus, same request surface as 4.7 — safe drop-in). Re-ran: still one
   ~1,592-word section.

Conclusion: the model treats a storyline as one coherent topic and will not
self-split, independent of prompt or model tier.

**Fix — deterministic time-bucketing.** `resegment` now:
1. Estimates total narrative length from the cached `narrative_word_count` of the
   chapters it's replacing.
2. `k = round(est_words / max_chapter_words)`, capped at `_MAX_CHAPTERS = 20`.
3. Splits the date-ordered excerpts into `k` contiguous buckets
   (`_split_excerpts_contiguous`, snapping boundaries so a same-date run isn't
   split across chapters).
4. Narrates each bucket separately; each section flows through the existing
   `_derive_section_windows` + plan loop → one chapter per bucket, model-written
   title, contiguous window.

Estimate-unknown path narrates once to measure and reuses that result when
`k == 1` (no wasted call). A transient per-bucket failure preserves existing
chapters. Tests: `_split_excerpts_contiguous` unit tests + resegment
multi-chapter / single-chapter paths.

## 1.3 Ops notes

- Prod container on `media` had to be `docker compose pull journal-server &&
  up -d` before the new CLI subcommands existed (it was running the pre-fix
  image). CLI is invoked as `docker exec journal-server /app/.venv/bin/journal …`
  (the venv binary, not the default `python`). DB lives at `/data/journal.db`
  (host `/srv/media/config/journal/data/journal.db`); a pre-change backup is in
  `.../data/backups/*.20260711-235004.bak`.
- `backfill-storyline-chapters` and `recheck-storylines` run synchronously in the
  CLI process (the server's job pools are in a different process), so they do the
  regeneration inline.

## 1.4 Shipped

- PR #58 — ingestion race fix + user_id + coalescing + backfill/recheck CLIs + embedding fallback.
- PR #59 — storyline-level `last_generated_at` on chapter regen.
- PR #60 — deterministic chaptering by time-bucketing.
- PR #61 — docs for the chaptering + timestamp behaviour.
- PR #62 — docs staleness fixes found by a post-session adversarial freshness audit
  (README storylines link; `jobs.md` stale "ingestion follow-up on Pool B" framing;
  `configuration.md` had no storyline env vars; `CLAUDE.md` structure tree).

## 1.5 Verified live

All four of user 1's storylines went from 1 long chapter → 5–7 titled chapters, with
`last_generated` timestamps current. Known follow-up (not a bug): individual chapters
still run ~500–1000 words vs the ~250 target, because the narrator writes verbose prose
per bucket and a sparse storyline can't bucket below one entry per chapter — tightening
that means constraining the narrator's per-bucket output length.
