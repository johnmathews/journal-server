"""SQLite implementation of EntryRepository.

The Protocol and shared helpers (``_row_to_entry``, ``_bin_start_sql``,
the ``_SUPPORTED_*BINS`` whitelists) live in ``protocol.py``.
Per-resource methods live in topic mixins (``core``, ``pages``,
``chunks``, ``search``, ``mood``, ``stats``, ``analytics``) and are
composed into ``SQLiteEntryRepository`` here. ``EntryRepository`` is
re-exported so existing call sites
(``from journal.db.repository.store import EntryRepository``) keep
working — the canonical compat path is via the package
``__init__.py`` re-export.

Construction accepts either a :class:`ConnectionFactory` (preferred,
used by production via ``mcp_server/bootstrap.py``) or a bare
``sqlite3.Connection`` (legacy, retained for tests that haven't been
migrated to the factory model yet — see
``docs/sqlite-per-thread-connections-plan.md`` W3).

Mixin methods call ``conn = self._conn()`` at the top and operate on
that local variable, so each thread gets its own connection on the
factory path and the shared-state commit race documented in
``docs/sqlite-threading.md`` is structurally impossible.
"""

import sqlite3

from journal.db.factory import ConnectionFactory
from journal.db.repository.analytics import _AnalyticsMixin
from journal.db.repository.chunks import _ChunksMixin
from journal.db.repository.core import _CoreMixin
from journal.db.repository.mood import _MoodMixin
from journal.db.repository.pages import _PagesMixin
from journal.db.repository.protocol import EntryRepository
from journal.db.repository.search import _SearchMixin
from journal.db.repository.stats import _StatsMixin

__all__ = ["EntryRepository", "SQLiteEntryRepository"]


class SQLiteEntryRepository(
    _CoreMixin,
    _PagesMixin,
    _ChunksMixin,
    _SearchMixin,
    _MoodMixin,
    _StatsMixin,
    _AnalyticsMixin,
):
    """SQLite-backed implementation of the ``EntryRepository`` Protocol.

    Composes seven topic mixins. Accepts either a
    :class:`ConnectionFactory` (production, per-thread connections) or
    a bare ``sqlite3.Connection`` (legacy migration ramp). All mixin
    methods route through ``self._conn()`` which dispatches to the
    factory's thread-local connection or to the shared connection
    depending on which path is active.
    """

    def __init__(
        self,
        factory_or_conn: ConnectionFactory | sqlite3.Connection,
    ) -> None:
        if isinstance(factory_or_conn, ConnectionFactory):
            self._factory: ConnectionFactory | None = factory_or_conn
            self._direct_conn: sqlite3.Connection | None = None
        else:
            self._factory = None
            self._direct_conn = factory_or_conn

    def _conn(self) -> sqlite3.Connection:
        """Return the connection for the current call.

        Factory path: returns this thread's connection (lazily opened
        on first use). Legacy path: returns the single shared
        connection passed at construction.
        """
        if self._factory is not None:
            return self._factory.get()
        assert self._direct_conn is not None
        return self._direct_conn

    @property
    def connection(self) -> sqlite3.Connection:
        """Expose the underlying SQLite connection for the current thread.

        Used by tests for direct SQL assertions and by the runtime-
        settings reload path. On the factory path this returns the
        *calling* thread's connection; cross-thread inspection from a
        test thread sees committed state via WAL. On the legacy path
        this returns the single shared connection. Not part of the
        EntryRepository Protocol — callers that take an
        ``EntryRepository`` should not depend on it.
        """
        return self._conn()
