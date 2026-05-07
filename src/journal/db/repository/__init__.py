"""Compatibility facade for the repository package.

Historical import path: ``from journal.db.repository import X``.
Continues to work after the file → package conversion. The actual
definitions live in ``_legacy`` during the shell phase and will move
to ``protocol`` (Protocol + helpers) and ``store`` (SQLiteEntryRepository
+ topic mixins) once commit B lands. See
``docs/refactor-repository-plan.md``.
"""

from journal.db.repository._legacy import (
    EntryRepository,
    SQLiteEntryRepository,
)

__all__ = ["EntryRepository", "SQLiteEntryRepository"]
