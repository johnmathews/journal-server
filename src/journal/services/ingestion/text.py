"""Plain-text ingest path for ``IngestionService``.

Mixin that the service composes via subclassing. The methods stay
bound to ``self`` so they keep using the constructor-injected
collaborators (``_repo``, ``_process_text``, …) without any
context-passing churn at the call site.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from journal.services.entry_dates import validate_entry_date

if TYPE_CHECKING:
    from journal.models import Entry

log = logging.getLogger(__name__)


class _TextIngestMixin:
    """``ingest_text``: store an already-formed text payload as an entry."""

    def ingest_text(
        self,
        text: str,
        date: str,
        source_type: str = "text_entry",
        *,
        skip_mood: bool = False,
        user_id: int = 1,
    ) -> Entry:
        """Ingest a plain-text entry (no OCR, no transcription).

        Used for manually typed entries and imported text/markdown
        files. The text is stored as both raw_text and final_text,
        then chunked, embedded, and stored in the vector DB.

        Args:
            text: The entry text content.
            date: Journal entry date (ISO 8601).
            source_type: Entry source type (e.g. "text_entry",
                "imported_text_file", "imported_audio_file").
            skip_mood: When True, skip inline mood scoring (caller
                will handle it separately, e.g. via an async job).
        """
        text = text.strip()
        if not text:
            raise ValueError("Text must not be empty")
        # Caller-supplied dates are hard-bounded (spec 2026-07-13); only
        # OCR/voice *detected* dates go through repair/quarantine instead.
        validate_entry_date(date, min_date=self._min_entry_date)  # type: ignore[attr-defined]

        log.info(
            "Ingesting text entry for date %s (source=%s, %d chars)",
            date, source_type, len(text),
        )

        word_count = len(text.split())
        entry = self._repo.create_entry(  # type: ignore[attr-defined]
            date, source_type, text, word_count, user_id=user_id,
        )

        chunk_count = self._process_text(  # type: ignore[attr-defined]
            entry.id, entry.final_text, date,
            skip_mood=skip_mood, user_id=user_id,
        )
        self._repo.update_chunk_count(entry.id, chunk_count)  # type: ignore[attr-defined]

        log.info(
            "Ingested text entry %d: %d words, date %s",
            entry.id, word_count, date,
        )
        return self._repo.get_entry(entry.id)  # type: ignore[attr-defined,return-value]
