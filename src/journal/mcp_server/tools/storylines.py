"""MCP tools for storylines (draft/published chapter model).

* ``journal_list_storylines`` — list user's storylines (unread + chapter counts)
* ``journal_get_storyline`` — fetch one storyline + its chapters' meta
  (state, read_at, derived dates). No panels — the narrative lives on
  each chapter itself.
* ``journal_get_storyline_chapter`` — fetch one chapter's full
  narrative segments + addenda.
* ``journal_create_storyline`` — seed a new storyline (1..15 anchor
  entities + name) and queue its bootstrap job, blocking to terminal.
* ``journal_refresh_storyline`` — re-narrate the draft chapter on demand.
* ``journal_unpublish_storyline_chapter`` — fold the newest published
  chapter back into the draft (escape hatch), blocking to terminal.
* ``journal_rename_storyline_chapter`` — rename a chapter's title.
* ``journal_set_storyline_anchors`` — replace the anchor set on an
  existing storyline.
* ``journal_delete_storyline`` — destructive cascade delete of a
  storyline and its chapters.
* ``journal_storylines_guide`` — concept + workflow primer (read-only,
  always available).

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

log = logging.getLogger(__name__)

#: Storylines are anchored on 1..MAX_ANCHORS entities. Mirrors the
#: constant in ``api/storylines_write.py`` — both layers enforce the
#: same soft cap independently (no shared service layer since the
#: storylines redesign; see docs/superpowers/specs/
#: 2026-07-12-storylines-redesign-design.md).
MAX_ANCHORS = 15


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


def _unread_count(chapters: list[Any]) -> int:
    return sum(1 for c in chapters if c.state == "published" and c.read_at is None)


def _format_chapter_meta(ch: Any) -> str:
    window = f"{ch.first_entry_date or '?'} – {ch.last_entry_date or '?'}"
    return (
        f"    [{ch.id}] seq {ch.seq}: {ch.title or '(untitled)'} "
        f"state={ch.state} read_at={ch.read_at or 'None'} "
        f"entries={ch.entry_count} ({window})"
    )


def _format_job_summary(result: dict[str, Any]) -> str:
    """Render a ``storyline_update`` job's result dict as a summary line."""
    parts: list[str] = []
    if "new_entry_count" in result:
        parts.append(f"new entries: {result['new_entry_count']}")
    if "draft_entry_count" in result:
        parts.append(f"draft entries: {result['draft_entry_count']}")
    if "published_chapter_id" in result:
        parts.append(
            f"published chapter [{result['published_chapter_id']}]: "
            f"{result.get('published_title', '')!r}"
        )
    if "addenda_chapter_ids" in result:
        parts.append(f"addenda added to chapters: {result['addenda_chapter_ids']}")
    if "chapter_count" in result:
        parts.append(f"chapters: {result['chapter_count']}")
    if result.get("warnings"):
        parts.append(f"warnings: {result['warnings']}")
    return "; ".join(parts) if parts else "no changes"


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
    ``journal_get_storyline``, ``journal_refresh_storyline``,
    ``journal_set_storyline_anchors``, or ``journal_delete_storyline``.
    Filter by ``status='active'`` to see only current storylines, or
    ``'archived'`` to see retired ones. Returns each storyline's id,
    name, anchor entities, status, unread chapter count, and total
    chapter count. If no storylines exist yet, the response says so;
    use ``journal_create_storyline`` to create the first one.
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
    unread = repo.unread_counts(user_id)
    chapter_counts = repo.chapter_counts(user_id)
    lines = [f"Found {len(rows)} storyline(s):"]
    for s in rows:
        anchors = _format_anchors(repo, entity_store, s.id)
        lines.append(
            f"  [{s.id}] {s.name} — anchors: {anchors}, "
            f"status={s.status}, "
            f"unread={unread.get(s.id, 0)}/{chapter_counts.get(s.id, 0)} chapters"
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
    """Fetch a single storyline by id, including its chapters' metadata.

    Each storyline has at most one draft chapter (the newest, still
    growing) and zero or more published chapters (immutable, delivered
    episodes). This returns chapter ids, titles, ``state``
    (draft/published), ``read_at``, entry counts, and derived date
    ranges — not the narrative text itself. Use
    ``journal_get_storyline_chapter`` to read one chapter's full prose.
    Use ``journal_list_storylines`` to find the id. This tool is
    read-only and safe to call repeatedly.
    """
    log.info("Tool call: journal_get_storyline(id=%d)", storyline_id)
    repo = _get_storyline_repository(ctx)
    if repo is None:
        return "Storylines feature is not configured on this server."
    user_id = _user_id(ctx)
    storyline = repo.get_storyline(storyline_id, user_id=user_id)
    if storyline is None:
        return f"Storyline {storyline_id} not found."
    entity_store = _get_entity_store(ctx)
    chapters = repo.list_chapters(storyline.id)

    lines = [
        f"Storyline {storyline.id}: {storyline.name}",
        f"  anchors: {_format_anchors(repo, entity_store, storyline.id)}",
        f"  status={storyline.status}, "
        f"unread={_unread_count(chapters)}/{len(chapters)} chapters",
    ]
    if chapters:
        lines.append(f"  chapters ({len(chapters)}):")
        for ch in chapters:
            lines.append(_format_chapter_meta(ch))
    else:
        lines.append(
            "  (no chapters yet — bootstrap may still be running; "
            "call journal_refresh_storyline to nudge it)"
        )
    return "\n".join(lines)


@mcp.tool(annotations={"readOnlyHint": True})
def journal_get_storyline_chapter(
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
                "The integer id of the chapter to fetch. Obtain from "
                "journal_get_storyline."
            ),
        ),
    ],
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Fetch one chapter's full narrative, with citations and addenda.

    Every claim in the narrative is grounded in a specific journal
    entry via Anthropic's Citations API. Addenda are short follow-on
    narrations appended to an already-published chapter when a later
    entry was judged to belong to it — each addendum clears the
    chapter's unread badge again. Draft chapters render the same way
    but ``published_at``/``read_at`` are unset.
    """
    log.info(
        "Tool call: journal_get_storyline_chapter(storyline_id=%d, chapter_id=%d)",
        storyline_id, chapter_id,
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

    lines = [
        f"Chapter [{chapter.id}] on storyline {storyline_id}: "
        f"{chapter.title or '(untitled)'}",
        _format_chapter_meta(chapter),
        f"  model={chapter.model_used or '?'}, "
        f"citations={chapter.citation_count}, "
        f"generated_at={chapter.generated_at or 'never'}",
        "",
    ]
    if not chapter.segments:
        lines.append("(no narrative yet)")
    for seg in chapter.segments:
        if seg.get("kind") == "text":
            lines.append(seg.get("text", ""))
        elif seg.get("kind") == "citation":
            lines.append(
                f"[entry {seg.get('entry_id')}] {seg.get('quote', '')!r}"
            )
    if chapter.addenda:
        lines.append("")
        lines.append(f"=== Addenda ({len(chapter.addenda)}) ===")
        for addendum in chapter.addenda:
            lines.append(f"-- added {addendum.get('added_at', '?')} --")
            for seg in addendum.get("segments", []):
                if seg.get("kind") == "text":
                    lines.append(seg.get("text", ""))
                elif seg.get("kind") == "citation":
                    lines.append(
                        f"[entry {seg.get('entry_id')}] {seg.get('quote', '')!r}"
                    )
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
                "Optional short description; passed to the narrator "
                "model so it can disambiguate what the storyline is about."
            ),
        ),
    ] = "",
    timeout_seconds: Annotated[
        int,
        Field(
            description=(
                "Max wait for the bootstrap job before returning its "
                "in-progress status (default 120). Use a longer value "
                "when the anchor set has a deep history — bootstrap "
                "partitions the storyline's full history into chapters "
                "in one pass and needs to chew through many entries."
            ),
        ),
    ] = 120,
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Create a multi-anchor storyline AND bootstrap its chapters in one call.

    A storyline is anchored on one or more entities. Candidate entries
    are pulled across all anchors and unioned by entry. The
    ``entity_ids`` must come from ``journal_list_entities``; if a
    storyline with the same name and exact anchor set already exists,
    the existing id is returned without queueing any generation work.
    On success this tool kicks off a bootstrap job — an AI judge
    partitions the full history into chapters — and blocks up to
    ``timeout_seconds`` waiting for it to finish.
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
            f"name={existing.name!r}. Use journal_refresh_storyline "
            f"to refresh its draft chapter."
        )
    storyline = repo.create_storyline(
        user_id=user_id, entity_ids=unique_ids, name=name,
        description=description,
    )

    runner = _get_job_runner(ctx)
    anchor_summary = ", ".join(canonical_names)
    try:
        job = runner.submit_storyline_update(
            storyline.id, user_id=user_id, bootstrap=True,
        )
    except RuntimeError as exc:
        return (
            f"Created storyline {storyline.id}: {storyline.name!r} "
            f"(anchors: {anchor_summary}). However, chapters could not "
            f"be generated: {exc}. Use journal_refresh_storyline("
            f"{storyline.id}) once the storylines engine is configured "
            "to retry."
        )

    start = time.monotonic()
    finished = _poll_job_until_terminal(
        _get_job_repository(ctx), job.id, timeout=timeout_seconds,
    )
    elapsed = time.monotonic() - start
    status = finished["status"]

    if status == "succeeded":
        result = finished.get("result") or {}
        return (
            f"Created storyline {storyline.id}: {storyline.name!r} "
            f"(anchors: {anchor_summary}). Bootstrap finished in "
            f"{elapsed:.1f}s ({_format_job_summary(result)}). Use "
            f"journal_get_storyline({storyline.id}) to read the chapters."
        )
    if status == "timeout":
        return (
            f"Created storyline {storyline.id}: {storyline.name!r}. "
            f"Bootstrap job {job.id} is still running after "
            f"{timeout_seconds}s; use "
            f"journal_get_job_status('{job.id}') to check progress, "
            f"then journal_get_storyline({storyline.id})."
        )
    error = finished.get("error_message") or "unknown error"
    return (
        f"Created storyline {storyline.id}: {storyline.name!r}, but "
        f"bootstrap failed: {error}. Use "
        f"journal_refresh_storyline({storyline.id}) to retry."
    )


@mcp.tool(annotations={"idempotentHint": True})
def journal_refresh_storyline(
    storyline_id: Annotated[
        int,
        Field(
            description=(
                "The integer id of the storyline whose draft chapter "
                "should be re-narrated. Obtain from "
                "journal_list_storylines."
            ),
        ),
    ],
    timeout_seconds: Annotated[
        int,
        Field(
            description=(
                "Maximum seconds to block waiting for the refresh job "
                "to finish (default 120). If the timeout fires the "
                "tool returns the job id so the caller can poll with "
                "journal_get_job_status."
            ),
        ),
    ] = 120,
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Re-narrate a storyline's draft chapter from its current members.

    New journal entries are normally picked up automatically as you
    journal (the extension-check pipeline judges continue-vs-break
    per entry). Use this to force a manual re-narration of the draft
    — for example after ``journal_set_storyline_anchors`` changes what
    counts as a member, or if you suspect the draft is stale. This
    does NOT consult the judge and does NOT change chapter membership
    or publish anything; it only re-writes the draft's prose. Blocks
    until the job completes or times out.
    """
    log.info(
        "Tool call: journal_refresh_storyline(id=%d)", storyline_id,
    )
    repo = _get_storyline_repository(ctx)
    if repo is None:
        return "Storylines feature is not configured on this server."
    user_id = _user_id(ctx)
    storyline = repo.get_storyline(storyline_id, user_id=user_id)
    if storyline is None:
        return f"Storyline {storyline_id} not found."
    runner = _get_job_runner(ctx)
    try:
        job = runner.submit_storyline_update(
            storyline_id, user_id=user_id, refresh_only=True,
        )
    except (RuntimeError, ValueError) as exc:
        return f"Cannot refresh: {exc}"
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
        result = finished.get("result") or {}
        return (
            f"Refresh succeeded ({_format_job_summary(result)}). "
            f"Use journal_get_storyline({storyline_id}) to read the result."
        )
    return (
        f"Refresh failed: {finished.get('error_message') or 'unknown error'}"
    )


