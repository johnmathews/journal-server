"""Tests for ingestion service."""

from unittest.mock import MagicMock

import pytest

from journal.db.repository import SQLiteEntryRepository
from journal.services.chunking import FixedTokenChunker
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
        chunker=FixedTokenChunker(max_tokens=150, overlap_tokens=40),
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


class TestChunkPersistence:
    """Chunks produced during ingestion must land in the entry_chunks table
    with the offsets the chunker computed, so the webapp overlay can read
    them back without re-running the chunker."""

    def test_ingest_image_persists_chunks(self, ingestion_service):
        entry = ingestion_service.ingest_image(b"page data", "image/jpeg", "2026-03-22")
        stored = ingestion_service._repo.get_chunks(entry.id)
        assert len(stored) == entry.chunk_count
        assert len(stored) > 0
        # Every persisted chunk must have its source range contained
        # within the entry's text.
        for chunk in stored:
            assert 0 <= chunk.char_start <= chunk.char_end <= len(entry.final_text)
            assert chunk.token_count > 0

    def test_ingest_voice_persists_chunks(self, ingestion_service):
        entry = ingestion_service.ingest_voice(b"audio data", "audio/mp3", "2026-03-22")
        stored = ingestion_service._repo.get_chunks(entry.id)
        assert len(stored) == entry.chunk_count
        assert len(stored) > 0

    def test_update_entry_text_replaces_chunks(self, ingestion_service):
        entry = ingestion_service.ingest_image(b"page data", "image/jpeg", "2026-03-22")
        original_chunks = ingestion_service._repo.get_chunks(entry.id)
        assert len(original_chunks) > 0

        new_text = "Completely different corrected text for the entry."
        ingestion_service.update_entry_text(entry.id, new_text)

        updated_chunks = ingestion_service._repo.get_chunks(entry.id)
        # New chunks reflect the new text.
        assert len(updated_chunks) > 0
        assert updated_chunks[0].text != original_chunks[0].text
        # All chunk offsets fit within the new text.
        for chunk in updated_chunks:
            assert chunk.char_end <= len(new_text)

    def test_delete_entry_removes_chunks(self, ingestion_service):
        entry = ingestion_service.ingest_image(b"page data", "image/jpeg", "2026-03-22")
        assert len(ingestion_service._repo.get_chunks(entry.id)) > 0
        ingestion_service.delete_entry(entry.id)
        assert ingestion_service._repo.get_chunks(entry.id) == []


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
        # Pages are joined with a single newline (not "\n\n") so page
        # boundaries don't force paragraph splits in the chunker. See
        # the join comment in `ingest_multi_page_entry` for the full
        # rationale.
        assert entry.raw_text == "Page one text.\nPage two text."
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

    def test_ingest_multi_page_strips_trailing_whitespace_before_join(
        self, ingestion_service, mock_ocr
    ):
        """Pages with trailing newlines must not re-introduce a blank-line join.

        OCR providers typically end their output with a newline. If we
        joined with `"\\n"` naively we'd get `"Page one.\\n\\nPage two."`
        which recreates the paragraph-split bug this change fixes.
        """
        mock_ocr.extract_text.side_effect = ["Page one.\n", "Page two.\n"]
        entry = ingestion_service.ingest_multi_page_entry(
            images=[(b"img1", "image/jpeg"), (b"img2", "image/jpeg")],
            date="2026-03-24",
        )
        assert entry.raw_text == "Page one.\nPage two."
        assert "\n\n" not in entry.raw_text

    def test_ingest_multi_page_packs_efficiently(
        self, ingestion_service, mock_ocr, mock_embeddings
    ):
        """Regression for 277-word/5-chunk pathology.

        With the old `"\\n\\n"` page join, three moderate pages
        (each ~80 tokens, well under the 150-token budget) were each
        flushed as their own chunk because adding the next page's
        paragraph would exceed the budget. After the fix, the greedy
        packer can combine pages up to the real budget and we should
        see ~2 chunks instead of 3.
        """
        # Three ~82-token pages, each a single paragraph. Any two pages
        # combined exceed the 150-token budget (2*82=164) so packing
        # must flush somewhere — but the chunker should cross the page
        # boundary freely, not use page boundaries as preferred cut
        # points. Old `"\n\n"` join: 3 chunks of ~82 each (budget 55%
        # utilised). New `"\n"` join: 2 chunks of ~140 each (budget 93%
        # utilised).
        page = (
            "Woke up late and the sky was grey, drizzly, the kind of "
            "morning that makes you want to stay under the covers. Made "
            "coffee and sat by the window watching the crows argue over "
            "the suet block hanging from the birch. Felt oddly content "
            "despite everything piling up. Thought again about Friday's "
            "meeting and whether to finally raise the staffing issue "
            "with the whole team present this time."
        )
        mock_ocr.extract_text.side_effect = [page, page, page]
        # The default mock returns one embedding regardless of input.
        # Provide a callable side_effect so the chunker-produced list
        # gets a matching number of vectors back.
        mock_embeddings.embed_texts.side_effect = lambda texts: [
            [0.1, 0.2, 0.3] for _ in texts
        ]
        entry = ingestion_service.ingest_multi_page_entry(
            images=[
                (b"img1", "image/jpeg"),
                (b"img2", "image/jpeg"),
                (b"img3", "image/jpeg"),
            ],
            date="2026-03-25",
        )
        # With the old "\n\n" join this produced 3 chunks of ~85 tokens
        # each (budget underfilled). With the new "\n" join the chunker
        # packs material up toward the 150-token budget, yielding 2
        # chunks. Assert strictly 2 to lock in the improvement.
        assert entry.chunk_count == 2, (
            f"expected 2 chunks for 3 moderate pages, got {entry.chunk_count} "
            "— page-join separator regression"
        )

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


