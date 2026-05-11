"""SQLite connection factory.

Thin shim that applies the project's standard PRAGMAs to a fresh
:class:`sqlite3.Connection`. Production code paths go through
:class:`journal.db.factory.ConnectionFactory`, which itself calls
``get_connection`` once per thread; this function remains as a direct
helper for ``run_migrations`` and the short-lived per-invocation
connections used by CLI commands.

``check_same_thread`` is **not** exposed as a parameter — the project
relies on Python's built-in same-thread guard as a tripwire. Multi-
thread access to one connection caused the
``OperationalError: cannot commit - no transaction is active``
incident on 2026-05-11; see
``docs/archive/sqlite-per-thread-connections-plan.md`` for the structural fix.
"""

import logging
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)


def get_connection(db_path: Path) -> sqlite3.Connection:
    """Create a configured SQLite connection bound to the calling thread.

    The returned connection has WAL mode, foreign keys, ``sqlite3.Row``
    rows, a 5-second ``busy_timeout``, ``NORMAL`` sync, and a 64 MiB
    cache. ``check_same_thread`` is left at its default (``True``);
    if a caller ever passes the resulting connection across threads,
    Python raises ``sqlite3.ProgrammingError`` immediately rather than
    silently corrupting transaction state. Use
    :class:`journal.db.factory.ConnectionFactory` in any code path
    that needs cross-thread access.
    """
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
