# Storyline chapters: word-sized, titled, auto-split

**Date:** 2026-06-15
**Branch:** `eng-storyline-chapters`

## What and why

The deployed storylines feature modelled a **chapter as a hand-painted date
window**: one ever-growing "open" chapter per storyline, no titles generated, no
notion of length. The goal of this work was to make chapters **word-sized
(~200 words), titled, and auto-split**, so a storyline reads as a sequence of
short titled beats instead of one wall of prose.

Design (decided with the user before building):

- **Narrative-driven sectioning.** The narrator now emits an ordered list of
  titled sections; each chapter's date window is *derived* from its citations,
  not painted by hand. This keeps the existing open/closed/contiguous-window and
  append machinery intact.
- **Re-segment is opt-in.** Plain regenerate still just refreshes existing
  chapters' panels. Re-carving is a separate, explicitly-triggered path.
- **Hand-painted windows are sacred.** `boundary_locked` chapters are preserved;
  re-segment only re-sections the unlocked spans around them. `title_locked`
  preserves manual titles. An explicit `override_locked` repaints locked chapters.
- **Two triggers.** Manual re-segment (REST/MCP/regenerate modal) and ingest-time
  auto-split (the open chapter crossing the word ceiling).
- **Word count is a soft target** — semantic coherence wins; no UI badge.

## Work units

- **W1 — schema/config/locking** (`migration 0031`): added `title_locked`,
  `boundary_locked`, `narrative_word_count` columns; config knobs
  `STORYLINE_CHAPTER_{TARGET,MIN,MAX}_WORDS` (210/180/240). `rename_chapter` locks
  the title; add/split/date-edit lock the boundary; `set_chapter_word_count`.
- **W2 — sectioning narrator**: `generate_sectioned_narrative` returns titled
  `NarrativeSection`s parsed from in-band `## Title` markers in the Citations-API
  stream. Shared API/doc-building code refactored out of `generate_narrative`
  (which is unchanged). Out-of-band word counts are logged, never rejected.
- **W3 — re-segment service**: `resegment_storyline(storyline_id,
  override_locked=False)` runs one narrator call per unlocked span, derives +
  clamps section windows, and rebuilds chapter rows atomically via
  `rebuild_chapters` (close all → offset seqs → delete non-preserved → place at
  final seq → promote exactly one open, last). Panels written only for new
  chapters; locked chapters keep theirs.
- **W4 — two-mode regenerate**: `resegment`/`override_locked` threaded through the
  MCP tool, REST route, runner, and worker; mutually exclusive with `chapter_id`
  and `mode="append"`. Default regenerate path is byte-for-byte unchanged.
- **W5 — ingest auto-split**: an `auto_split` flag (set only by the
  extension-check ingest path) makes `regenerate` re-check the open chapter's word
  count and fire a one-shot `resegment_storyline` when it exceeds the ceiling.
  resegment never calls regenerate, so no recursion.
- **W7 — docs**: `docs/storylines.md` updated (columns, sectioned-chapters
  section, generation pipeline, ingest auto-split, REST/MCP regenerate surface,
  config table). Webapp doc covered in the sibling repo.

## Code-review bug caught during wrap-up

A `/done` code-review pass found a **window-clamp inversion bug** in
`_derive_section_windows`: in a *bounded* unlocked span (one created by a
`boundary_locked` chapter), a non-last section whose citations reached `span_end`
was clamped to `span_end` itself, leaving the next section starting the day
*after* `span_end` — an inverted `start > end` window whose narrative was then
silently discarded. The unit tests missed it because the fake narrator spread
dates evenly. Fixed by reserving one day per still-to-come section
(`max_end = span_end - (n-1-i)`), plus two hardening changes: `rebuild_chapters`
now rejects any inverted spec (loud failure instead of silent corruption), and
the per-section excerpt filter tolerates a `None` window start. Reproduced with a
front-loaded-narrator regression test first, then fixed.

## Tests / status

- Storyline suite: 282 → **333** tests. Full unit suite: **2784 passed**, ruff
  clean, coverage 88%.
- The sectioning marker mechanism (`## ` in the Citations stream) is the one spot
  with model-reliability risk; if production shows unreliable markers, the robust
  follow-up is a tool-use structured-output variant for section boundaries.

## Follow-ups

- Watch real output for word-band drift (180–240 is LLM-approximate); a post-pass
  could merge runt sections / re-prompt over-long ones if needed.
- Pathological "more sections than days in a bounded span" now fails loudly rather
  than corrupting — revisit with a merge-sections strategy only if it ever fires.
