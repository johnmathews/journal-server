"""Auth and admin REST API endpoints for multi-user journal."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from starlette.responses import JSONResponse

from journal.auth import clear_session_cookie, get_authenticated_user, set_session_cookie

if TYPE_CHECKING:
    from collections.abc import Callable

    from mcp.server.fastmcp import FastMCP
    from starlette.requests import Request

    from journal.config import Config
    from journal.db.user_repository import SQLiteUserRepository
    from journal.models import ApiKeyInfo, User
    from journal.services.auth import AuthService
    from journal.services.email import EmailService

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _user_to_dict(user: User | object) -> dict:
    """Serialize a User model or AuthenticatedUser middleware object to JSON-safe dict."""
    return {
        "id": getattr(user, "id", None) or getattr(user, "user_id", None),
        "email": getattr(user, "email", ""),
        "display_name": getattr(user, "display_name", ""),
        "is_admin": getattr(user, "is_admin", False),
        "is_active": getattr(user, "is_active", True),
        "email_verified": getattr(user, "email_verified", False),
        "created_at": getattr(user, "created_at", ""),
        "updated_at": getattr(user, "updated_at", ""),
    }


def _api_key_info_to_dict(info: ApiKeyInfo) -> dict:
    """Serialize an ApiKeyInfo to a JSON-safe dict."""
    return {
        "id": info.id,
        "user_id": info.user_id,
        "key_prefix": info.key_prefix,
        "name": info.name,
        "created_at": info.created_at,
        "expires_at": info.expires_at,
        "last_used_at": info.last_used_at,
        "revoked_at": info.revoked_at,
    }


def _services_or_503(
    services_getter: Callable[[], dict | None],
) -> dict | JSONResponse:
    """Return the services dict or a 503 JSONResponse if not yet initialized."""
    services = services_getter()
    if services is None:
        return JSONResponse(
            {"error": "server_not_ready", "message": "Server not initialized"},
            status_code=503,
        )
    return services


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------


def register_auth_routes(
    mcp: FastMCP,
    services_getter: Callable[[], dict | None],
) -> None:
    """Register authentication-related REST API routes on the MCP server.

    Args:
        mcp: The FastMCP instance.
        services_getter: A callable returning the services dict (with
            ``auth_service``, ``email_service``, ``user_repo``, ``config``).
    """

    # ── POST /api/auth/login ───────────────────────────────────────────

    @mcp.custom_route("/api/auth/login", methods=["POST"], name="api_auth_login")
    async def auth_login(request: Request) -> JSONResponse:
        """Authenticate with email + password, return user JSON + session cookie."""
        result = _services_or_503(services_getter)
        if isinstance(result, JSONResponse):
            return result
        services = result

        auth_service: AuthService = services["auth_service"]

        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return JSONResponse(
                {"error": "invalid_body", "message": "Invalid JSON body"},
                status_code=400,
            )

        email = body.get("email", "").strip()
        password = body.get("password", "")

        if not email or not password:
            return JSONResponse(
                {"error": "missing_fields", "message": "Email and password are required"},
                status_code=400,
            )

        try:
            user = auth_service.authenticate(email, password)
        except ValueError as exc:
            log.info("Login failed for %s: %s", email, exc)
            return JSONResponse(
                {"error": "invalid_credentials", "message": str(exc)},
                status_code=401,
            )

        session_id = auth_service.create_session(
            user.id,
            user_agent=request.headers.get("user-agent"),
            ip_address=request.client.host if request.client else None,
        )

        response = JSONResponse({"user": _user_to_dict(user)})
        set_session_cookie(response, session_id)
        log.info("Login succeeded for user %d (%s)", user.id, user.email)
        return response

    # ── POST /api/auth/logout ──────────────────────────────────────────

    @mcp.custom_route("/api/auth/logout", methods=["POST"], name="api_auth_logout")
    async def auth_logout(request: Request) -> JSONResponse:
        """Log out the current session."""
        result = _services_or_503(services_getter)
        if isinstance(result, JSONResponse):
            return result
        services = result

        auth_service: AuthService = services["auth_service"]
        session_id = request.cookies.get("session_id")

        if session_id:
            auth_service.logout(session_id)

        response = JSONResponse({"ok": True})
        clear_session_cookie(response)
        return response

    # ── GET /api/auth/me ───────────────────────────────────────────────

    @mcp.custom_route("/api/auth/me", methods=["GET"], name="api_auth_me")
    async def auth_me(request: Request) -> JSONResponse:
        """Return the currently authenticated user."""
        user = get_authenticated_user(request)
        return JSONResponse({"user": _user_to_dict(user)})

    # ── PATCH /api/auth/me ────────────────────────────────────────────

    @mcp.custom_route("/api/auth/me", methods=["PATCH"], name="api_auth_me_update")
    async def auth_me_update(request: Request) -> JSONResponse:
        """Update the currently authenticated user's profile (display_name)."""
        result = _services_or_503(services_getter)
        if isinstance(result, JSONResponse):
            return result
        services = result

        user_repo: SQLiteUserRepository = services["user_repo"]
        user = get_authenticated_user(request)

        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return JSONResponse(
                {"error": "invalid_body", "message": "Invalid JSON body"},
                status_code=400,
            )

        display_name = (
            body.get("display_name", "").strip()
            if isinstance(body.get("display_name"), str)
            else ""
        )
        if not display_name:
            return JSONResponse(
                {
                    "error": "missing_fields",
                    "message": "display_name is required and must be non-empty",
                },
                status_code=400,
            )

        updated = user_repo.update_user(user.user_id, display_name=display_name)
        if updated is None:
            return JSONResponse(
                {"error": "not_found", "message": "User not found"},
                status_code=404,
            )

        log.info("User %d updated display_name to %r", user.user_id, display_name)
        return JSONResponse({"user": _user_to_dict(updated)})

    # ── POST /api/auth/register ────────────────────────────────────────

    @mcp.custom_route("/api/auth/register", methods=["POST"], name="api_auth_register")
    async def auth_register(request: Request) -> JSONResponse:
        """Register a new user account."""
        result = _services_or_503(services_getter)
        if isinstance(result, JSONResponse):
            return result
        services = result

        auth_service: AuthService = services["auth_service"]
        email_service: EmailService | None = services.get("email_service")
        config: Config = services["config"]

        from journal.api import _runtime_get

        if not _runtime_get(services, "registration_enabled"):
            return JSONResponse(
                {"error": "registration_disabled", "message": "Registration is currently disabled"},
                status_code=403,
            )

        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return JSONResponse(
                {"error": "invalid_body", "message": "Invalid JSON body"},
                status_code=400,
            )

        email = body.get("email", "").strip()
        password = body.get("password", "")
        display_name = body.get("display_name", "").strip()

        if not email or not password or not display_name:
            return JSONResponse(
                {
                    "error": "missing_fields",
                    "message": "Email, password, and display_name are required",
                },
                status_code=400,
            )

        if len(password) < 8 or len(password) > 1024:
            return JSONResponse(
                {
                    "error": "weak_password",
                    "message": "Password must be between 8 and 1024 characters",
                },
                status_code=400,
            )

        try:
            user = auth_service.register_user(email, password, display_name)
        except ValueError as exc:
            log.info("Registration failed for %s: %s", email, exc)
            return JSONResponse(
                {"error": "duplicate_email", "message": str(exc)},
                status_code=400,
            )

        # Send verification email (non-blocking — don't fail registration if SMTP is down)
        if email_service is not None:
            try:
                token = auth_service.generate_verification_token(email)
                await email_service.send_verification_email(
                    email,
                    token,
                    config.app_base_url,
                )
            except Exception:
                log.warning("Failed to send verification email to %s", email, exc_info=True)
        else:
            log.warning("Email service not configured — skipping verification email for %s", email)

        session_id = auth_service.create_session(
            user.id,
            user_agent=request.headers.get("user-agent"),
            ip_address=request.client.host if request.client else None,
        )

        response = JSONResponse({"user": _user_to_dict(user)}, status_code=201)
        set_session_cookie(response, session_id)
        log.info("Registered user %d (%s)", user.id, user.email)
        return response

    # ── GET /api/auth/config ───────────────────────────────────────────

    @mcp.custom_route("/api/auth/config", methods=["GET"], name="api_auth_config")
    async def auth_config(request: Request) -> JSONResponse:
        """Return public auth configuration (no auth required)."""
        result = _services_or_503(services_getter)
        if isinstance(result, JSONResponse):
            return result
        services = result

        from journal.api import _runtime_get

        reg = _runtime_get(services, "registration_enabled")
        return JSONResponse({"registration_enabled": reg})

    # ── POST /api/auth/forgot-password ─────────────────────────────────

    @mcp.custom_route(
        "/api/auth/forgot-password",
        methods=["POST"],
        name="api_auth_forgot_password",
    )
    async def auth_forgot_password(request: Request) -> JSONResponse:
        """Request a password-reset email. Always returns 200 to prevent email enumeration."""
        result = _services_or_503(services_getter)
        if isinstance(result, JSONResponse):
            return result
        services = result

        auth_service: AuthService = services["auth_service"]
        email_service: EmailService | None = services.get("email_service")
        config: Config = services["config"]
        user_repo: SQLiteUserRepository = services["user_repo"]

        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return JSONResponse(
                {"error": "invalid_body", "message": "Invalid JSON body"},
                status_code=400,
            )

        email = body.get("email", "").strip()
        if not email:
            # Still return 200 to avoid enumeration
            return JSONResponse({"message": "If that email exists, a reset link has been sent"})

        user = user_repo.get_user_by_email(email)
        if user and email_service is not None:
            try:
                token = auth_service.generate_reset_token(email)
                await email_service.send_password_reset_email(
                    email,
                    token,
                    config.app_base_url,
                )
                log.info("Password reset email sent to %s", email)
            except Exception:
                log.warning("Failed to send password reset email to %s", email, exc_info=True)
        elif user and email_service is None:
            log.warning("Email service not configured — cannot send reset email to %s", email)

        # Always 200 regardless of whether the user exists
        return JSONResponse({"message": "If that email exists, a reset link has been sent"})

    # ── GET /api/auth/verify-reset-token ───────────────────────────────

    @mcp.custom_route(
        "/api/auth/verify-reset-token",
        methods=["GET"],
        name="api_auth_verify_reset_token",
    )
    async def auth_verify_reset_token(request: Request) -> JSONResponse:
        """Check whether a password-reset token is still valid."""
        result = _services_or_503(services_getter)
        if isinstance(result, JSONResponse):
            return result
        services = result

        auth_service: AuthService = services["auth_service"]
        token = request.query_params.get("token", "")

        if not token:
            return JSONResponse(
                {"error": "missing_token", "message": "Token query parameter is required"},
                status_code=400,
            )

        try:
            auth_service.validate_reset_token(token)
        except ValueError as exc:
            return JSONResponse(
                {"error": "invalid_token", "message": str(exc)},
                status_code=400,
            )

        return JSONResponse({"valid": True})

    # ── POST /api/auth/reset-password ──────────────────────────────────

    @mcp.custom_route(
        "/api/auth/reset-password",
        methods=["POST"],
        name="api_auth_reset_password",
    )
    async def auth_reset_password(request: Request) -> JSONResponse:
        """Reset password using a valid reset token."""
        result = _services_or_503(services_getter)
        if isinstance(result, JSONResponse):
            return result
        services = result

        auth_service: AuthService = services["auth_service"]

        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return JSONResponse(
                {"error": "invalid_body", "message": "Invalid JSON body"},
                status_code=400,
            )

        token = body.get("token", "").strip()
        password = body.get("password", "")

        if not token or not password:
            return JSONResponse(
                {"error": "missing_fields", "message": "Token and password are required"},
                status_code=400,
            )

        if len(password) < 8 or len(password) > 1024:
            return JSONResponse(
                {
                    "error": "weak_password",
                    "message": "Password must be between 8 and 1024 characters",
                },
                status_code=400,
            )

        try:
            auth_service.reset_password(token, password)
        except ValueError as exc:
            return JSONResponse(
                {"error": "invalid_token", "message": str(exc)},
                status_code=400,
            )

        log.info("Password reset completed via token")
        return JSONResponse({"message": "Password has been reset successfully"})

    # ── GET /api/auth/verify-email ─────────────────────────────────────

    @mcp.custom_route(
        "/api/auth/verify-email",
        methods=["GET"],
        name="api_auth_verify_email",
    )
    async def auth_verify_email(request: Request) -> JSONResponse:
        """Verify a user's email address using a verification token."""
        result = _services_or_503(services_getter)
        if isinstance(result, JSONResponse):
            return result
        services = result

        auth_service: AuthService = services["auth_service"]
        token = request.query_params.get("token", "")

        if not token:
            return JSONResponse(
                {"error": "missing_token", "message": "Token query parameter is required"},
                status_code=400,
            )

        try:
            auth_service.verify_email(token)
        except ValueError as exc:
            return JSONResponse(
                {"error": "invalid_token", "message": str(exc)},
                status_code=400,
            )

        log.info("Email verified via token")
        return JSONResponse({"message": "Email verified successfully"})

    # ── POST /api/auth/resend-verification ─────────────────────────────

    @mcp.custom_route(
        "/api/auth/resend-verification",
        methods=["POST"],
        name="api_auth_resend_verification",
    )
    async def auth_resend_verification(request: Request) -> JSONResponse:
        """Resend the verification email for the currently authenticated user."""
        result = _services_or_503(services_getter)
        if isinstance(result, JSONResponse):
            return result
        services = result

        auth_service: AuthService = services["auth_service"]
        email_service: EmailService | None = services.get("email_service")
        config: Config = services["config"]

        user = get_authenticated_user(request)

        if user.email_verified:
            return JSONResponse({"message": "Email is already verified"})

        if email_service is not None:
            try:
                token = auth_service.generate_verification_token(user.email)
                await email_service.send_verification_email(
                    user.email,
                    token,
                    config.app_base_url,
                )
                log.info("Resent verification email to %s", user.email)
            except Exception:
                log.warning(
                    "Failed to resend verification email to %s",
                    user.email,
                    exc_info=True,
                )
                return JSONResponse(
                    {"error": "email_failed", "message": "Failed to send verification email"},
                    status_code=500,
                )
        else:
            log.warning(
                "Email service not configured — cannot resend verification to %s",
                user.email,
            )
            return JSONResponse(
                {"error": "email_not_configured", "message": "Email service is not configured"},
                status_code=500,
            )

        return JSONResponse({"message": "Verification email sent"})

    # ── POST /api/auth/api-keys ────────────────────────────────────────

    @mcp.custom_route(
        "/api/auth/api-keys",
        methods=["POST", "GET"],
        name="api_auth_api_keys",
    )
    async def auth_api_keys(request: Request) -> JSONResponse:
        """Create (POST) or list (GET) API keys for the authenticated user."""
        result = _services_or_503(services_getter)
        if isinstance(result, JSONResponse):
            return result
        services = result

        auth_service: AuthService = services["auth_service"]
        user = get_authenticated_user(request)

        if request.method == "POST":
            return await _create_api_key(request, auth_service, user.user_id)
        else:
            return _list_api_keys(auth_service, user.user_id)

    async def _create_api_key(
        request: Request,
        auth_service: AuthService,
        user_id: int,
    ) -> JSONResponse:
        """Handle POST /api/auth/api-keys — generate a new API key."""
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return JSONResponse(
                {"error": "invalid_body", "message": "Invalid JSON body"},
                status_code=400,
            )

        name = body.get("name", "").strip()
        if not name:
            return JSONResponse(
                {"error": "missing_fields", "message": "API key name is required"},
                status_code=400,
            )

        expires_days: int | None = body.get("expires_days")
        if expires_days is not None:
            try:
                expires_days = int(expires_days)
                if expires_days < 1:
                    raise ValueError("expires_days must be positive")
            except (TypeError, ValueError):
                return JSONResponse(
                    {
                        "error": "invalid_field",
                        "message": "expires_days must be a positive integer",
                    },
                    status_code=400,
                )

        full_key, key_info = auth_service.create_api_key(user_id, name, expires_days)

        response_data = _api_key_info_to_dict(key_info)
        response_data["key"] = full_key  # Full key shown exactly once

        log.info("Created API key '%s' for user %d", name, user_id)
        return JSONResponse(response_data, status_code=201)

    def _list_api_keys(auth_service: AuthService, user_id: int) -> JSONResponse:
        """Handle GET /api/auth/api-keys — list all API keys for the user."""
        keys = auth_service.list_api_keys(user_id)
        return JSONResponse({"items": [_api_key_info_to_dict(k) for k in keys]})

    # ── DELETE /api/auth/api-keys/{id} ─────────────────────────────────

    @mcp.custom_route(
        "/api/auth/api-keys/{key_id:int}",
        methods=["DELETE"],
        name="api_auth_api_key_revoke",
    )
    async def auth_api_key_revoke(request: Request) -> JSONResponse:
        """Revoke an API key owned by the authenticated user."""
        result = _services_or_503(services_getter)
        if isinstance(result, JSONResponse):
            return result
        services = result

        auth_service: AuthService = services["auth_service"]
        user = get_authenticated_user(request)
        key_id = int(request.path_params["key_id"])

        revoked = auth_service.revoke_api_key(key_id, user.user_id)
        if not revoked:
            return JSONResponse(
                {"error": "not_found", "message": "API key not found or already revoked"},
                status_code=404,
            )

        log.info("Revoked API key %d for user %d", key_id, user.user_id)
        return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Admin routes
