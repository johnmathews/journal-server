"""Entity routes — read, update, alias CRUD, quarantine, merge.

Sixteen routes under ``/api/entities/...`` covering:

- list / detail / mentions / relationships
- update / delete
- merge (action) and merge-candidates (queue)
- aliases (lookup, add, delete)
- quarantine (list, quarantine, release)
- merge-history

Entity *creation* happens implicitly during ingestion + extraction; there
is no public ``POST /api/entities``. The ``POST /api/entities/extract``
job-creation route lives in ``ingestion.py`` per the responsibility-override
routing rule (see ``_shared.py``).

The cross-resource ``GET /api/entries/{id}/entities`` route lives in
``entries.py`` because its URL prefix root is ``entries``.

This module is at the larger end of the api/ size budget (~660 lines).
A future split into ``entities.py`` (CRUD) + ``entity_merge.py`` (merge,
candidates, quarantine, aliases) is parked until growth pressure forces
it; see the refactor plan's Unit 1a "definition of done" for the rationale.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from starlette.responses import JSONResponse

from journal.api._shared import (
    _entity_detail,
    _entity_summary,
    _mention_dict,
    _relationship_dict,
)
from journal.auth import get_authenticated_user

if TYPE_CHECKING:
    from collections.abc import Callable

    from mcp.server.fastmcp import FastMCP
    from starlette.requests import Request

    from journal.entitystore.store import EntityStore
    from journal.services.jobs import JobRunner
    from journal.services.query import QueryService

log = logging.getLogger(__name__)


def register_entities_routes(
    mcp: FastMCP,
    services_getter: Callable[[], dict | None],
) -> None:
    """Register the ``/api/entities/...`` routes."""

    @mcp.custom_route("/api/entities", methods=["GET"], name="api_list_entities")
    async def list_entities_route(request: Request) -> JSONResponse:
        services = services_getter()
        if services is None:
            return JSONResponse({"error": "Server not initialized"}, status_code=503)
        entity_store: EntityStore = services["entity_store"]
        user = get_authenticated_user(request)
        user_id = user.user_id

        entity_type = request.query_params.get("type")
        search = request.query_params.get("search")
        try:
            limit = min(int(request.query_params.get("limit", "50")), 200)
        except ValueError:
            limit = 50
        try:
            offset = max(int(request.query_params.get("offset", "0")), 0)
        except ValueError:
            offset = 0

        rows = entity_store.list_entities_with_mention_counts(
            entity_type=entity_type,
            limit=limit,
            offset=offset,
            user_id=user_id,
            search=search,
        )
        total = entity_store.count_entities(
            entity_type=entity_type, user_id=user_id, search=search,
        )
        items = [_entity_summary(e, c, ls) for e, c, ls in rows]
        log.info("GET /api/entities — returned %d/%d entities", len(items), total)
        return JSONResponse(
            {
                "items": items,
                "total": total,
                "limit": limit,
                "offset": offset,
            }
        )

    @mcp.custom_route(
        "/api/entities/{entity_id:int}",
        methods=["GET"],
        name="api_entity_detail",
    )
    async def entity_detail(request: Request) -> JSONResponse:
        services = services_getter()
        if services is None:
            return JSONResponse({"error": "Server not initialized"}, status_code=503)
        entity_store: EntityStore = services["entity_store"]
        user = get_authenticated_user(request)
        user_id = user.user_id
        entity_id = int(request.path_params["entity_id"])

        entity = entity_store.get_entity(entity_id, user_id=user_id)
        if entity is None:
            log.warning("GET /api/entities/%d — not found", entity_id)
            return JSONResponse({"error": f"Entity {entity_id} not found"}, status_code=404)
        log.info("GET /api/entities/%d — %s", entity_id, entity.canonical_name)
        return JSONResponse(_entity_detail(entity))

    @mcp.custom_route(
        "/api/entities/{entity_id:int}/mentions",
        methods=["GET"],
        name="api_entity_mentions",
    )
    async def entity_mentions(request: Request) -> JSONResponse:
        services = services_getter()
        if services is None:
            return JSONResponse({"error": "Server not initialized"}, status_code=503)
        entity_store: EntityStore = services["entity_store"]
        query_svc: QueryService = services["query"]
        user = get_authenticated_user(request)
        user_id = user.user_id
        entity_id = int(request.path_params["entity_id"])

        entity = entity_store.get_entity(entity_id, user_id=user_id)
        if entity is None:
            return JSONResponse({"error": f"Entity {entity_id} not found"}, status_code=404)

        try:
            limit = min(int(request.query_params.get("limit", "50")), 200)
        except ValueError:
            limit = 50
        try:
            offset = max(int(request.query_params.get("offset", "0")), 0)
        except ValueError:
            offset = 0

        mentions = entity_store.get_mentions_for_entity(
            entity_id,
            limit=limit,
            offset=offset,
            user_id=user_id,
        )
        mention_payload: list[dict[str, Any]] = []
        for m in mentions:
            entry = query_svc._repo.get_entry(m.entry_id, user_id=user_id)
            entry_date = entry.entry_date if entry else None
            mention_payload.append(_mention_dict(m, entry_date))
        log.info(
            "GET /api/entities/%d/mentions — %d mentions",
            entity_id,
            len(mention_payload),
        )
        return JSONResponse(
            {
                "entity_id": entity_id,
                "mentions": mention_payload,
                "total": len(mention_payload),
            }
        )

    @mcp.custom_route(
        "/api/entities/{entity_id:int}/relationships",
        methods=["GET"],
        name="api_entity_relationships",
    )
    async def entity_relationships(request: Request) -> JSONResponse:
        services = services_getter()
        if services is None:
            return JSONResponse({"error": "Server not initialized"}, status_code=503)
        entity_store: EntityStore = services["entity_store"]
        user = get_authenticated_user(request)
        user_id = user.user_id
        entity_id = int(request.path_params["entity_id"])

        entity = entity_store.get_entity(entity_id, user_id=user_id)
        if entity is None:
            return JSONResponse({"error": f"Entity {entity_id} not found"}, status_code=404)

        outgoing, incoming = entity_store.get_relationships_for_entity(
            entity_id,
            user_id=user_id,
        )
        log.info(
            "GET /api/entities/%d/relationships — %d out, %d in",
            entity_id,
            len(outgoing),
            len(incoming),
        )
        return JSONResponse(
            {
                "entity_id": entity_id,
                "outgoing": [_relationship_dict(r) for r in outgoing],
                "incoming": [_relationship_dict(r) for r in incoming],
            }
        )

    # ---- entity management: update / delete / merge ----------------------

    @mcp.custom_route(
        "/api/entities/{entity_id:int}",
        methods=["PATCH"],
        name="api_update_entity",
    )
    async def update_entity(request: Request) -> JSONResponse:
        services = services_getter()
        if services is None:
            return JSONResponse({"error": "Server not initialized"}, status_code=503)
        entity_store: EntityStore = services["entity_store"]
        user = get_authenticated_user(request)
        user_id = user.user_id
        entity_id = int(request.path_params["entity_id"])

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        canonical_name = body.get("canonical_name")
        entity_type = body.get("entity_type")
        description = body.get("description")

        if canonical_name is not None and (
            not isinstance(canonical_name, str) or not canonical_name.strip()
        ):
            return JSONResponse(
                {"error": "'canonical_name' must be a non-empty string"},
                status_code=400,
            )
        valid_types = {"person", "place", "activity", "organization", "topic", "other"}
        if entity_type is not None and entity_type not in valid_types:
            return JSONResponse(
                {"error": f"'entity_type' must be one of {sorted(valid_types)}"},
                status_code=400,
            )

        # Snapshot the old description before update so we can detect
        # whether the patch actually changed it. A no-op edit (PATCH
        # with the same description) should not enqueue a re-embed job.
        old_description: str | None = None
        if description is not None:
            existing = entity_store.get_entity(entity_id, user_id=user_id)
            if existing is None:
                return JSONResponse(
                    {"error": f"Entity {entity_id} not found"}, status_code=404,
                )
            old_description = existing.description

        try:
            updated = entity_store.update_entity(
                entity_id,
                canonical_name=canonical_name,
                entity_type=entity_type,
                description=description,
                user_id=user_id,
            )
        except ValueError:
            return JSONResponse({"error": f"Entity {entity_id} not found"}, status_code=404)

        # If description actually changed, queue an async job to refresh
        # the entity's stored embedding so future recognition reflects
        # the new description (stage-c similarity match in extraction).
        # Best-effort: if no job runner is wired up (e.g. some test
        # setups), the PATCH still succeeds.
        reembed_job_id: str | None = None
        description_changed = (
            description is not None and description != old_description
        )
        if description_changed:
            job_runner: JobRunner | None = services.get("job_runner")
            if job_runner is not None:
                try:
                    reembed_job = job_runner.submit_entity_reembed(
                        entity_id, user_id=user_id,
                    )
                    reembed_job_id = reembed_job.id
                except Exception:
                    log.warning(
                        "PATCH /api/entities/%d — failed to queue reembed job",
                        entity_id, exc_info=True,
                    )

        log.info("PATCH /api/entities/%d — updated", entity_id)
        body = _entity_detail(updated)
        if reembed_job_id is not None:
            body["reembed_job_id"] = reembed_job_id
        return JSONResponse(body)

    @mcp.custom_route(
        "/api/entities/{entity_id:int}",
        methods=["DELETE"],
        name="api_delete_entity",
    )
    async def delete_entity_route(request: Request) -> JSONResponse:
        services = services_getter()
        if services is None:
            return JSONResponse({"error": "Server not initialized"}, status_code=503)
        entity_store: EntityStore = services["entity_store"]
        user = get_authenticated_user(request)
        user_id = user.user_id
        entity_id = int(request.path_params["entity_id"])

        try:
            entity_store.delete_entity(entity_id, user_id=user_id)
        except ValueError:
            return JSONResponse({"error": f"Entity {entity_id} not found"}, status_code=404)

        log.info("DELETE /api/entities/%d — deleted", entity_id)
        return JSONResponse({"deleted": True, "id": entity_id})

    @mcp.custom_route(
        "/api/entities/merge",
        methods=["POST"],
        name="api_merge_entities",
    )
    async def merge_entities_route(request: Request) -> JSONResponse:
        services = services_getter()
        if services is None:
            return JSONResponse({"error": "Server not initialized"}, status_code=503)
        entity_store: EntityStore = services["entity_store"]
        user = get_authenticated_user(request)
        user_id = user.user_id

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        survivor_id = body.get("survivor_id")
        absorbed_ids = body.get("absorbed_ids")

        if not isinstance(survivor_id, int):
            return JSONResponse({"error": "'survivor_id' must be an integer"}, status_code=400)
        if (
            not isinstance(absorbed_ids, list)
            or not absorbed_ids
            or not all(isinstance(i, int) for i in absorbed_ids)
        ):
            return JSONResponse(
                {"error": "'absorbed_ids' must be a non-empty list of integers"},
                status_code=400,
            )

        # Verify ownership of all entities before merging
        if entity_store.get_entity(survivor_id, user_id=user_id) is None:
            return JSONResponse({"error": f"Entity {survivor_id} not found"}, status_code=404)
        for aid in absorbed_ids:
            if entity_store.get_entity(aid, user_id=user_id) is None:
                return JSONResponse({"error": f"Entity {aid} not found"}, status_code=404)

        try:
            result = entity_store.merge_entities(survivor_id, absorbed_ids)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

        survivor = entity_store.get_entity(survivor_id, user_id=user_id)
        log.info(
            "POST /api/entities/merge — merged %s into %d",
            absorbed_ids,
            survivor_id,
        )
        return JSONResponse(
            {
                "survivor": _entity_detail(survivor) if survivor else None,
                "absorbed_ids": result.absorbed_ids,
                "mentions_reassigned": result.mentions_reassigned,
                "relationships_reassigned": result.relationships_reassigned,
                "aliases_added": result.aliases_added,
            }
        )

    # ---- entity aliases --------------------------------------------------

    @mcp.custom_route(
        "/api/entities/aliases/lookup",
        methods=["GET"],
        name="api_lookup_alias",
    )
    async def lookup_alias(request: Request) -> JSONResponse:
        services = services_getter()
        if services is None:
            return JSONResponse({"error": "Server not initialized"}, status_code=503)
        entity_store: EntityStore = services["entity_store"]
        user = get_authenticated_user(request)
        user_id = user.user_id

        alias = request.query_params.get("alias", "").strip()
        if not alias:
            return JSONResponse(
                {"error": "'alias' query parameter is required"}, status_code=400
            )

        existing = entity_store.find_entity_by_alias_for_user(alias, user_id=user_id)
        if existing is None:
            return JSONResponse({"entity_id": None})
        return JSONResponse(
            {
                "entity_id": existing.id,
                "canonical_name": existing.canonical_name,
                "entity_type": existing.entity_type,
            }
        )

    @mcp.custom_route(
        "/api/entities/{entity_id:int}/aliases",
        methods=["POST"],
        name="api_add_entity_alias",
    )
    async def add_entity_alias(request: Request) -> JSONResponse:
        services = services_getter()
        if services is None:
            return JSONResponse({"error": "Server not initialized"}, status_code=503)
        entity_store: EntityStore = services["entity_store"]
        user = get_authenticated_user(request)
        user_id = user.user_id
        entity_id = int(request.path_params["entity_id"])

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        alias_raw = body.get("alias") if isinstance(body, dict) else None
        if not isinstance(alias_raw, str) or not alias_raw.strip():
            return JSONResponse(
                {"error": "'alias' must be a non-empty string"}, status_code=400
            )

        entity = entity_store.get_entity(entity_id, user_id=user_id)
        if entity is None:
            return JSONResponse({"error": f"Entity {entity_id} not found"}, status_code=404)

        existing = entity_store.find_entity_by_alias_for_user(alias_raw, user_id=user_id)
        if existing is not None and existing.id != entity_id:
            return JSONResponse(
                {
                    "error": "alias already maps to a different entity",
                    "alias": alias_raw.strip(),
                    "existing_entity_id": existing.id,
                    "existing_canonical_name": existing.canonical_name,
                    "existing_entity_type": existing.entity_type,
                },
                status_code=409,
            )

        entity_store.add_alias(entity_id, alias_raw)
        updated = entity_store.get_entity(entity_id, user_id=user_id)
        assert updated is not None
        log.info("POST /api/entities/%d/aliases — added %r", entity_id, alias_raw)
        return JSONResponse(_entity_detail(updated), status_code=201)

    @mcp.custom_route(
        "/api/entities/{entity_id:int}/aliases/{alias:path}",
        methods=["DELETE"],
        name="api_delete_entity_alias",
    )
    async def delete_entity_alias(request: Request) -> JSONResponse:
        services = services_getter()
        if services is None:
            return JSONResponse({"error": "Server not initialized"}, status_code=503)
        entity_store: EntityStore = services["entity_store"]
        user = get_authenticated_user(request)
        user_id = user.user_id
        entity_id = int(request.path_params["entity_id"])
        alias = request.path_params["alias"]

        entity = entity_store.get_entity(entity_id, user_id=user_id)
        if entity is None:
            return JSONResponse({"error": f"Entity {entity_id} not found"}, status_code=404)

        removed = entity_store.remove_alias(entity_id, alias)
        if not removed:
            return JSONResponse(
                {"error": f"Alias {alias!r} not found on entity {entity_id}"},
                status_code=404,
            )

        updated = entity_store.get_entity(entity_id, user_id=user_id)
        assert updated is not None
        log.info("DELETE /api/entities/%d/aliases/%s — removed", entity_id, alias)
        return JSONResponse(_entity_detail(updated))

    # ---- quarantine ------------------------------------------------------

    @mcp.custom_route(
        "/api/entities/quarantined",
        methods=["GET"],
        name="api_list_quarantined_entities",
    )
    async def list_quarantined_entities_route(request: Request) -> JSONResponse:
        services = services_getter()
        if services is None:
            return JSONResponse({"error": "Server not initialized"}, status_code=503)
        entity_store: EntityStore = services["entity_store"]
        user = get_authenticated_user(request)
        user_id = user.user_id

        entities = entity_store.list_quarantined_entities(user_id=user_id)
        items = [_entity_detail(e) for e in entities]
        log.info(
            "GET /api/entities/quarantined — %d entities", len(items),
        )
        return JSONResponse({"items": items, "total": len(items)})

    @mcp.custom_route(
        "/api/entities/{entity_id:int}/quarantine",
        methods=["POST"],
        name="api_quarantine_entity",
    )
    async def quarantine_entity_route(request: Request) -> JSONResponse:
        services = services_getter()
        if services is None:
            return JSONResponse({"error": "Server not initialized"}, status_code=503)
        entity_store: EntityStore = services["entity_store"]
        user = get_authenticated_user(request)
        user_id = user.user_id
        entity_id = int(request.path_params["entity_id"])

        try:
            body = await request.json()
        except Exception:
            body = {}

        reason = body.get("reason", "") if isinstance(body, dict) else ""
        if reason is not None and not isinstance(reason, str):
            return JSONResponse(
                {"error": "'reason' must be a string"}, status_code=400,
            )
        reason_str = (reason or "").strip()

        # Ownership / existence check before mutating.
        existing = entity_store.get_entity(entity_id, user_id=user_id)
        if existing is None:
            return JSONResponse(
                {"error": f"Entity {entity_id} not found"}, status_code=404,
            )

        try:
            entity_store.quarantine_entity(entity_id, reason_str)
        except ValueError:
            return JSONResponse(
                {"error": f"Entity {entity_id} not found"}, status_code=404,
            )

        updated = entity_store.get_entity(entity_id, user_id=user_id)
        log.info(
            "POST /api/entities/%d/quarantine — reason=%r",
            entity_id, reason_str,
        )
        return JSONResponse(
            _entity_detail(updated) if updated else {"id": entity_id}
        )

    @mcp.custom_route(
        "/api/entities/{entity_id:int}/release-quarantine",
        methods=["POST"],
        name="api_release_quarantine_entity",
    )
    async def release_quarantine_route(request: Request) -> JSONResponse:
        services = services_getter()
        if services is None:
            return JSONResponse({"error": "Server not initialized"}, status_code=503)
        entity_store: EntityStore = services["entity_store"]
        user = get_authenticated_user(request)
        user_id = user.user_id
        entity_id = int(request.path_params["entity_id"])

        existing = entity_store.get_entity(entity_id, user_id=user_id)
        if existing is None:
            return JSONResponse(
                {"error": f"Entity {entity_id} not found"}, status_code=404,
            )

        try:
            entity_store.release_quarantine(entity_id)
        except ValueError:
            return JSONResponse(
                {"error": f"Entity {entity_id} not found"}, status_code=404,
            )

        updated = entity_store.get_entity(entity_id, user_id=user_id)
        log.info(
            "POST /api/entities/%d/release-quarantine", entity_id,
        )
        return JSONResponse(
            _entity_detail(updated) if updated else {"id": entity_id}
        )

    # ---- merge candidates -----------------------------------------------

    @mcp.custom_route(
        "/api/entities/merge-candidates",
        methods=["GET"],
        name="api_merge_candidates",
    )
    async def list_merge_candidates_route(
        request: Request,
    ) -> JSONResponse:
        services = services_getter()
        if services is None:
            return JSONResponse({"error": "Server not initialized"}, status_code=503)
        entity_store: EntityStore = services["entity_store"]
        user = get_authenticated_user(request)
        user_id = user.user_id

        status = request.query_params.get("status", "pending")
        try:
            limit = min(int(request.query_params.get("limit", "50")), 200)
        except ValueError:
            limit = 50

        candidates = entity_store.list_merge_candidates(
            status=status,
            limit=limit,
            user_id=user_id,
        )
        items = [
            {
                "id": c.id,
                "entity_a": _entity_summary(c.entity_a),
                "entity_b": _entity_summary(c.entity_b),
                "similarity": c.similarity,
                "status": c.status,
                "extraction_run_id": c.extraction_run_id,
                "created_at": c.created_at,
            }
            for c in candidates
        ]
        log.info("GET /api/entities/merge-candidates — %d candidates", len(items))
        return JSONResponse({"items": items, "total": len(items)})

    @mcp.custom_route(
        "/api/entities/merge-candidates/{candidate_id:int}",
        methods=["PATCH"],
        name="api_resolve_merge_candidate",
    )
    async def resolve_merge_candidate_route(
        request: Request,
    ) -> JSONResponse:
        services = services_getter()
        if services is None:
            return JSONResponse({"error": "Server not initialized"}, status_code=503)
        entity_store: EntityStore = services["entity_store"]
        user = get_authenticated_user(request)
        user_id = user.user_id
        candidate_id = int(request.path_params["candidate_id"])

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        status = body.get("status")
        if status not in ("accepted", "dismissed"):
            return JSONResponse(
                {"error": "'status' must be 'accepted' or 'dismissed'"},
                status_code=400,
            )

        # Verify the user owns both entities in the candidate
        candidates = entity_store.list_merge_candidates(
            status="pending",
            limit=1000,
            user_id=user_id,
        )
        if not any(c.id == candidate_id for c in candidates):
            return JSONResponse({"error": "Merge candidate not found"}, status_code=404)

        try:
            entity_store.resolve_merge_candidate(candidate_id, status)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

        log.info(
            "PATCH /api/entities/merge-candidates/%d — %s",
            candidate_id,
            status,
        )
        return JSONResponse({"id": candidate_id, "status": status})

    @mcp.custom_route(
        "/api/entities/{entity_id:int}/merge-history",
        methods=["GET"],
        name="api_entity_merge_history",
    )
    async def entity_merge_history(request: Request) -> JSONResponse:
        services = services_getter()
        if services is None:
            return JSONResponse({"error": "Server not initialized"}, status_code=503)
        entity_store: EntityStore = services["entity_store"]
        user = get_authenticated_user(request)
        user_id = user.user_id
        entity_id = int(request.path_params["entity_id"])

        entity = entity_store.get_entity(entity_id, user_id=user_id)
        if entity is None:
            return JSONResponse({"error": f"Entity {entity_id} not found"}, status_code=404)

        history = entity_store.get_merge_history(entity_id)
        log.info(
            "GET /api/entities/%d/merge-history — %d entries",
            entity_id,
            len(history),
        )
        return JSONResponse({"entity_id": entity_id, "history": history})
