"""Conversations REST routes.

CRUD for persisted chat threads about a journal answer. Reply synthesis
re-grounds against the journal and is delegated to ``ConversationService``.
All routes are bearer-authenticated and user-scoped; another user's id is
indistinguishable from a missing one (``404 not_found``).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from starlette.responses import JSONResponse, Response

from journal.api._handler import handler
from journal.auth import get_authenticated_user
from journal.providers.answerer import AnswerUnavailable
from journal.services.conversations import ConversationNotFoundError

if TYPE_CHECKING:
    from collections.abc import Callable

    from mcp.server.fastmcp import FastMCP
    from starlette.requests import Request

    from journal.models import Conversation, ConversationMessage
    from journal.service_registry import ServicesDict

log = logging.getLogger(__name__)


def _message_dict(m: ConversationMessage) -> dict[str, Any]:
    return {
        "id": m.id,
        "role": m.role,
        "content": m.content,
        "citations": m.citations,
        "created_at": m.created_at,
    }


def _conversation_dict(c: Conversation) -> dict[str, Any]:
    return {
        "id": c.id,
        "title": c.title,
        "created_at": c.created_at,
        "updated_at": c.updated_at,
        "messages": [_message_dict(m) for m in c.messages],
    }


def register_conversations_routes(
    mcp: FastMCP,
    services_getter: Callable[[], ServicesDict | None],
) -> None:
    """Register the /api/conversations routes."""

    @mcp.custom_route(
        "/api/conversations", methods=["POST"], name="api_conversations_create"
    )
    @handler(services_getter, parse_json=True)
    def create_conversation(
        request: Request, services: ServicesDict, body: dict
    ) -> JSONResponse:
        svc = services.get("conversation")
        if svc is None:
            return JSONResponse(
                {"error": "service_unavailable", "message": "Conversations not configured."},
                status_code=503,
            )
        user_id = get_authenticated_user(request).user_id
        question = (body.get("question") or "").strip()
        if not question:
            return JSONResponse(
                {"error": "missing_question", "message": "'question' is required"},
                status_code=400,
            )
        answer = body.get("answer") or ""
        citations = body.get("citations") or []
        conv = svc.start(user_id, question, answer, citations)
        log.info("POST /api/conversations — created conversation %d", conv.id)
        return JSONResponse(_conversation_dict(conv), status_code=201)

    @mcp.custom_route(
        "/api/conversations", methods=["GET"], name="api_conversations_list"
    )
    @handler(services_getter)
    def list_conversations(
        request: Request, services: ServicesDict, body: None
    ) -> JSONResponse:
        svc = services.get("conversation")
        if svc is None:
            return JSONResponse(
                {"error": "service_unavailable", "message": "Conversations not configured."},
                status_code=503,
            )
        user_id = get_authenticated_user(request).user_id
        convs = svc.list(user_id)
        return JSONResponse(
            {
                "conversations": [
                    {
                        "id": c.id,
                        "title": c.title,
                        "updated_at": c.updated_at,
                        "message_count": c.message_count,
                    }
                    for c in convs
                ]
            }
        )

    @mcp.custom_route(
        "/api/conversations/{conversation_id:int}",
        methods=["GET"],
        name="api_conversations_detail",
    )
    @handler(services_getter)
    def conversation_detail(
        request: Request, services: ServicesDict, body: None
    ) -> JSONResponse:
        svc = services.get("conversation")
        if svc is None:
            return JSONResponse(
                {"error": "service_unavailable", "message": "Conversations not configured."},
                status_code=503,
            )
        user_id = get_authenticated_user(request).user_id
        cid = int(request.path_params["conversation_id"])
        conv = svc.get(user_id, cid)
        if conv is None:
            return JSONResponse(
                {"error": "not_found", "message": "Conversation not found"},
                status_code=404,
            )
        return JSONResponse(_conversation_dict(conv))

    @mcp.custom_route(
        "/api/conversations/{conversation_id:int}/messages",
        methods=["POST"],
        name="api_conversations_reply",
    )
    @handler(services_getter, parse_json=True)
    def reply_conversation(
        request: Request, services: ServicesDict, body: dict
    ) -> JSONResponse:
        svc = services.get("conversation")
        if svc is None:
            return JSONResponse(
                {"error": "service_unavailable", "message": "Conversations not configured."},
                status_code=503,
            )
        user_id = get_authenticated_user(request).user_id
        cid = int(request.path_params["conversation_id"])
        message = (body.get("message") or "").strip()
        if not message:
            return JSONResponse(
                {"error": "missing_message", "message": "'message' is required"},
                status_code=400,
            )
        try:
            msg = svc.reply(user_id, cid, message)
        except ConversationNotFoundError:
            return JSONResponse(
                {"error": "not_found", "message": "Conversation not found"},
                status_code=404,
            )
        except AnswerUnavailable as e:
            log.info("conversation reply unavailable for %s: %s", cid, e)
            return JSONResponse(
                {
                    "error": "answer_unavailable",
                    "message": "Could not generate a reply right now.",
                },
                status_code=502,
            )
        log.info("POST /api/conversations/%d/messages — replied", cid)
        return JSONResponse(_message_dict(msg), status_code=201)

    @mcp.custom_route(
        "/api/conversations/{conversation_id:int}",
        methods=["DELETE"],
        name="api_conversations_delete",
    )
    @handler(services_getter)
    def delete_conversation(
        request: Request, services: ServicesDict, body: None
    ) -> Response:
        svc = services.get("conversation")
        if svc is None:
            return JSONResponse(
                {"error": "service_unavailable", "message": "Conversations not configured."},
                status_code=503,
            )
        user_id = get_authenticated_user(request).user_id
        cid = int(request.path_params["conversation_id"])
        if not svc.delete(user_id, cid):
            return JSONResponse(
                {"error": "not_found", "message": "Conversation not found"},
                status_code=404,
            )
        log.info("DELETE /api/conversations/%d — removed", cid)
        return Response(status_code=204)
