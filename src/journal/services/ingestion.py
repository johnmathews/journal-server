"""Ingestion service — orchestrates OCR/transcription, chunking, embedding, and storage."""

import hashlib
import logging

from journal.db.repository import EntryRepository
from journal.models import Entry
from journal.providers.embeddings import EmbeddingsProvider
from journal.providers.ocr import OCRProvider
from journal.providers.transcription import TranscriptionProvider
from journal.services.chunking import chunk_text
from journal.vectorstore.store import VectorStore

log = logging.getLogger(__name__)


class IngestionService:
    def __init__(
        self,
        repository: EntryRepository,
        vector_store: VectorStore,
        ocr_provider: OCRProvider,
        transcription_provider: TranscriptionProvider,
        embeddings_provider: EmbeddingsProvider,
        chunk_max_tokens: int = 150,
        chunk_overlap_tokens: int = 40,
    ) -> None:
        self._repo = repository
        self._vector_store = vector_store
        self._ocr = ocr_provider
        self._transcription = transcription_provider
        self._embeddings = embeddings_provider
        self._chunk_max_tokens = chunk_max_tokens
        self._chunk_overlap_tokens = chunk_overlap_tokens

    def ingest_image(
        self, image_data: bytes, media_type: str, date: str
    ) -> Entry:
        """Ingest a journal page image: OCR -> chunk -> embed -> store."""
        log.info("Ingesting image for date %s (%s, %d bytes)", date, media_type, len(image_data))

        # Check for duplicate
        file_hash = hashlib.sha256(image_data).hexdigest()
        if self._is_duplicate(file_hash):
            raise ValueError(f"Image already ingested (hash: {file_hash[:12]}...)")

        # Extract text via OCR
        raw_text = self._ocr.extract_text(image_data, media_type)
        if not raw_text.strip():
            raise ValueError("OCR extracted no text from image")

        # Store entry
        word_count = len(raw_text.split())
        entry = self._repo.create_entry(date, "ocr", raw_text, word_count)
        self._store_source_file(entry.id, f"image_{date}", media_type, file_hash)

        # Chunk, embed, and store in vector DB
        self._process_text(entry.id, raw_text, date)

        log.info("Ingested image entry %d: %d words, date %s", entry.id, word_count, date)
        return entry

    def ingest_voice(
        self, audio_data: bytes, media_type: str, date: str, language: str = "en"
    ) -> Entry:
        """Ingest a voice note: transcribe -> chunk -> embed -> store."""
        log.info(
            "Ingesting voice note for date %s (%s, %d bytes)", date, media_type, len(audio_data)
        )

        file_hash = hashlib.sha256(audio_data).hexdigest()
        if self._is_duplicate(file_hash):
            raise ValueError(f"Audio already ingested (hash: {file_hash[:12]}...)")

        # Transcribe
        raw_text = self._transcription.transcribe(audio_data, media_type, language)
        if not raw_text.strip():
            raise ValueError("Transcription produced no text from audio")

        # Store entry
        word_count = len(raw_text.split())
        entry = self._repo.create_entry(date, "voice", raw_text, word_count)
        self._store_source_file(entry.id, f"voice_{date}", media_type, file_hash)

        # Chunk, embed, and store in vector DB
        self._process_text(entry.id, raw_text, date)

        log.info("Ingested voice entry %d: %d words, date %s", entry.id, word_count, date)
        return entry

    def _process_text(self, entry_id: int, text: str, date: str) -> None:
        """Chunk text, generate embeddings, store in vector DB."""
        chunks = chunk_text(text, self._chunk_max_tokens, self._chunk_overlap_tokens)
        if not chunks:
            log.warning("No chunks produced for entry %d", entry_id)
            return

        embeddings = self._embeddings.embed_texts(chunks)
        self._vector_store.add_entry(
            entry_id=entry_id,
            chunks=chunks,
            embeddings=embeddings,
            metadata={"entry_date": date},
        )
        log.info("Stored %d chunks with embeddings for entry %d", len(chunks), entry_id)

    def _is_duplicate(self, file_hash: str) -> bool:
        """Check if a file with this hash has already been ingested."""
        row = self._repo._conn.execute(  # type: ignore[attr-defined]
            "SELECT id FROM source_files WHERE file_hash = ?", (file_hash,)
        ).fetchone()
        return row is not None

    def _store_source_file(
        self, entry_id: int, file_path: str, file_type: str, file_hash: str
    ) -> None:
        sql = (
            "INSERT INTO source_files (entry_id, file_path, file_type, file_hash)"
            " VALUES (?, ?, ?, ?)"
        )
        self._repo._conn.execute(sql, (entry_id, file_path, file_type, file_hash))  # type: ignore[attr-defined]
        self._repo._conn.commit()  # type: ignore[attr-defined]