@mcp.tool(annotations={"destructiveHint": True})
def journal_unpublish_storyline_chapter(
    storyline_id: Annotated[
        int,
        Field(
            description=(
                "The integer id of the storyline. Obtain from "
                "journal_list_storylines."
            ),
        ),
    ],
    timeout_seconds: Annotated[
        int,
        Field(
            description=(
                "Maximum seconds to block waiting for the fold + "
                "re-narration job to finish (default 120)."
            ),
        ),
    ] = 120,
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Fold the newest published chapter back into the draft.

    The escape hatch for a chapter that published too early or needs
    more material before it's final: the newest published chapter's
    entries move back into the draft, the chapter row is removed, and
    the draft is re-narrated to include them. Repeatable back to
    chapter 1 (each call folds one more chapter). Blocks until the job
    completes or times out.
    """
    log.info(
        "Tool call: journal_unpublish_storyline_chapter(id=%d)",
        storyline_id,
    )
    repo = _get_storyline_repository(ctx)
    if repo is None:
        return "Storylines feature is not configured on this server."
    user_id = _user_id(ctx)
    storyline = repo.get_storyline(storyline_id, user_id=user_id)
    if storyline is None:
        return f"Storyline {storyline_id} not found."
    chapters = repo.list_chapters(storyline_id)
    if not any(c.state == "published" for c in chapters):
        return f"Storyline {storyline_id} has no published chapter to unpublish."
    runner = _get_job_runner(ctx)
    try:
        job = runner.submit_storyline_update(
            storyline_id, user_id=user_id, unpublish=True,
        )
    except (RuntimeError, ValueError) as exc:
        return f"Cannot unpublish: {exc}"
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
        result = finished.get("result") or {}
        return (
            f"Unpublish succeeded ({_format_job_summary(result)}). "
            f"Use journal_get_storyline({storyline_id}) to read the result."
        )
    return (
        f"Unpublish failed: {finished.get('error_message') or 'unknown error'}"
    )


@mcp.tool()
def journal_rename_storyline_chapter(
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
                "The integer id of the chapter to rename. Obtain from "
                "journal_get_storyline."
            ),
        ),
    ],
    title: Annotated[
        str,
        Field(description="New title for the chapter (non-empty)."),
    ],
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Rename a chapter's title. Works on both draft and published chapters.

    This is metadata-only — it does not touch the narrative text,
    membership, or read state, and does not queue any job.
    """
    log.info(
        "Tool call: journal_rename_storyline_chapter"
        "(storyline_id=%d, chapter_id=%d, title=%s)",
        storyline_id, chapter_id, title,
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
    trimmed = title.strip()
    if not trimmed:
        return "title must be non-empty."
    repo.rename_chapter(chapter_id, trimmed)
    return (
        f"Renamed chapter [{chapter_id}] to {trimmed!r} "
        f"on storyline {storyline_id}."
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
                f"contain 1..{MAX_ANCHORS} entries. Existing chapters "
                "are unaffected — new anchors only change what future "
                "entries are considered as candidate members."
            ),
        ),
    ],
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Replace the anchor set on an existing storyline.

    Useful when you've created a storyline anchored on one entity and
    want to expand it to include related entities, or when you realise
    an anchor was the wrong disambiguation. The storyline's name,
    description, and existing chapters are preserved. Call
    ``journal_refresh_storyline`` afterwards if you want the draft
    re-narrated immediately against the new set.
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
        f"Call journal_refresh_storyline({storyline_id}) to re-narrate "
        "the draft against the new set."
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

    This cascades to all of the storyline's chapters (draft and
    published) and to its anchor rows. Any historical generation jobs
    in the jobs table are left in place as an audit trail and do not
    block the delete. Use ``journal_list_storylines`` to find the id
    first if you are uncertain. This action cannot be undone.
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


