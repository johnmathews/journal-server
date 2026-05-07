"""Voice (audio transcription) ingest paths for ``IngestionService``.

Mixin holding the single- and multi-recording entry points. Methods
stay bound to ``self`` so they keep using the constructor-injected
collaborators (``_transcription``, ``_repo``, ``_detect_heading``,
…) without any context-passing churn.
"""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from journal.models import Entry

log = logging.getLogger(__name__)


class _VoiceIngestMixin:
    """``ingest_voice`` and ``ingest_multi_voice`` — transcription paths."""

    def ingest_voice(
        self,
        audio_data: bytes,
        media_type: str,
        date: str,
        language: str = "en",
        *,
        source_type: str = "voice",
        skip_mood: bool = False,
        user_id: int = 1,
    ) -> Entry:
        """Ingest a voice note: transcribe -> chunk -> embed -> store."""
        log.info(
            "Ingesting voice note for date %s (%s, %d bytes)",
            date, media_type, len(audio_data),
        )

        file_hash = hashlib.sha256(audio_data).hexdigest()
        if self._is_duplicate(file_hash):  # type: ignore[attr-defined]
            raise ValueError(
                "This audio file has already been uploaded in another entry. "
                "Delete the existing entry first if you want to re-upload."
            )

        # Transcribe
        result = self._transcription.transcribe(  # type: ignore[attr-defined]
            audio_data, media_type, language,
        )
        raw_text = result.text if hasattr(result, "text") else result  # type: ignore[assignment]
        if not raw_text.strip():
            raise ValueError("Transcription produced no text from audio")

        # Try to extract a date from the start of the transcription before
        # detection — parity with the OCR paths. A backdated dictation
        # ("Friday 1 January 2026. Today I…") arrives with `date` set to
        # the upload day, but the user's intent is the date they spoke;
        # extract_date_from_text catches the regex-friendly forms here so
        # the entry is filed under the correct day even if the LLM
        # detector is disabled.
        from journal.services.date_extraction import extract_date_from_text

        extracted = extract_date_from_text(raw_text)
        if extracted:
            date = extracted

        # Detect a leading date — used to drive the entry's filing date
        # (the title renders from `entry_date`), not removed from the
        # body. The body keeps the date phrase intact so the entry text
        # reads naturally with the date as its first line.
        # If the detector resolved an ISO date, it overrides any earlier
        # extraction — it sees the entry_date hint and can resolve
        # spelled-out / relative phrases the regex can't.
        det = self._detect_heading(raw_text, date)  # type: ignore[attr-defined]
        if det.date_iso:
            date = det.date_iso
        formatted_body = (
            self._maybe_format_transcript(det.body)  # type: ignore[attr-defined]
            if det.body
            else det.body
        )

        word_count = len(raw_text.split())
        entry = self._repo.create_entry(  # type: ignore[attr-defined]
            date, source_type, raw_text, word_count, user_id=user_id,
            final_text=formatted_body if formatted_body != raw_text else None,
        )
        self.store_source_file(  # type: ignore[attr-defined]
            entry.id, f"voice_{date}", media_type, file_hash,
        )

        # Record uncertain spans from transcription confidence data.
        uncertain_spans = getattr(result, "uncertain_spans", [])
        if uncertain_spans:
            self._repo.add_uncertain_spans(entry.id, uncertain_spans)  # type: ignore[attr-defined]

        # Chunk, embed, and store in vector DB
        chunk_count = self._process_text(  # type: ignore[attr-defined]
            entry.id, entry.final_text, date,
            skip_mood=skip_mood, user_id=user_id,
        )
        self._repo.update_chunk_count(entry.id, chunk_count)  # type: ignore[attr-defined]

        log.info(
            "Ingested voice entry %d: %d words, date %s",
            entry.id, word_count, date,
        )
        return self._repo.get_entry(entry.id)  # type: ignore[attr-defined,return-value]

    def ingest_multi_voice(
        self,
        recordings: list[tuple[bytes, str]],
        date: str,
        language: str = "en",
        *,
        source_type: str = "voice",
        skip_mood: bool = False,
        on_progress: Callable[[int, int], None] | None = None,
        user_id: int = 1,
    ) -> Entry:
        """Ingest multiple voice recordings as a single journal entry.

        Each recording is transcribed separately, then texts are
        concatenated with newline separators. Mirrors the
        ``ingest_multi_page_entry`` pattern for images.

        Args:
            recordings: List of (audio_data, media_type) tuples.
            date: Journal entry date (ISO 8601).
            language: Language code for transcription.
            on_progress: Optional callback ``(current, total)`` called
                after each recording is transcribed.
        """
        if not recordings:
            raise ValueError("At least one audio recording is required")

        if len(recordings) == 1:
            return self.ingest_voice(
                recordings[0][0], recordings[0][1], date, language,
                source_type=source_type, skip_mood=skip_mood, user_id=user_id,
            )

        log.info(
            "Ingesting multi-voice entry for date %s (%d recordings)",
            date, len(recordings),
        )

        # Transcribe each recording and check for duplicates
        transcripts: list[str] = []
        per_recording_spans: list[list[tuple[int, int]]] = []
        file_hashes: list[str] = []
        file_media_types: list[str] = []
        for i, (audio_data, media_type) in enumerate(recordings):
            file_hash = hashlib.sha256(audio_data).hexdigest()
            if self._is_duplicate(file_hash):  # type: ignore[attr-defined]
                raise ValueError(
                    f"Recording {i + 1} has already been uploaded in "
                    f"another entry. Delete the existing entry first if "
                    f"you want to re-upload."
                )
            result = self._transcription.transcribe(  # type: ignore[attr-defined]
                audio_data, media_type, language,
            )
            text = result.text if hasattr(result, "text") else result  # type: ignore[assignment]
            if not text.strip():
                raise ValueError(
                    f"Transcription produced no text from recording {i + 1}"
                )
            stripped = text.strip()
            # Shift spans from raw-text coords to stripped-text coords
            # (same issue as _strip_and_shift_page_spans for OCR).
            leading = len(text) - len(text.lstrip())
            stripped_len = len(stripped)
            raw_spans = getattr(result, "uncertain_spans", [])
            shifted_spans: list[tuple[int, int]] = []
            for s, e in raw_spans:
                ns = max(0, s - leading)
                ne = min(stripped_len, e - leading)
                if ne > ns:
                    shifted_spans.append((ns, ne))
            transcripts.append(stripped)
            per_recording_spans.append(shifted_spans)
            file_hashes.append(file_hash)
            file_media_types.append(media_type)
            if on_progress is not None:
                on_progress(i + 1, len(recordings))

        # Combine transcripts with double newline — each recording segment
        # is a natural paragraph boundary in spoken journal entries, and
        # the blank line benefits downstream paragraph-aware chunking.
        raw_text = "\n\n".join(transcripts)

        # Try to extract a date from the combined transcription before
        # detection — parity with the OCR paths and single-voice. The
        # date typically appears at the very start of the first
        # recording, so this catches the regex-friendly forms even if
        # the LLM detector is disabled.
        from journal.services.date_extraction import extract_date_from_text

        extracted = extract_date_from_text(raw_text)
        if extracted:
            date = extracted

        # Heading detection runs against the combined raw text — the
        # date typically appears at the very start of the first
        # recording, so this catches it the same way single-voice does.
        # The detected date drives the entry's filing date (and thus the
        # title); it is NOT removed from the body, which keeps the date
        # phrase intact so the entry text reads naturally with the date
        # as its first line. If the detector resolved an ISO date, it
        # overrides — the LLM handles spelled-out and relative phrases
        # the regex can't.
        det = self._detect_heading(raw_text, date)  # type: ignore[attr-defined]
        if det.date_iso:
            date = det.date_iso
        formatted_body = (
            self._maybe_format_transcript(det.body)  # type: ignore[attr-defined]
            if det.body
            else det.body
        )

        word_count = len(raw_text.split())
        entry = self._repo.create_entry(  # type: ignore[attr-defined]
            date, source_type, raw_text, word_count, user_id=user_id,
            final_text=formatted_body if formatted_body != raw_text else None,
        )

        # Store source file records for each recording
        for i, (file_hash, media_type) in enumerate(
            zip(file_hashes, file_media_types, strict=True)
        ):
            self.store_source_file(  # type: ignore[attr-defined]
                entry.id, f"voice_{date}_part{i + 1}", media_type, file_hash,
            )

        # Shift per-recording uncertain spans into combined-text coordinates
        # (same approach as _strip_and_shift_page_spans for multi-page OCR).
        combined_spans: list[tuple[int, int]] = []
        cumulative_offset = 0
        for i, transcript in enumerate(transcripts):
            for start, end in per_recording_spans[i]:
                # Clip spans to the stripped transcript length
                clipped_end = min(end, len(transcript))
                if start < clipped_end:
                    combined_spans.append(
                        (cumulative_offset + start, cumulative_offset + clipped_end)
                    )
            cumulative_offset += len(transcript)
            if i < len(transcripts) - 1:
                cumulative_offset += 2  # the "\n\n" separator

        if combined_spans:
            self._repo.add_uncertain_spans(entry.id, combined_spans)  # type: ignore[attr-defined]

        # Chunk, embed, and store in vector DB
        chunk_count = self._process_text(  # type: ignore[attr-defined]
            entry.id, entry.final_text, date,
            skip_mood=skip_mood, user_id=user_id,
        )
        self._repo.update_chunk_count(entry.id, chunk_count)  # type: ignore[attr-defined]

        log.info(
            "Ingested multi-voice entry %d: %d recordings, %d words, date %s",
            entry.id, len(recordings), word_count, date,
        )
        return self._repo.get_entry(entry.id)  # type: ignore[attr-defined,return-value]
