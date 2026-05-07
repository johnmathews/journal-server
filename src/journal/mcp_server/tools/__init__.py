"""MCP tool registrations.

Importing the submodules has the side effect of running every
`@mcp.tool()` decorator, which registers the tool with the shared
`mcp` instance from `journal.mcp_server.app`. The package's own
`__init__.py` imports symbols from each tool module, which triggers
that registration.
"""
