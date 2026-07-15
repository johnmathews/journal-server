# 260715 — Mood/fatigue redesign: split the tiredness axis + divergence detector

Companion to the webapp entry `260715-mood-fatigue-redesign.md` (journal-webapp). This was a
cross-cutting change delivered as one engineering-team cycle (evaluate → plan → implement →
wrap-up); the run artifacts live in the parent workspace under
`.engineering-team/runs/manual-20260715T102441Z/`.

## 1. Motivation

The single `energy_fatigue` mood dimension (bipolar, energetic↔tired) scored **physical and
mental tiredness as one number** — its own notes told the model to score "the bodily / cognitive
sense of activation." That made an unanswerable question out of "I feel tired but run a lot": a
wrecked-legs day and a mentally-fried day both landed near −0.7 and were indistinguishable, and
the fitness↔mood correlation tools leaned on `frustration` as a stand-in for "stress" while the
objective Garmin `stress_avg` sat unused. The fatigue-science research (Marcora's psychobiological
model; Thayer's two-arousal model; MFI-20/Chalder/POMS) all say physical fatigue, mental fatigue,
energetic arousal, and tense arousal are separable constructs — so the fix is to stop collapsing
them.

## 2. What shipped (server)

Ten work units, implemented in dependency-ordered waves in an isolated worktree.

1. **Dimension redesign** (`config/mood-dimensions.toml`, version `2026-07-15`): removed
   `energy_fatigue`; added `energy_vigor` (bipolar, flat↔vigorous — a drop-in for the correlation
   SQL that kept the `energy` alias), `tension_calm` (bipolar, Thayer tense-arousal, calm=+1 so no
   render inversion), `physical_fatigue` and `mental_fatigue` (unipolar 0..1, high = more
   depleted). Notes carry explicit "Distinct from X" contrast examples so the LLM separates the
   facets. 7 → 10 facets. The scorer/loader/backfill needed **zero** code changes — the pipeline
   was already config-driven (sparse `mood_scores` table, prompt + tool schema built from config
   at call time). Old `energy_fatigue` rows are kept (not pruned) for reversibility.
2. **Normalize sentinel abort fix** (`services/fitness/normalize.py`): Garmin emits `-1`/`-2`
   sentinels on insufficient-wear days; `_int_or_none` passed them through, they tripped the
   `fitness_daily` CHECK constraints, and `upsert_daily` (which sat **outside** the `_Drift`
   try/except) raised `IntegrityError` that aborted the whole daily pass. Added
   `_bounded_int_or_none`/`_bounded_float_or_none` (null out-of-range values), moved the upsert
   inside the guard, and added an `IntegrityError` drift arm as defense-in-depth.
3. **Correlation refactor** (`mcp_server/tools/fitness.py`, new `services/fitness/correlation_stats.py`):
   Q2 now de-duplicates runs to one source per day (Garmin preferred) so a watch run recorded on
   both Strava and Garmin isn't double-counted; the `frustration` stress-proxy was replaced with
   objective `fitness_daily.stress_avg`; Q1/Q3 gained a `lag_days` param (yesterday's load → today's
   mood) and `physical_fatigue`/`mental_fatigue` columns; every tool now returns a Pearson `{r, n}`
   `stats` block (pure Python, no numpy).
4. **Divergence detector** (new `services/fitness/divergence.py`, `DivergenceDay` in `models.py`):
   the product answer to the motivating question. Per day it computes rolling **per-person
   z-scores** (28-day trailing baseline, ≥10 points required) for objective recovery signals (HRV,
   resting HR, sleep, training readiness, acute:chronic load ratio — all oriented so positive =
   better recovered) and for the self-reported fatigue facets, then classifies into quadrants:
   `likely_mental_fatigue` (feels tired but objectively recovered), `hidden_physical_under_recovery`
   (feels fine but under-recovered), and the two congruent cases. Exposed as MCP tool
   `fitness_divergence` and REST `GET /api/fitness/divergence`; plus `GET /api/fitness/mood-recovery`
   feeds the webapp overlay.
5. **Chat dimension resolution** (`services/conversations/{handlers,dimensions}.py`,
   `providers/intent_classifier.py`): the classifier is now told the valid facet names and the
   handler resolves free-form strings ("tired"→fatigue, "energy"→vigor) via a synonym map instead of
   exact-match-or-nothing; ambiguous terms fall back to all dimensions rather than "no data"; the
   trend heuristic now routes "tired/exhausted/fatigue" questions.
6. **MCP ASCII bar** (`mcp_server/tools/queries.py`): the mood-trends bar was scale-blind (unipolar
   0 rendered as a half-bar); now scale-aware using the loaded dimension bounds. Docstring corrected
   (no 3-month default; it's all-time).
7. **Schema hardening**: migration `0038_mood_scores_unique_dimension.sql` dedups then adds
   `UNIQUE(entry_id, dimension)` (re-runnable, dirty-fixture tested); `perceived_exertion` now
   populated from Strava's manual RPE (Garmin training-effect → `extras_json`); a code comment
   clarifies that `body_battery_high/low` are charge deltas, not levels.
8. Doc updates across `mood-scoring.md`, `fitness-schema.md` (new §9), `api.md`, `configuration.md`,
   `architecture.md`, `CLAUDE.md` (migration range 0001→0038, new modules, Sonnet-not-Haiku).

## 3. Key decisions

- **`energy_vigor` as a bipolar drop-in**, not derived from the fatigues — keeps the correlation SQL
  and response keys stable, and matches Thayer (energetic arousal is its own axis, independent of
  depletion).
- **Kept old `energy_fatigue` rows** rather than `--prune-retired` — reversible, and there's no
  meaningful chart continuity to stitch between the old conflated axis and the new `energy_vigor`.
- **z-scores are per-person and rolling**, and the journal self-report is the primary signal with
  objective data used to *cross-check* (per Saw 2016, subjective measures out-predict biomarkers for
  training response). The recovery axis uses an asymmetric threshold (recovered at `z≥0`,
  under-recovered only at `z≤−1`) to keep the two divergence quadrants high-confidence.

## 4. Gotchas / discovered during the work

- **Config↔SQL coupling**: `fitness.py` hard-codes dimension string literals in the correlation SQL,
  so a rename silently returns NULLs. Added `tests/test_config_sql_consistency.py` to fail loudly if
  a facet the SQL depends on disappears from the config.
- **Code review caught two real issues** (fixed + regression-tested): malformed `start`/`end` on the
  divergence route returned HTTP 500 (uncaught `date.fromisoformat` ValueError) — now 400; and the
  webapp chart had a `flush: 'pre'` cold-load bug (see the webapp entry).
- **Backfill is a deploy-time step**: the mood-dimension change means historical entries keep their
  old scores until `journal backfill-mood --force` runs against prod. Deferred behind an explicit
  cost gate. Note prod Garmin auth is currently broken (roadmap D8), so the divergence detector will
  have thin objective data until reconnected — the code is correct and degrades gracefully
  (`sufficient=false`).

## 5. Verification

Server: `3231 passed, 11 skipped`, coverage 88%, ruff clean. Full evaluation report and improvement
plan in the run dir.
