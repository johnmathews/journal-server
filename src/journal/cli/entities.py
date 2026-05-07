"""Entity-management CLI commands.

Three commands lifted out of ``cli/__init__.py``:

- ``journal extract-entities``: run the on-demand extraction batch.
- ``journal backfill-entity-embeddings``: refresh stored embeddings.
- ``journal repair-entity-names``: find LLM-clipped canonical names
  and optionally apply repairs.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from journal.cli._services import build_services
from journal.db.connection import get_connection
from journal.db.migrations import run_migrations
from journal.entitystore.store import SQLiteEntityStore
from journal.providers.embeddings import OpenAIEmbeddingsProvider

if TYPE_CHECKING:
    import argparse

    from journal.config import Config


def cmd_extract_entities(args: argparse.Namespace, config: Config) -> None:
    """Run the on-demand entity extraction batch job.

    Accepts a single ``--entry-id`` to extract one entry, or filter
    by ``--start-date``/``--end-date``/``--stale-only`` to pick a
    batch.
    """
    _, _, extraction = build_services(config)

    if args.entry_id is not None:
        try:
            results = [extraction.extract_from_entry(args.entry_id)]
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        results = extraction.extract_batch(
            start_date=args.start_date,
            end_date=args.end_date,
            stale_only=args.stale_only,
        )

    if not results:
        print("No entries matched the filter — nothing to extract.")
        return

    total_new = sum(r.entities_created for r in results)
    total_matched = sum(r.entities_matched for r in results)
    total_mentions = sum(r.mentions_created for r in results)
    total_rels = sum(r.relationships_created for r in results)
    total_warnings = sum(len(r.warnings) for r in results)

    print(f"Extracted entities for {len(results)} entries:")
    print(f"  Entities created:       {total_new}")
    print(f"  Entities matched:       {total_matched}")
    print(f"  Mentions recorded:      {total_mentions}")
    print(f"  Relationships recorded: {total_rels}")
    print(f"  Warnings:               {total_warnings}")
    if total_warnings:
        print()
        for r in results:
            for w in r.warnings:
                print(f"  [entry {r.entry_id}] {w}")


def cmd_backfill_entity_embeddings(
    args: argparse.Namespace, config: Config,
) -> None:
    """Re-embed every entity whose description is non-empty.

    The entity's stored embedding (from migration 0004's
    ``embedding_json`` column) feeds stage-c similarity matching
    during entity extraction. Without this command, the embedding is
    computed once at entity creation from name + description and
    never refreshed — so descriptions edited via the webapp after
    creation don't influence future recognition.

    Filters:
    - ``--user-id N`` — restrict to one user.
    - ``--dry-run`` — count candidates without making OpenAI calls.

    Idempotent. Safe to re-run. Cost is small: at
    text-embedding-3-large pricing ($0.13/M tokens, ~50 tokens per
    entity), 500 entities is roughly $0.003.
    """
    conn = get_connection(config.db_path)
    run_migrations(conn)
    entity_store = SQLiteEntityStore(conn)
    embeddings = OpenAIEmbeddingsProvider(
        api_key=config.openai_api_key,
        model=config.embedding_model,
        dimensions=config.embedding_dimensions,
    )

    sql = (
        "SELECT id, user_id, canonical_name, description"
        " FROM entities"
        " WHERE description IS NOT NULL"
        " AND TRIM(description) != ''"
    )
    params: list[object] = []
    if args.user_id is not None:
        sql += " AND user_id = ?"
        params.append(args.user_id)
    sql += " ORDER BY id"
    rows = list(conn.execute(sql, params).fetchall())

    scope = (
        f"user {args.user_id}" if args.user_id is not None else "all users"
    )
    print(f"Backfill scope: {scope}")
    print(f"Candidates with non-empty description: {len(rows)}")

    if args.dry_run:
        print("Dry run: no embeddings will be generated.")
        return

    if not rows:
        return

    succeeded = 0
    failed = 0
    for row in rows:
        entity_id = int(row["id"])
        text = f"{row['canonical_name']} {row['description']}".strip()
        try:
            vec = embeddings.embed_query(text)
            entity_store.set_entity_embedding(entity_id, vec)
            succeeded += 1
        except Exception as exc:  # noqa: BLE001 — keep going on per-row error
            failed += 1
            print(f"  ! entity {entity_id}: {exc}", file=sys.stderr)

    print(f"Re-embedded: {succeeded}")
    print(f"Failed:      {failed}")


def cmd_repair_entity_names(
    args: argparse.Namespace, config: Config,
) -> None:
    """Find and optionally repair entities whose ``canonical_name``
    looks like an LLM-clipped form of a longer token in their mention
    quotes (e.g. ``"Nautilin"`` for a quote ``"Nautiline, ..."``).

    Default is dry-run — pass ``--apply`` to actually update rows.
    Skips proposed repairs that would collide with an existing entity
    of the same canonical_name.
    """
    from journal.providers.extraction import _repair_canonical_name

    conn = get_connection(config.db_path)
    run_migrations(conn)
    entity_store = SQLiteEntityStore(conn)

    # Pull every entity, paginating in case the corpus is large.
    all_entities: list = []
    offset = 0
    page_size = 500
    while True:
        page = entity_store.list_entities(limit=page_size, offset=offset)
        all_entities.extend(page)
        if len(page) < page_size:
            break
        offset += page_size

    # Pre-build a lookup for collision detection. Collisions are
    # checked by (user_id, canonical_name) since canonical names are
    # scoped per user.
    by_name: dict[tuple[int, str], int] = {
        (e.user_id, e.canonical_name): e.id for e in all_entities
    }

    repairs: list[tuple[object, str]] = []  # (entity, proposed_name)
    skipped_collisions: list[tuple[object, str]] = []

    for entity in all_entities:
        # First quote that produces a repair wins. Iterate mentions
        # in order so the output is deterministic.
        mentions = entity_store.get_mentions_for_entity(entity.id)
        proposed: str | None = None
        for mention in mentions:
            repaired, was_repaired = _repair_canonical_name(
                entity.canonical_name, mention.quote,
            )
            if was_repaired:
                proposed = repaired
                break
        if proposed is None:
            continue

        if (entity.user_id, proposed) in by_name and by_name[
            (entity.user_id, proposed)
        ] != entity.id:
            skipped_collisions.append((entity, proposed))
            continue
        repairs.append((entity, proposed))

    if not repairs and not skipped_collisions:
        print("No entities need repair.")
        return

    print(f"Proposed repairs ({len(repairs)}):")
    for entity, proposed in repairs:
        print(
            f"  [{entity.id}] {entity.canonical_name!r} -> "
            f"{proposed!r}  (type={entity.entity_type}, "
            f"user_id={entity.user_id})"
        )
    if skipped_collisions:
        print()
        print(
            f"Skipped due to collision with existing entity "
            f"({len(skipped_collisions)}):"
        )
        for entity, proposed in skipped_collisions:
            existing_id = by_name[(entity.user_id, proposed)]
            print(
                f"  [{entity.id}] {entity.canonical_name!r} -> "
                f"{proposed!r} would collide with entity #"
                f"{existing_id}"
            )

    if not args.apply:
        print()
        print("Dry-run only. Pass --apply to make these changes.")
        return

    print()
    print(f"Applying {len(repairs)} repair(s)...")
    applied = 0
    for entity, proposed in repairs:
        try:
            entity_store.update_entity(
                entity.id,
                canonical_name=proposed,
                user_id=entity.user_id,
            )
            applied += 1
        except Exception as exc:  # noqa: BLE001
            print(
                f"  Failed to update entity {entity.id}: {exc}",
                file=sys.stderr,
            )
    print(f"Applied {applied}/{len(repairs)} repair(s).")
