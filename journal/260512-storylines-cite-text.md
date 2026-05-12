# Storylines narrator: switch to source="text" Citations documents

**Date:** 2026-05-12
**Branch:** `worktree-eng-storylines-cite-text` (server)
**Touches:** `providers/storyline_narrator.py`, `tests/test_storyline_generation.py`, `docs/storylines.md`, `docs/storylines-plan.md`

## What changed and why

Follow-up named in the closed `docs/storylines-plan.md` webapp-cycle handoff. After the server spike (`260512-storylines-server-spike.md`) shipped, every narrative-panel citation was carrying the whole wrapped journal entry as its `cited_text` — typically 1000+ characters — because the narrator wrapped all entries as content blocks inside a single `source="content"` document. The webapp papered over this by hiding the bloated quote behind a `<details>` disclosure (see `webapp/journal/260512-storylines-webapp.md`), but the right fix is on the server: switch to one `source="text"` document per entry so the Citations API returns short sentence-level excerpts and `cited_text` is actually usable.

## Approach

For each excerpt the narrator now builds:

```python
{
  "type": "document",
  "source": {"type": "text", "media_type": "text/plain", "data": ex.final_text},
  "title": f"Entry {ex.entry_id} ({ex.entry_date})",
  "citations": {"enabled": True},
}
```

The entry id / date used to ride inside the document text as an XML-like wrapper (`<entry id=N date=...>...</entry>`). With `source="text"` the API auto-chunks the document at sentence boundaries, which would have turned the wrapper lines into citable sentences — ugly. So the wrapper moves into the document's `title` field, which the Citations docs say is "passed to the model but not used towards cited content." The model still sees which entry it's looking at; the user-visible `cited_text` is clean.

## The Citations-API gotcha

The brief flagged this and the docs confirmed it: under `source="text"`, citations carry the `char_location` shape, not `content_block_location`. So `start_block_index` doesn't appear; the per-request index that survives is `document_index` (which document in the request was cited). Each entry being its own document means `document_index → entry_id` is the natural mapping — direct replacement for the old `block_index → entry_id`. The parser's index → entry map keeps the same shape; only the variable name and the field it reads off the citation change.

Convenient side note: `document_index` is present on all three citation shapes (`char_location`, `page_location`, `content_block_location`), so the parser is robust if we ever swap source types again.

## Cache-control placement

The old layout had two breakpoints: `cache_control: ephemeral` on the system prompt (1h TTL) and on the single document (5m TTL). With N documents the equivalent single-breakpoint translation is `cache_control` on the *last* document only — that breakpoint covers every preceding document in cache. Stays well under the four-breakpoint request limit, and stays the same in spirit as before (still 2 breakpoints total). The docstring inside `_build_documents` and `storylines.md` step 5 spell this out.

The provider's module docstring used to claim "three-breakpoint" caching; reality has been two breakpoints for the entire feature's lifetime. Fixed the docstring while in the neighborhood.

## Defense-in-depth that stays load-bearing-lite

Two pieces stay even though they no longer fire on the happy path:

1. **Embedder 32k-char cap** (`services/storylines/service._join_narrative_text`, commit 8396c7e). With short `cited_text`, the embed-input join no longer risks blowing the 8192-token limit. But the cap and its regression tests are cheap and protect against a future provider change or an unusually long sentence. Kept; the test docstring is updated to be honest about why.
2. **Citation quotes excluded from embed input.** Same rationale. The function header explains it remains text-only as a stability choice.

## Tests

Updated `TestNarratorParser` to assert the new shape end-to-end:

- Citation `type` is `char_location` with `document_index` + `start_char_index` + `end_char_index`
- `cited_text` is sentence-length (test asserts `< 200` chars per citation as a sanity bound)
- Request `content` is a flat list of `source="text"` document blocks followed by the user-query text — no single `source="content"` wrapper
- Each document's `data` is `excerpts[i].final_text`; `title` is `Entry {id} ({date})`
- `cache_control` attaches to the **last** document only (not all; not none)

Parser name change: the internal `block_to_entry` parameter is now `document_to_entry`. Caller updated. The function is private to the provider module; no external API.

Suite stays at 2376 passing, 35 warnings. Ruff clean.

## Webapp implications (deferred)

The webapp's `StorylineSegments.vue` collapses citation quotes behind a `<details>` disclosure (rationale in `webapp/journal/260512-storylines-webapp.md`). With this change quotes are sentence-length on the wire; the disclosure path is now dead weight. Leaving the webapp untouched in this commit — the renderer keeps working, quotes just look better unwrapped. Webapp simplification can be a second commit in `webapp/`'s own worktree; bias is to drop the disclosure (fewer code paths), but not in scope here.

## Follow-up items

1. Webapp cleanup: drop or simplify the `<details>` disclosure now that quotes are short. Separate worktree.
2. Entity backfill (other open webapp-cycle handoff item) is unchanged — still a separate workstream.
3. W10-style read on prod (Running entity 513, Atlas entity 511) is the qualitative gate. Doing that post-merge; if narrative quality regresses, kill criterion #1 in `storylines-plan.md` fires and we reassess. Bias is it shouldn't — `source=` only affects what the *response's* citations carry; the model sees the full corpus regardless.

## Out of scope, called out

- No webapp work in this server worktree.
- On-disk `Segment` shape unchanged (`{kind, entry_id, quote}`); webapp typing and Pinia store stay valid.
- Embedder cap + regression tests kept; not removed despite no longer load-bearing on the happy path.
- No feature flag — single-user prod is safe to regenerate against directly.