# ---------------------------------------------------------------------------


def register_admin_routes(
    mcp: FastMCP,
    services_getter: Callable[[], dict | None],
) -> None:
    """Register admin-only REST API routes on the MCP server.

    Args:
        mcp: The FastMCP instance.
        services_getter: A callable returning the services dict (with
            ``auth_service``, ``user_repo``, ``config``).
    """

    # ── GET /api/admin/users ───────────────────────────────────────────

    @mcp.custom_route("/api/admin/users", methods=["GET"], name="api_admin_users_list")
    async def admin_list_users(request: Request) -> JSONResponse:
        """List all users with stats (admin only)."""
        result = _services_or_503(services_getter)
        if isinstance(result, JSONResponse):
            return result
        services = result

        user = get_authenticated_user(request)
        if not user.is_admin:
            return JSONResponse(
                {"error": "forbidden", "message": "Admin access required"},
                status_code=403,
            )

        user_repo: SQLiteUserRepository = services["user_repo"]
        stats = user_repo.get_user_stats()

        log.info("Admin user %d listed %d users", user.user_id, len(stats))
        return JSONResponse({"items": stats})

    # ── PATCH /api/admin/users/{id} ────────────────────────────────────

    @mcp.custom_route(
        "/api/admin/users/{user_id:int}",
        methods=["PATCH"],
        name="api_admin_user_update",
    )
    async def admin_update_user(request: Request) -> JSONResponse:
        """Update a user's admin/active flags (admin only)."""
        result = _services_or_503(services_getter)
        if isinstance(result, JSONResponse):
            return result
        services = result

        admin_user = get_authenticated_user(request)
        if not admin_user.is_admin:
            return JSONResponse(
                {"error": "forbidden", "message": "Admin access required"},
                status_code=403,
            )

        user_repo: SQLiteUserRepository = services["user_repo"]
        target_user_id = int(request.path_params["user_id"])

        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return JSONResponse(
                {"error": "invalid_body", "message": "Invalid JSON body"},
                status_code=400,
            )

        # Only allow updating is_active and is_admin
        allowed_fields: dict[str, type] = {"is_active": bool, "is_admin": bool}
        update_kwargs: dict[str, bool] = {}
        for field_name, field_type in allowed_fields.items():
            if field_name in body:
                value = body[field_name]
                if not isinstance(value, field_type):
                    return JSONResponse(
                        {
                            "error": "invalid_field",
                            "message": f"{field_name} must be a boolean",
                        },
                        status_code=400,
                    )
                update_kwargs[field_name] = value

        if not update_kwargs:
            return JSONResponse(
                {
                    "error": "missing_fields",
                    "message": "At least one of is_active or is_admin is required",
                },
                status_code=400,
            )

        updated_user = user_repo.update_user(target_user_id, **update_kwargs)
        if updated_user is None:
            return JSONResponse(
                {"error": "not_found", "message": "User not found"},
                status_code=404,
            )

        log.info(
            "Admin user %d updated user %d: %s",
            admin_user.user_id,
            target_user_id,
            update_kwargs,
        )
        return JSONResponse({"user": _user_to_dict(updated_user)})
