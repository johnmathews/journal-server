"""Entity tools — extract, list, mentions, and relationships."""

import logging

from mcp.server.fastmcp import Context

from journal.mcp_server.app import mcp
from journal.mcp_server.tools._ctx import (
    _get_entity_extraction,
    _get_entity_store,
    _user_id,
)

log = logging.getLogger(__name__)


@mcp.tool()
def journal_extract_entities(
    entry_id: int | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    stale_only: bool = False,
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Run the entity extraction batch job over one or more entries.

    Args:
        entry_id: If provided, run extraction for this single entry only.
        start_date: Filter entries from this date (ISO 8601). Optional.
        end_date: Filter entries until this date (ISO 8601). Optional.
        stale_only: When True, only process entries flagged as stale
            (text updated since the last extraction run).
    """
    log.info(
        "Tool call: journal_extract_entities("
        "entry_id=%s, start_date=%s, end_date=%s, stale_only=%s)",
        entry_id, start_date, end_date, stale_only,
    )
    service = _get_entity_extraction(ctx)
    user_id = _user_id(ctx)
    try:
        if entry_id is not None:
            results = [service.extract_from_entry(entry_id)]
        else:
            results = service.extract_batch(
                start_date=start_date,
                end_date=end_date,
                stale_only=stale_only,
                user_id=user_id,
            )
    except ValueError as e:
        return f"Error: {e}"

    if not results:
        return "No entries matched the filter — nothing to extract."

    total_new = sum(r.entities_created for r in results)
    total_matched = sum(r.entities_matched for r in results)
    total_mentions = sum(r.mentions_created for r in results)
    total_rels = sum(r.relationships_created for r in results)
    warnings = [w for r in results for w in r.warnings]

    lines = [
        f"Extraction complete for {len(results)} entries:",
        f"  Entities created: {total_new}",
        f"  Entities matched: {total_matched}",
        f"  Mentions recorded: {total_mentions}",
        f"  Relationships recorded: {total_rels}",
    ]
    if warnings:
        lines.append(f"  Warnings: {len(warnings)}")
        for w in warnings[:20]:
            lines.append(f"    - {w}")
        if len(warnings) > 20:
            lines.append(f"    ... and {len(warnings) - 20} more")
    return "\n".join(lines)


@mcp.tool()
def journal_list_entities(
    entity_type: str | None = None,
    limit: int = 50,
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """List extracted entities, optionally filtered by type.

    Args:
        entity_type: One of 'person', 'place', 'activity', 'organization',
            'topic', 'other'. Omit to list all types.
        limit: Max results (default 50).
    """
    log.info(
        "Tool call: journal_list_entities(entity_type=%s, limit=%d)",
        entity_type, limit,
    )
    store = _get_entity_store(ctx)
    user_id = _user_id(ctx)
    rows = store.list_entities_with_mention_counts(
        entity_type=entity_type, limit=min(limit, 200), offset=0, user_id=user_id,
    )
    if not rows:
        return "No entities found."
    lines = [f"Showing {len(rows)} entities:"]
    for entity, count, _last_seen in rows:
        aliases = f" (aliases: {', '.join(entity.aliases)})" if entity.aliases else ""
        lines.append(
            f"  [{entity.id}] {entity.entity_type}: {entity.canonical_name}"
            f" — {count} mentions{aliases}"
        )
    return "\n".join(lines)


@mcp.tool()
def journal_get_entity_mentions(
    entity_id: int,
    limit: int = 50,
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Return every mention of a specific entity across the journal.

    Args:
        entity_id: The entity to look up.
        limit: Max mentions to return (default 50).
    """
    log.info(
        "Tool call: journal_get_entity_mentions(entity_id=%d, limit=%d)",
        entity_id, limit,
    )
    store = _get_entity_store(ctx)
    user_id = _user_id(ctx)
    entity = store.get_entity(entity_id, user_id=user_id)
    if entity is None:
        return f"Entity {entity_id} not found."
    mentions = store.get_mentions_for_entity(entity_id, limit=limit, user_id=user_id)
    if not mentions:
        return f"No mentions recorded for {entity.canonical_name}."
    lines = [f"{len(mentions)} mentions of {entity.canonical_name}:"]
    for m in mentions:
        lines.append(
            f"  entry {m.entry_id}: \"{m.quote}\" (confidence {m.confidence:.2f})"
        )
    return "\n".join(lines)


@mcp.tool()
def journal_get_entity_relationships(
    entity_id: int,
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Return the outgoing and incoming relationships for an entity.

    Args:
        entity_id: The entity whose edges to return.
    """
    log.info(
        "Tool call: journal_get_entity_relationships(entity_id=%d)",
        entity_id,
    )
    store = _get_entity_store(ctx)
    user_id = _user_id(ctx)
    entity = store.get_entity(entity_id, user_id=user_id)
    if entity is None:
        return f"Entity {entity_id} not found."
    outgoing, incoming = store.get_relationships_for_entity(entity_id, user_id=user_id)
    if not outgoing and not incoming:
        return f"No relationships recorded for {entity.canonical_name}."
    lines = [f"Relationships for {entity.canonical_name}:"]
    if outgoing:
        lines.append(f"  Outgoing ({len(outgoing)}):")
        for r in outgoing:
            other = store.get_entity(r.object_entity_id, user_id=user_id)
            other_name = other.canonical_name if other else f"#{r.object_entity_id}"
            lines.append(
                f"    -> {r.predicate} -> {other_name} "
                f"(entry {r.entry_id}, conf {r.confidence:.2f})"
            )
    if incoming:
        lines.append(f"  Incoming ({len(incoming)}):")
        for r in incoming:
            other = store.get_entity(r.subject_entity_id, user_id=user_id)
            other_name = other.canonical_name if other else f"#{r.subject_entity_id}"
            lines.append(
                f"    <- {r.predicate} <- {other_name} "
                f"(entry {r.entry_id}, conf {r.confidence:.2f})"
            )
    return "\n".join(lines)
