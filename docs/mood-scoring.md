# Mood Scoring

Per-entry emotional scoring against a user-configurable set of facets. On by default. Opt out explicitly via
`JOURNAL_ENABLE_MOOD_SCORING=false`.

The pipeline sits at the tail of `IngestionService._process_text`: after chunks + embeddings are persisted, the
`MoodScoringService` is called with the entry's final text and the currently-loaded dimensions. The scorer (default:
Claude Sonnet 4.5 via the Anthropic Messages tool-use API) returns one score per facet; the service writes them to the
`mood_scores` table via `replace_mood_scores`. Scoring failures are logged but never propagate back into ingestion — an
entry is always saved even if scoring fails.

## Facets live in `config/mood-dimensions.toml`

The facet set is stored as data, not code. Each `[[dimension]]` block defines one facet:

```toml
[[dimension]]
name = "joy_sadness"
positive_pole = "joy"
negative_pole = "sadness"
scale_type = "bipolar"
notes = """
Predominantly joyful vs predominantly sad. Score 0 when neither
dominates. Reserve extreme values for genuinely strong cases.
"""
```

Fields:

| Field           | Required | Notes                                                      |
| --------------- | -------- | ---------------------------------------------------------- |
| `name`          | yes      | Stable snake_case key. Stored in `mood_scores.dimension`.  |
| `positive_pole` | yes      | Human-readable label for the high end of the scale.        |
| `negative_pole` | yes      | Label for the low end.                                     |
| `scale_type`    | yes      | `"bipolar"` (-1..+1) or `"unipolar"` (0..+1).              |
| `notes`         | yes      | 1-2 sentence scoring criteria inlined into the LLM prompt. |

### Bipolar vs unipolar

The scale type is per-facet because some emotional axes are genuinely bipolar (`joy ↔ sadness`: the opposite of joy is a
real feeling, not absence) while others are unipolar (`agency vs apathy`: the opposite of agency is felt as _absence_ of
agency, not an active opposing feeling).

- **Bipolar** scores range `[-1.0, +1.0]`. `0.0` means neither pole dominates — a calm, flat mood. `-1.0` is maximum
  negative pole, `+1.0` is maximum positive pole.
- **Unipolar** scores range `[0.0, +1.0]`. `0.0` means the positive pole is _absent_ from the entry. `+1.0` means it is
  strongly present. A unipolar `0.0` does NOT mean neutral — it means "nothing of this feeling was detected".

Mixing both is deliberate: it lets the schema model your real intuition about each facet instead of forcing everything
into a single shape. The backend stores all scores in the existing `mood_scores` table whose
`CHECK(score BETWEEN -1.0 AND 1.0)` constraint accommodates both.

### Rationale

Each score is accompanied by a brief rationale (1-2 sentences) explaining why the LLM assigned that score. The rationale
is stored in the `mood_scores.rationale` column (added in migration 0014) and surfaced in the Insights page drill-down
panel. The LLM is instructed to be concrete — quoting or paraphrasing the entry rather than restating the scale
definition.

Entries scored before migration 0014 have `rationale = NULL`. Run `journal backfill-mood --force` to populate rationales
for all entries.

## The 7-facet starting set

The facets shipped in `config/mood-dimensions.toml` at the time of writing are:

| Facet                | Scale    | Notes                                                    |
| -------------------- | -------- | -------------------------------------------------------- |
| `joy_sadness`        | bipolar  | Joyful vs sad.                                           |
| `anxiety_eagerness`  | bipolar  | Anxious vs eager/enthusiastic. 0 = calm centre.          |
| `agency`             | unipolar | Strong sense of agency (1) vs apathy/resignation (0).    |
| `comfort_discomfort` | bipolar  | Comfortable vs uncomfortable (physical + emotional).     |
| `energy_fatigue`     | bipolar  | Energetic/alert vs tired/drained. Arousal axis.          |
| `fulfillment`        | unipolar | Meaningful fulfillment (1) vs indifference (0).          |
| `proactive_reactive` | bipolar  | Proactive/initiating vs reactive. More stance than mood. |

