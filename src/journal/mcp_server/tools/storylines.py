"""MCP tools for storylines.

* ``journal_list_storylines`` — list user's storylines
* ``journal_get_storyline`` — fetch one storyline + both panels
* ``journal_create_storyline`` — seed a new storyline (entity_id + name)
* ``journal_regenerate_storyline`` — queue a regeneration job and
  block until terminal state (uses ``_poll_job_until_terminal``).
* ``journal_storylines_guide`` — concept + workflow primer (read-only,
  always available).
* ``journal_delete_storyline`` — destructive cascade delete of a
  storyline and its panels.

Tools refuse with an actionable message when the storylines feature
isn't wired on this server. Output is formatted text (per the
project's tool-output convention).
"""

import logging
import time
from typing import Annotated

from mcp.server.fastmcp import Context
from pydantic import Field

from journal.mcp_server.app import mcp
from journal.mcp_server.tools._ctx import (
    _get_entity_store,
    _get_job_repository,
    _get_job_runner,
    _get_storyline_repository,
    _user_id,
)
from journal.mcp_server.tools.jobs import _poll_job_until_terminal

log = logging.getLogger(__name__)


@mcp.tool(annotations={"readOnlyHint": True})
def journal_list_storylines(
    status: Annotated[
        str | None,
        Field(
            description=(
                "Filter by status: 'active' for current storylines, "
                "'archived' for retired ones. Omit (None) to return all."
            ),
        ),
    ] = None,
    limit: Annotated[
        int,
        Field(
            description=(
                "Maximum number of storylines to return (capped at 200). "
                "Default 50."
            ),
        ),
    ] = 50,
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """List all storylines belonging to the authenticated user.

    Use this to discover existing storyline ids before calling
    ``journal_get_storyline``, ``journal_regenerate_storyline``, or
    ``journal_delete_storyline``. Filter by ``status='active'`` to see
    only current storylines, or ``'archived'`` to see retired ones.
    Returns each storyline's id, name, linked entity_id, status, and
    last_generated_at timestamp. If no storylines exist yet, the
    response says so; use ``journal_create_storyline`` to create the
    first one.
    """
    log.info("Tool call: journal_list_storylines(status=%s)", status)
    repo = _get_storyline_repository(ctx)
    if repo is None:
        return "Storylines feature is not configured on this server."
    user_id = _user_id(ctx)
    rows = repo.list_storylines(user_id=user_id, status=status, limit=min(limit, 200))
    if not rows:
        return "No storylines yet."
    lines = [f"Found {len(rows)} storyline(s):"]
    for s in rows:
        last_gen = s.last_generated_at or "never"
        lines.append(
            f"  [{s.id}] {s.name} — entity_id={s.entity_id}, "
            f"status={s.status}, last_generated={last_gen}"
        )
    return "\n".join(lines)


@mcp.tool(annotations={"readOnlyHint": True})
def journal_get_storyline(
    storyline_id: Annotated[
        int,
        Field(
            description=(
                "The integer id of the storyline to fetch. Obtain from "
                "journal_list_storylines."
            ),
        ),
    ],
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Fetch a single storyline by id, including both its AI-generated panels.

    The curation panel is a verbatim-quotes timeline of dated journal
    excerpts with transition phrases. The narrative panel is a
    synthesized third-person prose account backed by Anthropic's
    Citations API. Panels are empty until ``journal_regenerate_storyline``
    has completed at least once. Use ``journal_list_storylines`` to find
    the id. This tool is read-only and safe to call repeatedly.
    """
    log.info("Tool call: journal_get_storyline(id=%d)", storyline_id)
    repo = _get_storyline_repository(ctx)
    if repo is None:
        return "Storylines feature is not configured on this server."
    user_id = _user_id(ctx)
    storyline = repo.get_storyline(storyline_id, user_id=user_id)
    if storyline is None:
        return f"Storyline {storyline_id} not found."
    panels = {p.panel_kind: p for p in repo.list_panels(storyline.id)}

    lines = [
        f"Storyline {storyline.id}: {storyline.name}",
        f"  entity_id={storyline.entity_id}, status={storyline.status}",
        f"  last_generated_at={storyline.last_generated_at or 'never'}",
        "",
    ]
    for kind in ("curation", "narrative"):
        panel = panels.get(kind)
        lines.append(f"=== Panel: {kind} ===")
        if panel is None:
            lines.append("  (not generated yet)")
            continue
        lines.append(
            f"  citations={panel.citation_count}, "
            f"source_entries={len(panel.source_entry_ids)}, "
            f"model={panel.model_used}"
        )
        for seg in panel.segments:
            if seg.get("kind") == "text":
                lines.append(f"  {seg.get('text', '')}")
            elif seg.get("kind") == "citation":
                lines.append(
                    f"  [entry {seg.get('entry_id')}] "
                    f"{seg.get('quote', '')!r}"
                )
        lines.append("")
    return "\n".join(lines)


@mcp.tool()
def journal_create_storyline(
    entity_id: Annotated[
        int,
        Field(
            description=(
                "The id of the entity (person, activity, place, etc.) to "
                "anchor the storyline on. Obtain from journal_list_entities. "
                "Resolve disambiguation here — for ambiguous names like "
                "'Atlas', pick the right entity_id (e.g. the person, not "
                "the organization)."
            ),
        ),
    ],
    name: Annotated[
        str,
        Field(
            description=(
                "Display name for the storyline (e.g. 'Running', "
                "'Atlas-the-son'). Together with entity_id this must be "
                "unique for the user."
            ),
        ),
    ],
    description: Annotated[
        str,
        Field(
            description=(
                "Optional short description; passed to the narrative "
                "model so it can disambiguate what the storyline is about."
            ),
        ),
    ] = "",
    start_date: Annotated[
        str | None,
        Field(
            description=(
                "ISO date (YYYY-MM-DD); entries before this are excluded. "
                "Optional — defaults to 90 days before today when omitted."
            ),
        ),
    ] = None,
    end_date: Annotated[
        str | None,
        Field(
            description=(
                "ISO date (YYYY-MM-DD); entries after this are excluded. "
                "Optional — defaults to today when omitted."
            ),
        ),
    ] = None,
    timeout_seconds: Annotated[
        int,
        Field(
            description=(
                "Max wait for generation before returning the in-progress "
                "job's status (default 120). Use a longer value when the "
                "anchor entity has a deep history and the initial "
                "generation needs to chew through many entries."
            ),
        ),
    ] = 120,
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Create a new storyline AND generate its panels in one call.

    The ``entity_id`` must come from ``journal_list_entities``; if a
    storyline with the same entity and name already exists, the
    existing id is returned without queueing any generation work. On
    success this tool kicks off a generation job and blocks up to
    ``timeout_seconds`` waiting for the panels to be produced — on
    completion it points the caller at ``journal_get_storyline`` to
    read the result. If the job is still in flight when the timeout
    fires, the returned message includes the job id so the caller can
    poll ``journal_get_job_status`` directly.
    """
    log.info(
        "Tool call: journal_create_storyline(entity_id=%d, name=%s)",
        entity_id, name,
    )
    repo = _get_storyline_repository(ctx)
    if repo is None:
        return "Storylines feature is not configured on this server."
    user_id = _user_id(ctx)

    # Verify entity exists for this user
    entity_store = _get_entity_store(ctx)
    entity = entity_store.get_entity(entity_id, user_id=user_id)
    if entity is None:
        return (
            f"Entity {entity_id} not found for this user. "
            "Use journal_list_entities to find the right id."
        )

    existing = repo.find_by_entity(
        user_id=user_id, entity_id=entity_id, name=name.strip(),
    )
    if existing is not None:
        return (
            f"Storyline already exists: id={existing.id}, "
            f"name={existing.name!r}. Use journal_regenerate_storyline "
            f"to refresh its panels."
        )
    storyline = repo.create_storyline(
        user_id=user_id, entity_id=entity_id, name=name.strip(),
        description=description, start_date=start_date, end_date=end_date,
    )

    # Auto-kick the generation job and poll until terminal. We do
    # not roll back the create on submit/poll failure — the storyline
    # row remains; the caller learns it can retry via
    # journal_regenerate_storyline.
    runner = _get_job_runner(ctx)
    try:
        job = runner.submit_storyline_generation(
            storyline.id, user_id=user_id,
        )
    except RuntimeError as exc:
        return (
            f"Created storyline {storyline.id}: {storyline.name!r} "
            f"(entity {entity.canonical_name}). However, generation "
            f"could not be queued: {exc}. Use "
            f"journal_regenerate_storyline({storyline.id}) to retry."
        )

    start = time.monotonic()
    finished = _poll_job_until_terminal(
        _get_job_repository(ctx), job.id, timeout=timeout_seconds,
    )
    elapsed = time.monotonic() - start
    status = finished["status"]

    if status == "succeeded":
        return (
            f"Created storyline {storyline.id}: {storyline.name!r} "
            f"(entity {entity.canonical_name}). Panels generated in "
            f"{elapsed:.1f}s. Use journal_get_storyline({storyline.id}) "
            f"to read."
        )
    if status == "timeout":
        return (
            f"Created storyline {storyline.id}: {storyline.name!r}. "
            f"Generation job {job.id} is still running after "
            f"{timeout_seconds}s; use "
            f"journal_get_job_status('{job.id}') to check progress, "
            f"then journal_get_storyline({storyline.id})."
        )
    # status == "failed"
    error = finished.get("error_message") or "unknown error"
    return (
        f"Created storyline {storyline.id}: {storyline.name!r}, but "
        f"generation failed: {error}. Use "
        f"journal_regenerate_storyline({storyline.id}) to retry."
    )


@mcp.tool(annotations={"idempotentHint": True})
def journal_regenerate_storyline(
    storyline_id: Annotated[
        int,
        Field(
            description=(
                "The integer id of the storyline to regenerate. Obtain "
                "from journal_list_storylines."
            ),
        ),
    ],
    timeout_seconds: Annotated[
        int,
        Field(
            description=(
                "Maximum seconds to block waiting for the regeneration "
                "job to finish (default 120). Use a longer value for "
                "cold generation of a large corpus. If the timeout fires "
                "the tool returns the job id so the caller can poll with "
                "journal_get_job_status."
            ),
        ),
    ] = 120,
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Regenerate both AI panels (narrative prose + curation timeline) for a storyline.

    This call blocks until the job completes or times out — AI
    generation typically takes 15-60 seconds depending on corpus size.
    After success, call ``journal_get_storyline`` to read the panels. If
    the job times out, use ``journal_get_job_status`` with the returned
    job_id to check progress later. Regeneration is idempotent; call it
    whenever you want the panels refreshed with the latest journal
    entries.
    """
    log.info(
        "Tool call: journal_regenerate_storyline(id=%d)", storyline_id,
    )
    repo = _get_storyline_repository(ctx)
    if repo is None:
        return "Storylines feature is not configured on this server."
    runner = _get_job_runner(ctx)
    user_id = _user_id(ctx)
    storyline = repo.get_storyline(storyline_id, user_id=user_id)
    if storyline is None:
        return f"Storyline {storyline_id} not found."
    try:
        job = runner.submit_storyline_generation(
            storyline_id, user_id=user_id,
        )
    except RuntimeError as exc:
        return f"Cannot regenerate: {exc}"
    finished = _poll_job_until_terminal(
        _get_job_repository(ctx), job.id, timeout=timeout_seconds,
    )
    status = finished["status"]
    if status == "timeout":
        return (
            f"Job {job.id} did not finish within {timeout_seconds}s. "
            f"Use journal_get_job_status('{job.id}') to check status later."
        )
    if status == "succeeded":
        r = finished.get("result") or {}
        return (
            f"Regeneration succeeded.\n"
            f"  entries: {r.get('entry_count', 0)} "
            f"({r.get('entity_mention_count', 0)} via entity, "
            f"{r.get('fts_fallback_count', 0)} via FTS fallback)\n"
            f"  narrative citations: {r.get('narrative_citation_count', 0)} "
            f"(model {r.get('narrative_model', '?')})\n"
            f"  curation citations: {r.get('curation_citation_count', 0)} "
            f"(model {r.get('curation_model', '?')})\n"
            f"  Use journal_get_storyline({storyline_id}) to read the result."
        )
    return (
        f"Regeneration failed: "
        f"{finished.get('error_message') or 'unknown error'}"
    )


_STORYLINES_GUIDE = """\
# Storylines — Concept and Workflow Guide

## What is a storyline?

A storyline is a long-running, AI-curated thread through your journal
anchored on a specific entity — a person, an activity, a place, a
project. Examples: "Running", "Atlas-the-son", "Lisbon trip", "Job
search 2026". Each storyline pulls together every journal entry that
mentions its anchor entity (with FTS fallback for older entries that
predate entity extraction) and renders two complementary panels.

## The two-panel concept

- **Curation panel** — a verbatim-quotes timeline. Dated excerpts
  pulled directly from your journal in chronological order, stitched
  with short transition phrases. Nothing is paraphrased. This is the
  "what did I actually write?" view.
- **Narrative panel** — a synthesized third-person prose account.
  Built with Anthropic's Citations API, every claim is anchored to a
  specific source entry. This is the "what story does it tell?" view.

Panels are empty until ``journal_regenerate_storyline`` has run at
least once.

## Typical workflow

1. ``journal_list_entities`` — find the entity_id you want to anchor
   on. Disambiguate here for common names.
2. ``journal_create_storyline(entity_id, name)`` — create the storyline
   record. This does NOT generate content on its own (yet); the
   storyline starts empty.
3. ``journal_regenerate_storyline(storyline_id)`` — generate the two
   panels. Blocks 15-60 seconds typically; returns a job id on timeout.
4. ``journal_get_storyline(storyline_id)`` — read the rendered panels.
5. ``journal_list_storylines()`` — anytime, to discover what storylines
   already exist.
6. ``journal_delete_storyline(storyline_id)`` — when a storyline has
   served its purpose. Cascades to its panels; cannot be undone.

## Date range and regeneration behavior

- By default, ``journal_create_storyline`` includes only journal entries
  from the last 90 days. Pass ``start_date`` and/or ``end_date`` (ISO
  YYYY-MM-DD) to widen the window.
- Regeneration currently runs in **Replace** mode: every call rebuilds
  the panels from scratch over the current window. An append-update
  mode that extends panels with only the newest entries is planned for
  a follow-up release.
- Regeneration is idempotent — calling it twice in a row produces the
  same result, modulo any new journal entries that landed in between.

## Configuration requirements

The storylines feature requires ``ANTHROPIC_API_KEY`` to be configured
on the server (the Citations API powers the narrative panel). If the
key is missing at boot, every storylines tool except this guide will
return ``"Storylines feature is not configured on this server."`` —
ask your admin to set the key and restart.
"""


@mcp.tool(annotations={"readOnlyHint": True})
def journal_storylines_guide(
    ctx: Context = None,  # type: ignore[assignment]  # noqa: ARG001
) -> str:
    """Returns a guide explaining the storylines feature.

    Call this first if you are unfamiliar with storylines or unsure
    which storyline tool to use. The guide covers what storylines are,
    the two-panel concept, the typical workflow, and configuration
    requirements. Read-only and safe to call anytime.
    """
    log.info("Tool call: journal_storylines_guide()")
    return _STORYLINES_GUIDE


@mcp.tool(annotations={"destructiveHint": True})
def journal_delete_storyline(
    storyline_id: Annotated[
        int,
        Field(
            description=(
                "The integer id of the storyline to delete. Obtain from "
                "journal_list_storylines."
            ),
        ),
    ],
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Delete a storyline permanently.

    This cascades to the two AI-generated panels (curation and
    narrative). Any historical generation jobs in the jobs table are
    left in place as an audit trail and do not block the delete. Use
    ``journal_list_storylines`` to find the id first if you are
    uncertain. This action cannot be undone.
    """
    log.info("Tool call: journal_delete_storyline(id=%d)", storyline_id)
    repo = _get_storyline_repository(ctx)
    if repo is None:
        return "Storylines feature is not configured on this server."
    user_id = _user_id(ctx)
    deleted = repo.delete_storyline(storyline_id, user_id=user_id)
    if not deleted:
        return f"Storyline {storyline_id} not found for this user."
    return f"Deleted storyline {storyline_id}."
