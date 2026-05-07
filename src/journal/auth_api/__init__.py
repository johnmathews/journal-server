"""Auth and admin REST API endpoints package.

Facade that re-exports the route registration functions. The package is the
result of carving the original ``src/journal/auth_api.py`` (840 lines) into
focused per-cluster modules (see ``docs/refactor-item-6-exceptions-plan.md``
§ Item 3). During the in-progress split this facade points at ``_legacy.py``;
once the carve completes, the re-exports will point at the per-cluster
modules and ``_legacy.py`` will be removed.
"""

from journal.auth_api._legacy import (
    _api_key_info_to_dict,
    _user_to_dict,
    register_admin_routes,
    register_auth_routes,
)

__all__ = [
    "_api_key_info_to_dict",
    "_user_to_dict",
    "register_admin_routes",
    "register_auth_routes",
]
