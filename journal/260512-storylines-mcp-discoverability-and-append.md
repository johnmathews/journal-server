# Storylines — MCP discoverability + append-update mode + create auto-kick

**Date:** 2026-05-12
**Branch:** `eng-storylines-ux`
**Sibling commit (webapp):** `<sibling commit on webapp repo>`
**Plan:** `.engineering-team/plan-storylines-ux.md`
**Reference doc:** [`docs/storylines-2026-05-mcp-and-append.md`](../docs/storylines-2026-05-mcp-and-append.md)

## Context

The morning incident: an MCP client (Nanoclaw) hit the storyline tools
cold, didn't know what storylines were, and couldn't figure out which
of the four tools to call first. The webapp side of the original cycle
hadn't shipped yet, so there was no UI to scaffold the client's mental
model — and the tool docstrings were one-line shorthand ("Create a
storyline.") that worked fine for a developer with context but were
useless to a fresh agent.

That cascaded into two adjacent asks the user has had on the storyline
TODO for a while:

1. "When I create a storyline from a fresh client, I want the panels
   generated immediately — why is that a separate call?"
2. "When new entries land, I want to refresh the storyline without
   re-doing the whole window — append the new bit."

This session bundles those three threads.

## Research findings

- **Anthropic tool-use guidance** (re-read in the plan phase): tool
  docstrings should be 3–5 sentences and answer four questions: what
  it does, when to call, what the result means, what to do next. They
  should read like documentation for another agent, not like a comment
  for the human author.
- **FastMCP conventions**: `Annotated[T, Field(description=...)]` on
  parameters is surfaced to MCP clients via the input schema. MCP tool
  annotations (`readOnlyHint`, `idempotentHint`, `destructiveHint`)
  are first-class — well-behaved clients use them for UX choices like
  destructive-action confirmation prompts.
- **MCP guide tools as a pattern**: a zero-parameter "read me first"
  tool that returns Markdown is a known idiom for getting new clients
  oriented without forcing them to read the source. Cheap to ship, big
  win for cold-start discoverability.

## Key decisions

### D2. Append-mode = append-only-at-end (chose Option A+ over LLM-merge / extended-window-replay)

Three options were on the table:

- **Concat (Option A)**: Append new excerpts, no LLM re-glue. Cheapest
  but the narrative panel stops mid-thought, and the seam between old
  and new reads awkwardly.
- **LLM-merge (Option B)**: Pass existing + new to a merge prompt and
  let the model interleave. Most flexible. Prompt-engineering cost is
  real, ordering semantics are something the model would need
  instruction on every call, and it's hard to test the merge step in
  isolation.
- **Extended-window-replay (Option C)**: Re-run the full pipeline over
  an expanded window. Simple but expensive (every regen pays for re-
  narrating the entire history) and re-stamps a stable narrative for
  no semantic reason.

Picked Option A+ — append-only with a hard server-side check that
`start_date >= storyline.last_generated_at`. Tradeoff: filling in a
past range needs Replace mode. Acceptable for v1: the use case the
user actually wants is "keep this long-running thread current," not
"retcon a missed week."

The seam still gets one LLM call (a transition phrase between the last
old citation and the first new one) and the narrator runs with the
existing narrative as a "previous chapters" preamble so the
continuation paragraph picks up where the old one left off. Net cost:
roughly one Haiku call + one Opus call against the new window only.

### D3. Auto-kick generation on create

Two-stage create-then-regenerate was always a wart. The user's
reaction was "it can kick off immediately — why not?" The auto-kick
adds `generation_job_id` to the POST response, and
`journal_create_storyline` blocks-and-polls the same way
`journal_regenerate_storyline` already does. Soft-failure: if the job
runner refuses (unwired in tests, restricted mode), the storyline is
still returned without the job id and the caller can retry via
regenerate. Intentional — the create contract is "row exists in DB" not
"row exists with panels."

## Plumbing chain — useful for the next person adding an optional param

Adding a single optional kwarg to a job worker is a five-stop trip.
Documented here so the next person doesn't have to grep:

```
POST /api/storylines/{id}/regenerate {start_date?, end_date?, mode?}
  ↓
src/journal/services/jobs/validation.py
    STORYLINE_GENERATION_KEYS — add new keys + types
  ↓
src/journal/services/jobs/runner.py
    submit_storyline_generation(start_date=, end_date=, mode=)
    — accepts kwargs, persists into job.params blob
  ↓
src/journal/services/jobs/workers/storyline_generation.py
    — reads params from job row, passes through to service
  ↓
src/journal/services/storylines/service.py
    regenerate(start_date=, end_date=, mode=)
    — validation + happy path
  ↓
src/journal/providers/storyline_narrator.py
    narrate(..., prior_narrative=) — optional kwarg, not a new method
```

Five files, but each diff is small. The validation layer is the only
one that exists specifically to police kwarg names — if you forget to
update it, the runner silently drops the new keys.

## Plan-drift items

- **Narrator extended via kwarg, not new method.** The plan said "the
  narrator gets a new mode" — in practice, adding
  `narrate_continuation()` would have duplicated the entire
  document-build + citation-parse pipeline. A single optional
  `prior_narrative` kwarg threading into the system-prompt preamble is
  the cleaner extension. Two-line change at the call site, no
  duplicated parser code.
- **Append test fixtures need future dates.** The append-mode boundary
  check is `start_date >= last_generated_at`. The fixture initially
  used the same backdated `last_generated_at` as the rest of the
  storyline tests, which made the validation only loosely covered —
  any sufficiently old `start_date` passed by accident. Bumped the
  fixture's `last_generated_at` ahead of the test clock so the
  boundary is genuinely exercised.

## Open items (carried into the reference doc)

1. **Empty-window append still bumps `last_generated_at`.** If
   `mode=append` is called against an empty range past the last
   entry, the excerpt fetch returns nothing, the panels don't change,
   but the timestamp gets stamped. Cosmetic, but a same-`start_date`
   retry then gets rejected by the boundary check. Fix is a guard in
   the service to no-op (and skip the timestamp bump) when the new
   window is empty. Left for now — cost is one confused retry per
   dead window.
2. **Auto-kick soft-fail is silent.** If the runner refuses, the MCP
   response is "Created storyline 42" with no hint that generation
   didn't kick. Surfacing a fallback string ("Storyline created but
   generation could not be queued — call
   `journal_regenerate_storyline(42)` to retry.") is a one-line
   change. Out of scope this cycle.

## Deferred

**Multi-entity storylines (Option B)** — one storyline anchored on N
entities — was the original D1 of the plan and got punted. See the
"Deferred / out-of-scope" section of
`.engineering-team/plan-storylines-ux.md` (units W5b/c/d) for the
breakdown. Picks up next: migration `0028_storyline_entities.sql`
with proper data-shape probing per the migration-testing convention,
service excerpt-union, classifier any-anchor matching, API + MCP +
webapp list-typed `entity_ids`. The webapp's `StorylineCreateModal`
was already built multi-select-ready so that piece is largely a
one-liner.

## Files touched

- `src/journal/mcp_server/tools/storylines.py` — docstrings,
  annotations, new `journal_storylines_guide` +
  `journal_delete_storyline`, create poll-block.
- `src/journal/api/ingestion.py` — POST regenerate body shape, POST
  create auto-kick.
- `src/journal/services/jobs/validation.py`,
  `services/jobs/runner.py`, `services/jobs/workers/
  storyline_generation.py` — param plumbing.
- `src/journal/services/storylines/service.py` — append-mode happy
  path, validation, seam transition.
- `src/journal/providers/storyline_narrator.py` —
  `prior_narrative` kwarg.
- `tests/test_mcp_tools_storylines.py` — guide, delete, create,
  timeout-fallback, not-configured.
- `tests/test_api_storylines_write.py` — new file.
- `tests/test_storyline_generation.py` — `TestAppendMode`.
- `tests/test_storyline_jobs.py` — worker param pass-through.

## Acceptance

Plan unit W12 ("docs + journal entries") was the last open item.
Reference doc: `docs/storylines-2026-05-mcp-and-append.md`. Roadmap
link added.
