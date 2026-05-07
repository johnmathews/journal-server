# Unit 1a — `api.py` mechanical split

Date: 2026-05-07

## What landed

`src/journal/api.py` (3170 lines, 54 routes) became `src/journal/api/`
(11 files, ~3600 lines including the docstrings the new modules each grew).
The public surface (`from journal.api import register_api_routes`) is
unchanged. Two helpers consumed externally — `_convert_heic_to_jpeg`
(by `cli.py`) and `_runtime_get` (by `auth_api.py`) — are re-exported
from `journal/api/__init__.py` so those import sites keep working
without modification. 1769 tests pass throughout; each commit ran the
full suite.

Final layout:

| File | Lines | Routes |
|---|---:|---:|
| `__init__.py` | 63 | orchestrator |
| `_shared.py` | 245 | helpers + routing-rule docstring |
| `entries.py` | 506 | 6 |
| `ingestion.py` | 591 | 6 |
| `entities.py` | 717 | 16 |
| `dashboard.py` | 609 | 9 |
| `settings.py` | 241 | 5 |
| `search.py` | 152 | 1 |
| `notifications.py` | 133 | 4 |
| `health.py` | 160 | 3 |
| `users.py` | 97 | 2 |
| `jobs.py` | 113 | 2 |

## Decisions worth remembering

1. **Routing rules — codified, two-rule.** Default = primary URL
   resource. Override = ingestion.py for write/job-creation routes,
   regardless of URL prefix. Both rules are written into
   `code-quality-principles.md` and the `_shared.py` / `ingestion.py`
   module docstrings, so a future agent encounters the rule on first
   read of any module in the package. The override exists because
   strict URL-prefix routing pushes ~530 lines of ingestion handlers
   (which share `IngestionService` / `JobRunner` / OCR-transcription
   provider deps) into `entries.py`, mixing reads with long-running
   writes.

2. **Two soft-cap exceptions, recorded.** `dashboard.py` (609) and
   `entities.py` (717) sit slightly over the ~600-line target.
   Resource cohesion outweighs splitting either further at this stage.
   `entities.py`'s docstring records the planned future split shape
   (`entities.py` core CRUD + `entity_merge.py` for merge / candidates
   / quarantine / aliases) so the breadcrumb is in the file.

3. **`_require_services()` wrapper dropped.** It was a no-op around
   `services_getter()`; resource modules now call `services_getter()`
   directly. This is the only intentional non-mechanical change in the
   diff.

4. **Migration shape — transitional `_legacy.py`.** Each commit moved
   one resource group out of a transitional `_legacy.py` into its own
   module. The final commit renamed `_legacy.py` → `entries.py` (the
   remaining file was nothing but the entries routes by then) — `git
   mv` preserves history.

5. **Plan v2 → v3 corrections.** The doc framed the resource list as
   `entries / entities / jobs / auth / users / health / media / query`
   — an 8-module list that turned out to be wrong on contact: no auth
   routes existed in `api.py` (they live in `journal.auth`), no media
   routes existed (HEIC conversion is a helper used inside ingestion),
   and three resource clusters were missing entirely (settings,
   notifications, dashboard). `code-quality-refactor-plan.md` Unit 1a
   now lists the actual eleven modules.

## What this enables / what it doesn't

This was deliberately mechanical. Behaviour is unchanged. The reach-ins
into private service state (`query_svc._repo.*`, `query_svc._vector_store`,
`ingestion_svc._repo`, `ingestion_svc._store_source_file`, etc.) are
still present — Unit 1b targets those next, with the call sites now
naturally clustered by resource module so the public API additions can
be designed against coherent groups.

## Coverage

Per-module coverage of the new files broadly mirrors the original:

- `entries.py` 92%, `health.py` 88%, `jobs.py` 90%, `search.py` 93%,
  `users.py` 90%, `settings.py` 83%, `ingestion.py` 78%, `entities.py`
  73%, `dashboard.py` 68%, `notifications.py` 23% — same gaps that
  existed pre-split.
- Overall journal package coverage: 84% (≥80% gate, unchanged).
