"""MCP tools for storylines.

* ``journal_list_storylines`` — list user's storylines
* ``journal_get_storyline`` — fetch one storyline + both panels
* ``journal_create_storyline`` — seed a new storyline (entity_id + name)
* ``journal_regenerate_storyline`` — queue a regeneration job and
  block until terminal state (uses ``_poll_job_until_terminal``).

Tools refuse with an actionable message when the storylines feature
isn't wired on this server. Output is formatted text (per the
project's tool-output convention).
"""

import logging

from mcp.server.fastmcp import Context

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


@mcp.tool()
def journal_list_storylines(
    status: str | None = None,
    limit: int = 50,
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """List the caller's storylines.

    Args:
        status: 'active' or 'archived'. Omit for all.
        limit: Max results (default 50).
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


@mcp.tool()
def journal_get_storyline(
    storyline_id: int,
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Fetch one storyline with both rendered panels.

    Args:
        storyline_id: The storyline to fetch.
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
    entity_id: int,
    name: str,
    description: str = "",
    start_date: str | None = None,
    end_date: str | None = None,
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Seed a new storyline anchored on an existing entity.

    Args:
        entity_id: The id of the entity (person / activity / etc.) to
            anchor on. Resolves disambiguation up front — for
            ambiguous names like 'Atlas', pick the right entity_id
            (e.g. the person, not the organization) via
            ``journal_list_entities`` first.
        name: Display name for the storyline (e.g. 'Running',
            'Atlas-the-son').
        description: Optional short description; passed to the
            narrative model so it disambiguates what the storyline
            is about.
        start_date: ISO date; entries before this are excluded.
            Optional — defaults to the last 90 days.
        end_date: ISO date; entries after this are excluded.
            Optional — defaults to today.
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
    return (
        f"Created storyline {storyline.id}: {storyline.name!r} "
        f"(entity {entity.canonical_name}). "
        f"Run journal_regenerate_storyline({storyline.id}) to generate panels."
    )


@mcp.tool()
def journal_regenerate_storyline(
    storyline_id: int,
    timeout_seconds: int = 120,
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Queue a regeneration job and block until it reaches a terminal state.

    Args:
        storyline_id: The storyline to regenerate.
        timeout_seconds: Max wait before returning the in-progress
            job's status (default 120). Use a longer value for cold
            generation of a large corpus.
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
        _get_job_repository(ctx), job.id, timeout_seconds=timeout_seconds,
    )
    if finished is None:
        return (
            f"Job {job.id} did not finish within {timeout_seconds}s. "
            "Use journal_get_job(...) to check status later."
        )
    if finished.status == "succeeded":
        r = finished.result or {}
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
        f"Regeneration failed: {finished.error_message or 'unknown error'}"
    )
