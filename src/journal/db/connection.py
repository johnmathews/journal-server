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

    WARNING: `check_same_thread=False` is only safe when callers
    serialise writes externally. The JobRunner achieves this by
    funnelling every job through one worker, but any other caller
    that sets this flag must guarantee the same property or risk
    SQLite-level data corruption. Do not toggle it just to paper
    over threading errors.
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
