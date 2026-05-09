"""Per-tool context helpers.

Every `@mcp.tool()` function calls one of these to fish a service out
of the lifespan_context dict. Keeping them in their own module avoids
duplicating boilerplate across `queries.py` / `ingestion.py` /
`entities.py` / `jobs.py` / `fitness.py`.
"""

import sqlite3

from mcp.server.fastmcp import Context

from journal.auth import get_current_user_id
from journal.db.fitness_repository import FitnessRepository
from journal.db.jobs_repository import SQLiteJobRepository
from journal.entitystore.store import SQLiteEntityStore
from journal.services.entity_extraction import EntityExtractionService
from journal.services.ingestion import IngestionService
from journal.services.jobs import JobRunner
from journal.services.query import QueryService


def _get_query(ctx: Context) -> QueryService:
    return ctx.request_context.lifespan_context["query"]


def _get_ingestion(ctx: Context) -> IngestionService:
    return ctx.request_context.lifespan_context["ingestion"]


def _get_entity_extraction(ctx: Context) -> EntityExtractionService:
    return ctx.request_context.lifespan_context["entity_extraction"]


def _get_entity_store(ctx: Context) -> SQLiteEntityStore:
    return ctx.request_context.lifespan_context["entity_store"]


def _get_job_runner(ctx: Context) -> JobRunner:
    return ctx.request_context.lifespan_context["job_runner"]


def _get_job_repository(ctx: Context) -> SQLiteJobRepository:
    return ctx.request_context.lifespan_context["job_repository"]


def _get_fitness_repo(ctx: Context) -> FitnessRepository:
    return ctx.request_context.lifespan_context["fitness_repo"]


def _get_db_conn(ctx: Context) -> sqlite3.Connection:
    """Raw SQLite connection — used by tools that run hand-written SQL
    (correlation queries, integrity checks) where wrapping every join
    in a repository method would just add ceremony."""
    return ctx.request_context.lifespan_context["db_conn"]


def _user_id(ctx: Context) -> int:
    """Return the authenticated user_id for the current MCP request."""
    return get_current_user_id()
