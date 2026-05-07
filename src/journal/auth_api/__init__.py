"""Auth and admin REST API endpoints package.

The package was carved from the original ``src/journal/auth_api.py`` (840
lines) per ``docs/refactor-item-6-exceptions-plan.md`` § Item 3. Routes are
grouped by concern:

- ``core.py`` — session + current-user reads (login, logout, GET /me, the
  public auth-config flag).
- ``account.py`` — account lifecycle (register, email verification, password
  reset).
- ``profile.py`` — profile mutations (PATCH /me).
- ``api_keys.py`` — API key CRUD.
- ``admin.py`` — admin user-management + dynamic-reload endpoints.
- ``_shared.py`` — helpers (``_services_or_503``, ``_user_to_dict``,
  ``_api_key_info_to_dict``).

The package re-exports ``register_auth_routes`` and ``register_admin_routes``
so callers (notably ``mcp_server/app.py``) need not change. The two
``_*_to_dict`` helpers are also re-exported because
``tests/test_auth_api.py`` imports them directly from
``journal.auth_api``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from journal.auth_api._shared import _api_key_info_to_dict, _user_to_dict
from journal.auth_api.account import register_account_routes
from journal.auth_api.admin import register_admin_routes
from journal.auth_api.api_keys import register_api_keys_routes
from journal.auth_api.core import register_core_routes
from journal.auth_api.profile import register_profile_routes

if TYPE_CHECKING:
    from collections.abc import Callable

    from mcp.server.fastmcp import FastMCP


def register_auth_routes(
    mcp: FastMCP,
    services_getter: Callable[[], dict | None],
) -> None:
    """Register user-facing authentication routes on the MCP server.

    Composes the four user-facing cluster registrations: core (session +
    current user), account (lifecycle), profile (mutations), api_keys (CRUD).
    Admin routes register through ``register_admin_routes`` instead.
    """
    register_core_routes(mcp, services_getter)
    register_account_routes(mcp, services_getter)
    register_profile_routes(mcp, services_getter)
    register_api_keys_routes(mcp, services_getter)


__all__ = [
    "_api_key_info_to_dict",
    "_user_to_dict",
    "register_admin_routes",
    "register_auth_routes",
]
