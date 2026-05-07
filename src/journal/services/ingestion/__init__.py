"""Ingestion service package.

Re-exports ``IngestionService`` so the historical
``from journal.services.ingestion import IngestionService`` import
keeps working unchanged. The class itself, plus its helpers and
per-media-type method bodies, lives across multiple files in this
package — see ``service.py`` for the wiring and the per-media-type
modules (``image.py``, ``voice.py``, ``text.py``, ``url_sources.py``)
for the ingest method bodies.
"""

from journal.services.ingestion.service import IngestionService

__all__ = ["IngestionService"]
