"""Ingestion service — orchestrates OCR/transcription, chunking, embedding, and storage."""

import hashlib
import logging
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

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
        slack_bot_token: str = "",
    ) -> None:
        self._repo = repository
        self._vector_store = vector_store
        self._ocr = ocr_provider
        self._transcription = transcription_provider
        self._embeddings = embeddings_provider
        self._chunk_max_tokens = chunk_max_tokens
        self._chunk_overlap_tokens = chunk_overlap_tokens
        self._slack_bot_token = slack_bot_token

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

        # Store entry (final_text defaults to raw_text)
        word_count = len(raw_text.split())
        entry = self._repo.create_entry(date, "ocr", raw_text, word_count)
        source_file_id = self._store_source_file(
            entry.id, f"image_{date}", media_type, file_hash,
        )

        # Add page record
        self._repo.add_entry_page(entry.id, 1, raw_text, source_file_id)

        # Chunk, embed, and store in vector DB
        chunk_count = self._process_text(entry.id, entry.final_text, date)
        self._repo.update_chunk_count(entry.id, chunk_count)

        log.info("Ingested image entry %d: %d words, date %s", entry.id, word_count, date)
        return self._repo.get_entry(entry.id)  # type: ignore[return-value]

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

        # Store entry (final_text defaults to raw_text)
        word_count = len(raw_text.split())
        entry = self._repo.create_entry(date, "voice", raw_text, word_count)
        self._store_source_file(entry.id, f"voice_{date}", media_type, file_hash)

        # Chunk, embed, and store in vector DB
        chunk_count = self._process_text(entry.id, entry.final_text, date)
        self._repo.update_chunk_count(entry.id, chunk_count)

        log.info("Ingested voice entry %d: %d words, date %s", entry.id, word_count, date)
        return self._repo.get_entry(entry.id)  # type: ignore[return-value]

    def ingest_image_from_url(
        self,
        url: str,
        date: str,
        media_type: str | None = None,
    ) -> Entry:
        """Download an image from a URL and ingest it."""
        data, resolved_type = self._download(url, media_type)
        return self.ingest_image(data, resolved_type, date)

    def ingest_voice_from_url(
        self,
        url: str,
        date: str,
        media_type: str | None = None,
        language: str = "en",
    ) -> Entry:
        """Download audio from a URL and ingest it."""
        data, resolved_type = self._download(url, media_type)
        return self.ingest_voice(data, resolved_type, date, language)

    def _download(
        self, url: str, media_type: str | None = None
    ) -> tuple[bytes, str]:
        """Download a file from a URL, return (data, media_type)."""
        log.info("Downloading from %s", url)
        try:
            req = Request(url, headers={"User-Agent": "journal-agent/0.1"})
            if (
                "files.slack.com" in url
                and self._slack_bot_token
            ):
                req.add_header(
                    "Authorization",
                    f"Bearer {self._slack_bot_token}",
                )
            with urlopen(req) as resp:  # noqa: S310
                data = resp.read()
                if media_type is None:
                    media_type = resp.headers.get(
                        "Content-Type", "application/octet-stream"
                    )
        except HTTPError as e:
            raise ValueError(
                f"Failed to download {url}: HTTP {e.code}"
            ) from e
        except URLError as e:
            raise ValueError(
                f"Failed to download {url}: {e.reason}"
            ) from e

        log.info("Downloaded %d bytes (type: %s)", len(data), media_type)
        return data, media_type

    def _process_text(self, entry_id: int, text: str, date: str) -> int:
        """Chunk text, generate embeddings, store in vector DB. Returns chunk count."""
        chunks = chunk_text(text, self._chunk_max_tokens, self._chunk_overlap_tokens)
        if not chunks:
            log.warning("No chunks produced for entry %d", entry_id)
            return 0

        embeddings = self._embeddings.embed_texts(chunks)
        self._vector_store.add_entry(
            entry_id=entry_id,
            chunks=chunks,
            embeddings=embeddings,
            metadata={"entry_date": date},
        )
        log.info("Stored %d chunks with embeddings for entry %d", len(chunks), entry_id)
        return len(chunks)

    def _is_duplicate(self, file_hash: str) -> bool:
        """Check if a file with this hash has already been ingested."""
        row = self._repo._conn.execute(  # type: ignore[attr-defined]
            "SELECT id FROM source_files WHERE file_hash = ?", (file_hash,)
        ).fetchone()
        return row is not None

    def ingest_multi_page_entry(
        self,
        images: list[tuple[bytes, str]],
        date: str,
    ) -> Entry:
        """Ingest multiple page images as a single journal entry.

        Args:
            images: List of (image_data, media_type) tuples, one per page in order.
            date: Journal entry date (ISO 8601).
        """
        if not images:
            raise ValueError("At least one image is required")

        log.info("Ingesting multi-page entry for date %s (%d pages)", date, len(images))

        # OCR each page and check for duplicates
        page_texts: list[str] = []
        page_hashes: list[str] = []
        page_media_types: list[str] = []
        for i, (image_data, media_type) in enumerate(images):
            file_hash = hashlib.sha256(image_data).hexdigest()
            if self._is_duplicate(file_hash):
                raise ValueError(
                    f"Page {i + 1} already ingested (hash: {file_hash[:12]}...)"
                )
            raw_text = self._ocr.extract_text(image_data, media_type)
            if not raw_text.strip():
                raise ValueError(f"OCR extracted no text from page {i + 1}")
            page_texts.append(raw_text)
            page_hashes.append(file_hash)
            page_media_types.append(media_type)

        # Combine page texts
        combined_text = "\n\n".join(page_texts)
        word_count = len(combined_text.split())

        # Create single entry
        entry = self._repo.create_entry(date, "ocr", combined_text, word_count)

        # Store source files and pages
        for i, (image_data, _) in enumerate(images):
            source_file_id = self._store_source_file(
                entry.id, f"image_{date}_p{i + 1}", page_media_types[i], page_hashes[i],
            )
            self._repo.add_entry_page(entry.id, i + 1, page_texts[i], source_file_id)

        # Chunk, embed, and store
        chunk_count = self._process_text(entry.id, entry.final_text, date)
        self._repo.update_chunk_count(entry.id, chunk_count)

        log.info(
            "Ingested multi-page entry %d: %d pages, %d words, date %s",
            entry.id, len(images), word_count, date,
        )
        return self._repo.get_entry(entry.id)  # type: ignore[return-value]

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

    def _store_source_file(
        self, entry_id: int, file_path: str, file_type: str, file_hash: str
    ) -> int | None:
        """Store source file metadata. Returns the source_file id."""
        sql = (
            "INSERT INTO source_files (entry_id, file_path, file_type, file_hash)"
            " VALUES (?, ?, ?, ?)"
        )
        cursor = self._repo._conn.execute(sql, (entry_id, file_path, file_type, file_hash))  # type: ignore[attr-defined]
        self._repo._conn.commit()  # type: ignore[attr-defined]
        return cursor.lastrowid
