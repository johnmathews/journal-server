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

Construction takes a :class:`ConnectionFactory`. Mixin methods call
``conn = self._conn()`` at the top and operate on that local variable,
so each thread gets its own connection and the shared-state commit
race documented in ``docs/sqlite-per-thread-connections-plan.md`` is
structurally impossible.
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

    Composes seven topic mixins. Takes a :class:`ConnectionFactory`;
    every mixin method routes through ``self._conn()`` which returns
    the calling thread's connection from the factory.
    """

    def __init__(self, factory: ConnectionFactory) -> None:
        self._factory = factory

    def _conn(self) -> sqlite3.Connection:
        return self._factory.get()

    @property
    def connection(self) -> sqlite3.Connection:
        """Expose the underlying SQLite connection for the calling thread.

        Used by tests for direct SQL assertions and by the runtime-
        settings reload path. Not part of the EntryRepository Protocol
        — callers that take an ``EntryRepository`` should not depend
        on it.
        """
        return self._factory.get()
