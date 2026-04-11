# 2026-04-11 — Multipage page-join separator fix

Closed the "277 words → 5 chunks" mystery from the 2026-04-10
chunk/token overlay session.

## Diagnosis

The hypothesis from the overlay notes was roughly right — OCR page
joins were causing extra chunk flushes — but the mechanism was more
specific than "every page boundary forces a flush".

`FixedTokenChunker` doesn't flush at paragraph boundaries. It only
flushes when adding the next paragraph would push the running token
count above `max_tokens`. So a single long paragraph of 250 tokens
packs into ~2 chunks happily.

The pathological case is **three moderate paragraphs**, each ~80
tokens. Because `80 + 80 = 160 > 150`, the chunker flushes chunk 1
at 80 tokens (well under the 150 budget), carries 40 tokens of
overlap, then flushes again on the next page boundary, and so on.
Budget utilisation collapses to ~55% and chunk count inflates by
~1.5×.

That is exactly what `ingestion.py` was producing:

```python
combined_text = "\n\n".join(page_texts)
```

`FixedTokenChunker._split_paragraphs_with_offsets` splits on `"\n\n"`,
so every page boundary became a paragraph boundary, and a 277-word
handwritten entry across 3 pages would land in the
"each-page-is-one-moderate-paragraph" regime. I reproduced this
locally with synthetic ~82-token pages: old join = 3 chunks of 82,
new join = 2 chunks of 142/137.

## Fix

One line in `services/ingestion.py`:

```python
combined_text = "\n".join(p.strip() for p in page_texts)
```

Two things going on:

1. **Strip each page.** OCR output commonly ends with a trailing
   newline. Joining un-stripped pages with `"\n"` would re-synthesise
   `"\n\n"` at the boundary and defeat the fix. The strip is
   load-bearing.
2. **Single newline, not blank line.** The paragraph splitter only
   splits on `"\n\n"`, so `"\n"` is invisible to it. But a single
   newline still preserves a visible page hint in `raw_text` /
   `final_text` for anyone reading the stored text. The true
   verbatim per-page OCR is still in `entry_pages.raw_text`, which
   is unchanged.

Considered and rejected: joining with `" "` (cleaner but loses all
page information in the concatenated text); rewriting the chunker to
distinguish page joins from real paragraph breaks (much more invasive
for a problem that's purely a separator choice).

## Tests

Added two tests in `tests/test_services/test_ingestion.py`:

1. `test_ingest_multi_page_strips_trailing_whitespace_before_join`
   — pages with trailing `\n` must not produce `\n\n` in the combined
   text. This is the regression that'd silently break the fix if
   someone reverted the `.strip()`.
2. `test_ingest_multi_page_packs_efficiently` — three ~82-token pages
   must produce exactly 2 chunks. Locks in the utilisation
   improvement. Picked 82 deliberately because `2*82 = 164 > 150`
   forces the packer to flush somewhere but the boundary should NOT
   be a page boundary.

Updated `test_ingest_multi_page` to assert the new `raw_text` format
(`"Page one text.\nPage two text."` instead of `"…\n\n…"`).

The efficient-packing test needed a callable `side_effect` on the
embeddings mock because the default fixture returns exactly one
embedding regardless of input — fine when the old behaviour produced
one chunk for short test data, but now that 2+ chunks are expected,
the mock has to return a vector per chunk.

Full suite: 368 passed. Ruff clean. Coverage 73% (threshold 65%).

## Legacy entries

Entries already in the database still have `"\n\n"` page joins baked
into their `final_text`, so re-running the chunker via
`rechunk_entries` won't help them on its own — the join separator is
part of the input, not a chunker parameter. Options for a legacy fix:

- Rebuild `final_text` for legacy multipage entries by re-joining
  `entry_pages.raw_text` with the new separator, then rechunk. This
  is destructive to any user edits to `final_text`, so it should be
  opt-in, not a migration.
- Ignore — the user's current DB has 5 small entries (none multipage
  with the pathology), and re-ingesting is fine for a single-user
  tool.

Leaving this as a known gap; not worth a one-shot migration right
now. Noted for next session if real multipage entries show up.

## What NOT to do

Do not change `FixedTokenChunker` to "ignore short paragraph
boundaries" or add paragraph-merging heuristics. The chunker
algorithm is correct — the input was wrong. This fix leaves the
chunker completely untouched and solves the problem at the layer
that created it.
