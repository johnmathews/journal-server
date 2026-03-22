"""SQLite connection factory."""

import logging
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)


def get_connection(db_path: Path) -> sqlite3.Connection:
    """Create a configured SQLite connection."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-64000")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    log.info("Connected to database at %s", db_path)
    return conn
