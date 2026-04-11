"""Tests for MCP server lifespan initialization."""

from unittest.mock import MagicMock, patch

import pytest

import journal.mcp_server as mcp_module
from journal.mcp_server import lifespan


@pytest.fixture(autouse=True)
def _reset_services():
    """Reset the global services singleton between tests."""
    mcp_module._services = None
    yield
    mcp_module._services = None


@pytest.fixture
def _mock_chromadb():
    """Patch ChromaVectorStore so tests don't need a running ChromaDB."""
    with patch("journal.mcp_server.ChromaVectorStore") as mock_cls:
        mock_cls.return_value = MagicMock()
        yield mock_cls


async def test_first_call_initializes(monkeypatch, config, _mock_chromadb):
    monkeypatch.setattr("journal.mcp_server.load_config", lambda: config)

    async with lifespan(None) as services:
        assert "ingestion" in services
        assert "query" in services


async def test_jobs_wired_into_services(
    monkeypatch, config, _mock_chromadb
):
    """JobRunner + JobRepository must land in the services dict.

    This is the integration test for Work Unit 4 — if either key is
    missing, API handlers and MCP tool wrappers cannot retrieve the
    runner via `services_getter()`.
    """
    from journal.db.jobs_repository import SQLiteJobRepository
    from journal.services.jobs import JobRunner

    monkeypatch.setattr("journal.mcp_server.load_config", lambda: config)

    async with lifespan(None) as services:
        assert "job_repository" in services
        assert "job_runner" in services
        assert isinstance(services["job_repository"], SQLiteJobRepository)
        assert isinstance(services["job_runner"], JobRunner)


async def test_reconcile_stuck_jobs_runs_at_startup(
    monkeypatch, config, _mock_chromadb
):
    """Any job left queued/running from a previous process is marked
    failed at boot. The wiring in `_init_services` must invoke
    `reconcile_stuck_jobs` before the runner starts accepting new
    submissions."""
    from journal.db.connection import get_connection
    from journal.db.jobs_repository import SQLiteJobRepository
    from journal.db.migrations import run_migrations

    # Seed a stuck job in the same database the lifespan will open.
    seed_conn = get_connection(config.db_path, check_same_thread=False)
    run_migrations(seed_conn)
    seed_repo = SQLiteJobRepository(seed_conn)
    stuck = seed_repo.create("entity_extraction", {"entry_id": 1})
    seed_repo.mark_running(stuck.id)
    seed_conn.close()

    monkeypatch.setattr("journal.mcp_server.load_config", lambda: config)

    async with lifespan(None) as services:
        repo = services["job_repository"]
        reconciled = repo.get(stuck.id)
        assert reconciled is not None
        assert reconciled.status == "failed"
        assert reconciled.error_message == (
            "server restarted before job completed"
        )


async def test_second_call_reuses(monkeypatch, config, _mock_chromadb):
    monkeypatch.setattr("journal.mcp_server.load_config", lambda: config)

    async with lifespan(None) as first:
        pass

    async with lifespan(None) as second:
        assert first is second


async def test_config_loaded_once(monkeypatch, config, _mock_chromadb):
    call_count = 0
    original_config = config

    def counting_load():
        nonlocal call_count
        call_count += 1
        return original_config

    monkeypatch.setattr("journal.mcp_server.load_config", counting_load)

    async with lifespan(None):
        pass
    async with lifespan(None):
        pass

    assert call_count == 1
