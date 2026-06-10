"""Uniform async→sync handler decorator for the REST API layer.

Every route in ``journal/api/`` is registered with Starlette as an ``async``
endpoint, but the actual work — SQLite reads/writes, and worse, full
ingestion/search pipelines with synchronous OpenAI/Anthropic/Gemini calls —
is synchronous. Before this decorator existed each handler ran that sync
work directly on the event loop, so one slow embedding or rerank call
stalled every concurrent request.

:func:`handler` centralises the three things every route body repeated and
moves the body off the event loop:

1. **Services 503** — ``services_getter()`` returning ``None`` produces the
   canonical ``{"error": "Server not initialized"}`` 503 (override the shape
   via ``on_uninitialized`` for the two health routes, which historically
   return ``{"status": "error", "message": ...}``).
2. **Body parsing** — optional JSON parsing with the exact 400 shapes the
   handlers already used (see :class:`JsonBody`), ``"raw"`` mode handing the
   body bytes to the sync body for handlers whose parse/validation ordering
   must be preserved exactly, or multipart parsing with the canonical
   ``Failed to parse multipart request`` 400.
3. **Thread boundary** — the decorated SYNC body
   ``fn(request, services, body) -> Response`` runs via
   ``asyncio.to_thread``, so the event loop stays free while the body does
   blocking work. ``asyncio.to_thread`` copies the current
   ``contextvars.Context``, so ``journal.auth._current_user_id`` (and any
   other context vars set by middleware) remain visible inside the body.

Thread-safety contract: repositories acquire their ``sqlite3.Connection``
per call through ``journal.db.factory.ConnectionFactory`` (one connection
per OS thread via ``threading.local``), so running bodies on worker threads
is safe by construction. ``get_authenticated_user`` reads
``request.scope["user"]`` synchronously and stays inside the bodies.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.requests import Request
    from starlette.responses import Response

    from journal.service_registry import ServicesDict

    SyncHandler = Callable[[Request, ServicesDict, Any], Response]
    AsyncEndpoint = Callable[[Request], Awaitable[Response]]

log = logging.getLogger(__name__)

#: Canonical 503 body used by every route when services are not wired yet.
_UNINITIALIZED_BODY: dict[str, str] = {"error": "Server not initialized"}


@dataclass(frozen=True)
class JsonBody:
    """Policy for decorator-side JSON body parsing.

    ``invalid_error`` is the 400 ``{"error": ...}`` message returned when the
    body is not valid JSON; ``None`` means "tolerate invalid JSON and pass
    ``{}`` to the body" (the lenient pattern used by the job-submission
    routes). ``require_dict`` controls whether a parsed non-object body is
    rejected with the canonical
    ``{"error": "Request body must be a JSON object"}`` 400; handlers that
    historically did their own ``isinstance(body, dict)`` handling (or none)
    set this to ``False`` so the parsed value passes through unchanged.

    The defaults reproduce the canonical strict shape from the text-ingest
    route: invalid JSON → ``{"error": "Invalid JSON body"}``, non-dict →
    ``{"error": "Request body must be a JSON object"}``.
    """

    invalid_error: str | None = "Invalid JSON body"
    require_dict: bool = True


def handler(
    services_getter: Callable[[], ServicesDict | None],
    *,
    parse_json: bool | JsonBody | Literal["raw"] = False,
    parse_multipart: bool = False,
    thread: bool = True,
    on_uninitialized: Callable[[], Response] | None = None,
) -> Callable[[SyncHandler], AsyncEndpoint]:
    """Wrap a sync handler body in the uniform async endpoint shell.

    Args:
        services_getter: Returns the shared services dict, or ``None``
            before initialization (→ 503).
        parse_json: ``False`` (default) — ``body`` is ``None``;
            ``True`` — strict canonical parse (:class:`JsonBody` defaults);
            a :class:`JsonBody` instance — customised parse policy;
            ``"raw"`` — ``body`` is the raw ``bytes`` of the request body
            and the sync body does its own parsing (used where parse
            ordering relative to other checks must be preserved exactly).
        parse_multipart: parse ``multipart/form-data``; ``body`` becomes the
            ``(fields, files)`` tuple from
            :func:`journal.api_utils.parse_multipart_request`, with the
            canonical ``Failed to parse multipart request`` 400 on error.
        thread: run the body via ``asyncio.to_thread`` (default). ``False``
            runs it inline on the loop — only for bodies that are known
            trivial and need loop affinity (none currently).
        on_uninitialized: optional factory overriding the canonical 503
            response shape (used by the health routes).
    """

    def decorate(fn: SyncHandler) -> AsyncEndpoint:
        async def endpoint(request: Request) -> Response:
            services = services_getter()
            if services is None:
                if on_uninitialized is not None:
                    return on_uninitialized()
                return JSONResponse(_UNINITIALIZED_BODY, status_code=503)

            body: Any = None
            if parse_multipart:
                from journal.api_utils import parse_multipart_request

                try:
                    body = await parse_multipart_request(request)
                except Exception as e:
                    log.warning(
                        "%s %s — parse error: %s",
                        request.method, request.url.path, e,
                    )
                    return JSONResponse(
                        {"error": f"Failed to parse multipart request: {e}"},
                        status_code=400,
                    )
            elif parse_json == "raw":
                body = await request.body()
            elif parse_json:
                policy = JsonBody() if parse_json is True else parse_json
                raw = await request.body()
                try:
                    body = json.loads(raw)
                except ValueError:
                    if policy.invalid_error is None:
                        body = {}
                    else:
                        return JSONResponse(
                            {"error": policy.invalid_error}, status_code=400,
                        )
                if policy.require_dict and not isinstance(body, dict):
                    return JSONResponse(
                        {"error": "Request body must be a JSON object"},
                        status_code=400,
                    )

            if thread:
                return await asyncio.to_thread(fn, request, services, body)
            return fn(request, services, body)

        endpoint.__name__ = fn.__name__
        endpoint.__qualname__ = fn.__qualname__
        endpoint.__doc__ = fn.__doc__
        return endpoint

    return decorate
