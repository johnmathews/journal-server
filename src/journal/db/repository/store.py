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
"""

import sqlite3

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

    Composes seven topic mixins. Owns nothing of its own beyond the
    connection it stores in ``self._conn`` and the public
    ``connection`` property that exposes it for tests and diagnostics.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    @property
    def connection(self) -> sqlite3.Connection:
        """Expose the underlying SQLite connection.

        Used by tests for direct SQL assertions and by the runtime-
        settings reload path. Not part of the EntryRepository
        Protocol — callers that take an ``EntryRepository`` should
        not depend on it.
        """
        return self._conn
