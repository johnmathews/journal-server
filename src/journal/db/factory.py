"""Per-thread ``sqlite3.Connection`` factory.

Replaces the shared-``Connection`` model that produced the
``OperationalError: not an error`` (2026-04) and
``OperationalError: cannot commit - no transaction is active``
(2026-05-11) races. See ``docs/archive/sqlite-per-thread-connections-plan.md``
for the rationale and the migration sequence (W1 builds this; W2
onwards migrates each repo to use it).

The factory owns one ``sqlite3.Connection`` per OS thread, lazily
opened on first use. Each connection is opened with
``check_same_thread=True`` so accidental cross-thread use trips
Python's built-in guard instead of silently corrupting transaction
state. WAL mode (already on for ``get_connection``) lets independent
connections read while another writes, and SQLite's writer lock plus
the 5s ``busy_timeout`` serialises writers at the file level — which
is the only place writer-vs-writer serialisation belongs.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

from journal.db.connection import get_connection

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

log = logging.getLogger(__name__)


class ConnectionFactory:
    """Hands out per-thread SQLite connections.

    Usage::

        factory = ConnectionFactory(db_path)
        run_migrations(factory.get())   # main thread
        repo = SQLiteJobRepository(factory)
        repo.create(...)                # any thread; safe

    Each thread's first ``get()`` opens a fresh connection with the
    standard PRAGMAs and caches it on a ``threading.local``. Subsequent
    calls on that thread return the same instance. Threads do **not**
    share connections — Python's sqlite3 transaction state is per-
    connection, so this is what makes the shared-state race impossible.

    Connections opened by this factory should not be stored on
    instance attributes of services or repositories. Always call
    ``factory.get()`` at the start of each method, then operate on
    the returned connection. Holding a reference across method calls
    is technically safe (a thread keeps its own connection across
    calls) but obscures the model and would silently break if a
    method ever got called from a different thread than the one
    that captured the reference.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._local = threading.local()

    @property
    def db_path(self) -> Path:
        return self._db_path

    def get(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            # check_same_thread=True is the default of get_connection
            # and the whole point of this factory: each thread opens
            # its own connection, so the built-in guard stays armed.
            conn = get_connection(self._db_path)
            self._local.conn = conn
            log.debug(
                "ConnectionFactory opened connection for thread %s",
                threading.get_ident(),
            )
        return conn

    def close_current(self) -> None:
        """Close the connection bound to the current thread, if any.

        Tests use this to release connections deterministically.
        Production normally relies on process exit; long-lived threads
        that finish early can call this to free the file descriptor.
        Calling from a thread that has no open connection is a no-op.
        """
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
            log.debug(
                "ConnectionFactory closed connection for thread %s",
                threading.get_ident(),
            )