class TestMetadataPrefix:
    """WU-E: chunks are embedded with a date prefix but stored as plain text."""

    @pytest.fixture
    def service_with_prefix(self, db_conn, mock_ocr, mock_transcription, mock_embeddings):
        repo = SQLiteEntryRepository(db_conn)
        vector_store = InMemoryVectorStore()
        return IngestionService(
            repository=repo,
            vector_store=vector_store,
            ocr_provider=mock_ocr,
            transcription_provider=mock_transcription,
            embeddings_provider=mock_embeddings,
            chunker=FixedTokenChunker(max_tokens=150, overlap_tokens=40),
            embed_metadata_prefix=True,
        )

    @pytest.fixture
    def service_without_prefix(self, db_conn, mock_ocr, mock_transcription, mock_embeddings):
        repo = SQLiteEntryRepository(db_conn)
        vector_store = InMemoryVectorStore()
        return IngestionService(
            repository=repo,
            vector_store=vector_store,
            ocr_provider=mock_ocr,
            transcription_provider=mock_transcription,
            embeddings_provider=mock_embeddings,
            chunker=FixedTokenChunker(max_tokens=150, overlap_tokens=40),
            embed_metadata_prefix=False,
        )

    def test_embed_texts_receives_date_prefix_when_enabled(
        self, service_with_prefix, mock_embeddings
    ):
        service_with_prefix.ingest_image(b"fake image", "image/jpeg", "2026-02-15")

        # Inspect the actual text passed to embed_texts.
        call_args = mock_embeddings.embed_texts.call_args
        embed_inputs = call_args.args[0] if call_args.args else call_args.kwargs["texts"]
        # Every input should start with the date header.
        for text in embed_inputs:
            assert text.startswith("Date: 2026-02-15. Sunday.\n\n")

    def test_embed_texts_has_no_prefix_when_disabled(
        self, service_without_prefix, mock_embeddings
    ):
        service_without_prefix.ingest_image(b"fake image", "image/jpeg", "2026-02-15")

        call_args = mock_embeddings.embed_texts.call_args
        embed_inputs = call_args.args[0] if call_args.args else call_args.kwargs["texts"]
        for text in embed_inputs:
            assert not text.startswith("Date:")

    def test_vector_store_receives_unprefixed_chunks(
        self, service_with_prefix, mock_ocr
    ):
        # OCR returns a deterministic string so we can compare exactly.
        mock_ocr.extract_text.return_value = "This is the raw journal text."
        service_with_prefix.ingest_image(b"fake image", "image/jpeg", "2026-02-15")

        # Fetch what was stored in the vector store for this entry.
        stored = service_with_prefix._vector_store.get_chunks_for_entry(1) \
            if hasattr(service_with_prefix._vector_store, "get_chunks_for_entry") else None

        # Fall back to searching — InMemoryVectorStore doesn't yet expose
        # get_chunks_for_entry (that's added in WU-H). Assert via a search.
        if stored is None:
            results = service_with_prefix._vector_store.search(
                query_embedding=[0.1, 0.2, 0.3], limit=10
            )
            assert len(results) >= 1
            # The stored chunk text should be exactly what the OCR produced,
            # NOT prefixed with "Date: ...".
            assert results[0].chunk_text == "This is the raw journal text."
            assert not results[0].chunk_text.startswith("Date:")

    def test_weekday_calculation(self, service_with_prefix, mock_embeddings):
        # 2026-02-15 is a Sunday.
        service_with_prefix.ingest_image(b"fake image", "image/jpeg", "2026-02-15")
        embed_inputs = mock_embeddings.embed_texts.call_args.args[0]
        assert "Sunday" in embed_inputs[0]

    def test_malformed_date_falls_back_gracefully(
        self, service_with_prefix, mock_embeddings
    ):
        # An invalid date shouldn't crash ingestion — fall back to a
        # prefix without a weekday.
        service_with_prefix.ingest_image(b"fake image", "image/jpeg", "not-a-date")
        embed_inputs = mock_embeddings.embed_texts.call_args.args[0]
        assert embed_inputs[0].startswith("Date: not-a-date.\n\n")
        # No weekday component when the date can't be parsed.
        assert "Monday" not in embed_inputs[0]
        assert "Sunday" not in embed_inputs[0]


