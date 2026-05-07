"""Process entry point — `main()` boots the uvicorn server."""

import logging

from mcp.server.transport_security import TransportSecuritySettings

from journal.config import load_config
from journal.mcp_server.app import mcp
from journal.mcp_server.bootstrap import _init_services

log = logging.getLogger(__name__)


def main() -> None:
    """Run the MCP server with REST API, session/key auth, and optional CORS."""
    import anyio
    import uvicorn
    from starlette.middleware.cors import CORSMiddleware

    from journal.auth import build_auth_middleware_stack

    config = load_config()

    # Fail-closed: refuse to start without a secret key for session
    # tokens and signed URLs. Generate one with:
    #     python -c "import secrets; print(secrets.token_urlsafe(32))"
    if not config.secret_key:
        raise RuntimeError(
            "JOURNAL_SECRET_KEY is not set. The auth system requires "
            "a secret key — generate one with:\n"
            '    python -c "import secrets; print(secrets.token_urlsafe(32))"\n'
            "and add it to your .env file as JOURNAL_SECRET_KEY=..."
        )

    # DNS rebinding protection is always on. `mcp_allowed_hosts` defaults
    # to loopback in config.py, so there is no path that disables it.
    mcp.settings.host = config.mcp_host
    mcp.settings.port = config.mcp_port
    allowed_origins = [f"http://{h}" for h in config.mcp_allowed_hosts]
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=config.mcp_allowed_hosts,
        allowed_origins=allowed_origins,
    )
    log.info(
        "MCP transport security: DNS rebinding protection ON, allowed hosts=%s",
        config.mcp_allowed_hosts,
    )

    # Initialize services eagerly so REST API routes work immediately,
    # without waiting for the first MCP session to connect.
    services = _init_services()

    # Build the Starlette app from FastMCP (includes MCP routes + custom_routes)
    app = mcp.streamable_http_app()

    # Log registered routes for debugging
    for route in app.routes:
        methods = getattr(route, "methods", None)
        log.info("  Route: %s %s", route.path, methods or "(all)")

    # Middleware stack: CORS outermost so that 401/403 responses still
    # carry Access-Control-Allow-Origin headers — otherwise the browser
    # swallows them as a CORS error.
    #
    # Request flow:
    #   client -> CORS -> AuthenticationMiddleware -> RequireAuth -> route
    if config.api_cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=config.api_cors_origins,
            allow_methods=["GET", "PATCH", "DELETE", "POST", "OPTIONS"],
            allow_headers=["Content-Type", "Authorization"],
            allow_credentials=True,
        )

    # Session + API key authentication middleware. Replaces the old
    # single bearer token approach with per-user auth.
    auth_service = services["auth_service"]
    app = build_auth_middleware_stack(app, auth_service)
    log.info("Auth middleware installed (session + API key)")

    async def _serve() -> None:
        uvi_config = uvicorn.Config(
            app,
            host=config.mcp_host,
            port=config.mcp_port,
            log_level="info",
        )
        server = uvicorn.Server(uvi_config)
        await server.serve()

    anyio.run(_serve)
