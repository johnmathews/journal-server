# 2026-06-10 — Split api/ingestion.py and api/fitness.py per the size rule (W21)

Quality-round-2606 W21. The two remaining >800-line `api/` modules were split
along responsibility boundaries. Pure moves — no logic, signature, or route
changes; the registered route table is byte-identical before and after
(verified by dumping sorted `method path` pairs from the built Starlette app
on `origin/main` vs this branch: 119 entries, empty diff).

Sequenced after W20 (the `handler` decorator refactor), which had already
touched every handler body — line numbers in the plan were re-measured on
post-W20 main before cutting.

## What moved

`api/ingestion.py` (1,098 → 541) — its docstring claimed 8 routes but the
module owned 12; storyline writes had drifted in undocumented. It keeps the
entry-ingest + extract + mood-backfill cluster:

- kept: `POST /api/entries/ingest/{text,file,images,audio}`,
  `POST /api/entities/extract`, `POST /api/mood/backfill`
- → **new `api/storylines_write.py` (407):** `POST /api/storylines`,
  `POST /api/storylines/{id}/regenerate`, `DELETE /api/storylines/{id}`,
  `PUT /api/storylines/{id}/anchors`, plus `_anchors_for`
- → **new `api/fitness_jobs.py` (261):** `POST /api/fitness/sync/{source}`,
  `POST /api/fitness/backfill/{source}`

`api/fitness.py` (1,025 → 286) keeps the four reads + serializers
(`list_activities`, `list_daily`, `sync_status`, `integrity`,
`_activity_to_dict`, `_daily_to_dict`, `_sync_run_to_dict`,
`_per_source_status` — the latter three still imported by
`mcp_server/tools/fitness.py`, whose import path is unchanged):

- → **new `api/fitness_garmin.py` (525):** `POST /api/fitness/garmin/connect`,
  `POST /api/fitness/garmin/connect/mfa`, `POST /api/fitness/garmin/disconnect`,
  plus `_extract_upstream_user_id` and `_persist_garmin_auth`
- → **new `api/fitness_strava.py` (307):** `GET /api/fitness/strava/authorize_url`,
  `POST /api/fitness/strava/exchange`, `POST /api/fitness/strava/disconnect`,
  plus the nested `_persist_strava_auth`

`_now_iso` (the auth-state timestamp helper) was the only helper needed by
both new fitness-auth modules, so it moved to `api/_shared.py`.

## Registration order

`api/__init__.py` registers `storylines_write` and `fitness_jobs` immediately
after `ingestion` (where those routes used to be registered), and
`fitness_garmin` / `fitness_strava` immediately after `fitness` (ditto), so
every route keeps its prior position in the Starlette route list.

## Routing-rule bookkeeping

The write/job-creation override (`docs/code-quality-principles.md` § "Routing
rules") previously named `api/ingestion.py` as the single override module.
The section now documents the override as a three-module family
(`ingestion.py`, `storylines_write.py`, `fitness_jobs.py`) — a size-rule
split of the same category, not a new deviation category — and requires
updating both the principles doc **and** `api/_shared.py`'s docstring for any
future category change. `api/_shared.py`'s docstring and each module's
docstring were updated in the same commit, per the doc's own rule. The
Garmin/Strava auth modules are plain URL-resource modules (auth flows, not
job creation) and are not part of the override.

## Line counts after (`wc -l src/journal/api/*.py`)

```
  81 __init__.py        236 health.py
 167 _handler.py        541 ingestion.py
 256 _shared.py         115 jobs.py
 600 dashboard.py       131 notifications.py
 417 entities.py        155 search.py
 387 entity_merge.py    250 settings.py
 509 entries.py         159 storylines.py
 286 fitness.py         407 storylines_write.py
 525 fitness_garmin.py   90 users.py
 261 fitness_jobs.py
 307 fitness_strava.py
```

Every `api/` file is now ≤ 600 lines (rule: ≤ ~700, smell at ~800).

## Tests

Existing suites go through the TestClient and passed unchanged, except
`tests/test_api_storylines.py` and `tests/test_api_storylines_write.py`,
which imported `register_ingestion_routes` directly to mount the storyline
write routes — they now import `register_storylines_write_routes`. No
assertion changes anywhere. Full suite: 2594 passed. `ruff check` clean.
