# MCP `journal_regenerate_storyline` — fix three stacked bugs

**Date:** 2026-05-12
**Branch:** `worktree-eng-mcp-storylines-regen-fix` (server)
**Touches:** `src/journal/mcp_server/tools/storylines.py`, `tests/test_mcp_tools_storylines.py` (new)

## How it surfaced

While doing the W10 prod read for the `source="text"` Citations change (`260512-storylines-cite-text.md`), another agent session called `journal_regenerate_storyline` against the Running and Atlas storylines on prod. Every call returned an error to the agent:

> `_poll_job_until_terminal() got an unexpected keyword argument 'timeout_seconds'`

The agent first concluded the tool was broken and the jobs hadn't run. After waiting and re-checking the `last_generated_at` timestamps, it discovered the regeneration *had* completed successfully — the job worker queued and ran fine; only the polling wrapper raised. The TypeError fired *after* `runner.submit_storyline_generation(...)` had already returned, so the work was kicked off and ran asynchronously while the tool reported failure. Misleading at best; in a more cautious agent it would have caused redundant resubmissions.

## What was actually broken

`src/journal/mcp_server/tools/storylines.py:206-228` had three stacked bugs in the tool's success/failure path. Only #1 surfaced in prod because it raised before the others could fire.

1. **Kwarg name mismatch.** The poll helper's parameter is `timeout: float = 3600.0` (`mcp_server/tools/jobs.py:46`). The caller passed `timeout_seconds=timeout_seconds`. TypeError on every call regardless of whether the job succeeded, failed, or was still running.
2. **Dead `is None` check.** `_poll_job_until_terminal` always returns a dict — the timeout path returns `{"status": "timeout", ...}`, not `None`. The wrapper's `if finished is None:` branch was unreachable, and the human-readable "did not finish within Ns" message it carried was therefore impossible to ever produce.
3. **Attribute access on a dict.** Even after fixing #1, the success branch did `finished.status`, `finished.result`, `finished.error_message`. The helper returns `dict[str, Any]`. Would have raised AttributeError on the first job that actually completed in time.

## Why nothing caught it

W9 in `storylines-plan.md` named `tests/test_mcp_tools_storylines.py` as a required deliverable for the MCP-tools layer. The file never got written. The tool shipped with zero unit coverage; the only thing that would have caught any of these bugs was an end-to-end run, and the W10 acceptance gate was a qualitative *read* of generated storylines, not a check that the regeneration tool returns a clean string. The bug was masked because the underlying job worker functioned correctly.

## Fix

`src/journal/mcp_server/tools/storylines.py` — three targeted edits inside `journal_regenerate_storyline`:

- Pass `timeout=timeout_seconds` (the helper's actual parameter).
- Branch on `finished["status"]` instead of `finished is None` / `finished.status`. The new explicit branches are `timeout` (actionable follow-up message that names the job id and points at `journal_get_job_status`), `succeeded` (formatted result blob), and the default catch-all `failed`.
- Use `finished.get("result") or {}` and `finished.get("error_message")` for the dict accesses.

The user-visible strings are unchanged in shape — only the now-unreachable "did not finish" branch became reachable, and the follow-up pointer there switched from `journal_get_job` to `journal_get_job_status` (the actual tool name; the old string named a tool that doesn't exist).

## Tests

New `tests/test_mcp_tools_storylines.py` (12 cases) — the file W9 owed.

Coverage breakdown:

- **Regenerate (7 cases):** success path with full result blob → asserts the summary string includes entry count, citation count, model name, and the `journal_get_storyline(...)` follow-up pointer. Failed-with-error-message, failed-with-None-error-message ("unknown error" fallback), timeout (uses `timeout_seconds=0` so the poll loop exits without sleeping — fast test, no monkeypatching of `time`). Not-configured short-circuit, storyline-not-found, runner-raises-RuntimeError.
- **List (3 cases):** lists user's storylines with `last_generated` formatting (timestamp vs "never"), not-configured short-circuit, empty list.
- **Get (2 cases):** not-configured short-circuit, storyline-not-found.

One surfacing-test friction: `_get_storyline_repository` asserts `isinstance(repo, SQLiteStorylineRepository)`. Plain `MagicMock()` fails it. Fix: use `MagicMock(spec=SQLiteStorylineRepository)` via a small `_storyline_repo_mock()` helper in the test file. Keeps the production isinstance guard intact while unblocking unit tests. The other lifespan helpers (`_get_job_runner`, `_get_job_repository`) don't have isinstance assertions and accept plain mocks; only the storyline repo has the stricter check.

## Out of scope, called out

- The other "agent didn't see the journal MCP server" issue from the transcript is a Claude Code session-config issue on that side, not a journal-repo issue. Not addressable from here.
- Test coverage for `journal_create_storyline` deferred — its surface is straightforward (entity lookup + duplicate check + create) and the bug pattern that surfaced wasn't in it. Worth adding later as part of a broader MCP-tools coverage pass.
- No webapp changes. The webapp talks to the REST API, not MCP; the REST regeneration endpoint (`POST /api/storylines/{id}/regenerate`) is in `api/ingestion.py` and uses a different code path that doesn't share this polling wrapper.
