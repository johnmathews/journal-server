"""Tests for ingestion service."""

from unittest.mock import MagicMock

import pytest

from journal.db.repository import SQLiteEntryRepository
from journal.services.ingestion import IngestionService
from journal.vectorstore.store import InMemoryVectorStore


@pytest.fixture
def mock_ocr():
    provider = MagicMock()
    provider.extract_text.return_value = "Today I walked through Vienna and met Atlas for coffee."
    return provider


@pytest.fixture
def mock_transcription():
    provider = MagicMock()
    provider.transcribe.return_value = "Voice journal entry about my day at work."
    return provider


@pytest.fixture
def mock_embeddings():
    provider = MagicMock()
    provider.embed_texts.return_value = [[0.1, 0.2, 0.3]]
    provider.embed_query.return_value = [0.1, 0.2, 0.3]
    return provider


@pytest.fixture
def ingestion_service(db_conn, mock_ocr, mock_transcription, mock_embeddings):
    repo = SQLiteEntryRepository(db_conn)
    vector_store = InMemoryVectorStore()
    return IngestionService(
        repository=repo,
        vector_store=vector_store,
        ocr_provider=mock_ocr,
        transcription_provider=mock_transcription,
        embeddings_provider=mock_embeddings,
    )


class TestIngestImage:
    def test_ingest_image(self, ingestion_service, mock_ocr, mock_embeddings):
        entry = ingestion_service.ingest_image(
            image_data=b"fake image data",
            media_type="image/jpeg",
            date="2026-03-22",
        )

        assert entry.entry_date == "2026-03-22"
        assert entry.source_type == "ocr"
        assert "Vienna" in entry.raw_text
        assert entry.word_count == 10

        mock_ocr.extract_text.assert_called_once_with(b"fake image data", "image/jpeg")
        mock_embeddings.embed_texts.assert_called_once()

    def test_ingest_image_duplicate(self, ingestion_service):
        ingestion_service.ingest_image(b"same data", "image/jpeg", "2026-03-22")

        with pytest.raises(ValueError, match="already ingested"):
            ingestion_service.ingest_image(b"same data", "image/jpeg", "2026-03-23")

    def test_ingest_image_empty_text(self, ingestion_service, mock_ocr):
        mock_ocr.extract_text.return_value = "   "

        with pytest.raises(ValueError, match="no text"):
            ingestion_service.ingest_image(b"blank page", "image/jpeg", "2026-03-22")


class TestIngestVoice:
    def test_ingest_voice(self, ingestion_service, mock_transcription, mock_embeddings):
        entry = ingestion_service.ingest_voice(
            audio_data=b"fake audio data",
            media_type="audio/mp3",
            date="2026-03-22",
        )

        assert entry.entry_date == "2026-03-22"
        assert entry.source_type == "voice"
        assert "work" in entry.raw_text

        mock_transcription.transcribe.assert_called_once_with(b"fake audio data", "audio/mp3", "en")
        mock_embeddings.embed_texts.assert_called_once()

    def test_ingest_voice_duplicate(self, ingestion_service):
        ingestion_service.ingest_voice(b"same audio", "audio/mp3", "2026-03-22")

        with pytest.raises(ValueError, match="already ingested"):
            ingestion_service.ingest_voice(b"same audio", "audio/mp3", "2026-03-23")


