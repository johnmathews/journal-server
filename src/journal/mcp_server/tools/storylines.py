"""MCP tools for storylines.

* ``journal_list_storylines`` — list user's storylines
* ``journal_get_storyline`` — fetch one storyline + both panels
* ``journal_create_storyline`` — seed a new storyline (1..15 anchor entities + name)
* ``journal_set_storyline_anchors`` — replace the anchor set on an existing storyline
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
from typing import Annotated, Any

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
from journal.services.storylines.service import MAX_ANCHORS

log = logging.getLogger(__name__)


def _format_anchors(
    repo: Any, entity_store: Any, storyline_id: int,
) -> str:
    """Render anchors as a comma-separated 'name (id)' string."""
    anchor_ids = repo.list_anchors(storyline_id)
    parts: list[str] = []
    for entity_id in anchor_ids:
        if entity_store is not None:
            entity = entity_store.get_entity(entity_id)
            name = entity.canonical_name if entity else f"<missing-{entity_id}>"
        else:
            name = f"entity_{entity_id}"
        parts.append(f"{name} (id={entity_id})")
    return ", ".join(parts) if parts else "<no anchors>"


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
    ``journal_get_storyline``, ``journal_regenerate_storyline``,
    ``journal_set_storyline_anchors``, or ``journal_delete_storyline``.
    Filter by ``status='active'`` to see only current storylines, or
    ``'archived'`` to see retired ones. Returns each storyline's id,
    name, anchor entities, status, and last_generated_at timestamp. If
    no storylines exist yet, the response says so; use
    ``journal_create_storyline`` to create the first one.
    """
    log.info("Tool call: journal_list_storylines(status=%s)", status)
    repo = _get_storyline_repository(ctx)
    if repo is None:
        return "Storylines feature is not configured on this server."
    user_id = _user_id(ctx)
    rows = repo.list_storylines(user_id=user_id, status=status, limit=min(limit, 200))
    if not rows:
        return "No storylines yet."
    entity_store = _get_entity_store(ctx)
    lines = [f"Found {len(rows)} storyline(s):"]
    for s in rows:
        last_gen = s.last_generated_at or "never"
        anchors = _format_anchors(repo, entity_store, s.id)
        lines.append(
            f"  [{s.id}] {s.name} — anchors: {anchors}, "
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
    entity_store = _get_entity_store(ctx)

    chapters = repo.list_chapters(storyline.id)
    lines = [
        f"Storyline {storyline.id}: {storyline.name}",
        f"  anchors: {_format_anchors(repo, entity_store, storyline.id)}",
        f"  status={storyline.status}, "
        f"last_generated_at={storyline.last_generated_at or 'never'}",
    ]
    if chapters:
        lines.append(f"  chapters ({len(chapters)}):")
        for ch in chapters:
            window = f"{ch.start_date or '…'} – {ch.end_date or 'now'}"
            lines.append(
                f"    [{ch.id}] seq {ch.seq}: {ch.title or '(untitled)'} "
                f"({window}) state={ch.state}"
            )
    lines.append("")
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
    entity_ids: Annotated[
        list[int],
        Field(
            description=(
                "List of entity ids to anchor this storyline on. Must "
                f"contain 1..{MAX_ANCHORS} entries. Each anchor is an "
                "entity (person, activity, place, etc.) — obtain ids "
                "from journal_list_entities. Resolve disambiguation "
                "here. For a single-entity storyline, pass a one-item "
                "list (e.g. [42]). For a 'X and Y together' storyline, "
                "pass multiple ids (e.g. [42, 99])."
            ),
        ),
    ],
    name: Annotated[
        str,
        Field(
            description=(
                "Display name for the storyline (e.g. 'Running', "
                "'Atlas and Vienna together'). Two storylines with the "
                "same user, same exact anchor set, and same name are "
                "considered duplicates and a create call will return "
                "the existing one."
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
                "anchor set has a deep history and the initial "
                "generation needs to chew through many entries."
            ),
        ),
    ] = 120,
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Create a multi-anchor storyline AND generate its panels in one call.

    A storyline is anchored on one or more entities. Excerpts are
    pulled across all anchors and unioned by entry. The
    ``entity_ids`` must come from ``journal_list_entities``; if a
    storyline with the same name and exact anchor set already
    exists, the existing id is returned without queueing any
    generation work. On success this tool kicks off a generation job
    and blocks up to ``timeout_seconds`` waiting for the panels to be
    produced.
    """
    log.info(
        "Tool call: journal_create_storyline(entity_ids=%s, name=%s)",
        entity_ids, name,
    )
    repo = _get_storyline_repository(ctx)
    if repo is None:
        return "Storylines feature is not configured on this server."
    user_id = _user_id(ctx)

    if not entity_ids:
        return "entity_ids must contain at least one entity id."
    unique_ids = sorted(set(entity_ids))
    if len(unique_ids) > MAX_ANCHORS:
        return (
            f"entity_ids has {len(unique_ids)} unique anchors; the cap "
            f"is {MAX_ANCHORS}."
        )

    entity_store = _get_entity_store(ctx)
    missing: list[int] = []
    canonical_names: list[str] = []
    for eid in unique_ids:
        entity = entity_store.get_entity(eid, user_id=user_id)
        if entity is None:
            missing.append(eid)
        else:
            canonical_names.append(entity.canonical_name)
    if missing:
        return (
            f"Entity id(s) not found for this user: {missing}. "
            "Use journal_list_entities to find the right ids."
        )

    name = name.strip()
    existing = repo.find_by_anchor_set(
        user_id=user_id, entity_ids=unique_ids, name=name,
    )
    if existing is not None:
        return (
            f"Storyline already exists: id={existing.id}, "
            f"name={existing.name!r}. Use journal_regenerate_storyline "
            f"to refresh its panels."
        )
    storyline = repo.create_storyline(
        user_id=user_id, entity_ids=unique_ids, name=name,
        description=description, start_date=start_date, end_date=end_date,
    )

    runner = _get_job_runner(ctx)
    anchor_summary = ", ".join(canonical_names)
    try:
        job = runner.submit_storyline_generation(
            storyline.id, user_id=user_id,
        )
    except RuntimeError as exc:
        return (
            f"Created storyline {storyline.id}: {storyline.name!r} "
            f"(anchors: {anchor_summary}). However, generation "
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
            f"(anchors: {anchor_summary}). Panels generated in "
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
    error = finished.get("error_message") or "unknown error"
    return (
        f"Created storyline {storyline.id}: {storyline.name!r}, but "
        f"generation failed: {error}. Use "
        f"journal_regenerate_storyline({storyline.id}) to retry."
    )


@mcp.tool()
def journal_set_storyline_anchors(
    storyline_id: Annotated[
        int,
        Field(
            description=(
                "The integer id of the storyline to update. Obtain "
                "from journal_list_storylines."
            ),
        ),
    ],
    entity_ids: Annotated[
        list[int],
        Field(
            description=(
                "The new anchor set. Replaces the existing anchors "
                "entirely (this is set semantics, not patch). Must "
                f"contain 1..{MAX_ANCHORS} entries. After calling this, "
                "the storyline's panels are stale — call "
                "journal_regenerate_storyline to rebuild them against "
                "the new anchor set."
            ),
        ),
    ],
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Replace the anchor set on an existing storyline.

    Useful when you've created a storyline anchored on one entity
    and want to expand it to include related entities, or when you
    realise an anchor was the wrong disambiguation. The storyline's
    name, description, date range, and panels are preserved, but
    the panels become stale — typically you'll call
    ``journal_regenerate_storyline`` next.
    """
    log.info(
        "Tool call: journal_set_storyline_anchors(id=%d, entity_ids=%s)",
        storyline_id, entity_ids,
    )
    repo = _get_storyline_repository(ctx)
    if repo is None:
        return "Storylines feature is not configured on this server."
    user_id = _user_id(ctx)
    storyline = repo.get_storyline(storyline_id, user_id=user_id)
    if storyline is None:
        return f"Storyline {storyline_id} not found."
    if not entity_ids:
        return "entity_ids must contain at least one entity id."
    unique_ids = sorted(set(entity_ids))
    if len(unique_ids) > MAX_ANCHORS:
        return (
            f"entity_ids has {len(unique_ids)} unique anchors; the cap "
            f"is {MAX_ANCHORS}."
        )
    entity_store = _get_entity_store(ctx)
    missing = [
        eid for eid in unique_ids
        if entity_store.get_entity(eid, user_id=user_id) is None
    ]
    if missing:
        return (
            f"Entity id(s) not found for this user: {missing}. "
            "Use journal_list_entities to find the right ids."
        )
    repo.set_anchors(storyline_id, unique_ids)
    new_anchors = _format_anchors(repo, entity_store, storyline_id)
    return (
        f"Updated anchors for storyline {storyline_id}: {new_anchors}. "
        f"Panels are now stale — call journal_regenerate_storyline("
        f"{storyline_id}) to rebuild."
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
    chapter_id: Annotated[
        int | None,
        Field(
            description=(
                "Optional: regenerate a single chapter rather than the "
                "storyline's open chapter. The chapter's own date window "
                "is authoritative (replace mode only). Obtain chapter ids "
                "from journal_get_storyline. Omit to regenerate the open "
                "chapter."
            ),
        ),
    ] = None,
    resegment: Annotated[
        bool,
        Field(
            description=(
                "Re-carve the storyline into titled ~200-word chapters "
                "(re-segmentation) instead of refreshing the open "
                "chapter's panels over its current window. Storyline-level "
                "only — mutually exclusive with chapter_id. Default False."
            ),
        ),
    ] = False,
    override_locked: Annotated[
        bool,
        Field(
            description=(
                "Only with resegment=True: also re-carve across your "
                "hand-painted (locked) chapter boundaries, treating the "
                "whole timeline as one span. Ignored when resegment is "
                "False. Default False."
            ),
        ),
    ] = False,
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Regenerate both AI panels (narrative prose + curation timeline) for a storyline.

    This call blocks until the job completes or times out — AI
    generation typically takes 15-60 seconds depending on corpus size.
    After success, call ``journal_get_storyline`` to read the panels. If
    the job times out, use ``journal_get_job_status`` with the returned
    job_id to check progress later. Regeneration is idempotent; call it
    whenever you want the panels refreshed with the latest journal
    entries. Pass ``chapter_id`` to regenerate one specific chapter, or
    ``resegment=True`` to re-carve the whole storyline into titled
    word-sized chapters (optionally with ``override_locked=True`` to
    cross hand-painted chapter boundaries).
    """
    log.info(
        "Tool call: journal_regenerate_storyline(id=%d, chapter_id=%s, "
        "resegment=%s, override_locked=%s)",
        storyline_id, chapter_id, resegment, override_locked,
    )
    repo = _get_storyline_repository(ctx)
    if repo is None:
        return "Storylines feature is not configured on this server."
    if chapter_id is not None and resegment:
        return (
            "chapter_id and resegment are mutually exclusive: a "
            "chapter-scoped regeneration cannot re-segment the storyline. "
            "Omit chapter_id to re-segment, or omit resegment to "
            "regenerate a single chapter."
        )
    runner = _get_job_runner(ctx)
    user_id = _user_id(ctx)
    storyline = repo.get_storyline(storyline_id, user_id=user_id)
    if storyline is None:
        return f"Storyline {storyline_id} not found."
    submit_kwargs: dict[str, Any] = {"user_id": user_id}
    if chapter_id is not None:
        chapter = repo.get_chapter(chapter_id)
        if chapter is None or chapter.storyline_id != storyline_id:
            return (
                f"Chapter {chapter_id} not found on storyline "
                f"{storyline_id}."
            )
        submit_kwargs["chapter_id"] = chapter_id
        submit_kwargs["mode"] = "replace"
    if resegment:
        submit_kwargs["resegment"] = True
        submit_kwargs["override_locked"] = override_locked
    try:
        job = runner.submit_storyline_generation(
            storyline_id, **submit_kwargs,
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


_STORYLINES_GUIDE = f"""\
# Storylines — Concept and Workflow Guide

## What is a storyline?

A storyline is a long-running, AI-curated thread through your journal
anchored on one or more entities — people, activities, places, or
projects. Examples: a single-anchor storyline like "Running" (anchored
on Running), or a multi-anchor storyline like "Atlas and Vienna
together" (anchored on both Atlas and Vienna).

Each storyline pulls together every journal entry that mentions any of
its anchor entities (with FTS fallback for older entries that predate
entity extraction). When an entry mentions multiple anchors, it is
contributed once — anchors are semantic equals, not weighted positions.

The current soft cap is {MAX_ANCHORS} anchors per storyline.

## The two-panel concept

- **Curation panel** — a verbatim-quotes timeline. Dated excerpts
  pulled directly from your journal in chronological order, stitched
  with short transition phrases. Nothing is paraphrased. This is the
  "what did I actually write?" view.
- **Narrative panel** — a synthesized third-person prose account.
  Built with Anthropic's Citations API, every claim is anchored to a
  specific source entry. This is the "what story does it tell?" view.

Panels are empty until ``journal_regenerate_storyline`` has run at
least once (which ``journal_create_storyline`` does automatically).

## Typical workflow

1. ``journal_list_entities`` — find the entity_ids you want to anchor
   on. Disambiguate here for common names.
2. ``journal_create_storyline(entity_ids, name)`` — create the storyline
   and auto-kick generation. Single-anchor: pass a one-item list,
   e.g. ``[42]``. Multi-anchor: pass several ids, e.g. ``[42, 99]``.
3. ``journal_get_storyline(storyline_id)`` — read the rendered panels.
4. ``journal_list_storylines()`` — anytime, to discover what storylines
   already exist.
5. ``journal_set_storyline_anchors(storyline_id, entity_ids)`` — replace
   the anchor set on an existing storyline (e.g. add a co-anchor).
   After this, call ``journal_regenerate_storyline`` to rebuild the
   panels against the new set.
6. ``journal_regenerate_storyline(storyline_id)`` — refresh the panels
   after the anchor set changes or new entries arrive.
7. ``journal_delete_storyline(storyline_id)`` — when a storyline has
   served its purpose. Cascades to its panels and anchor rows.

## Date range and regeneration behavior

- By default, ``journal_create_storyline`` includes only journal entries
  from the last 90 days. Pass ``start_date`` and/or ``end_date`` (ISO
  YYYY-MM-DD) to widen the window.
- Regeneration runs in **Replace** mode by default — the panels are
  rebuilt from scratch over the current window. An **Append** mode is
  also available via the REST endpoint and the in-app UI when the
  storyline has been generated at least once.
- Regeneration is idempotent — calling it twice in a row produces the
  same result modulo new entries.

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


def _enqueue_chapter_regen(
    ctx: Any,
    storyline_id: int,
    chapter_id: int,
    end_date: str | None,
) -> str:
    """Queue regeneration for a chapter; return a note about it.

    Uses replace mode for closed chapters (end_date set), omits mode
    for open chapters (end_date None). If the runner is unavailable,
    returns a note but does not raise.
    """
    runner = _get_job_runner(ctx)
    user_id = _user_id(ctx)
    kwargs: dict[str, Any] = {
        "user_id": user_id,
        "chapter_id": chapter_id,
    }
    if end_date is not None:
        kwargs["mode"] = "replace"
    try:
        runner.submit_storyline_generation(storyline_id, **kwargs)
        return "Regeneration queued."
    except Exception:  # noqa: BLE001
        return (
            "Note: regeneration could not be queued — call "
            f"journal_regenerate_storyline({storyline_id}) manually."
        )


@mcp.tool()
def journal_add_storyline_chapter(
    storyline_id: Annotated[
        int,
        Field(
            description=(
                "The integer id of the storyline to add a chapter to. "
                "Obtain from journal_list_storylines."
            ),
        ),
    ],
    start_date: Annotated[
        str,
        Field(
            description=(
                "ISO date (YYYY-MM-DD) for the chapter's start date."
            ),
        ),
    ],
    end_date: Annotated[
        str | None,
        Field(
            description=(
                "ISO date (YYYY-MM-DD) for the chapter's end date. "
                "Omit (None) to create an open chapter."
            ),
        ),
    ] = None,
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Add a new chapter to an existing storyline.

    The chapter will be positioned after any existing chapters. Pass
    ``start_date`` (required) and optionally ``end_date``; omitting
    ``end_date`` creates an open chapter. The repo enforces that
    chapters do not overlap; a ``ValueError`` is surfaced as a
    human-readable error message. After creation, regeneration of the
    new chapter is queued automatically.
    """
    log.info(
        "Tool call: journal_add_storyline_chapter"
        "(storyline_id=%d, start_date=%s, end_date=%s)",
        storyline_id, start_date, end_date,
    )
    repo = _get_storyline_repository(ctx)
    if repo is None:
        return "Storylines feature is not configured on this server."
    user_id = _user_id(ctx)
    storyline = repo.get_storyline(storyline_id, user_id=user_id)
    if storyline is None:
        return f"Storyline {storyline_id} not found."
    try:
        chapter = repo.add_chapter(storyline_id, start_date, end_date)
    except ValueError as exc:
        return f"Could not add chapter: {exc}"
    regen_note = _enqueue_chapter_regen(
        ctx, storyline_id, chapter.id, chapter.end_date,
    )
    window = f"{chapter.start_date} – {chapter.end_date or 'now'}"
    return (
        f"Added chapter [{chapter.id}] to storyline {storyline_id} "
        f"({window}). {regen_note}"
    )


@mcp.tool()
def journal_split_storyline_chapter(
    storyline_id: Annotated[
        int,
        Field(
            description=(
                "The integer id of the storyline. Obtain from "
                "journal_list_storylines."
            ),
        ),
    ],
    chapter_id: Annotated[
        int,
        Field(
            description=(
                "The integer id of the chapter to split. Obtain from "
                "journal_get_storyline."
            ),
        ),
    ],
    date: Annotated[
        str,
        Field(
            description=(
                "ISO date (YYYY-MM-DD) at which to split. The left half "
                "ends the day before this date; the right half starts on "
                "this date. Must be strictly after the chapter's "
                "start_date."
            ),
        ),
    ],
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Split an existing chapter into two at a given date.

    The original chapter is replaced by two new chapters: the left half
    covers the original start up to (but not including) ``date``; the
    right half covers ``date`` onwards. Regeneration is queued for both
    halves automatically.
    """
    log.info(
        "Tool call: journal_split_storyline_chapter"
        "(storyline_id=%d, chapter_id=%d, date=%s)",
        storyline_id, chapter_id, date,
    )
    repo = _get_storyline_repository(ctx)
    if repo is None:
        return "Storylines feature is not configured on this server."
    user_id = _user_id(ctx)
    storyline = repo.get_storyline(storyline_id, user_id=user_id)
    if storyline is None:
        return f"Storyline {storyline_id} not found."
    chapter = repo.get_chapter(chapter_id)
    if chapter is None or chapter.storyline_id != storyline_id:
        return f"Chapter {chapter_id} not found on storyline {storyline_id}."
    try:
        left, right = repo.split_chapter(chapter_id, date)
    except ValueError as exc:
        return f"Could not split chapter: {exc}"
    _enqueue_chapter_regen(ctx, storyline_id, left.id, left.end_date)
    _enqueue_chapter_regen(ctx, storyline_id, right.id, right.end_date)
    return (
        f"Split chapter {chapter_id} into [{left.id}] "
        f"({left.start_date} – {left.end_date or 'now'}) and "
        f"[{right.id}] ({right.start_date} – {right.end_date or 'now'}). "
        "Regeneration queued for both."
    )


@mcp.tool()
def journal_merge_storyline_chapters(
    storyline_id: Annotated[
        int,
        Field(
            description=(
                "The integer id of the storyline. Obtain from "
                "journal_list_storylines."
            ),
        ),
    ],
    chapter_ids: Annotated[
        list[int],
        Field(
            description=(
                "List of chapter ids to merge (must be adjacent/contiguous). "
                "Obtain from journal_get_storyline."
            ),
        ),
    ],
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Merge two or more adjacent chapters into a single chapter.

    All supplied ``chapter_ids`` must belong to the same storyline and
    must be contiguous (no gaps in seq). The repo enforces adjacency; a
    ``ValueError`` is surfaced as a human-readable error message.
    Regeneration is queued for the merged chapter automatically.
    """
    log.info(
        "Tool call: journal_merge_storyline_chapters"
        "(storyline_id=%d, chapter_ids=%s)",
        storyline_id, chapter_ids,
    )
    repo = _get_storyline_repository(ctx)
    if repo is None:
        return "Storylines feature is not configured on this server."
    user_id = _user_id(ctx)
    storyline = repo.get_storyline(storyline_id, user_id=user_id)
    if storyline is None:
        return f"Storyline {storyline_id} not found."
    for cid in chapter_ids:
        ch = repo.get_chapter(cid)
        if ch is None or ch.storyline_id != storyline_id:
            return f"Chapter {cid} not found on storyline {storyline_id}."
    try:
        merged = repo.merge_chapters(chapter_ids)
    except ValueError as exc:
        return f"Could not merge chapters: {exc}"
    _enqueue_chapter_regen(ctx, storyline_id, merged.id, merged.end_date)
    return (
        f"Merged {len(chapter_ids)} chapters into [{merged.id}] "
        f"({merged.start_date} – {merged.end_date or 'now'}) "
        f"on storyline {storyline_id}. Regeneration queued."
    )


@mcp.tool()
def journal_update_storyline_chapter(
    storyline_id: Annotated[
        int,
        Field(
            description=(
                "The integer id of the storyline. Obtain from "
                "journal_list_storylines."
            ),
        ),
    ],
    chapter_id: Annotated[
        int,
        Field(
            description=(
                "The integer id of the chapter to update. Obtain from "
                "journal_get_storyline."
            ),
        ),
    ],
    title: Annotated[
        str | None,
        Field(
            description=(
                "New title for the chapter. If this is the only change "
                "(both start_date and end_date are omitted), only "
                "rename_chapter is called and regeneration is not queued."
            ),
        ),
    ] = None,
    start_date: Annotated[
        str | None,
        Field(
            description=(
                "New ISO start date (YYYY-MM-DD) for the chapter. "
                "Defaults to the existing start_date when omitted."
            ),
        ),
    ] = None,
    end_date: Annotated[
        str | None,
        Field(
            description=(
                "New ISO end date (YYYY-MM-DD) for the chapter. "
                "Defaults to the existing end_date when omitted."
            ),
        ),
    ] = None,
    allow_gap: Annotated[
        bool,
        Field(
            description=(
                "When True, permit a gap to form between this chapter "
                "and its neighbours after the window shift. Default False."
            ),
        ),
    ] = False,
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Update a chapter's title and/or date window.

    If only ``title`` is provided (both date arguments are None), the
    chapter is renamed without queuing regeneration. If either
    ``start_date`` or ``end_date`` is provided, ``update_chapter_window``
    is called — missing date args default to the chapter's current values.
    Regeneration is queued automatically when the window changes.
    """
    log.info(
        "Tool call: journal_update_storyline_chapter"
        "(storyline_id=%d, chapter_id=%d, title=%s, start_date=%s, end_date=%s)",
        storyline_id, chapter_id, title, start_date, end_date,
    )
    repo = _get_storyline_repository(ctx)
    if repo is None:
        return "Storylines feature is not configured on this server."
    user_id = _user_id(ctx)
    storyline = repo.get_storyline(storyline_id, user_id=user_id)
    if storyline is None:
        return f"Storyline {storyline_id} not found."
    chapter = repo.get_chapter(chapter_id)
    if chapter is None or chapter.storyline_id != storyline_id:
        return f"Chapter {chapter_id} not found on storyline {storyline_id}."

    rename_note = ""
    window_changed = start_date is not None or end_date is not None

    if title is not None and not window_changed:
        # Rename-only path
        try:
            repo.rename_chapter(chapter_id, title)
        except ValueError as exc:
            return f"Could not rename chapter: {exc}"
        return (
            f"Renamed chapter [{chapter_id}] to {title!r} "
            f"on storyline {storyline_id}."
        )

    if title is not None:
        # Rename as part of a window update
        try:
            repo.rename_chapter(chapter_id, title)
            rename_note = f" Title updated to {title!r}."
        except ValueError as exc:
            return f"Could not rename chapter: {exc}"

    # Window update path
    effective_start = start_date if start_date is not None else chapter.start_date
    effective_end = end_date if end_date is not None else chapter.end_date
    try:
        affected = repo.update_chapter_window(
            chapter_id, effective_start, effective_end, allow_gap=allow_gap,
        )
    except ValueError as exc:
        return f"Could not update chapter window: {exc}"

    # Determine the final end_date for regen mode selection
    updated = next((c for c in affected if c.id == chapter_id), None)
    final_end = updated.end_date if updated is not None else effective_end
    regen_note = _enqueue_chapter_regen(ctx, storyline_id, chapter_id, final_end)
    return (
        f"Updated chapter [{chapter_id}] on storyline {storyline_id}."
        f"{rename_note} {regen_note}"
    )


@mcp.tool(annotations={"destructiveHint": True})
def journal_delete_storyline_chapter(
    storyline_id: Annotated[
        int,
        Field(
            description=(
                "The integer id of the storyline. Obtain from "
                "journal_list_storylines."
            ),
        ),
    ],
    chapter_id: Annotated[
        int,
        Field(
            description=(
                "The integer id of the chapter to delete. Obtain from "
                "journal_get_storyline."
            ),
        ),
    ],
    allow_gap: Annotated[
        bool,
        Field(
            description=(
                "When True, permit a gap to form in the storyline after "
                "deletion. Default False (the adjacent chapter's window "
                "is extended to fill the gap)."
            ),
        ),
    ] = False,
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Delete a chapter from a storyline.

    The adjacent chapter (if any) may have its date window adjusted to
    fill the gap created by the deletion. Pass ``allow_gap=True`` to
    skip that adjustment. Regeneration is queued for any affected
    neighbour chapters. This action cannot be undone.
    """
    log.info(
        "Tool call: journal_delete_storyline_chapter"
        "(storyline_id=%d, chapter_id=%d, allow_gap=%s)",
        storyline_id, chapter_id, allow_gap,
    )
    repo = _get_storyline_repository(ctx)
    if repo is None:
        return "Storylines feature is not configured on this server."
    user_id = _user_id(ctx)
    storyline = repo.get_storyline(storyline_id, user_id=user_id)
    if storyline is None:
        return f"Storyline {storyline_id} not found."
    chapter = repo.get_chapter(chapter_id)
    if chapter is None or chapter.storyline_id != storyline_id:
        return f"Chapter {chapter_id} not found on storyline {storyline_id}."
    try:
        affected_ids = repo.delete_chapter(chapter_id, allow_gap=allow_gap)
    except ValueError as exc:
        return f"Could not delete chapter: {exc}"
    for affected_id in affected_ids:
        neighbour = repo.get_chapter(affected_id)
        end = neighbour.end_date if neighbour is not None else None
        _enqueue_chapter_regen(ctx, storyline_id, affected_id, end)
    return (
        f"Deleted chapter [{chapter_id}] from storyline {storyline_id}."
    )


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
    narrative) and to the storyline's anchor rows. Any historical
    generation jobs in the jobs table are left in place as an audit
    trail and do not block the delete. Use
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