This is a **starting set**, not a committed schema. After ~60-100 entries, run the correlation / factor analysis
described in the [Post-hoc analysis](#post-hoc-analysis) section to decide which facets to keep, merge, or drop.

## Cadence

Scoring runs per journal entry. Assume one entry per day (the "morning pages" practice), which gives ~30 scored entries
per month. No batching — each entry fires one LLM call at ingestion time.

Skipping days or writing partial entries is fine. The `mood_scores` table is sparse by `(entry_id, dimension)`, so a
missed day simply has no row. Adding a new facet later does not require rewriting old entries — they remain validly
scored against the previous set and return `null` for the new facet until you run `journal backfill-mood --stale-only`.

## Regeneration

All four typical edits are cheap:

### Adding a facet

1. Append a new `[[dimension]]` block to the TOML file.
2. Restart the server (the dimension set is loaded once at startup).
3. New entries are scored against the new facet automatically.
4. Optionally run `journal backfill-mood --stale-only` to score historical entries against the new facet. `--stale-only`
   is idempotent: it only scores entries that are missing one or more currently-configured dimensions.

### Removing a facet

1. Delete the `[[dimension]]` block.
2. Restart the server. New entries are no longer scored against the removed facet.
3. Historical scores for the removed facet are preserved by default — they're still in the `mood_scores` table under
   their original `dimension` name, just no longer showing up in the dashboard or being written by new ingestions.
4. To actually delete them, run `journal backfill-mood --prune-retired`. Off by default because historical mood data has
   archival value even after a schema change.

### Editing a facet's description or labels

1. Edit the `notes`, `positive_pole`, or `negative_pole` field.
2. Restart the server.
3. Run `journal backfill-mood --force` to rescore every entry against the new interpretation. This is the only time you
   need `--force` — for pure additions, `--stale-only` is enough.

### Reordering facets

Ordering in the TOML file is load-bearing only for chart display (the dashboard renders lines in the order defined).
Reordering is a one-file edit with no backfill required.

## `journal backfill-mood` CLI

```
usage: journal backfill-mood [--force] [--prune-retired] [--dry-run]
                             [--start-date START_DATE] [--end-date END_DATE]
```

- `--force` — rescore every entry, not just those missing a currently-configured dimension. Used after editing a facet's
  `notes` / labels.
- `--prune-retired` — delete `mood_scores` rows whose `dimension` is not in the current config. Off by default.
- `--dry-run` — count what would be scored / pruned without making any LLM calls or DB writes. Combined with
  `--prune-retired`, it reports how many rows would be deleted.
- `--start-date` / `--end-date` — inclusive ISO-8601 window (optional).

Dry-run output is safe to run on a large corpus; the real run prints a per-run cost estimate based on public Sonnet 4.5
pricing so you can decide whether to proceed.

## Cost

Per-entry cost with Sonnet 4.5 (`claude-sonnet-4-5`), ~500-word entry:

- ~1250 input tokens (system prompt ~500 + entry ~750)
- ~150 output tokens (tool call payload for 7 facets)
- Input: 1250 × $3.00 / 1M = $0.00375
- Output: 150 × $15.00 / 1M = $0.00225
- **Total: ~$0.006 per entry**

Scaling: one entry per day ≈ 30 entries per month ≈ **$0.18/month**. Backfilling 100 historical entries is ~$0.60. If you
switch to Claude Haiku 4.5 (via `MOOD_SCORER_MODEL=claude-haiku-4-5-20251001`), cost drops by ~5× to ~$0.03/month, at the
price of slightly less calibrated subjective scoring on short entries.

## Post-hoc analysis

After 4-6 weeks of scored data (~60-100 entries) the dashboard chart will show visible trends. At that point the real
analysis questions become:

1. **Correlation matrix** across facets. Which facets move together? Are `joy_sadness` and `fulfillment` actually
   independent, or is one predicting the other? Python's `pandas.DataFrame.corr()` applied to a pivot of `mood_scores` by
   `(entry_date, dimension)` answers this in one line.
2. **PCA or factor analysis** — how many _true_ underlying dimensions does the corpus have? If two facets are >0.9
   correlated, they're one axis wearing two labels. Merge or drop.
3. **Day-of-week patterns** — aggregate by weekday. Does Sunday look different from Tuesday? This is lagged behaviour
   analysis, not a pure mood question, but the mood scores are the right input.
4. **Prune / merge / rename** facets based on what the data shows. Edit the TOML file and run
   `journal backfill-mood --prune-retired --force` to regenerate.

None of this analysis is built in yet — it lives in a future session as a Tier 3 item. The capture pipeline must run for
a few weeks first to produce meaningful data.

## Endpoint surfaces

The webapp consumes two new endpoints, both bearer-authenticated via the app-wide middleware:

- `GET /api/dashboard/mood-dimensions` — returns the currently loaded facet definitions (name, scale type, poles, notes).
  The frontend queries this on page load so adding a facet in the TOML file flows through to the UI on the next request
  without a webapp rebuild.
- `GET /api/dashboard/mood-trends?bin=&from=&to=&dimension=` — returns per-bucket averaged scores per dimension. Same bin
  vocabulary as `/api/dashboard/writing-stats` (week / month / quarter / year). Optional `dimension` filter narrows the
  response to a single facet. Empty buckets are omitted.

## MCP tool

`journal_get_mood_trends` (LLM-facing MCP tool) still accepts `day / week / month / quarter / year` as granularity. The
`period` field in its response is now a canonical ISO date instead of a `%Y-W%W` format string, matching the REST shape.
Existing LLM consumers that displayed the field as-is should still render correctly — the format is human-readable in
both shapes.
