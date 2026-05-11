"""Shared test fixtures."""

import sqlite3
from collections.abc import Generator
from pathlib import Path

import pytest

from journal.config import Config
from journal.db.factory import ConnectionFactory
from journal.db.migrations import run_migrations


@pytest.fixture
def tmp_db_path(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


@pytest.fixture
def factory(tmp_db_path: Path) -> Generator[ConnectionFactory]:
    """Provide a migrated ``ConnectionFactory`` for testing.

    Tests that construct repositories should depend on this fixture
    and pass the factory to repo constructors. Tests that read raw
    SQL can depend on :func:`db_conn`, which returns the calling
    thread's connection from the same factory.
    """
    f = ConnectionFactory(tmp_db_path)
    run_migrations(f.get())
    yield f
    f.close_current()


@pytest.fixture
def db_conn(factory: ConnectionFactory) -> sqlite3.Connection:
    """Return the calling thread's connection from :func:`factory`.

    Kept so the many tests that do raw SQL via ``db_conn.execute(...)``
    keep working unchanged. New tests should prefer ``factory``.
    """
    return factory.get()


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
