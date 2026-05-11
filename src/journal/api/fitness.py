"""Fitness pipeline read-side routes.

Owns the four GET endpoints under ``/api/fitness/``:

- ``GET /api/fitness/activities?start=&end=&type=`` — windowed activities.
- ``GET /api/fitness/daily?start=&end=`` — windowed daily rollups.
- ``GET /api/fitness/sync/status`` — per-source auth + last-runs snapshot.
- ``GET /api/fitness/integrity`` — soft-pointer orphan report.

Plus the W2 per-user Garmin connect endpoints:

- ``POST /api/fitness/garmin/connect`` — start a Garmin login (sync;
  may return ``mfa_required`` and a pending session token).
- ``POST /api/fitness/garmin/connect/mfa`` — complete an MFA-required
  Garmin login.
- ``POST /api/fitness/garmin/disconnect`` — drop the user's Garmin tokens.

Job creation (``POST /api/fitness/sync/{source}``) lives in
``api/ingestion.py`` per the routing override (write/job creation —
see ``api/_shared.py``'s docstring).

Auth is enforced by ``RequireAuthMiddleware``: every route below assumes
``request.user`` is an :class:`AuthenticatedUser`. The per-route
``get_authenticated_user`` call extracts the user_id for query scoping.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectTooManyRequestsError,
)
from starlette.responses import JSONResponse

from journal.auth import get_authenticated_user
from journal.db.fitness_integrity import check_fitness_integrity
from journal.models import FitnessAuthState
from journal.providers.strava import exchange_code as strava_exchange_code_default
from journal.services.fitness.garmin_pending import (
    GarminCooldownTracker,
    GarminPendingStore,
)
from journal.services.fitness.strava_pending import StravaPendingStore

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable

    from mcp.server.fastmcp import FastMCP
    from starlette.requests import Request

    from journal.db.fitness_repository import FitnessRepository
    from journal.models import (
        FitnessActivity,
        FitnessDaily,
        FitnessSyncRun,
    )

log = logging.getLogger(__name__)

_VALID_SOURCES = ("strava", "garmin")


def _activity_to_dict(a: FitnessActivity) -> dict[str, Any]:
    return {
        "id": a.id,
        "user_id": a.user_id,
        "source": a.source,
        "source_id": a.source_id,
        "activity_type": a.activity_type,
        "source_subtype": a.source_subtype,
        "start_time": a.start_time,
        "local_date": a.local_date,
        "duration_s": a.duration_s,
        "moving_time_s": a.moving_time_s,
        "distance_m": a.distance_m,
        "elevation_gain_m": a.elevation_gain_m,
        "avg_hr_bpm": a.avg_hr_bpm,
        "max_hr_bpm": a.max_hr_bpm,
        "avg_pace_s_per_km": a.avg_pace_s_per_km,
        "calories_kcal": a.calories_kcal,
        "perceived_exertion": a.perceived_exertion,
        "extras": a.extras,
        "raw_ref_id": a.raw_ref_id,
        "normalized_at": a.normalized_at,
    }


def _daily_to_dict(d: FitnessDaily) -> dict[str, Any]:
    return {
        "id": d.id,
        "user_id": d.user_id,
        "source": d.source,
        "local_date": d.local_date,
        "sleep_score": d.sleep_score,
        "sleep_duration_s": d.sleep_duration_s,
        "sleep_efficiency_pct": d.sleep_efficiency_pct,
        "hrv_overnight_ms": d.hrv_overnight_ms,
        "resting_hr_bpm": d.resting_hr_bpm,
        "body_battery_high": d.body_battery_high,
        "body_battery_low": d.body_battery_low,
        "stress_avg": d.stress_avg,
        "training_load_acute": d.training_load_acute,
        "training_load_chronic": d.training_load_chronic,
        "training_readiness": d.training_readiness,
        "extras": d.extras,
        "raw_ref_ids": d.raw_ref_ids,
        "normalized_at": d.normalized_at,
    }


def _sync_run_to_dict(run: FitnessSyncRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "status": run.status,
        "rows_fetched": run.rows_fetched,
        "rows_normalized": run.rows_normalized,
        "error_class": run.error_class,
        "error_message": run.error_message,
    }


def _per_source_status(
    repo: FitnessRepository,
    *,
    user_id: int,
    source: str,
) -> dict[str, Any] | None:
    """Return the status payload for *source*, or ``None`` if this user
    has never had any fitness activity on this source — i.e. no
    ``fitness_auth_state`` row AND no ``fitness_sync_runs`` rows.

    Returning ``None`` (rather than a default-populated dict) lets the
    webapp tell "first-use, never connected" apart from "configured but
    no successful sync yet" — only the first deserves the connect CTA.
    """
    auth: FitnessAuthState | None = repo.get_auth_state(
        user_id=user_id, source=source,
    )
    last_runs = repo.list_recent_sync_runs(
        user_id=user_id, source=source, limit=10,
    )
    if auth is None and not last_runs:
        return None
    last_success_at = repo.last_successful_sync_at(
        user_id=user_id, source=source,
    )
    return {
        "auth_status": auth.auth_status if auth is not None else "unknown",
        "auth_broken_since": auth.auth_broken_since if auth is not None else None,
        "last_success_at": last_success_at,
        "last_runs": [_sync_run_to_dict(r) for r in last_runs],
    }


def _missing_param(name: str) -> JSONResponse:
    return JSONResponse(
        {"error": f"Query parameter '{name}' is required"},
        status_code=400,
    )


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _extract_upstream_user_id(client: Any, fallback: str) -> str | None:
    """Pull the stable upstream account identifier from a logged-in client.

    Garmin's ``get_user_profile`` returns a dict with ``displayName``
    (typically the username, stable across sessions). We use that as the
    upstream id (D8) to detect silent reconnects with a *different* Garmin
    account. ``fallback`` is the username the user typed — only used if
    the profile call returns an empty or malformed response, which the
    W2 spec treats as a post-MFA failure.
    """
    try:
        profile = client.get_user_profile()
    except Exception:  # noqa: BLE001  caller decides whether to surface
        raise
    if not isinstance(profile, dict):
        return None
    upstream = profile.get("displayName") or profile.get("userName")
    if isinstance(upstream, str) and upstream:
        return upstream
    return fallback or None


def _persist_garmin_auth(
    repo: FitnessRepository,
    *,
    user_id: int,
    tokens_blob: str,
    upstream_user_id: str,
) -> None:
    """Upsert the user's Garmin auth row after a successful connect or MFA.

    Mirrors the operator-driven semantics of
    ``cli/fitness.cmd_fitness_reauth_garmin``: forces ``auth_status="ok"``,
    clears ``auth_broken_since``, stamps ``last_successful_login_at``, and
    preserves any unrelated ``extra_state`` keys (e.g. fields the fetch
    service writes during a sync).
    """
    existing = repo.get_auth_state(user_id=user_id, source="garmin")
    extra = dict(existing.extra_state) if existing else {}
    extra["tokens_blob"] = tokens_blob
    extra["upstream_user_id"] = upstream_user_id
    repo.upsert_auth_state(
        FitnessAuthState(
            user_id=user_id,
            source="garmin",
            access_token=existing.access_token if existing else None,
            refresh_token=existing.refresh_token if existing else None,
            token_expires_at=existing.token_expires_at if existing else None,
            extra_state=extra,
            last_successful_login_at=_now_iso(),
            last_refresh_at=existing.last_refresh_at if existing else None,
            auth_status="ok",
            auth_broken_since=None,
            created_at=existing.created_at if existing else "",
        ),
    )


def register_fitness_routes(
    mcp: FastMCP,
    services_getter: Callable[[], dict | None],
) -> None:
    """Register the four ``GET /api/fitness/*`` read routes.

    The job-creation companion (``POST /api/fitness/sync/{source}``)
    is registered by ``register_ingestion_routes`` per the routing
    override.
    """

    @mcp.custom_route(
        "/api/fitness/activities",
        methods=["GET"],
        name="api_fitness_list_activities",
    )
    async def list_activities(request: Request) -> JSONResponse:
        services = services_getter()
        if services is None:
            return JSONResponse({"error": "Server not initialized"}, status_code=503)
        user = get_authenticated_user(request)
        repo: FitnessRepository = services["fitness_repo"]

        start = request.query_params.get("start")
        end = request.query_params.get("end")
        if not start:
            return _missing_param("start")
        if not end:
            return _missing_param("end")
        activity_type = request.query_params.get("type")

        activities = repo.list_activities(
            user_id=user.user_id,
            start=start,
            end=end,
            activity_type=activity_type,
        )
        log.info(
            "GET /api/fitness/activities — %d items (%s..%s, type=%s)",
            len(activities), start, end, activity_type,
        )
        return JSONResponse({"items": [_activity_to_dict(a) for a in activities]})

    @mcp.custom_route(
        "/api/fitness/daily",
        methods=["GET"],
        name="api_fitness_list_daily",
    )
    async def list_daily(request: Request) -> JSONResponse:
        services = services_getter()
        if services is None:
            return JSONResponse({"error": "Server not initialized"}, status_code=503)
        user = get_authenticated_user(request)
        repo: FitnessRepository = services["fitness_repo"]

        start = request.query_params.get("start")
        end = request.query_params.get("end")
        if not start:
            return _missing_param("start")
        if not end:
            return _missing_param("end")

        daily = repo.list_daily(user_id=user.user_id, start=start, end=end)
        log.info(
            "GET /api/fitness/daily — %d items (%s..%s)",
            len(daily), start, end,
        )
        return JSONResponse({"items": [_daily_to_dict(d) for d in daily]})

    @mcp.custom_route(
        "/api/fitness/sync/status",
        methods=["GET"],
        name="api_fitness_sync_status",
    )
    async def sync_status(request: Request) -> JSONResponse:
        services = services_getter()
        if services is None:
            return JSONResponse({"error": "Server not initialized"}, status_code=503)
        user = get_authenticated_user(request)
        repo: FitnessRepository = services["fitness_repo"]

        body = {
            source: _per_source_status(repo, user_id=user.user_id, source=source)
            for source in _VALID_SOURCES
        }
        log.info(
            "GET /api/fitness/sync/status — strava=%s garmin=%s",
            "configured" if body["strava"] else "null",
            "configured" if body["garmin"] else "null",
        )
        return JSONResponse(body)

    @mcp.custom_route(
        "/api/fitness/integrity",
        methods=["GET"],
        name="api_fitness_integrity",
    )
    async def integrity(request: Request) -> JSONResponse:
        services = services_getter()
        if services is None:
            return JSONResponse({"error": "Server not initialized"}, status_code=503)
        # Per-user scoped (W4 of the fitness multi-user plan): each caller
        # sees only their own orphans. A non-admin caller never sees other
        # users' raw rows in their report, even via the existence of orphan
        # references.
        user = get_authenticated_user(request)
        conn: sqlite3.Connection = services["db_factory"].get()

        report = check_fitness_integrity(conn, user_id=user.user_id)
        body = {
            "activities": [asdict(o) for o in report.activities],
            "daily": [asdict(o) for o in report.daily],
        }
        log.info(
            "GET /api/fitness/integrity — user_id=%d %d activity orphans, "
            "%d daily orphans",
            user.user_id, len(report.activities), len(report.daily),
        )
        return JSONResponse(body)

    # ── W2: Garmin per-user connect / MFA / disconnect ───────────────

    def _services_or_503() -> tuple[dict | None, JSONResponse | None]:
        services = services_getter()
        if services is None:
            return None, JSONResponse(
                {"error": "Server not initialized"}, status_code=503,
            )
        return services, None

    def _garmin_pending(services: dict) -> GarminPendingStore:
        store = services.get("garmin_pending")
        if store is None:
            store = GarminPendingStore()
            services["garmin_pending"] = store
        return store

    def _garmin_cooldown(services: dict) -> GarminCooldownTracker:
        tracker = services.get("garmin_cooldown")
        if tracker is None:
            tracker = GarminCooldownTracker()
            services["garmin_cooldown"] = tracker
        return tracker

    @mcp.custom_route(
        "/api/fitness/garmin/connect",
        methods=["POST"],
        name="api_fitness_garmin_connect",
    )
    async def garmin_connect(request: Request) -> JSONResponse:
        services, err = _services_or_503()
        if err is not None:
            return err
        user = get_authenticated_user(request)
        repo: FitnessRepository = services["fitness_repo"]
        pending = _garmin_pending(services)
        cooldown = _garmin_cooldown(services)
        client_factory = services.get("garmin_client_factory") or Garmin

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        if not username or not password:
            return JSONResponse(
                {"error": "username and password are required"},
                status_code=400,
            )

        # Cool-down before any upstream call. Garmin's rate-limiter keys on
        # clientId+email; if we let the user keep retrying after a few wrong
        # passwords we deepen the upstream lockout. The local tracker
        # protects them by refusing inside the same window.
        retry_after = cooldown.check(username)
        if retry_after is not None:
            return JSONResponse(
                {
                    "error": (
                        "Too many failed Garmin login attempts for that "
                        "account. Try again in a few minutes."
                    ),
                    "reason": "local_cooldown",
                    "retry_after_seconds": int(retry_after),
                },
                status_code=429,
            )

        # Build the client outside the threadpool — instantiation is cheap
        # and we want the password living in handler scope only as long as
        # absolutely needed.
        client = client_factory(
            email=username, password=password, return_on_mfa=True,
        )
        # Drop the local password reference. The Garmin client holds its
        # own copy briefly; we'll let GC reclaim that once the call returns.
        del password

        try:
            result = await asyncio.to_thread(client.login)
        except GarminConnectAuthenticationError as exc:
            cooldown.record_failure(username)
            log.info(
                "POST /api/fitness/garmin/connect — invalid credentials "
                "for user_id=%d", user.user_id,
            )
            return JSONResponse(
                {
                    "error": "Garmin rejected those credentials.",
                    "reason": "invalid_credentials",
                    "detail": str(exc),
                },
                status_code=401,
            )
        except GarminConnectTooManyRequestsError as exc:
            cooldown.record_failure(username)
            log.warning(
                "POST /api/fitness/garmin/connect — Garmin returned 429 "
                "for user_id=%d", user.user_id,
            )
            return JSONResponse(
                {
                    "error": (
                        "Garmin is rate-limiting login attempts. Wait a few "
                        "minutes before retrying."
                    ),
                    "reason": "upstream_rate_limited",
                    "retry_after_seconds": 300,
                    "detail": str(exc),
                },
                status_code=429,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception(
                "POST /api/fitness/garmin/connect — unexpected error "
                "for user_id=%d", user.user_id,
            )
            return JSONResponse(
                {
                    "error": f"Garmin login failed: {exc}",
                    "reason": "upstream_error",
                },
                status_code=502,
            )

        # ``Garmin.login()`` returns ``("needs_mfa", legacy)`` when MFA is
        # required (``return_on_mfa=True``) and ``(None, legacy)`` on
        # successful no-MFA login.
        mfa_status: Any = None
        legacy_token: Any = None
        if isinstance(result, tuple) and len(result) >= 1:
            mfa_status = result[0]
            if len(result) >= 2:
                legacy_token = result[1]

        if mfa_status == "needs_mfa":
            token, expires_at_iso = pending.issue(
                user_id=user.user_id, client=client, state_token=legacy_token,
            )
            log.info(
                "POST /api/fitness/garmin/connect — MFA required for "
                "user_id=%d (pending session minted)", user.user_id,
            )
            return JSONResponse(
                {
                    "mfa_required": True,
                    "pending_session": token,
                    "expires_at": expires_at_iso,
                },
                status_code=200,
            )

        # No-MFA success: capture upstream id + token blob, persist.
        try:
            upstream = await asyncio.to_thread(
                _extract_upstream_user_id, client, username,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception(
                "POST /api/fitness/garmin/connect — profile fetch failed "
                "for user_id=%d after no-MFA login", user.user_id,
            )
            return JSONResponse(
                {
                    "error": (
                        "Garmin login succeeded but the post-login profile "
                        "fetch failed. Please retry."
                    ),
                    "reason": "post_login_profile_fetch_failed",
                    "detail": str(exc),
                },
                status_code=502,
            )
        if not upstream:
            return JSONResponse(
                {
                    "error": (
                        "Garmin login succeeded but no upstream identity "
                        "could be determined."
                    ),
                    "reason": "post_login_profile_fetch_failed",
                },
                status_code=502,
            )

        existing = repo.get_auth_state(user_id=user.user_id, source="garmin")
        if existing is not None:
            stored = (existing.extra_state or {}).get("upstream_user_id")
            if stored and stored != upstream:
                return JSONResponse(
                    {
                        "error": (
                            "This Garmin account differs from the one "
                            "previously connected. Disconnect Garmin first, "
                            "then reconnect."
                        ),
                        "reason": "upstream_account_mismatch",
                        "stored_upstream_user_id": stored,
                        "incoming_upstream_user_id": upstream,
                    },
                    status_code=409,
                )

        try:
            blob = client.client.dumps()
        except Exception as exc:  # noqa: BLE001
            log.exception(
                "POST /api/fitness/garmin/connect — token blob dump failed "
                "for user_id=%d", user.user_id,
            )
            return JSONResponse(
                {
                    "error": f"Garmin login succeeded but token capture failed: {exc}",
                    "reason": "token_capture_failed",
                },
                status_code=502,
            )

        _persist_garmin_auth(
            repo, user_id=user.user_id, tokens_blob=blob, upstream_user_id=upstream,
        )
        cooldown.reset(username)
        log.info(
            "POST /api/fitness/garmin/connect — connected user_id=%d "
            "(upstream=%s)", user.user_id, upstream,
        )
        return JSONResponse(
            {"connected": True, "upstream_user_id": upstream}, status_code=200,
        )

    @mcp.custom_route(
        "/api/fitness/garmin/connect/mfa",
        methods=["POST"],
        name="api_fitness_garmin_connect_mfa",
    )
    async def garmin_connect_mfa(request: Request) -> JSONResponse:
        services, err = _services_or_503()
        if err is not None:
            return err
        user = get_authenticated_user(request)
        repo: FitnessRepository = services["fitness_repo"]
        pending = _garmin_pending(services)
        cooldown = _garmin_cooldown(services)

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)
        token = body.get("pending_session") or ""
        code = (body.get("code") or "").strip()
        if not token or not code:
            return JSONResponse(
                {"error": "pending_session and code are required"},
                status_code=400,
            )

        # Peek first so we can enforce the user-binding check without
        # consuming the entry on a 403 — the legitimate user can still
        # complete their flow.
        entry = pending.peek(token)
        if entry is None:
            return JSONResponse(
                {
                    "error": (
                        "Pending session expired or unknown. Repeat the "
                        "connect step."
                    ),
                    "reason": "expired_pending_session",
                },
                status_code=410,
            )
        if entry.user_id != user.user_id:
            log.warning(
                "POST /api/fitness/garmin/connect/mfa — cross-user "
                "pending-session attempt: token issued to user_id=%d, "
                "consumed by user_id=%d (rejected)",
                entry.user_id, user.user_id,
            )
            return JSONResponse(
                {
                    "error": "Pending session does not belong to this user.",
                    "reason": "cross_user_pending_session",
                },
                status_code=403,
            )

        client = entry.client
        try:
            await asyncio.to_thread(
                client.resume_login, entry.state_token, code,
            )
        except GarminConnectAuthenticationError as exc:
            log.info(
                "POST /api/fitness/garmin/connect/mfa — bad MFA code "
                "for user_id=%d", user.user_id,
            )
            return JSONResponse(
                {
                    "error": "Garmin rejected the MFA code.",
                    "reason": "invalid_mfa_code",
                    "detail": str(exc),
                },
                status_code=401,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception(
                "POST /api/fitness/garmin/connect/mfa — unexpected error "
                "for user_id=%d", user.user_id,
            )
            return JSONResponse(
                {
                    "error": f"Garmin MFA resume failed: {exc}",
                    "reason": "upstream_error",
                },
                status_code=502,
            )

        # Resume_login succeeded. Now fetch the upstream profile —
        # python-garminconnect issues #312/#337 record this call as
        # intermittently flaky right after a fresh MFA, distinct from
        # "wrong code" so the UI can ask for a retry rather than blame
        # the user's credentials.
        try:
            upstream = await asyncio.to_thread(
                _extract_upstream_user_id, client, "",
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "POST /api/fitness/garmin/connect/mfa — post-MFA profile "
                "fetch failed for user_id=%d (%s)", user.user_id, exc,
            )
            return JSONResponse(
                {
                    "error": (
                        "Garmin accepted the MFA code but the post-login "
                        "profile fetch failed. Please retry."
                    ),
                    "reason": "post_mfa_profile_fetch_failed",
                    "detail": str(exc),
                },
                status_code=502,
            )
        if not upstream:
            return JSONResponse(
                {
                    "error": (
                        "Garmin accepted the MFA code but no upstream "
                        "identity could be determined. Please retry."
                    ),
                    "reason": "post_mfa_profile_fetch_failed",
                },
                status_code=502,
            )

        # D8 mismatch check on the MFA path too.
        existing = repo.get_auth_state(user_id=user.user_id, source="garmin")
        if existing is not None:
            stored = (existing.extra_state or {}).get("upstream_user_id")
            if stored and stored != upstream:
                # Consume the pending entry — the MFA was valid; the user
                # just authenticated as a different upstream account, so
                # the in-flight challenge has served its purpose.
                pending.consume(token)
                return JSONResponse(
                    {
                        "error": (
                            "This Garmin account differs from the one "
                            "previously connected. Disconnect Garmin first, "
                            "then reconnect."
                        ),
                        "reason": "upstream_account_mismatch",
                        "stored_upstream_user_id": stored,
                        "incoming_upstream_user_id": upstream,
                    },
                    status_code=409,
                )

        try:
            blob = client.client.dumps()
        except Exception as exc:  # noqa: BLE001
            log.exception(
                "POST /api/fitness/garmin/connect/mfa — token blob dump "
                "failed for user_id=%d", user.user_id,
            )
            return JSONResponse(
                {
                    "error": f"Garmin login succeeded but token capture failed: {exc}",
                    "reason": "token_capture_failed",
                },
                status_code=502,
            )

        _persist_garmin_auth(
            repo, user_id=user.user_id, tokens_blob=blob, upstream_user_id=upstream,
        )
        # Now consume the pending entry — login is committed.
        pending.consume(token)
        # Reset the per-email cool-down — the user just succeeded.
        # We don't have the original email here, but the cooldown is keyed
        # by what the user typed; if a stale failure-counter hangs around
        # it ages out within the window. Acceptable trade for not having
        # to plumb the email through the pending entry.
        log.info(
            "POST /api/fitness/garmin/connect/mfa — connected user_id=%d "
            "(upstream=%s)", user.user_id, upstream,
        )
        # silence unused-variable complaints from optional cooldown reset
        del cooldown
        return JSONResponse(
            {"connected": True, "upstream_user_id": upstream}, status_code=200,
        )

    @mcp.custom_route(
        "/api/fitness/garmin/disconnect",
        methods=["POST"],
        name="api_fitness_garmin_disconnect",
    )
    async def garmin_disconnect(request: Request) -> JSONResponse:
        services, err = _services_or_503()
        if err is not None:
            return err
        user = get_authenticated_user(request)
        repo: FitnessRepository = services["fitness_repo"]
        deleted = repo.delete_auth_state(user_id=user.user_id, source="garmin")
        log.info(
            "POST /api/fitness/garmin/disconnect — user_id=%d deleted=%s",
            user.user_id, deleted,
        )
        return JSONResponse({"disconnected": bool(deleted)}, status_code=200)

    # ── W3: Strava per-user authorize_url / exchange / disconnect ────

    def _strava_pending(services: dict) -> StravaPendingStore:
        store = services.get("strava_pending")
        if store is None:
            store = StravaPendingStore()
            services["strava_pending"] = store
        return store

    def _strava_exchange(services: dict) -> Any:
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
        ``extra_state`` keys (mirrors :func:`_persist_garmin_auth` and
        the CLI re-auth semantics in ``cli/fitness.cmd_fitness_reauth_strava``).
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
    async def strava_authorize_url(request: Request) -> JSONResponse:
        services, err = _services_or_503()
        if err is not None:
            return err
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
    async def strava_exchange(request: Request) -> JSONResponse:
        services, err = _services_or_503()
        if err is not None:
            return err
        user = get_authenticated_user(request)
        repo: FitnessRepository = services["fitness_repo"]
        pending = _strava_pending(services)
        exchange = _strava_exchange(services)
        config = services.get("config")
        client_id = getattr(config, "strava_client_id", "") if config else ""
        client_secret = (
            getattr(config, "strava_client_secret", "") if config else ""
        )

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)
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
            tokens, athlete_id = await asyncio.to_thread(
                exchange,
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
    async def strava_disconnect(request: Request) -> JSONResponse:
        services, err = _services_or_503()
        if err is not None:
            return err
        user = get_authenticated_user(request)
        repo: FitnessRepository = services["fitness_repo"]
        deleted = repo.delete_auth_state(user_id=user.user_id, source="strava")
        log.info(
            "POST /api/fitness/strava/disconnect — user_id=%d deleted=%s",
            user.user_id, deleted,
        )
        return JSONResponse({"disconnected": bool(deleted)}, status_code=200)
