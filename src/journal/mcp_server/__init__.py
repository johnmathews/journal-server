"""MCP server package — facade re-exporting symbols from the submodules.

The package is split into:

- `bootstrap.py` — `_init_services`, `lifespan`, `_services` global,
  the runtime-settings on-change callback (closure inside
  `_init_services`).
- `app.py` — the singleton `mcp = FastMCP(...)` instance plus the
  three REST `register_*_routes(mcp, ...)` registrations.
- `runserver.py` — `main()`, the uvicorn boot path.
- `tools/` — every `@mcp.tool()` registration. Importing the
  submodules has the side effect of registering the tools against
  the `mcp` instance from `app.py`.

This `__init__.py` re-exports the public surface so existing callers
keep working at the same paths (`journal.mcp_server.lifespan`,
`journal.mcp_server.journal_ingest_text`, etc.). Tests that monkeypatch
implementation details (`load_config`, `ChromaVectorStore`) must target
the originating module — `journal.mcp_server.bootstrap.X` — because
re-exports do not share binding with their source.
"""

from journal.mcp_server.app import mcp
from journal.mcp_server.bootstrap import _init_services, _services, lifespan
from journal.mcp_server.runserver import main
from journal.mcp_server.tools._ctx import (
    _get_db_conn,
    _get_entity_extraction,
    _get_entity_store,
    _get_fitness_repo,
    _get_ingestion,
    _get_job_repository,
    _get_job_runner,
    _get_query,
    _user_id,
)
from journal.mcp_server.tools.entities import (
    journal_extract_entities,
    journal_get_entity_mentions,
    journal_get_entity_relationships,
    journal_list_entities,
)
from journal.mcp_server.tools.fitness import (
    fitness_correlate_hrv_mood,
    fitness_correlate_sleep_mood,
    fitness_correlate_weekly_runs_stress,
    fitness_integrity_check,
    fitness_list_activities,
    fitness_list_daily,
    fitness_sync_status,
    fitness_trigger_sync,
)
from journal.mcp_server.tools.ingestion import (
    journal_ingest_media,
    journal_ingest_media_from_url,
    journal_ingest_multi_page,
    journal_ingest_multi_page_from_url,
    journal_ingest_text,
    journal_update_entry_text,
)
from journal.mcp_server.tools.jobs import (
    _job_to_tool_dict,
    _poll_job_until_terminal,
    journal_backfill_mood_scores_batch,
    journal_extract_entities_batch,
    journal_get_job_status,
)
from journal.mcp_server.tools.queries import (
    journal_get_entries_by_date,
    journal_get_mood_trends,
    journal_get_statistics,
    journal_get_topic_frequency,
    journal_list_entries,
    journal_search_entries,
)
from journal.mcp_server.tools.storylines import (
    journal_create_storyline,
    journal_delete_storyline,
    journal_get_storyline,
    journal_list_storylines,
    journal_regenerate_storyline,
    journal_set_storyline_anchors,
    journal_storylines_guide,
)

__all__ = [
    "_get_db_conn",
    "_get_entity_extraction",
    "_get_entity_store",
    "_get_fitness_repo",
    "_get_ingestion",
    "_get_job_repository",
    "_get_job_runner",
    "_get_query",
    "_init_services",
    "_job_to_tool_dict",
    "_poll_job_until_terminal",
    "_services",
    "_user_id",
    "fitness_correlate_hrv_mood",
    "fitness_correlate_sleep_mood",
    "fitness_correlate_weekly_runs_stress",
    "fitness_integrity_check",
    "fitness_list_activities",
    "fitness_list_daily",
    "fitness_sync_status",
    "fitness_trigger_sync",
    "journal_backfill_mood_scores_batch",
    "journal_create_storyline",
    "journal_delete_storyline",
    "journal_extract_entities",
    "journal_extract_entities_batch",
    "journal_get_entity_mentions",
    "journal_get_entity_relationships",
    "journal_get_entries_by_date",
    "journal_get_job_status",
    "journal_get_mood_trends",
    "journal_get_statistics",
    "journal_get_storyline",
    "journal_get_topic_frequency",
    "journal_ingest_media",
    "journal_ingest_media_from_url",
    "journal_ingest_multi_page",
    "journal_ingest_multi_page_from_url",
    "journal_ingest_text",
    "journal_list_entities",
    "journal_list_entries",
    "journal_list_storylines",
    "journal_regenerate_storyline",
    "journal_search_entries",
    "journal_set_storyline_anchors",
    "journal_storylines_guide",
    "journal_update_entry_text",
    "lifespan",
    "main",
    "mcp",
]
