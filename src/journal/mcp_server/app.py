"""FastMCP instance and REST route registrations.

`mcp` is the single FastMCP application — every `@mcp.tool()` in the
`tools/` package and every `register_*_routes(mcp, ...)` call below
attaches to this instance. Tools modules import `mcp` from here.

Importing this module has the side effect of registering the three
REST route groups (api, auth, admin) against `mcp`. The `_services`
lambda resolves lazily at request time, so the dict can stay empty
until `bootstrap._init_services()` runs.
"""

from mcp.server.fastmcp import FastMCP

from journal.api import register_api_routes
from journal.auth_api import register_admin_routes, register_auth_routes
from journal.mcp_server import bootstrap

mcp = FastMCP("journal", lifespan=bootstrap.lifespan)

# Register REST API routes — they access the shared services dict
# through a lambda so the registration sees a freshly populated dict
# at request time, even though `_services` is still None at import.
register_api_routes(mcp, lambda: bootstrap._services)
register_auth_routes(mcp, lambda: bootstrap._services)
register_admin_routes(mcp, lambda: bootstrap._services)
