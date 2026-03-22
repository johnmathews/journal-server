"""Simple migration runner using PRAGMA user_version."""

import logging
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def get_current_version(conn: sqlite3.Connection) -> int:
    result = conn.execute("PRAGMA user_version").fetchone()
    return result[0]


def get_migration_files() -> list[Path]:
    """Return migration SQL files sorted by version number."""
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


def run_migrations(conn: sqlite3.Connection) -> None:
    """Apply pending migrations."""
    current_version = get_current_version(conn)
    migration_files = get_migration_files()

    for migration_file in migration_files:
        file_version = int(migration_file.stem.split("_")[0])
        if file_version <= current_version:
            continue

        log.info(
            "Applying migration %s (version %d -> %d)",
            migration_file.name,
            current_version,
            file_version,
        )

        sql = migration_file.read_text()
        conn.executescript(sql)
        conn.execute(f"PRAGMA user_version = {file_version}")
        current_version = file_version

    log.info("Database at version %d", current_version)
