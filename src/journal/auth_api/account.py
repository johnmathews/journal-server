"""Account lifecycle routes: register, email verify, password reset.

Six routes covering account creation and credential recovery:

- ``POST /api/auth/register`` — create a new user, issue session, send verification.
- ``GET  /api/auth/verify-email`` — confirm an email-verification token.
- ``POST /api/auth/forgot-password`` — request a password-reset email.
- ``GET  /api/auth/verify-reset-token`` — check whether a reset token is still valid.
- ``POST /api/auth/reset-password`` — reset password using a valid reset token.
- ``POST /api/auth/resend-verification`` — resend the verification email for the current user.

All email dispatch is best-effort; failures log but never break the request
flow. The registration route checks the runtime ``registration_enabled``
flag and returns 403 when disabled.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from starlette.responses import JSONResponse

from journal.api import _runtime_get
from journal.auth import get_authenticated_user, set_session_cookie
from journal.auth_api._shared import _services_or_503, _user_to_dict

if TYPE_CHECKING:
    from collections.abc import Callable

    from mcp.server.fastmcp import FastMCP
    from starlette.requests import Request

    from journal.config import Config
    from journal.db.user_repository import SQLiteUserRepository
    from journal.services.auth import AuthService
    from journal.services.email import EmailService

log = logging.getLogger(__name__)


def register_account_routes(
    mcp: FastMCP,
    services_getter: Callable[[], dict | None],
) -> None:
    """Register account-lifecycle routes on the MCP server."""

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
            # The specific reason (e.g. duplicate email) stays in the
            # log only — echoing it to the client would let anonymous
            # callers enumerate registered addresses.
            log.info("Registration failed for %s: %s", email, exc)
            return JSONResponse(
                {
                    "error": "registration_failed",
                    "message": "Unable to register with the provided details",
                },
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