class TestIngestImageUpdates:
    def test_ingest_image_sets_final_text(self, ingestion_service):
        entry = ingestion_service.ingest_image(b"page data", "image/jpeg", "2026-03-22")
        assert entry.final_text == entry.raw_text
        assert entry.final_text != ""

    def test_ingest_image_sets_chunk_count(self, ingestion_service):
        entry = ingestion_service.ingest_image(b"page data", "image/jpeg", "2026-03-22")
        assert entry.chunk_count > 0

    def test_ingest_image_creates_page(self, ingestion_service):
        entry = ingestion_service.ingest_image(b"page data", "image/jpeg", "2026-03-22")
        pages = ingestion_service._repo.get_entry_pages(entry.id)
        assert len(pages) == 1
        assert pages[0].page_number == 1
        assert pages[0].raw_text == entry.raw_text

    def test_ingest_voice_sets_final_text(self, ingestion_service):
        entry = ingestion_service.ingest_voice(b"audio data", "audio/mp3", "2026-03-22")
        assert entry.final_text == entry.raw_text

    def test_ingest_voice_sets_chunk_count(self, ingestion_service):
        entry = ingestion_service.ingest_voice(b"audio data", "audio/mp3", "2026-03-22")
        assert entry.chunk_count > 0

    def test_ingest_voice_no_pages(self, ingestion_service):
        entry = ingestion_service.ingest_voice(b"audio data", "audio/mp3", "2026-03-22")
        pages = ingestion_service._repo.get_entry_pages(entry.id)
        assert len(pages) == 0


class TestMultiPageIngestion:
    def test_ingest_multi_page(self, ingestion_service, mock_ocr, mock_embeddings):
        mock_ocr.extract_text.side_effect = ["Page one text.", "Page two text."]
        entry = ingestion_service.ingest_multi_page_entry(
            images=[(b"img1", "image/jpeg"), (b"img2", "image/jpeg")],
            date="2026-03-22",
        )

        assert entry.entry_date == "2026-03-22"
        assert entry.source_type == "ocr"
        assert "Page one text." in entry.raw_text
        assert "Page two text." in entry.raw_text
        assert entry.raw_text == "Page one text.\n\nPage two text."
        assert entry.final_text == entry.raw_text
        assert entry.chunk_count > 0

    def test_ingest_multi_page_creates_pages(self, ingestion_service, mock_ocr):
        mock_ocr.extract_text.side_effect = ["First page.", "Second page."]
        entry = ingestion_service.ingest_multi_page_entry(
            images=[(b"img1", "image/jpeg"), (b"img2", "image/jpeg")],
            date="2026-03-22",
        )

        pages = ingestion_service._repo.get_entry_pages(entry.id)
        assert len(pages) == 2
        assert pages[0].page_number == 1
        assert pages[0].raw_text == "First page."
        assert pages[1].page_number == 2
        assert pages[1].raw_text == "Second page."

    def test_ingest_multi_page_empty_list(self, ingestion_service):
        with pytest.raises(ValueError, match="At least one image"):
            ingestion_service.ingest_multi_page_entry(images=[], date="2026-03-22")

    def test_ingest_multi_page_duplicate_page(self, ingestion_service, mock_ocr):
        # First page OCRs fine, second has same hash so should fail at duplicate check
        # But both hashes are checked before OCR in the loop, so the first image
        # passes hash check + OCR, and the second fails at hash check
        mock_ocr.extract_text.return_value = "Page text."
        ingestion_service.ingest_image(b"same", "image/jpeg", "2026-03-22")

        mock_ocr.extract_text.return_value = "Another page."
        with pytest.raises(ValueError, match="already ingested"):
            ingestion_service.ingest_multi_page_entry(
                images=[(b"same", "image/jpeg")],
                date="2026-03-23",
            )


class TestUpdateEntryText:
    def test_update_entry_text(self, ingestion_service, mock_embeddings):
        entry = ingestion_service.ingest_image(b"page data", "image/jpeg", "2026-03-22")
        original_text = entry.final_text

        mock_embeddings.embed_texts.reset_mock()
        updated = ingestion_service.update_entry_text(entry.id, "Corrected text here.")

        assert updated.final_text == "Corrected text here."
        assert updated.raw_text == original_text  # raw_text unchanged
        assert updated.word_count == 3
        assert updated.chunk_count > 0
        mock_embeddings.embed_texts.assert_called_once()  # re-embedded

    def test_update_entry_text_not_found(self, ingestion_service):
        with pytest.raises(ValueError, match="not found"):
            ingestion_service.update_entry_text(999, "text")
