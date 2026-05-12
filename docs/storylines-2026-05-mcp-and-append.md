# Storylines — MCP Discoverability + Append Mode (2026-05-12)

**Status:** active reference. **Last updated:** 2026-05-12. **Supersedes:** none.
**Related:** [`storylines.md`](./storylines.md) (feature reference),
[`storylines-plan.md`](./storylines-plan.md) (closed 2026-05-12 server cycle).

This is a follow-up cycle on top of the closed `storylines-plan.md`, captured
in its own doc so the original plan stays archived as the record of the spike.
The driving issue: an MCP client (Nanoclaw) hit `/storylines` cold and
couldn't figure out how to use the four storyline tools without a webapp
session to scaffold its understanding. Three threads dropped out of that:
tool discoverability, fresh client workflow (create with no panels), and
keeping a long-running storyline up to date without re-doing the whole
window.

The plan that scoped this work is at
`/Users/john/projects/journal/.engineering-team/plan-storylines-ux.md`.

## Decisions

### D1. MCP discoverability is a docstring + new-tool problem, not a transport problem

Anthropic's tool-use guidance says tool docstrings should answer four
questions in 3–5 sentences: what the tool does, when to call it, what the
result means, what to do next. FastMCP also surfaces `Annotated[T,
Field(description=...)]` to the client and respects MCP tool annotations
(`readOnlyHint`, `idempotentHint`, `destructiveHint`). All four existing
storyline tools got rewritten docstrings, per-parameter `Field`
descriptions, and the right annotation set. A new zero-param
`journal_storylines_guide` tool returns a Markdown-formatted overview so a
fresh client has a single "read me first" surface that works even without
`ANTHROPIC_API_KEY` (the guide doesn't call the model).

`journal_delete_storyline` filled the missing CRUD verb (the REST API
already had `DELETE`) — `destructiveHint: True` so well-behaved clients
prompt for confirmation.

### D2. Append-update is "append-only-at-end", validated against `last_generated_at`

Three options were on the table for keeping a storyline current without
full replay:

- **Concat (Option A)**: dumb append of new excerpts, no LLM re-glue.
  Cheapest, but seams read awkwardly and the narrative panel would just
  stop mid-thought.
- **LLM-merge (Option B)**: pass existing curation + new excerpts to a
  merge prompt, let it interleave. Most flexible but prompt-engineering
  complexity is real and the merge model would need access to ordering
  semantics it doesn't naturally have.
- **Extended-window-replay (Option C)**: re-run the whole pipeline over an
  expanded window. Simple but expensive and re-stamps the existing
  narrative for no semantic reason.

Picked **Option A+**: append-only-at-end with a server-side validation
that `start_date >= storyline.last_generated_at`. Pragmatic for the
"keeping a long-running storyline up to date" use case, which is what the
user actually wants. The tradeoff: filling in a past date range needs
Replace mode. Worth it for v1 — the validation is one line and the merge
seam is a single LLM call against the existing narrative as "previous
chapters" context.

Mode plumbing reaches all the way down:

```
POST /api/storylines/{id}/regenerate {start_date?, end_date?, mode?}
  → validation (STORYLINE_GENERATION_KEYS)
  → JobRunner.submit_storyline_generation(start_date=, end_date=, mode=)
  → workers/storyline_generation.py (reads params from job row)
  → StorylineGenerationService.regenerate(start_date=, end_date=, mode=)
  → AnthropicStorylineNarrator(prior_narrative=)   # new optional kwarg
```

The narrator was extended via an optional `prior_narrative` kwarg rather
than a new method — adding `narrate_continuation()` would have duplicated
the document-build/citation-parse pipeline. The kwarg threads through to
the system prompt as a "previous chapters" preamble.

### D3. Auto-kick generation on create

The user's reaction to "should we offer a `generate=true` flag on POST"
was "it can kick off immediately — why not?" `POST /api/storylines` now
submits a `storyline_generation` job after the row is created and returns
`generation_job_id` in the 201 body. `journal_create_storyline` follows
the existing `journal_regenerate_storyline` pattern: submit + poll until
terminal (default 120s) + return the rendered panels (or a fallback
message with the job id on timeout).

Soft-failure path: if the job runner refuses or isn't wired (test
fixtures, restricted modes), the storyline is still returned without a
`generation_job_id`. The caller can recover by calling
`journal_regenerate_storyline` later. Intentional — preserves the
single-purpose "create" contract.

## New MCP tools

