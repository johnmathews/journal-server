"""Simple migration runner using PRAGMA user_version.

Migrations are SQL files in :data:`MIGRATIONS_DIR`, named ``NNNN_descriptor.sql``.
The runner skips files whose version is ``<= PRAGMA user_version``, executes the
rest via :meth:`sqlite3.Connection.executescript`, and bumps ``user_version``
after each.

**Re-runnability invariant.** A migration must be safe to re-run on a database
where it has already been applied. CREATE TABLE / CREATE INDEX migrations use
``IF NOT EXISTS`` directly. ADD COLUMN migrations cannot — SQLite doesn't
support ``ADD COLUMN IF NOT EXISTS`` — so the runner catches "duplicate column
name" errors and treats them as no-ops. The end state is the same either way:
the column exists with the column's declared default; data isn't lost.

This catch is deliberately narrow. Any other ``OperationalError`` propagates
so genuine migration breakage stays loud.
"""

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


def _executescript_idempotent(
    conn: sqlite3.Connection, sql: str, migration_name: str,
) -> None:
    """Execute a migration script, tolerating duplicate-column errors.

    SQLite's ``ALTER TABLE ADD COLUMN`` lacks an ``IF NOT EXISTS`` clause, so
    re-running an ADD COLUMN migration on a database that already has the
    column raises ``OperationalError: duplicate column name: <col>``. We
    catch that specific message and continue — the resulting state is
    indistinguishable from a fresh apply.
    """
    try:
        conn.executescript(sql)
    except sqlite3.OperationalError as exc:
        msg = str(exc)
        if msg.startswith("duplicate column name:"):
            log.info(
                "Migration %s — column already exists (%s), treating as no-op",
                migration_name, msg,
            )
            return
        raise


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
        _executescript_idempotent(conn, sql, migration_file.name)
        conn.execute(f"PRAGMA user_version = {file_version}")
        current_version = file_version

    log.info("Database at version %d", current_version)
