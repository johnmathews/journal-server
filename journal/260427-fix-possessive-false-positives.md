# 2026-04-27 — Fix possessive/plural false positives in canonical_name repair

## What broke

Ran `journal repair-entity-names --dry-run` on prod after shipping the validator
yesterday. It proposed 17 repairs. **Only one** was correct (`Nautilin` -> `Nautiline`).
The other 16 were the same class of bug:

```
[417] 'Albus' -> "Albus's"
[52]  'Daniel' -> 'Daniels'
[283] 'Dumbledore' -> "Dumbledore's"
[472] 'Hedwig' -> "Hedwig's"
... etc
```

The repair logic was treating possessive (`'s`) and plural (`s`) suffixes as
"clipped trailing characters". When the LLM correctly extracted `"Hermione"`
for a quote that says `"I was at Hermione's house"`, the strict-prefix-of-token
rule was promoting the canonical to `"Hermione's"`. Same false positive for
plural `"Daniels"` and the rare plural-possessive `"Smiths'"`.

## Fix

Added an explicit inflection check in `_repair_canonical_name`:

- A new helper `_is_inflection_of(name_lower, token_lower)` returns True if the
  only extra characters between the canonical and the token are `'s`, `s'`,
  or `s`.
- Two places this is consulted:
  1. **Trust path** — if the LLM's canonical_name appears in the quote only as
     an inflected form (e.g. canonical `"Hermione"`, quote contains `"Hermione's"`),
     short-circuit: trust the LLM, no repair.
  2. **Repair path** — when looking for a longer-token repair candidate, skip
     any candidate whose extra characters are an inflection suffix. Only treat
     non-inflection extensions (a clipped letter mid-name like `"Nautilin"` ->
     `"Nautiline"`) as real repair candidates.

The inflection-trust rule short-circuits the iteration as soon as it fires,
even if a non-inflection extension also exists later in the same quote. That's
the safer choice given how prevalent inflection false-positives were on the
first prod dry-run — better to under-repair than over-repair across a corpus
where every proper noun is at risk.

## What this means

- The runtime validator (which runs on every extraction) is now safe — it won't
  silently corrupt `"Albus"` into `"Albus's"` on a future entry that mentions
  Albus's house.
- The CLI's next dry-run on prod should drop from 17 proposed repairs to ~1
  (the single legitimate `Nautilin` -> `Nautiline`).

## Tests

5 new unit tests for the inflection cases (apostrophe-s, plain-s, s-apostrophe,
real repair still works alongside an inflection in the quote, inflection
short-circuits later non-inflection candidates), plus an updated punctuation-
stripping test that uses a non-inflection extension (`"Vienn"` -> `"Vienna"`)
to keep that path covered without colliding with the new guard. 1345 tests pass.

## Lesson

The first dry-run on real data was load-bearing — without it the validator
would have shipped and quietly mutated the corpus on every subsequent edit.
"Test the helper unit-style + hold a real-data dry-run before applying" needs
to be the rule for any repair tool that touches existing rows in bulk.

## Prod verification

After the fix shipped and CI rebuilt the image, re-ran the dry-run on prod:

```
docker exec journal-server uv run journal repair-entity-names
Proposed repairs (1):
  [671] 'Nautilin' -> 'Nautiline'  (type=other, user_id=1)
```

Down from 17 to 1. Applied:

```
docker exec journal-server uv run journal repair-entity-names --apply
Applying 1 repair(s)...
Updated entity 671: Nautiline
Applied 1/1 repair(s).
```

Entity 77 (the original report that triggered this work) now shows the
correct `Nautiline` canonical_name in the UI. Going forward, the runtime
validator catches the same class of LLM clipping on every new extraction,
so the corpus shouldn't accumulate clipped canonicals again.

## Cost impact

None — the validator is pure local Python string manipulation that runs
after Claude's tool-use response has already been received. No extra LLM
calls, no extra tokens, no embeddings. A repair is a single SQL UPDATE on
the `entities` row; mention/quote text is untouched so no re-chunking or
re-embedding fires either. The webapp `/settings` cost estimates do not
need to change.

The only thing that *would* shift cost is if we ever decided to re-prompt
Claude on detected mismatches — explicitly out of scope for this work
since the deterministic fix already handles the failure mode. The
`WARNING` logs from `_repair_canonical_name` give us visibility into the
mis-extraction rate; if it spikes, we'd revisit.
