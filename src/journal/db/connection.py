"""SQLite connection factory."""

import logging
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)


def get_connection(
    db_path: Path, *, check_same_thread: bool = True
) -> sqlite3.Connection:
    """Create a configured SQLite connection.

    When `check_same_thread` is True (the default) the returned
    connection is bound to the creating thread, matching sqlite3's
    default guard. Passing `check_same_thread=False` lifts that
    restriction so the connection can be used from threads other than
    the one that created it — needed for the background JobRunner,
    which executes queued work on a single-worker executor thread.

    WARNING — known threading hazard:

    `check_same_thread=False` is needed for the MCP server (the
    request thread + the JobRunner's single-worker thread share
    one connection), but Python's sqlite3 module is not actually
    thread-safe at the connection level. Multiple operations
    that access the connection touch shared state:

      - ``execute()`` / ``executemany()`` mutate the connection's
        transaction state.
      - ``cursor.lastrowid`` reads ``sqlite3_last_insert_rowid()``,
        which is per-connection. A concurrent INSERT from another
        thread can update it before this thread's lastrowid read.
      - ``cursor.fetchone()`` / ``fetchall()`` read from a result set
        that the cursor binds to its statement, but the underlying
        statement handle is owned by the connection.

    A re-entrant lock around individual ``execute`` / ``commit``
    calls (attempted in item 1.1) is *not* sufficient — the
    multi-step "execute → fetch" or "execute → lastrowid → commit"
    windows are still exposed. Properly closing every gap requires
    either:

      1. Per-thread connections (each thread opens its own
         ``sqlite3.Connection``; SQLite's WAL handles cross-
         connection coordination correctly). Big architectural
         change — services and repos currently share one connection
         instance.
      2. Holding a connection-wide lock for the entire duration of
         every multi-step read/write (every repo method must
         acquire and release explicitly).

    Today we accept the residual risk: the only race we have
    actually observed (the within-call race in
    ``submit_save_entry_pipeline``) was fixed in item 1 by deferring
    worker dispatch until the API thread's writes complete. Cross-
    call races are theoretical — workers spend most of their time
    in LLM calls, not SQLite, so concurrent SQLite writes are rare.
    Revisit if production logs surface fresh
    ``sqlite3.OperationalError: not an error`` reports. See
    ``docs/refactor-follow-ups.md`` item 1.1.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-64000")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    log.info("Connected to database at %s", db_path)
    return conn
