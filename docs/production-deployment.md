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

### Optional secret: `FITNESS_CREDENTIAL_KEY`

Enables encrypted Garmin credential persistence (one-click reconnect + unattended
re-login — [`fitness-operations.md` §6](./fitness-operations.md#6-saved-credentials--unattended-re-login)).
Like the other optional secrets (`SMTP_PASSWORD`, `PUSHOVER_*`), the value lives in
`/srv/media/.env` and is injected via a `${VAR}` interpolation line in the
`journal-server` `environment:` block. To enable it:

1. Generate a key:
   `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
2. Add `FITNESS_CREDENTIAL_KEY=<key>` to `/srv/media/.env`.
3. Add `- FITNESS_CREDENTIAL_KEY=${FITNESS_CREDENTIAL_KEY}` to the `journal-server`
   `environment:` block in `/srv/media/docker-compose.yml`, then re-sync the
   [versioned mirror](../deploy/docker-compose.prod.yml).
4. `docker compose up -d journal-server`.

Unset = feature off (the server runs fine without it). A malformed value fails startup
fast with the generation command in the error. Rotating or losing the key is safe but
degrades saved credentials to unusable until each user reconnects once — see
[`configuration.md`](./configuration.md#optional--fitness-integration) for full key
lifecycle semantics. Treat the key like the other `.env` secrets: it decrypts the Garmin
passwords stored in `journal.db`, so key + DB backup together reveal them.

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

## Deploy runbook (releasing a new build)

Use this whenever a change has merged to `main` and needs to reach the running stack.
Deploying is **pull + up**; migrations apply themselves. The steps below add the checks
that turn "it pulled" into "it's actually serving the new code with the schema it expects".

### Do migrations or backfills need to run?

**Schema migrations: automatic, no operator step.** `journal-server` calls `run_migrations`
on startup (`src/journal/mcp_server/bootstrap.py` → `src/journal/db/migrations.py`). The
runner compares each `src/journal/db/migrations/NNNN_*.sql` file's number against
`PRAGMA user_version` on `/data/journal.db`, executes every file with a higher number in
order via `executescript`, and bumps `user_version`. So the moment the new
`journal-server` container starts (`docker compose up -d`), any pending migrations run
against the bind-mounted DB. There is **no separate `migrate` command** and no manual step.

**Backfills: check per release, but they are usually migrations too.** Data backfills in
this project are written as ordinary `INSERT OR IGNORE` / `UPDATE` migration files (e.g.
`0035_pricing_backfill.sql`), so they run through the same automatic path — no standalone
script. Before deploying, decide whether the release needs an *out-of-band* backfill (a
one-off script over historical rows that a migration can't express) by scanning the diff:

```bash
# From a clean checkout of the release commit:
git diff --stat <previous-deployed-sha>..main -- src/journal/db/migrations/   # new schema/backfill migrations
git diff --stat <previous-deployed-sha>..main -- scripts/ bin/                # any standalone backfill/one-off scripts
```

If `migrations/` shows new files, they apply automatically. If `scripts/`/`bin/` gained a
backfill you were told to run, run it *after* `up -d` (so the schema exists) via
`docker exec journal-server python3 -m <module>` and record it below. Forward-only columns
(new nullable columns that populate for *new* rows only, like per-job `input_tokens` /
`output_tokens` / `cost_usd` from `0034`) need **no** historical backfill — old rows stay
NULL by design.

### Pre-deploy checks

1. **CI is green on `main`** for the commit you're shipping (both `test` and
   `integration-test`, plus `build-and-push` so the `:latest` image exists):
   ```bash
   gh run list --repo johnmathews/journal-server --branch main --limit 1 \
     --json headSha,conclusion,status
   ```
2. **Note the currently-deployed version** so you can verify the bump and roll back if
   needed:
   ```bash
   ssh media 'docker inspect --format "{{index .Config.Labels \"org.opencontainers.image.revision\"}}" journal-server'
   ssh media 'docker exec journal-server python3 -c "import sqlite3;print(sqlite3.connect(\"/data/journal.db\").execute(\"PRAGMA user_version\").fetchone()[0])"'
   ```
3. **Back up the DB** before a release that carries migrations (cheap insurance; the whole
   state dir is the backup target — see [Backup target](#backup-target)):
   ```bash
   ssh media 'cp /srv/media/config/journal/data/journal.db /srv/media/config/journal/data/journal.db.pre-deploy-$(date +%Y%m%d)'
   ```

### Deploy

```bash
ssh media 'cd /srv/media && docker compose pull journal-server journal-webapp journal-chromadb && docker compose up -d'
```

Only the containers whose image digest changed are recreated; `up -d` is a no-op for the
rest. Migrations run during `journal-server` startup, inside this step.

### Post-deploy verification

1. **Containers healthy:** `ssh media 'cd /srv/media && docker compose ps'` — all journal
   services `Up`.
2. **Migrations applied:** `PRAGMA user_version` now equals the highest migration number
   shipped (re-run the command from pre-deploy step 2). If it didn't advance, the new
   migrations did **not** run — check the startup logs.
3. **No startup errors:** `ssh media 'docker logs --since 5m journal-server 2>&1 | grep -iE "error|traceback|migrat"'` —
   expect the `SQLite connected and migrated` line and no tracebacks.
4. **API up:** `ssh media 'curl -fsS localhost:8400/api/health'` (or the app's health route).
5. **Smoke-test the shipped change** end-to-end, not just the process being up. (E.g. for the
   jobs throughput/observability release: load `/jobs`, confirm running jobs show the live
   spinner + counting duration and that In/Out/Cost columns render.)

### Rollback

Images are `:latest`, so rollback means pinning to the previous digest, not re-pulling
`:latest` (which would fetch the same bad build):

```bash
# Find the previous image digest (from step 2's revision, or `docker images --digests`),
# then in /srv/media/docker-compose.yml pin the image to that digest:
#   image: ghcr.io/johnmathews/journal-server@sha256:<previous-digest>
ssh media 'cd /srv/media && docker compose up -d journal-server'
```

**Schema caveat:** the migrations in this project are additive and nullable, so an older
image running against a newer schema is safe — it simply ignores columns it doesn't know
about. There are no down-migrations. If a release ever ships a *destructive* migration
(dropped/renamed column, narrowed type), that safety no longer holds and the DB backup from
pre-deploy step 3 becomes the rollback path — restore it and redeploy the old image.

## Public exposure

There is no reverse proxy on `media`. The webapp is exposed on `:8402` to the LAN only.

Public exposure is via a Cloudflare Tunnel running on a separate tailnet host
(`100.117.104.102`, hostname `cloudflared`), which fronts `media:8402`. The cloudflared
config and credentials live on that host — not on `media` — so an outage of the
cloudflared host takes down public access even when `media` itself is healthy.

**Canonical public hostname:** `https://journal.itsa-pizza.com`. The webapp is served
same-origin (relative `/api`), so the only places the public hostname appears are the
Cloudflare tunnel ingress and four env vars in `/srv/media/.env` (see below).

### Renaming the public hostname

The app hardcodes no domain — a rename is Cloudflare + `.env` + Strava, in this order:

1. **Cloudflare** (Zero Trust → Networks → Tunnels → the tunnel → Public Hostnames):
   point the new hostname at `http://media:8402`. This auto-creates the proxied DNS
   record in the (already-onboarded) Cloudflare zone. Remove the old public-hostname
   route after cutover.
2. **`/srv/media/.env`** — set all four to the new origin, then redeploy:
   - `APP_BASE_URL=https://journal.itsa-pizza.com` (email verification / reset links)
   - `API_CORS_ORIGINS=https://journal.itsa-pizza.com` (REST CORS allow-list)
   - `MCP_ALLOWED_HOSTS=…,journal.itsa-pizza.com` (Host-header / DNS-rebinding allow-list)
   - `STRAVA_REDIRECT_URI=https://journal.itsa-pizza.com/strava/callback` (OAuth return)

   ```bash
   ssh media && cd /srv/media
   # edit .env (the four vars above)
   docker compose up -d journal-server journal-webapp
   ```
3. **Strava** (developer dashboard → the app → *Authorization Callback Domain*): set it to
   `journal.itsa-pizza.com` (Strava stores only the bare domain). Without this the SPA
   connect flow returns a `redirect_uri` mismatch.
4. **Verify:** load the new host; a fresh register/reset email links to the new domain; run
   the Strava connect flow end-to-end (Settings · Fitness → Connect Strava → returns to
   `/strava/callback` → connected).

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
