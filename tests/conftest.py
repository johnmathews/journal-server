"""Shared test fixtures."""

import sqlite3
from collections.abc import Generator
from pathlib import Path

import pytest

from journal.config import Config
from journal.db.connection import get_connection
from journal.db.migrations import run_migrations


@pytest.fixture
def tmp_db_path(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


@pytest.fixture
def db_conn(tmp_db_path: Path) -> Generator[sqlite3.Connection]:
    """Provide a migrated SQLite connection for testing."""
    conn = get_connection(tmp_db_path)
    run_migrations(conn)
    yield conn
    conn.close()


@pytest.fixture
def config(tmp_db_path: Path) -> Config:
    """Provide a test configuration."""
    return Config(
        db_path=tmp_db_path,
        chromadb_host="localhost",
        chromadb_port=8000,
        anthropic_api_key="test-key",
        openai_api_key="test-key",
    )
