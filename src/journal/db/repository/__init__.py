"""Compatibility facade for the repository package.

Historical import path: ``from journal.db.repository import X``.
After the file → package conversion (commit A) and the carve into
topic mixins (commit B), the only two names callers ever import
remain re-exported from this facade:

- ``EntryRepository`` — the Protocol (defined in ``protocol.py``).
- ``SQLiteEntryRepository`` — the implementation that composes the
  seven topic mixins (defined in ``store.py``).

See ``docs/refactor-repository-plan.md`` for the cluster mapping.
"""

from journal.db.repository.protocol import EntryRepository
from journal.db.repository.store import SQLiteEntryRepository

__all__ = ["EntryRepository", "SQLiteEntryRepository"]
