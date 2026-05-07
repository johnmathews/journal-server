# Item 5 — write-method ownership

Date: 2026-05-07

Mechanical follow-up to Unit 1b. The two writes that landed on
`QueryService` because they matched an existing api/ call-site service
handle have moved to `IngestionService`, where mutating operations
already live (`save_final_text`, `delete_entry`, `store_source_file`,
…).

## Changes

- `services/query.py` — removed `update_entry_date` and `verify_doubts`.
- `services/ingestion.py` — added `update_entry_date` and
  `verify_doubts` next to the other public pass-throughs. Bodies are
  one-line delegations to `self._repo.<method>` with the same kwargs
  the QueryService versions had.
- `api/entries.py` — PATCH route's date-update path and the
  `POST /verify-doubts` route now call `ingestion_svc.<method>`. The
  verify-doubts handler keeps its `query_svc` handle for the read-side
  enrichment (`get_entry`, `get_page_count`).
- Tests — moved the two delegate-shape tests off
  `test_query_service_public_api.py` and replaced them with four
  behavioural tests under `test_ingestion.py::TestIngestionPublicAPI`
  that exercise the real repository (matching the rest of that class's
  shape).

## Verification

`uv run pytest -m 'not integration'` → **1797 passed** (1795 baseline
after item 1 + net +2 from -2 query tests / +4 ingestion tests).

## Notes

The api/ verify-doubts handler still imports `QueryService` for the
read-side enrichment after the write. Keeping both handles in scope is
fine — the rule from `code-quality-principles.md` is that *writes*
belong on `IngestionService` and *reads* on `QueryService`, not that a
route may only touch one service.
