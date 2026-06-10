# Production Deployment

## Overview

The Journal Analysis Tool runs on the `media` VM as part of a multi-service docker-compose
stack alongside other self-hosted services (sonarr, radarr, qbittorrent, cadvisor, alloy).
There is no dedicated host for the journal — it shares the VM with the rest of the home
media stack, which keeps operations simple but means an unrelated container outage on the
host can affect the journal.

The application's runtime state (SQLite DB, ChromaDB volume, OCR context files,
mood-dimensions config) lives entirely under bind mounts from the host, so the containers
themselves are stateless and can be re-pulled at any time.

## Compose layout

- **Compose file:** `/srv/media/docker-compose.yml`
- **Environment file:** `/srv/media/.env`
- **Project name:** `media`
- **Versioned mirror (this repo):** [`deploy/docker-compose.prod.yml`](../deploy/docker-compose.prod.yml)
  — the three journal services extracted verbatim from the VM file, with a
  last-synced date in its header. Re-sync the mirror whenever the journal
  section of the VM file changes; the journal stack is reproducible from this
  repo + `/srv/media/.env` alone.

All three journal services are containers within this single compose project — they aren't
in their own stack. Compose subcommands run from `/srv/media` operate on the whole VM, so
when targeting only the journal services, name them explicitly (see
[Operational commands](#operational-commands)).

## Containers

| Container | Image | Port (host:container) | Restart |
|---|---|---|---|
| `journal-server` | `ghcr.io/johnmathews/journal-server:latest` | `8400:8400` | `always` |
| `journal-webapp` | `ghcr.io/johnmathews/journal-webapp:latest` | `8402:80` | `unless-stopped` |
| `journal-chromadb` | `ghcr.io/johnmathews/journal-chromadb:latest` (custom image, not upstream `chromadb/chroma`) | `8401:8000` | (compose default) |

The `journal-chromadb` image is a custom build defined by `Dockerfile.chromadb` in the
server repo, not the upstream `chromadb/chroma` image. A future ChromaDB version bump
therefore requires a coordinated rebuild and push.

## Bind mounts

All persistent data is bind-mounted from `/srv/media/config/journal/` on the host:

| Host path | Container | Container path |
|---|---|---|
| `/srv/media/config/journal/data` | `journal-server` | `/data` (SQLite DB lives here as `journal.db`) |
| `/srv/media/config/journal/context` | `journal-server` | `/app/context` (OCR context files) |
| `/srv/media/config/journal/mood-dimensions.toml` | `journal-server` | `/app/config/mood-dimensions.toml` (read-only) |
| `/srv/media/config/journal/chromadb` | `journal-chromadb` | `/data` |

`mood-dimensions.toml` is a single-file mount — edits on the host are picked up by the
in-process hot reload (`POST /api/admin/reload/mood_dimensions`) without a container
restart.

## Image source and update workflow

Images are built and pushed to `ghcr.io/johnmathews/journal-{server,webapp,chromadb}` by
GitHub Actions on push to `main` (see each repo's `.github/workflows/`).

**There is no auto-update.** The operator updates the stack manually:

```bash
ssh media
cd /srv/media
docker compose pull journal-server journal-webapp journal-chromadb
docker compose up -d
```

This is a known fragility — image tags are pinned to `:latest`, so an upstream regression
lands on the host the next time `pull` is run. Pinning to image SHAs in the compose file,
or running Watchtower with a label allowlist scoped to the journal containers, would be a
meaningful robustness improvement. Out of scope for now; flagged here so future work has
context.

## Public exposure

There is no reverse proxy on `media`. The webapp is exposed on `:8402` to the LAN only.

Public exposure is via a Cloudflare Tunnel running on a separate tailnet host
(`100.117.104.102`, hostname `cloudflared`), which fronts `media:8402`. The cloudflared
config and credentials live on that host — not on `media` — so an outage of the
cloudflared host takes down public access even when `media` itself is healthy.

## Operational commands

Runbook-style snippets. All assume the operator has SSH access to `media` and Docker
permissions.

```bash
# Check stack status
ssh media 'cd /srv/media && docker compose ps'

# Tail server logs
ssh media 'docker logs -f --tail 200 journal-server'

# Recent errors only
ssh media 'docker logs --since 1h journal-server 2>&1 | grep -i error'

# Restart server
ssh media 'cd /srv/media && docker compose restart journal-server'

# Read-only DB query (no sqlite3 binary in container — use python3 -c)
ssh media 'docker exec journal-server python3 -c "
import sqlite3
c = sqlite3.connect(\"/data/journal.db\")
for row in c.execute(\"SELECT id, canonical_name FROM entities LIMIT 5\"):
    print(row)
"'

# Pull and restart all journal services
ssh media 'cd /srv/media && docker compose pull journal-server journal-webapp journal-chromadb && docker compose up -d'
```

## Backup target

The single directory `/srv/media/config/journal/` covers:

- The SQLite database (`data/journal.db`)
- The ChromaDB volume (`chromadb/`)
- OCR context files (`context/`)
- Mood-dimensions config (`mood-dimensions.toml`)

Backing up this directory captures the full application state. The container images
themselves are reproducible from `ghcr.io` and don't need to be backed up.

## Known fragilities

- ~~**The compose file exists only on the VM.**~~ Resolved 2026-06-10: the journal
  services are mirrored at [`deploy/docker-compose.prod.yml`](../deploy/docker-compose.prod.yml)
  with a sync-provenance header. Residual risk: the mirror goes stale if VM-side
  edits aren't re-synced — check the header's last-synced date when in doubt.
- **All images pinned to `:latest`, no auto-update.** A bad release on `main` becomes
  the operator's problem the next time they run `docker compose pull`. Mitigation:
  pin to SHAs, or add Watchtower with a label allowlist.
- **Custom `journal-chromadb` image.** It is built locally from `Dockerfile.chromadb` and
  pushed to `ghcr.io/johnmathews/journal-chromadb`, not pulled from upstream
  `chromadb/chroma`. A ChromaDB version bump requires a coordinated rebuild.
- **No `sqlite3` binary inside `journal-server`.** Quick DB queries from outside the
  container have to go through `python3 -c '...'` (see [Operational commands](#operational-commands)).
- **Cloudflared lives on a separate host.** An outage of the cloudflared host takes down
  public access to the journal even when `media` is healthy. The two hosts are not
  monitored as a single unit.
- **Single-VM deployment.** All three journal services share `media` with unrelated
  containers (sonarr, radarr, qbittorrent, cadvisor, alloy). A noisy neighbour can
  affect journal performance; an unrelated kernel panic or VM reboot takes the journal
  down with it.

## Related docs

- [`development.md`](./development.md) — local dev setup, including the local full-stack
  quickstart that mirrors the production layout.
- [`architecture.md`](./architecture.md) — application architecture, which is the same
  in production and dev (the deployment surface is just the container packaging).
- [`configuration.md`](./configuration.md) — environment variables consumed by
  `journal-server`, all of which are read from `/srv/media/.env` in production.
