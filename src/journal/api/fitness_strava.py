"""Strava per-user auth routes (W3 of the fitness multi-user plan).

Owns the three Strava OAuth-flow endpoints under ``/api/fitness/strava/``:

- ``GET /api/fitness/strava/authorize_url`` — mint a CSRF state token and
  build the Strava authorize URL.
- ``POST /api/fitness/strava/exchange`` — exchange the OAuth code for
  tokens and persist the user's Strava auth row.
- ``POST /api/fitness/strava/disconnect`` — drop the user's Strava tokens.

These routes are direct upstream writes against Strava's OAuth API —
auth flow, not job creation — so they place by URL-resource root and
were carved out of ``api/fitness.py`` when that file outgrew the
~800-line size rule. Reads live in ``api/fitness.py``; job creation
(sync/backfill) lives in ``api/fitness_jobs.py``; the Garmin
counterpart lives in ``api/fitness_garmin.py``.

Auth is enforced by ``RequireAuthMiddleware``: every route below assumes
``request.user`` is an :class:`AuthenticatedUser`. The per-route
``get_authenticated_user`` call extracts the user_id for query scoping.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from starlette.responses import JSONResponse

from journal.api._handler import JsonBody, handler
from journal.api._shared import STRAVA_DISABLED_ERROR, _now_iso, _strava_enabled
from journal.auth import get_authenticated_user
from journal.models import FitnessAuthState
from journal.providers.strava import exchange_code as strava_exchange_code_default
from journal.services.fitness.strava_pending import StravaPendingStore

if TYPE_CHECKING:
    from collections.abc import Callable

    from mcp.server.fastmcp import FastMCP
    from starlette.requests import Request

    from journal.db.fitness_repository import FitnessRepository
    from journal.service_registry import ServicesDict

log = logging.getLogger(__name__)


def register_fitness_strava_routes(
    mcp: FastMCP,
    services_getter: Callable[[], ServicesDict | None],
) -> None:
    """Register the Strava authorize_url / exchange / disconnect routes."""

    def _disabled_response(services: ServicesDict) -> JSONResponse | None:
        """W1 strava-mothball: 404 every Strava OAuth route unless
        ``STRAVA_ENABLED`` is true.

        The guard lives at request time (not registration time) because
        config reaches the API layer through the services dict, which is
        only populated after ``bootstrap._init_services()`` — route
        registration in ``mcp_server/app.py`` happens at import, before
        any config exists. Observable behavior matches an unregistered
        route: 404, with an explicit reason in the body.
        """
        if _strava_enabled(services):
            return None
        return JSONResponse({"error": STRAVA_DISABLED_ERROR}, status_code=404)

    def _strava_pending(services: ServicesDict) -> StravaPendingStore:
        store = services.get("strava_pending")
        if store is None:
            store = StravaPendingStore()
            services["strava_pending"] = store
        return store

    def _strava_exchange(services: ServicesDict) -> Any:
        return services.get("strava_exchange_code") or strava_exchange_code_default

    def _persist_strava_auth(
        repo: FitnessRepository,
        *,
        user_id: int,
        access_token: str,
        refresh_token: str,
        token_expires_at: str,
        upstream_user_id: str,
    ) -> None:
        """Upsert the user's Strava auth row after a successful exchange.

        Forces ``auth_status="ok"``, clears ``auth_broken_since``, stamps
        ``last_successful_login_at``, and preserves any unrelated
        ``extra_state`` keys (mirrors ``fitness_garmin._persist_garmin_auth``
        and the CLI re-auth semantics in ``cli/fitness.cmd_fitness_reauth_strava``).
        """
        existing = repo.get_auth_state(user_id=user_id, source="strava")
        extra = dict(existing.extra_state) if existing else {}
        extra["upstream_user_id"] = upstream_user_id
        repo.upsert_auth_state(
            FitnessAuthState(
                user_id=user_id,
                source="strava",
                access_token=access_token,
                refresh_token=refresh_token,
                token_expires_at=token_expires_at,
                extra_state=extra,
                last_successful_login_at=_now_iso(),
                last_refresh_at=existing.last_refresh_at if existing else None,
                auth_status="ok",
                auth_broken_since=None,
                created_at=existing.created_at if existing else "",
            ),
        )

    @mcp.custom_route(
        "/api/fitness/strava/authorize_url",
        methods=["GET"],
        name="api_fitness_strava_authorize_url",
    )
    @handler(services_getter)
    def strava_authorize_url(
        request: Request, services: ServicesDict, body: None
    ) -> JSONResponse:
        disabled = _disabled_response(services)
        if disabled is not None:
            return disabled
        user = get_authenticated_user(request)
        config = services.get("config")
        client_id = getattr(config, "strava_client_id", "") if config else ""
        redirect_uri = getattr(config, "strava_redirect_uri", "") if config else ""
        if not client_id:
            return JSONResponse(
                {
                    "error": (
                        "STRAVA_CLIENT_ID is not configured on the server. "
                        "Ask the operator to set it."
                    ),
                },
                status_code=500,
            )

        pending = _strava_pending(services)
        token, expires_at_iso = pending.issue(user_id=user.user_id)
        from urllib.parse import urlencode

        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "approval_prompt": "auto",
            "scope": "read,activity:read_all",
            "state": token,
        }
        authorize_url = (
            f"https://www.strava.com/oauth/authorize?{urlencode(params)}"
        )
        log.info(
            "GET /api/fitness/strava/authorize_url — user_id=%d (state minted)",
            user.user_id,
        )
        return JSONResponse(
            {
                "authorize_url": authorize_url,
                "state": token,
                "expires_at": expires_at_iso,
            },
            status_code=200,
        )

    @mcp.custom_route(
        "/api/fitness/strava/exchange",
        methods=["POST"],
        name="api_fitness_strava_exchange",
    )
    @handler(services_getter, parse_json=JsonBody(invalid_error="Invalid JSON", require_dict=False))
    def strava_exchange(
        request: Request, services: ServicesDict, body: dict | object
    ) -> JSONResponse:
        disabled = _disabled_response(services)
        if disabled is not None:
            return disabled
        user = get_authenticated_user(request)
        repo: FitnessRepository = services["fitness_repo"]
        pending = _strava_pending(services)
        exchange = _strava_exchange(services)
        config = services.get("config")
        client_id = getattr(config, "strava_client_id", "") if config else ""
        client_secret = (
            getattr(config, "strava_client_secret", "") if config else ""
        )

        code = (body.get("code") or "").strip()
        state = (body.get("state") or "").strip()
        if not code or not state:
            return JSONResponse(
                {"error": "code and state are required"}, status_code=400,
            )

        # Peek first so cross-user replay attempts don't burn the
        # legitimate user's pending entry — same shape as the Garmin
        # MFA endpoint.
        entry = pending.peek(state)
        if entry is None:
            return JSONResponse(
                {
                    "error": (
                        "Pending state expired or unknown. Repeat the "
                        "connect step."
                    ),
                    "reason": "expired_pending_state",
                },
                status_code=410,
            )
        if entry.user_id != user.user_id:
            log.warning(
                "POST /api/fitness/strava/exchange — cross-user state "
                "attempt: token issued to user_id=%d, consumed by "
                "user_id=%d (rejected)",
                entry.user_id, user.user_id,
            )
            return JSONResponse(
                {
                    "error": "Pending state does not belong to this user.",
                    "reason": "cross_user_pending_session",
                },
                status_code=403,
            )

        # State matches calling user — consume it. Both success and
        # SDK-error paths burn the state, matching OAuth's CSRF-state
        # one-shot semantics (re-using a state on retry would defeat the
        # guarantee).
        pending.consume(state)

        try:
            tokens, athlete_id = exchange(
                client_id=client_id,
                client_secret=client_secret,
                code=code,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "POST /api/fitness/strava/exchange — Strava rejected the "
                "code for user_id=%d (%s)", user.user_id, exc,
            )
            return JSONResponse(
                {
                    "error": "Strava rejected the authorization code.",
                    "reason": "upstream_error",
                    "detail": str(exc),
                },
                status_code=502,
            )
        finally:
            # Drop the local code reference — single-use OAuth code, but
            # still good hygiene per the W3 brief.
            del code

        if not athlete_id:
            log.warning(
                "POST /api/fitness/strava/exchange — no athlete id in "
                "Strava response for user_id=%d (D8 retrofit impossible "
                "without it)", user.user_id,
            )
            return JSONResponse(
                {
                    "error": (
                        "Strava accepted the code but no athlete identity "
                        "was returned. Please retry."
                    ),
                    "reason": "missing_upstream_identity",
                },
                status_code=502,
            )

        # D8 mismatch check.
        existing = repo.get_auth_state(user_id=user.user_id, source="strava")
        if existing is not None:
            stored = (existing.extra_state or {}).get("upstream_user_id")
            if stored and stored != athlete_id:
                return JSONResponse(
                    {
                        "error": (
                            "This Strava account differs from the one "
                            "previously connected. Disconnect Strava first, "
                            "then reconnect."
                        ),
                        "reason": "upstream_account_mismatch",
                        "stored_upstream_user_id": stored,
                        "incoming_upstream_user_id": athlete_id,
                    },
                    status_code=409,
                )

        _persist_strava_auth(
            repo,
            user_id=user.user_id,
            access_token=tokens["access_token"],
            refresh_token=tokens["refresh_token"],
            token_expires_at=tokens["token_expires_at"],
            upstream_user_id=athlete_id,
        )
        log.info(
            "POST /api/fitness/strava/exchange — connected user_id=%d "
            "(upstream=%s)", user.user_id, athlete_id,
        )
        return JSONResponse(
            {"connected": True, "upstream_user_id": athlete_id},
            status_code=200,
        )

    @mcp.custom_route(
        "/api/fitness/strava/disconnect",
        methods=["POST"],
        name="api_fitness_strava_disconnect",
    )
    @handler(services_getter)
    def strava_disconnect(
        request: Request, services: ServicesDict, body: None
    ) -> JSONResponse:
        disabled = _disabled_response(services)
        if disabled is not None:
            return disabled
        user = get_authenticated_user(request)
        repo: FitnessRepository = services["fitness_repo"]
        deleted = repo.delete_auth_state(user_id=user.user_id, source="strava")
        log.info(
            "POST /api/fitness/strava/disconnect — user_id=%d deleted=%s",
            user.user_id, deleted,
        )
        return JSONResponse({"disconnected": bool(deleted)}, status_code=200)
