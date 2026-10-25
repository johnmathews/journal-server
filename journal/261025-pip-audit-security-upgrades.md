# Clearing 60 pip-audit findings so journal-server can publish again

**Date:** 2026-10-25
**Branch:** `eng-deps-security-upgrade` → fixes #89

## What broke

The `pip-audit` step in the `test` job started failing with **60 known
vulnerabilities in 7 packages**. It first showed up on PR #88 (pinning
Actions to SHAs), which changed nothing in the dependency set. `main` had
last built green on 2026-09-07, and every one of these advisories was
published after that. The same blind spot recorded in
`260907-gitignore-tool-caches.md` (and filed as #80) applies: the audit only
runs on PRs and pushes to `main`, so nothing surfaced it until a PR was
opened.

The consequence was larger than a red badge. `build-and-push` `needs` both
test jobs, so no image could be published at all. Since 2026-10-25 What's Up
Docker on `media` redeploys `journal-server` within about a minute of a new
`latest`, so the only route to production was blocked, and production kept
running the vulnerable versions.

## The findings and the upgrades

Each package went to the smallest version that clears all of its advisories,
via `uv lock --upgrade-package <pkg>==<version>`:

| Package | From | To | Advisories |
|---|---|---|---|
| anyio | 4.13.0 | 4.14.2 | 2 |
| certifi | 2026.2.25 | 2026.10.19 | 1 |
| mcp | 1.28.1 | 1.30.0 | 1 |
| opentelemetry-api | 1.40.0 | 1.45.1 | 1 |
| pillow | 12.3.0 | 13.0.0 | 37 |
| pyjwt | 2.13.0 | 2.15.0 | 15 |
| urllib3 | 2.7.0 | 2.8.0 | 3 |

`mcp` stayed on 1.x (1.30.0 rather than 2.2.0). `pyproject.toml` pins `<2`,
and the 2.x move is its own Dependabot PR (#82) with its own API surface to
check.

`pyjwt` needed 2.15.0, not 2.14.0: two advisories (PYSEC-2026-4141,
PYSEC-2026-4183) list 2.15.0 as their first fix. One more, PYSEC-2026-4146
(GHSA-gvp8-978c-rx2q, `decode()` mutating a caller's `options` dict), shows
**no fix version** in pip-audit's table. That looked like a stop condition,
but the OSV record's range is `introduced: 2.11.0, last_affected: 2.13.0`, so
2.14.0 and later are outside it, and the post-upgrade audit agrees. Worth
knowing: an empty "Fix Versions" cell in pip-audit can mean "the advisory uses
`last_affected` instead of `fixed`", not "unfixed". Check OSV before
concluding there is no fix. This codebase does not call `jwt.decode` itself;
pyjwt arrives through `mcp[crypto]`.

### What the resolver moved on its own

- The OpenTelemetry family moves in lockstep with `opentelemetry-api`:
  `opentelemetry-sdk`, `-proto`, `-exporter-otlp-proto-common` and
  `-exporter-otlp-proto-grpc` 1.40.0 → 1.45.1, `-semantic-conventions`
  0.61b0 → 0.66b1, and a new `opentelemetry-exporter-otlp-common` 0.66b1.
  All of it is pulled in by `chromadb-client`; this app does not use
  OpenTelemetry directly.
- `importlib-metadata` 8.7.1 and its dependency `zipp` 3.23.0 dropped out,
  because newer `opentelemetry-api` no longer requires them.

Nothing else changed in `uv.lock`; `pillow-heif` stays at 1.6.0 (it declares
`pillow>=11.1.0` with no ceiling).

## Pillow 13

Pillow was the only fix outside the declared range: `pyproject.toml` said
`Pillow>=12.2,<13`. It is now `>=13.0,<14`. The floor moved too, so the lock
cannot fall back onto a 12.x that carries the 37 advisories.

The 13.0.0 backwards-incompatible changes were checked against every Pillow
call in `src/`: `services/preprocessing.py` (`Image.open`,
`ImageOps.exif_transpose`, `convert`, `ImageOps.invert`, `point`, `getbbox`,
`crop`, `resize(..., Image.LANCZOS)`, `ImageOps.autocontrast`, JPEG `save`)
and `api/_shared.py` (HEIC → JPEG via `pillow_heif.register_heif_opener()`).
None of the removals (Python 3.10, `Image._show`, `get_child_images` on
`Image`, `ImageCms` product fields, `ExifTags.IFD.Makernote`, clipping when
saving `I` mode as PNG) or behaviour changes (premultiplied `getcolor`,
duplicate TIFF tags, small ICO sizes) touch those paths. No source change
was needed.

One test change: `tests/test_services/test_preprocessing.py` called
`Image.getdata()`, deprecated since Pillow 12.1 and removed in Pillow 14. It
now uses `get_flattened_data()`, which returns the same pixel values as a
tuple. The new `>=13.0` floor guarantees the method exists. Not a breakage
today, but it is the one Pillow deprecation warning the suite raised.

## Verification

- `pip-audit`, reproduced with CI's exact flags (`uv export --no-emit-project
  --format requirements-txt`, then `uvx pip-audit -r ... --disable-pip`):
  60 findings before, **"No known vulnerabilities found"** after.
- `uv run ruff check src/ tests/`: clean.
- `uv run pytest -m "not integration" --cov`: **3270 passed, 11 deselected**,
  coverage 88.27%. The same count as the 2026-09-07 run.
- Integration suite against a throwaway `chromadb/chroma:latest` container
  (CI's image, `CHROMADB_PORT` pointed at it): **11 passed**.
- `docker build` of the production `Dockerfile` (native arm64), then an import
  of `PIL`, `pillow_heif` (opener registered), `jwt`, `mcp` and
  `journal.mcp_server` inside the image: all clean on Pillow 13.0.0. CI only
  builds the image after merge, so this was the one pre-merge check of it.

No migration files changed, so this release runs no schema migration on
start.

## Docs

`docs/production-deployment.md` still said images had "no auto-update" and
suggested Watchtower as future work. That has been wrong since 2026-10-25:
WUD redeploys `journal-server` and `journal-webapp` (not `journal-chromadb`)
within about a minute of a new `latest`. The update-workflow section, the
deploy-runbook intro, the known-fragilities bullet and the deployment
paragraph in `docs/architecture.md` now say so, and that a green merge to
`main` is a production deploy.
