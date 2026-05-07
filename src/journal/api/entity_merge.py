"""Entity merge, deduplication candidates, quarantine, and merge history.

Seven routes under ``/api/entities/...`` covering the entity-deduplication
side of the entity surface:

- ``POST /api/entities/merge`` — merge N entities into a survivor
- ``GET /api/entities/merge-candidates`` — pairs flagged for potential merge
- ``PATCH /api/entities/merge-candidates/{id}`` — accept / dismiss a candidate
- ``GET /api/entities/{id}/merge-history`` — audit trail for a merged entity
- ``GET /api/entities/quarantined`` — list quarantined entities
- ``POST /api/entities/{id}/quarantine`` — flag an entity as problematic
- ``POST /api/entities/{id}/release-quarantine`` — undo a quarantine

Core entity CRUD + read sub-resources (list, detail, mentions, relationships,
update, delete, aliases) live in ``entities.py``. Aliases are kept with
core entity metadata — the alias-as-side-effect-of-merge implementation
detail does not bleed into the file layout.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from starlette.responses import JSONResponse

from journal.api._shared import _entity_detail, _entity_summary
from journal.auth import get_authenticated_user

if TYPE_CHECKING:
    from collections.abc import Callable

    from mcp.server.fastmcp import FastMCP
    from starlette.requests import Request

    from journal.entitystore.store import EntityStore

log = logging.getLogger(__name__)


def register_entity_merge_routes(
    mcp: FastMCP,
    services_getter: Callable[[], dict | None],
) -> None:
    """Register the entity merge / quarantine / candidate routes."""

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
