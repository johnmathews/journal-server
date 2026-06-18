"""Ingestion service — orchestrates OCR/transcription, chunking, embedding, and storage."""

import datetime
import logging
from typing import TYPE_CHECKING

from journal.db.repository import EntryRepository
from journal.models import Entry
from journal.providers.embeddings import EmbeddingsProvider
from journal.providers.ocr import OCRProvider
from journal.providers.transcription import TranscriptionProvider
from journal.services.chunking import ChunkingStrategy
from journal.services.ingestion.image import _ImageIngestMixin
from journal.services.ingestion.text import _TextIngestMixin
from journal.services.ingestion.url_sources import _UrlIngestMixin
from journal.services.ingestion.voice import _VoiceIngestMixin
from journal.vectorstore.store import VectorStore

if TYPE_CHECKING:
    from journal.providers.formatter import FormatterProtocol
    from journal.services.heading_detector import (
        HeadingDetectionResult,
        HeadingDetector,
    )
    from journal.services.mood_scoring import MoodScoringService

log = logging.getLogger(__name__)


class IngestionService(
    _ImageIngestMixin, _VoiceIngestMixin, _TextIngestMixin, _UrlIngestMixin,
):
    """Ingests OCR/voice/text/URL content into journal entries.

    The class body wires up the constructor-injected collaborators
    and owns the mutating helpers (``save_final_text``,
    ``delete_entry``, ``reprocess_embeddings``, …) plus the
    cross-method internals (``_process_text``, ``_is_duplicate``,
    ``_detect_heading``, ``_maybe_preprocess``,
    ``_maybe_format_transcript``).

    Per-media-type ingest methods live on dedicated mixins so each
    cluster (image, voice, text, URL fetchers) sits in its own file
    while still sharing ``self``-bound state with the rest of the
    service.
    """

    def __init__(
        self,
        repository: EntryRepository,
        vector_store: VectorStore,
        ocr_provider: OCRProvider,
        transcription_provider: TranscriptionProvider,
        embeddings_provider: EmbeddingsProvider,
        chunker: ChunkingStrategy,
        slack_bot_token: str = "",
        embed_metadata_prefix: bool = True,
        preprocess_images: bool = True,
        mood_scoring: "MoodScoringService | None" = None,
        formatter: "FormatterProtocol | None" = None,
        heading_detector: "HeadingDetector | None" = None,
    ) -> None:
        self._repo = repository
        self._vector_store = vector_store
        self._ocr = ocr_provider
        self._transcription = transcription_provider
        self._embeddings = embeddings_provider
        self._chunker = chunker
        self._slack_bot_token = slack_bot_token
        self._embed_metadata_prefix = embed_metadata_prefix
        self._preprocess_images = preprocess_images
        # Optional mood scoring. When `None`, ingestion and update
        # paths skip the step entirely — no LLM calls, no DB
        # writes. When set, `_process_text` calls `score_entry` at
        # the end of every ingestion/update path so the stored
        # `mood_scores` stay consistent with the current
        # `final_text`. Scoring failures are logged by the service
        # and never propagate back into the ingestion flow.
        self._mood_scoring = mood_scoring
        # Optional transcript formatter. When set, voice ingestion
        # paths run the raw transcript through an LLM to insert
        # paragraph breaks, storing the formatted version as
        # final_text while keeping raw_text unchanged.
        self._formatter = formatter
        # Optional date-heading detector. When set, both voice and OCR
        # paths lift a leading date in the input into a markdown
        # heading on final_text. raw_text is never touched.
        self._heading_detector = heading_detector

    @property
    def vector_store(self) -> "VectorStore":
        """Read-only accessor mirroring ``QueryService.vector_store``.

        Operational tools (admin endpoints, the CLI's rechunk command,
        tests asserting post-ingest vector state) need a stable handle
        to the store; the leading-underscore attribute kept on signalling
        "private" without being so in practice.
        """
        return self._vector_store

    @property
    def repository(self) -> EntryRepository:
        """Read-only accessor for the ``EntryRepository`` instance.

        Public counterpart to the long-standing ``self._repo``.
        ``services/reload.py`` needs the repository to construct a
        new ``MoodScoringService`` when reloading mood dimensions
        (the repository owns the SQLite connection and can't be
        rebuilt from config); the ``repair_entity_names`` CLI uses
        it to enumerate entities. Both previously reached into
        ``ingestion._repo``.
        """
        return self._repo

    @property
    def mood_scoring(self) -> "MoodScoringService | None":
        """Read-only accessor for the optional mood-scoring service.

        ``reload_mood_dimensions`` reads this to decide whether to
        reuse the existing scoring service's repository or fall
        back to ``self._repo``.
        """
        return self._mood_scoring

    # ── runtime swap-in for hot-reload --------------------------------
    #
    # ``services/reload.py`` rebuilds the OCR / transcription / mood-
    # scoring providers from disk and rebinds them on the live
    # service. Python attribute writes are atomic, so a request mid-
    # call that already resolved e.g. ``self._ocr`` keeps its old
    # reference and finishes against it; the next request resolves
    # the attribute and gets the new one. No locks, no special
    # teardown — the old provider is garbage-collected once no
    # in-flight code holds a reference.
    #
    # These named methods exist so the reload helpers don't have to
    # write the underscore-prefixed attributes from outside the
    # class — the previous reach-in pattern was the only place in
    # production that wrote ``ingestion._ocr`` etc. directly.

    def replace_ocr(self, provider: "OCRProvider") -> None:
        """Atomically swap the OCR provider used by image ingestion."""
        self._ocr = provider

    def replace_transcription(
        self, provider: "TranscriptionProvider",
    ) -> None:
        """Atomically swap the transcription provider stack used by
        voice ingestion. The new provider can be a wrapped
        Retrying / Shadow stack.
        """
        self._transcription = provider

    def replace_mood_scoring(
        self, scoring: "MoodScoringService | None",
    ) -> None:
        """Atomically swap the optional mood-scoring service used at
        the end of every ingestion path. Pass ``None`` to disable
        mood scoring at runtime.
        """
        self._mood_scoring = scoring

    def replace_formatter(
        self, formatter: "FormatterProtocol | None",
    ) -> None:
        """Atomically swap the optional transcript formatter used by
        the voice ingest paths. Pass ``None`` to disable.
        """
        self._formatter = formatter

    def replace_heading_detector(
        self, detector: "HeadingDetector | None",
    ) -> None:
        """Atomically swap the optional date-heading detector used by
        OCR + voice paths. Pass ``None`` to disable.
        """
        self._heading_detector = detector

    def set_preprocess_images(self, enabled: bool) -> None:
        """Toggle the image preprocessing flag at runtime.

        Companion to the runtime-settings UI; controls whether the
        OCR path runs PIL preprocessing before sending to the model.
        """
        self._preprocess_images = enabled

    def _detect_heading(
        self, text: str, entry_date: str
    ) -> "HeadingDetectionResult":
        """Run the heading detector if available; fail safe to no-heading.

        The Anthropic detector already swallows API errors internally, but
        this wrapper makes the no-detector path explicit and centralises
        the exception net so any future detector implementation can crash
        without breaking ingestion.
        """
        from journal.services.heading_detector import HeadingDetectionResult

        if self._heading_detector is None:
            return HeadingDetectionResult(heading_text="", body=text)
        try:
            return self._heading_detector.detect(text, entry_date=entry_date)
        except Exception:
            log.warning(
                "Heading detection raised — using text unchanged",
                exc_info=True,
            )
            return HeadingDetectionResult(heading_text="", body=text)

    def _maybe_format_transcript(self, raw_text: str) -> str:
        """Run the transcript through the LLM formatter if available.

        Returns the formatted text, or *raw_text* unchanged if formatting
        is disabled or fails.
        """
        if self._formatter is None:
            return raw_text
        try:
            formatted = self._formatter.format_paragraphs(raw_text)
            if formatted != raw_text:
                log.info("Transcript formatted: %d → %d chars", len(raw_text), len(formatted))
            return formatted
        except Exception:
            log.warning("Transcript formatting failed — using raw text", exc_info=True)
            return raw_text

    def _maybe_preprocess(self, image_data: bytes, media_type: str) -> tuple[bytes, str]:
        """Apply image preprocessing if enabled."""
        if not self._preprocess_images:
            return image_data, media_type
        from journal.services.preprocessing import preprocess_image

        return preprocess_image(image_data, media_type)

    def _process_text(
        self, entry_id: int, text: str, date: str, *, skip_mood: bool = False,
        user_id: int = 1,
    ) -> int:
        """Chunk text, persist chunks, generate embeddings, store vectors.

        Returns the number of chunks produced.

        Persistence order is deliberate: chunks are written to SQLite
        (`entry_chunks` table via `replace_chunks`) BEFORE embeddings
        are computed. If the embedding call fails, the entry still has
        accurate offset information in SQLite, and the vector store is
        updated last — this matches the existing rechunk flow and keeps
        the failure modes contained.

        When `embed_metadata_prefix` is enabled, each chunk is embedded
        with a small date header prepended (e.g. "Date: 2026-02-15.
        Sunday.\\n\\n<chunk>"), but the un-prefixed chunk text is stored
        as the ChromaDB document so downstream consumers get clean text
        back. This helps date-sensitive semantic queries match the right
        entries without polluting the stored content.
        """
        chunks = self._chunker.chunk(text)
        if not chunks:
            log.warning("No chunks produced for entry %d", entry_id)
            # Still clear any stale persisted chunks from a prior run.
            self._repo.replace_chunks(entry_id, [])
            return 0

        # Persist chunks (with offsets) to SQLite so the webapp overlay
        # and any re-read path can recover them without re-running the
        # chunker.
        self._repo.replace_chunks(entry_id, chunks)

        chunk_texts = [c.text for c in chunks]

        if self._embed_metadata_prefix:
            try:
                weekday = datetime.date.fromisoformat(date).strftime("%A")
                prefix = f"Date: {date}. {weekday}.\n\n"
            except ValueError:
                # Malformed date — fall back to a no-weekday prefix rather
                # than dropping the context entirely.
                log.warning("Entry %d has malformed date %r, skipping weekday", entry_id, date)
                prefix = f"Date: {date}.\n\n"
            embed_inputs = [f"{prefix}{t}" for t in chunk_texts]
        else:
            embed_inputs = chunk_texts

        embeddings = self._embeddings.embed_texts(embed_inputs)
        self._vector_store.add_entry(
            entry_id=entry_id,
            chunks=chunk_texts,  # store un-prefixed text
            embeddings=embeddings,  # computed from prefixed text
            metadata={"entry_date": date, "user_id": user_id},
        )
        log.info("Stored %d chunks with embeddings for entry %d", len(chunks), entry_id)

        # Optional mood scoring. Called after embeddings so that a
        # scoring failure cannot roll back the (expensive)
        # embedding step. `score_entry` never raises — it logs and
        # returns 0 on failure — so ingestion continues cleanly
        # even if the LLM is unreachable.
        if self._mood_scoring is not None and not skip_mood:
            self._mood_scoring.score_entry(entry_id, text)

        return len(chunks)

    def _is_duplicate(self, file_hash: str) -> bool:
        """Check if a file with this hash has already been ingested."""
        row = self._repo.connection.execute(
            "SELECT id FROM source_files WHERE file_hash = ?", (file_hash,)
        ).fetchone()
        return row is not None

    def update_entry_text(self, entry_id: int, final_text: str) -> Entry:
        """Update an entry's final_text and re-process embeddings.

        Args:
            entry_id: The entry to update.
            final_text: The corrected text.
        """
        entry = self._repo.get_entry(entry_id)
        if entry is None:
            raise ValueError(f"Entry {entry_id} not found")

        log.info("Updating final_text for entry %d", entry_id)

        word_count = len(final_text.split())

        # Delete old vector chunks
        self._vector_store.delete_entry(entry_id)

        # Re-chunk, re-embed, store new vectors
        chunk_count = self._process_text(entry_id, final_text, entry.entry_date)

        # Update SQLite (FTS5 trigger handles re-indexing automatically)
        updated = self._repo.update_final_text(entry_id, final_text, word_count, chunk_count)

        log.info("Updated entry %d: %d words, %d chunks", entry_id, word_count, chunk_count)
        return updated  # type: ignore[return-value]

    def save_final_text(
        self, entry_id: int, final_text: str, *, user_id: int | None = None,
    ) -> Entry:
        """Update an entry's final_text in SQLite only (fast).

        Does NOT re-chunk or re-embed. Call ``reprocess_embeddings``
        separately for the slow embedding pipeline.
        """
        entry = self._repo.get_entry(entry_id, user_id=user_id)
        if entry is None:
            raise ValueError(f"Entry {entry_id} not found")

        word_count = len(final_text.split())
        updated = self._repo.update_final_text(
            entry_id, final_text, word_count, entry.chunk_count, user_id=user_id,
        )
        log.info("Saved final_text for entry %d (%d words)", entry_id, word_count)
        return updated  # type: ignore[return-value]

    def reprocess_embeddings(self, entry_id: int, *, user_id: int = 1) -> int:
        """Re-chunk and re-embed an entry's text (slow).

        Deletes old vectors, re-chunks, calls the embedding provider,
        stores new vectors, and updates the chunk_count in SQLite.
        Returns the number of chunks produced.
        """
        entry = self._repo.get_entry(entry_id)
        if entry is None:
            raise ValueError(f"Entry {entry_id} not found")

        text = entry.final_text or entry.raw_text
        if not text or not text.strip():
            raise ValueError(f"Entry {entry_id} has no text to embed")

        self._vector_store.delete_entry(entry_id)
        chunk_count = self._process_text(entry_id, text, entry.entry_date, user_id=user_id)
        self._repo.update_chunk_count(entry_id, chunk_count)
        log.info("Reprocessed embeddings for entry %d: %d chunks", entry_id, chunk_count)
        return chunk_count

    def delete_entry(self, entry_id: int, *, user_id: int | None = None) -> bool:
        """Delete an entry from both SQLite and the vector store.

        Returns True if the entry existed and was removed, False if it
        was not found. Vector chunks are removed first so that a failure
        to clean up ChromaDB surfaces before we drop the SQLite row.
        """
        entry = self._repo.get_entry(entry_id, user_id=user_id)
        if entry is None:
            return False

        log.info("Deleting entry %d", entry_id)
        self._vector_store.delete_entry(entry_id)
        return self._repo.delete_entry(entry_id, user_id=user_id)

    def rechunk_entry(self, entry_id: int, *, dry_run: bool = False, user_id: int = 1) -> int:
        """Re-chunk and re-embed an existing entry in place.

        Used by the `journal rechunk` CLI and by future tuning scripts.
        Reads `final_text` (or falls back to `raw_text`), runs the full
        chunk → embed → store pipeline, updates the stored chunk_count.
        Does not touch the entry's text content.

        When `dry_run=True`, runs the chunker only — no embeddings are
        computed, the vector store is left alone, and `chunk_count` in
        SQLite is NOT updated. Returns the chunk count that would have
        been written.
        """
        entry = self._repo.get_entry(entry_id)
        if entry is None:
            raise ValueError(f"Entry {entry_id} not found")

        text = entry.final_text or entry.raw_text
        if not text or not text.strip():
            log.warning("Entry %d has no text — skipping rechunk", entry_id)
            return 0

        if dry_run:
            return len(self._chunker.chunk(text))

        # Delete the old vectors BEFORE producing new ones so we don't
        # accidentally end up with duplicates if _process_text fails
        # partway through. If embed_texts then raises, the entry will
        # have zero chunks in ChromaDB — callers should handle this and
        # (re-)retry.
        self._vector_store.delete_entry(entry_id)
        chunk_count = self._process_text(entry_id, text, entry.entry_date, user_id=user_id)
        self._repo.update_chunk_count(entry_id, chunk_count)
        return chunk_count

    def get_page_count(self, entry_id: int) -> int:
        """Per-entry page count. Public pass-through; same shape as
        ``QueryService.get_page_count`` so api/ ingest routes don't need to
        reach into ``self._repo`` to enrich the response payload.
        """
        return self._repo.get_page_count(entry_id)

    def update_entry_date(
        self, entry_id: int, entry_date: str, *, user_id: int | None = None,
    ) -> Entry | None:
        """Update an entry's date. Write — lives on IngestionService rather
        than QueryService so api/ routes use the service that owns mutating
        operations (Unit 1b carryover from refactor-follow-ups item 5).
        """
        return self._repo.update_entry_date(entry_id, entry_date, user_id=user_id)

    def verify_doubts(
        self, entry_id: int, *, user_id: int | None = None,
    ) -> bool:
        """Mark all uncertain spans on an entry as verified. Write — same
        ownership rationale as ``update_entry_date``.
        """
        return self._repo.verify_doubts(entry_id, user_id=user_id)

    def update_content_window(
        self,
        entry_id: int,
        start: int | None,
        end: int | None,
        user_id: int = 1,
    ) -> "Entry":
        """Set the content window and re-derive final_text from the slice.

        When *start* and *end* are both ``None`` the window is cleared and
        ``final_text`` is re-derived from the full ``raw_text``.  Otherwise
        the slice ``raw_text[start:end]`` is used after heading detection.
        Raises ``ValueError`` if the sliced content is empty or the entry is
        not found.
        """
        from journal.services.date_extraction import extract_date_from_text

        entry = self._repo.get_entry(entry_id, user_id=user_id)
        if entry is None:
            raise ValueError(f"Entry {entry_id} not found")

        # Persist the new window coordinates first.
        self._repo.set_content_window(entry_id, start, end, user_id=user_id)

        # Re-derive the content slice.
        lo = start if start is not None else 0
        hi = end if end is not None else len(entry.raw_text or "")
        content = (entry.raw_text or "")[lo:hi]

        if not content.strip():
            raise ValueError(
                f"content window [{lo}:{hi}] produces empty content for entry {entry_id}"
            )

        # Optionally detect and strip a leading date heading.
        date = entry.entry_date
        extracted = extract_date_from_text(content)
        if extracted:
            date = extracted
        det = self._detect_heading(content, date)
        final_text = det.body if det.has_heading else content

        # Persist the updated final_text (updates word_count, leaves chunk_count).
        return self.save_final_text(entry_id, final_text, user_id=user_id)

    def store_source_file(
        self, entry_id: int, file_path: str, file_type: str, file_hash: str
    ) -> int | None:
        """Store source file metadata. Returns the source_file id."""
        sql = (
            "INSERT INTO source_files (entry_id, file_path, file_type, file_hash)"
            " VALUES (?, ?, ?, ?)"
        )
        conn = self._repo.connection
        cursor = conn.execute(sql, (entry_id, file_path, file_type, file_hash))
        conn.commit()
        return cursor.lastrowid
