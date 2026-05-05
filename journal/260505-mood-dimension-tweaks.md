# 2026-05-05 — Mood-dimension tweaks: tighten `connection`, prep for display-inverted `frustration`

Two small follow-ups to the dimension restructure from 260504. Both motivated by reading the new
config from a "would I trust this on the dashboard?" lens rather than by any concrete bug.

## Why

1. **`frustration` is the only "higher = bad" dimension.** Every other facet reads "higher = better"
   on the chart (joy, agency, fulfillment, connection — and energy/proactive are mostly neutral
   descriptive). Eyeballing the chart with frustration in it required mental work — was that line
   above the others good or bad? Not great for a glance-and-go view.
2. **`connection` was defined too broadly.** The notes mixed three different things — interpersonal
   warmth, group belonging, and "part of something beyond the self" (transcendent / spiritual). A
   single 0..1 score across that union doesn't tell you what was actually present in the entry, and
   the LLM has to make awkward judgment calls about which sub-bucket to weight.

## What I considered for `frustration`

Discussed four options with the user:

1. **Rename to unipolar `calm`.** Cleanest visually but wrong on the modelling side. Calm is a
   baseline state, not a signal — an entry where the writer is just heads-down working would score
   low on calm without being frustrated. The 0-end stops meaning "absent" and starts meaning
   "ambiguous."
2. **Convert to bipolar `calm_frustrated`** with 0 = no signal. Works, but adds redundancy with
   `joy_sadness` on the negative side (angry days and sad days both end up below zero on two axes).
3. **Display-only inversion.** Keep storage and the LLM prompt as `frustration` (unipolar 0..1,
   detecting an active negative signal — the easy task), but render it as `1 - score` with the
   label "calm" in the chart. Stored data stays interpretable; the dashboard stays consistent.
4. **Leave it and add a "↓ better" annotation.** Lowest churn but admits the asymmetry rather
   than fixing it.

Picked (3). Detecting frustration as an active signal is cheaper than trying to detect baseline
calm, and a render-time helper keeps the API contract and the `mood_scores` table identical.

## What changed in this repo

Just the `connection` notes. Storage, scale, and `name` are all untouched, so a backfill is optional
— run `journal backfill-mood --force` whenever you want the rescore to land:

- Tightened the scope to **felt closeness with specific other people** (a partner, friend, family
  member, colleague, or a group the writer is with).
- Added an explicit "out of scope: abstract collectives, communities, anything spiritual or
  transcendent" clause so the LLM doesn't widen the bucket.
- Loneliness wording stays — felt isolation despite wanting connection still scores 0 here, with
  the negative emotional tone landing on `joy_sadness`.

Also updated `docs/mood-scoring.md`:
- The "starting set" table was stale (still listed `anxiety_eagerness` and `comfort_discomfort`).
  Refreshed to match the current 7-facet set from `config/mood-dimensions.toml`.
- Added a paragraph noting that `frustration` is the only "higher = worse" facet and that the
  webapp inverts it at render time. Pointer to `webapp/src/utils/mood-display.ts` so the storage /
  display split is easy to find.

## What changed in the webapp

See the matching webapp journal entry from today. The display-inversion lives entirely on the
client — server config and the LLM tool schema are unchanged.

## Tests / lint

- `uv run python -m pytest`: 1586 passed; one pre-existing flaky test
  (`test_api_ingest.py::TestPatchMoodScoring::test_patch_text_queues_mood_scoring`) failed in the
  full run but passes in isolation. Unrelated to this change (a notes-only TOML edit).
- `uv run ruff check src/ tests/ config/`: clean.
