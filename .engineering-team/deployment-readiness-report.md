# Deployment Readiness Report — Media VM

**Date:** 2026-03-23
**Target:** Existing Docker Compose stack on media VM (Ansible-templated)

## Verdict: NOT READY — 2 critical issues, 2 high issues

The code is well-structured and tests pass (76/76, 68% coverage), but deploying
as-is onto the media VM would fail at runtime due to a bug in the MCP server
startup code and a port conflict with Gluetun.

---

## Critical Issues

### 1. MCP server crashes on startup [BUG]

**File:** `src/journal/mcp_server.py:315`

```python
mcp.run(transport="streamable-http", host=config.mcp_host, port=config.mcp_port)
```

`FastMCP.run()` does NOT accept `host` or `port` kwargs. This raises
`TypeError: FastMCP.run() got an unexpected keyword argument 'host'` at runtime.
Host/port must be set on the `FastMCP` constructor or via `mcp.settings`.

**Fix:** Set host/port on settings before calling run:
```python
def main() -> None:
    config = load_config()
    mcp.settings.host = config.mcp_host
    mcp.settings.port = config.mcp_port
    mcp.run(transport="streamable-http")
```

### 2. Port 8000 conflict with Gluetun

Gluetun already binds `8000:8000/tcp` for its control server. The journal MCP
server defaults to port 8000 and the docker-compose maps `8000:8000`.

**Fix:** Use a different port (e.g., 8400 for MCP server, 8401 for ChromaDB).

---

## High Priority Issues

### 3. Volume convention mismatch

The journal docker-compose uses named volumes (`journal_data`, `chroma_data`).
The existing stack uses bind mounts to `/srv/media/config/<service>/`.

**Fix:** Use bind mounts:
- `/srv/media/config/journal/data:/data` (SQLite DB)
- `/srv/media/config/journal/chromadb:/data` (ChromaDB data)

### 4. Cannot integrate as-is into existing compose

The project's `docker-compose.yml`:
- Uses `build: .` (should pull from `ghcr.io/johnmathews/journal-agent`)
- Is a standalone file (needs to merge into the Ansible-templated compose)
- Uses different conventions (no PUID/PGID, no TZ, no restart policy)

**Fix:** Create service definitions that match the existing stack's patterns.

---

## What's Working Fine

- Docker image builds successfully
- `chromadb-client` package version 1.5.x is correct and matches server 1.5.5
- ChromaDB healthcheck endpoint (`/api/v2/heartbeat`) is valid
- SQLite with WAL mode, FTS5, and migrations — solid
- Protocol-based architecture allows easy testing with mocks
- All 76 tests pass, 68% coverage (above 65% threshold)
- CI workflow correctly builds and pushes to `ghcr.io/johnmathews/journal-agent`

---

## Service Definitions for Media VM Compose

```yaml
  journal:
    image: ghcr.io/johnmathews/journal-agent:latest
    container_name: journal
    environment:
      - TZ=${TZ}
      - DB_PATH=/data/journal.db
      - CHROMADB_HOST=journal-chromadb
      - CHROMADB_PORT=8000
      - MCP_HOST=0.0.0.0
      - MCP_PORT=8400
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
      - OPENAI_API_KEY=${OPENAI_API_KEY}
    volumes:
      - /srv/media/config/journal/data:/data
    ports:
      - "8400:8400"
    depends_on:
      journal-chromadb:
        condition: service_healthy
    restart: always

  journal-chromadb:
    image: chromadb/chroma:1.5.5
    container_name: journal-chromadb
    environment:
      - TZ=${TZ}
    volumes:
      - /srv/media/config/journal/chromadb:/data
    ports:
      - "8401:8000"
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/api/v2/heartbeat"]
      interval: 30s
      timeout: 10s
      retries: 3
    restart: always
```

Note: The MCP endpoint will be at `http://<media-vm-ip>:8400/mcp`
