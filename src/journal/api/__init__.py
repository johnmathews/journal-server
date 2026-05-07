"""REST API endpoints for the journal webapp.

Public entry point: ``register_api_routes(mcp, services_getter)``. It calls
each per-resource ``register_*_routes`` function in sequence.

Routes are organised by resource module under ``journal/api/`` — see
``_shared.py``'s docstring for the routing rules (default = primary URL
resource; override = ingestion.py for write/job-creation routes).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

# Re-exported for external callers (auth_api.py, cli.py) that imported these
# helpers directly from the old single-file api module. New code should import
# from journal.api._shared instead.
from journal.api._shared import _convert_heic_to_jpeg, _runtime_get
from journal.api.dashboard import register_dashboard_routes
from journal.api.entities import register_entities_routes
from journal.api.entries import register_entries_routes
from journal.api.health import register_health_routes
from journal.api.ingestion import register_ingestion_routes
from journal.api.jobs import register_jobs_routes
from journal.api.notifications import register_notifications_routes
from journal.api.search import register_search_routes
from journal.api.settings import register_settings_routes
from journal.api.users import register_users_routes

if TYPE_CHECKING:
    from collections.abc import Callable

    from mcp.server.fastmcp import FastMCP


def register_api_routes(
    mcp: FastMCP,
    services_getter: Callable[[], dict | None],
) -> None:
    """Register REST API routes on the MCP server.

    Args:
        mcp: The FastMCP instance.
        services_getter: A callable that returns the services dict
            (with 'query' and 'ingestion' keys).
    """
    register_entries_routes(mcp, services_getter)
    register_ingestion_routes(mcp, services_getter)
    register_settings_routes(mcp, services_getter)
    register_users_routes(mcp, services_getter)
    register_notifications_routes(mcp, services_getter)
    register_health_routes(mcp, services_getter)
    register_dashboard_routes(mcp, services_getter)
    register_search_routes(mcp, services_getter)
    register_jobs_routes(mcp, services_getter)
    register_entities_routes(mcp, services_getter)


__all__ = [
    "_convert_heic_to_jpeg",
    "_runtime_get",
    "register_api_routes",
]