_STORYLINES_GUIDE = """\
# Storylines — Concept and Workflow Guide

## What is a storyline?

A long-running, AI-narrated thread through your journal anchored on
one or more entities — people, activities, places, projects. Every
entry mentioning an anchor is a candidate member. Soft cap: 15
anchors per storyline.

## Draft and published chapters

Each storyline has **at most one draft chapter** (the newest, still
growing) plus zero or more **published** chapters — immutable, finished
episodes delivered to the reader. The narrative prose, grounded with
per-entry citations, IS the chapter — there's nothing else to read.

## How chapters form — semantic boundaries via the judge

An AI judge reads the draft's narrative plus new candidate entries and
decides, per entry: fold into the draft, start a new chapter, or
attach as an addendum to an already-published chapter. When the arc
is judged complete, the draft **publishes** — a light closing revision
and a title, then a fresh empty draft opens behind it. This is a
semantic decision, not a word count or a fixed date range — there is
no manual re-carving of chapter boundaries; rename and unpublish
(below) are the only edits.

## Unread state

Publishing a chapter marks it unread. An addendum to a published
chapter clears its ``read_at`` again, so it resurfaces as updated.
``journal_get_storyline`` reports each chapter's ``state``/``read_at``.

## Escape hatch: unpublish

Chapter published too early, or want more material folded in first?
``journal_unpublish_storyline_chapter`` folds the newest published
chapter back into the draft and re-narrates it. Repeatable to chapter 1.

## Typical workflow

1. ``journal_list_entities`` — find entity_ids to anchor on.
2. ``journal_create_storyline(entity_ids, name)`` — creates the
   storyline and runs a **bootstrap** job partitioning its full history
   into chapters; blocks until that finishes.
3. ``journal_list_storylines()`` — anytime, to see storylines + unread
   counts.
4. ``journal_get_storyline(storyline_id)`` — chapter meta (state,
   read_at, dates, entry counts).
5. ``journal_get_storyline_chapter(storyline_id, chapter_id)`` — one
   chapter's full narrative + addenda.
6. ``journal_refresh_storyline(storyline_id)`` — manually re-narrate the
   draft (entries are usually picked up automatically).
7. ``journal_set_storyline_anchors`` / ``journal_rename_storyline_chapter``
   / ``journal_unpublish_storyline_chapter`` / ``journal_delete_storyline``
   — as needed.

## Configuration requirements

Requires ``ANTHROPIC_API_KEY`` at boot (judge + narrator are both
Claude tool calls). If missing, every storylines tool except this
guide returns "Storylines feature is not configured on this server" —
ask your admin to set the key and restart.
"""


@mcp.tool(annotations={"readOnlyHint": True})
def journal_storylines_guide(
    ctx: Context = None,  # type: ignore[assignment]  # noqa: ARG001
) -> str:
    """Returns a guide explaining the storylines feature.

    Call this first if you are unfamiliar with storylines or unsure
    which storyline tool to use. The guide covers what storylines are,
    the draft/published chapter model, how chapters form, unread
    state, the unpublish escape hatch, the typical workflow, and
    configuration requirements. Read-only and safe to call anytime.
    """
    log.info("Tool call: journal_storylines_guide()")
    return _STORYLINES_GUIDE
