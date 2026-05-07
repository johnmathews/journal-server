"""Async job runner package — public re-exports.

Importers should use ``from journal.services.jobs import JobRunner``. The
helpers (``friendly_error``, ``is_transient``, ``validate_params``, the
per-type ``*_KEYS`` constants) live in sibling modules and are part of
the package's testable surface but are not re-exported here — direct
module imports keep the attack surface small.

The ``EntityReembedder`` Protocol is re-exported because external
production wiring (mcp_server.py) needs the type for its constructor
parameter.
"""

from journal.services.jobs.runner import EntityReembedder, JobRunner

__all__ = [
    "EntityReembedder",
    "JobRunner",
]