| Tool                          | Annotations                | Notes                                                                 |
| ----------------------------- | -------------------------- | --------------------------------------------------------------------- |
| `journal_storylines_guide`    | `{"readOnlyHint": True}`   | Zero params. Returns Markdown guide. Works without `ANTHROPIC_API_KEY`. |
| `journal_delete_storyline`    | `{"destructiveHint": True}`| Wraps repo `delete_storyline`. Cascades to panels. Jobs not cascaded.  |

The four existing tools (`list`, `get`, `create`, `regenerate`) got:
3–5 sentence docstrings; `Annotated[T, Field(description=...)]` on every
parameter; `readOnlyHint=True` on `list` + `get`; `idempotentHint=True`
on `regenerate`. The line 213 timeout message had a literal `...`
placeholder where the job id should have been — fixed.

## Request shape: `POST /api/storylines/{id}/regenerate`

Body is optional. All three fields independent:

```json
{
  "start_date": "2026-04-01",
  "end_date": "2026-04-30",
  "mode": "append"
}
```

- `mode` defaults to `"replace"` (existing behavior; no body still works).
- `mode: "append"` requires `start_date >= storyline.last_generated_at`.
  Validation lives at the service layer; API surfaces a 400 with the
  reason. Client-side validation in the webapp catches the obvious cases
  before the round-trip.
- `start_date` and `end_date` are independent of mode — Replace can also
  scope to a new window.

## Open behavioral questions

These came out of W6/W7 review. None blocking, but worth a future eyeball.

1. **Empty-window append still stamps `last_generated_at`.** If
   `mode=append` is called with a `start_date` past the last entry, the
   excerpt fetch returns empty, no work happens, but `last_generated_at`
   gets updated anyway. Cosmetic — a future append with the same
   `start_date` would now be rejected. Fix would be a guard in the
   service to no-op (and skip the timestamp bump) when the new window is
   empty. Left in place because the cost is one user-confused regenerate
   per dead window.

2. **Auto-kick soft-fails silently when the job runner refuses.** The
   storyline is returned without `generation_job_id` and the MCP tool's
   poll-block is skipped. Intentional but documented here so a future
   reader doesn't see this as a missing error path. Surfacing this to
   the MCP response (e.g. `"Storyline created but generation could not
   be queued — call journal_regenerate_storyline({id}) to retry."`) is a
   one-line follow-up.

## Deferred — multi-entity storylines

The plan's D1 was "one storyline anchored on multiple entities" (Option
B). Deferred in this cycle — see the **Deferred / out-of-scope** section
of `plan-storylines-ux.md` (units W5b/c/d). Multi-entity needs:

- Migration `0028_storyline_entities.sql` adding a join table with backfill
  from existing `storylines.entity_id`.
- Service-layer change: `_fetch_excerpts()` unions across anchors with
  dedup; the extension classifier matches any anchor for `yes`.
- API + MCP: `entity_ids: list[int]` on `POST /api/storylines` and
  `journal_create_storyline`.
- Webapp: the entity picker in `StorylineCreateModal` upgrades from
  single-select to multi-select (the component scaffold was already
  built multi-select-ready).

Per the project's migration-testing convention, the schema work needs
proper data-shape probing first. Single-entity storylines remain the
contract until that follow-up cycle picks the multi-entity work up.

## Test coverage

New surfaces all have tests:

- `tests/test_mcp_tools_storylines.py` — `TestStorylinesGuide`,
  `TestDeleteStoryline`, `TestCreateStoryline` (incl. timeout fallback,
  not-configured, soft-fail).
- `tests/test_api_storylines_write.py` — new file. POST create returns
  `generation_job_id`; regenerate accepts body variants;
  `mode=append` validation surfaces 400.
- `tests/test_storyline_generation.py` — `TestAppendMode` covers happy
  path (existing panels grow), seam transition, and
  `start_date < last_generated_at` rejection.
- `tests/test_storyline_jobs.py` — worker passes through new params.

The `TestAppendMode` fixtures intentionally use a `last_generated_at`
**in the future** relative to the test clock so the boundary
(`start_date >= last_generated_at`) is exercised meaningfully — sticking
to backdated fixtures would have left the validation only loosely
covered.

## Related files

- `src/journal/mcp_server/tools/storylines.py` — all docstring/annotation
  changes, new guide + delete tools, create poll-block.
- `src/journal/api/ingestion.py` — POST regenerate body, POST create
  auto-kick.
- `src/journal/services/jobs/{validation,runner}.py` — new param plumbing.
- `src/journal/services/jobs/workers/storyline_generation.py` — read
  new params from job row.
- `src/journal/services/storylines/service.py` — append-mode happy path,
  validation, seam transition logic.
- `src/journal/providers/storyline_narrator.py` — `prior_narrative` kwarg.