class TestRechunkEntry:
    """WU-D: IngestionService.rechunk_entry() end-to-end test with an in-memory
    vector store so we can observe the old → new chunk transition."""

    def test_rechunk_replaces_existing_vectors(self, ingestion_service, mock_embeddings):
        # Ingest once with a chunker that produces N chunks.
        entry = ingestion_service.ingest_image(
            b"fake image", "image/jpeg", "2026-03-22"
        )
        original_chunks = entry.chunk_count
        assert original_chunks > 0

        # Reset the embeddings mock so we can assert the rechunk call.
        mock_embeddings.embed_texts.reset_mock()

        new_count = ingestion_service.rechunk_entry(entry.id)

        # Rechunk called embed_texts again (one call for the new chunks).
        mock_embeddings.embed_texts.assert_called_once()
        # The stored chunk_count is updated.
        refreshed = ingestion_service._repo.get_entry(entry.id)
        assert refreshed.chunk_count == new_count

    def test_rechunk_missing_entry_raises(self, ingestion_service):
        with pytest.raises(ValueError, match="not found"):
            ingestion_service.rechunk_entry(999)

    def test_rechunk_dry_run_does_not_touch_embeddings_or_db(
        self, ingestion_service, mock_embeddings
    ):
        entry = ingestion_service.ingest_image(
            b"fake image", "image/jpeg", "2026-03-22"
        )
        original_count = entry.chunk_count

        mock_embeddings.embed_texts.reset_mock()
        # Also snapshot the vector store size so we can verify nothing
        # was deleted or added.
        before_count = ingestion_service._vector_store.count()

        new_count = ingestion_service.rechunk_entry(entry.id, dry_run=True)

        # Dry run still returns a chunk count.
        assert new_count >= 1
        # But it did NOT call embed_texts.
        mock_embeddings.embed_texts.assert_not_called()
        # Vector store unchanged.
        assert ingestion_service._vector_store.count() == before_count
        # SQLite chunk_count unchanged.
        refreshed = ingestion_service._repo.get_entry(entry.id)
        assert refreshed.chunk_count == original_count

    def test_rechunk_empty_text_returns_zero(self, ingestion_service):
        # Manually create an entry with no text (bypasses ingest_image
        # which would have rejected empty OCR output).
        entry = ingestion_service._repo.create_entry("2026-03-22", "ocr", "", 0)
        ingestion_service._repo.update_chunk_count(entry.id, 0)

        result = ingestion_service.rechunk_entry(entry.id)
        assert result == 0
