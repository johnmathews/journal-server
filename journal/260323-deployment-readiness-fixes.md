# Deployment Readiness Fixes

Assessed whether the journal tool was ready to deploy onto the media VM's existing
Docker Compose stack. Found and fixed several blockers.

## Issues Found

### Critical: MCP server startup crash
`mcp_server.py:main()` passed `host` and `port` as kwargs to `FastMCP.run()`, but
the MCP SDK (>=1.26) only accepts `transport` there. Host/port must be set on
`mcp.settings` before calling `run()`. This would have been a TypeError on first
startup — never caught because `main()` has 0% test coverage (it's an integration
entry point).

### Critical: Port 8000 conflict
Gluetun already binds 8000:8000/tcp for its control server on the media VM. Changed
the journal MCP server to port 8400, ChromaDB external port to 8401.

### High: Volume and integration conventions
The original docker-compose.yml used named volumes and `build: .`. Rewrote it to:
- Pull from `ghcr.io/johnmathews/journal-server:latest`
- Bind mounts to `/srv/media/config/journal/` (matches existing stack convention)
- Container names, restart policy, TZ — all matching existing services

## Decisions
- Kept the docker-compose.yml in the repo as the production-ready definition.
  The services are designed to paste into the Ansible-templated compose file.
- Did not change the default MCP_PORT in config.py (still 8000) — the override
  happens via environment variable in docker-compose.yml. This keeps local dev
  on the standard port.
