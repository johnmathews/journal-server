"""Tests for ingestion service."""

import logging
from unittest.mock import MagicMock, patch

import httpx
import openai
import pytest

from journal.db.factory import ConnectionFactory
from journal.db.repository import SQLiteEntryRepository
from journal.models import TranscriptionResult
from journal.providers.ocr import OCRResult
from journal.providers.transcription import (
    RetryingTranscriptionProvider,
    ShadowTranscriptionProvider,
    TranscriptionProvider,
)
from journal.services.chunking import FixedTokenChunker
from journal.services.ingestion import IngestionService
from journal.vectorstore.store import InMemoryVectorStore


def _ocr_result(text: str, spans: list[tuple[int, int]] | None = None) -> OCRResult:
    """Helper for ingestion tests — build an OCRResult fixture tersely."""
    return OCRResult(text=text, uncertain_spans=list(spans) if spans else [])


@pytest.fixture
def mock_ocr():
    provider = MagicMock()
    provider.extract.return_value = _ocr_result(
        "Today I walked through Vienna and met Atlas for coffee."
    )
    return provider


@pytest.fixture
def mock_transcription():
    provider = MagicMock()
    provider.transcribe.return_value = TranscriptionResult(
        text="Voice journal entry about my day at work.",
    )
    return provider


@pytest.fixture
def mock_embeddings():
    provider = MagicMock()
    provider.embed_texts.return_value = [[0.1, 0.2, 0.3]]
    provider.embed_query.return_value = [0.1, 0.2, 0.3]
    return provider


@pytest.fixture
def repo(factory):
    """Shared EntryRepository for tests that need to peek at repo state
    after an ingestion call (e.g. to assert chunks/spans/pages were
    written). Keeping this as its own fixture avoids the ``ingestion_service._repo``
    reach-in pattern that earlier tests used.
    """
    return SQLiteEntryRepository(factory)


@pytest.fixture
def ingestion_service(repo, mock_ocr, mock_transcription, mock_embeddings):
    vector_store = InMemoryVectorStore()
    return IngestionService(
        repository=repo,
        vector_store=vector_store,
        ocr_provider=mock_ocr,
        transcription_provider=mock_transcription,
        embeddings_provider=mock_embeddings,
        chunker=FixedTokenChunker(max_tokens=150, overlap_tokens=40),
        preprocess_images=False,
    )


class TestIngestImage:
    def test_ingest_image(self, ingestion_service, mock_ocr, mock_embeddings):
        entry = ingestion_service.ingest_image(
            image_data=b"fake image data",
            media_type="image/jpeg",
            date="2026-03-22",
        )

        assert entry.entry_date == "2026-03-22"
        assert entry.source_type == "photo"
        assert "Vienna" in entry.raw_text
        assert entry.word_count == 10

        # extract is now called with a PageRole as the third positional arg.
        call_args = mock_ocr.extract.call_args
        assert call_args.args[0] == b"fake image data"
        assert call_args.args[1] == "image/jpeg"
        mock_embeddings.embed_texts.assert_called_once()

    def test_ingest_image_duplicate(self, ingestion_service):
        ingestion_service.ingest_image(b"same data", "image/jpeg", "2026-03-22")

        with pytest.raises(ValueError, match="already been uploaded"):
            ingestion_service.ingest_image(b"same data", "image/jpeg", "2026-03-23")

    def test_ingest_image_empty_text(self, ingestion_service, mock_ocr):
        mock_ocr.extract.return_value = _ocr_result("   ")

        with pytest.raises(ValueError, match="no text"):
            ingestion_service.ingest_image(b"blank page", "image/jpeg", "2026-03-22")


class TestIngestImageSingleEntry:
    """Contract: a single image always produces exactly one entry.

    The old fan-out (ENTRY_DELIMITER → N entries) is removed. Boundary
    trimming is tested in test_ingestion_boundaries.py.
    """

    def test_no_markers_single_entry(
        self, ingestion_service, mock_ocr, repo
    ) -> None:
        # Plain text with no ENTRY_BEGINS/ENDS → one entry, window NULL.
        before = len(repo.list_entries(limit=100))
        entry = ingestion_service.ingest_image(
            b"single-entry page", "image/jpeg", "2026-03-22",
        )
        after = len(repo.list_entries(limit=100))
        assert after - before == 1
        assert entry.content_start_char is None
        assert entry.content_end_char is None


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

        with pytest.raises(ValueError, match="already been uploaded"):
            ingestion_service.ingest_voice(b"same audio", "audio/mp3", "2026-03-23")


class TestIngestMultiVoice:
    def test_single_recording_delegates(
        self, ingestion_service, mock_transcription, mock_embeddings,
    ):
        """A single recording should delegate to ingest_voice."""
        entry = ingestion_service.ingest_multi_voice(
            [(b"audio data", "audio/webm")], "2026-03-22",
        )
        assert entry.source_type == "voice"
        mock_transcription.transcribe.assert_called_once()

    def test_multiple_recordings(
        self, ingestion_service, mock_transcription, mock_embeddings,
    ):
        mock_transcription.transcribe.side_effect = [
            TranscriptionResult(text="First recording text."),
            TranscriptionResult(text="Second recording text."),
        ]
        entry = ingestion_service.ingest_multi_voice(
            [(b"audio1", "audio/webm"), (b"audio2", "audio/webm")],
            "2026-03-22",
        )
        assert entry.source_type == "voice"
        assert "First recording text." in entry.raw_text
        assert "Second recording text." in entry.raw_text
        assert mock_transcription.transcribe.call_count == 2

    def test_multiple_recordings_joined_with_double_newline(
        self, ingestion_service, mock_transcription, mock_embeddings,
    ):
        mock_transcription.transcribe.side_effect = [
            TranscriptionResult(text="  Part one.  "),
            TranscriptionResult(text="  Part two.  "),
        ]
        entry = ingestion_service.ingest_multi_voice(
            [(b"a1", "audio/webm"), (b"a2", "audio/mp3")],
            "2026-03-22",
        )
        assert entry.raw_text == "Part one.\n\nPart two."

    def test_duplicate_recording_rejected(self, ingestion_service, mock_transcription):
        mock_transcription.transcribe.return_value = TranscriptionResult(text="text")
        ingestion_service.ingest_voice(b"same audio", "audio/mp3", "2026-03-20")
        with pytest.raises(ValueError, match="already been uploaded"):
            ingestion_service.ingest_multi_voice(
                [(b"same audio", "audio/mp3")], "2026-03-22",
            )

    def test_empty_transcription_rejected(
        self, ingestion_service, mock_transcription,
    ):
        mock_transcription.transcribe.return_value = TranscriptionResult(text="   ")
        with pytest.raises(ValueError, match="no text"):
            ingestion_service.ingest_multi_voice(
                [(b"silent audio", "audio/webm")], "2026-03-22",
            )

    def test_empty_transcription_multi_rejected(
        self, ingestion_service, mock_transcription,
    ):
        mock_transcription.transcribe.side_effect = [
            TranscriptionResult(text="Good text."),
            TranscriptionResult(text="   "),
        ]
        with pytest.raises(ValueError, match="no text from recording 2"):
            ingestion_service.ingest_multi_voice(
                [(b"audio1", "audio/webm"), (b"audio2", "audio/webm")],
                "2026-03-22",
            )

    def test_empty_recordings_rejected(self, ingestion_service):
        with pytest.raises(ValueError, match="At least one"):
            ingestion_service.ingest_multi_voice([], "2026-03-22")

    def test_progress_callback(
        self, ingestion_service, mock_transcription, mock_embeddings,
    ):
        mock_transcription.transcribe.side_effect = [
            TranscriptionResult(text="Text one."),
            TranscriptionResult(text="Text two."),
        ]
        calls: list[tuple[int, int]] = []
        ingestion_service.ingest_multi_voice(
            [(b"a1", "audio/webm"), (b"a2", "audio/webm")],
            "2026-03-22",
            on_progress=lambda c, t: calls.append((c, t)),
        )
        assert calls == [(1, 2), (2, 2)]

    def test_persists_chunks(
        self, ingestion_service, repo, mock_transcription, mock_embeddings,
    ):
        mock_transcription.transcribe.side_effect = [
            TranscriptionResult(text="First recording text about the day."),
            TranscriptionResult(text="Second recording text about the evening."),
        ]
        entry = ingestion_service.ingest_multi_voice(
            [(b"a1", "audio/webm"), (b"a2", "audio/webm")],
            "2026-03-22",
        )
        stored = repo.get_chunks(entry.id)
        assert len(stored) == entry.chunk_count
        assert len(stored) > 0


class TestIngestImageUpdates:
    def test_ingest_image_sets_final_text(self, ingestion_service):
        entry = ingestion_service.ingest_image(b"page data", "image/jpeg", "2026-03-22")
        assert entry.final_text == entry.raw_text
        assert entry.final_text != ""

    def test_ingest_image_sets_chunk_count(self, ingestion_service):
        entry = ingestion_service.ingest_image(b"page data", "image/jpeg", "2026-03-22")
        assert entry.chunk_count > 0

    def test_ingest_image_creates_page(self, ingestion_service, repo):
        entry = ingestion_service.ingest_image(b"page data", "image/jpeg", "2026-03-22")
        pages = repo.get_entry_pages(entry.id)
        assert len(pages) == 1
        assert pages[0].page_number == 1
        assert pages[0].raw_text == entry.raw_text

    def test_ingest_voice_sets_final_text(self, ingestion_service):
        entry = ingestion_service.ingest_voice(b"audio data", "audio/mp3", "2026-03-22")
        assert entry.final_text == entry.raw_text

    def test_ingest_voice_sets_chunk_count(self, ingestion_service):
        entry = ingestion_service.ingest_voice(b"audio data", "audio/mp3", "2026-03-22")
        assert entry.chunk_count > 0

    def test_ingest_voice_no_pages(self, ingestion_service, repo):
        entry = ingestion_service.ingest_voice(b"audio data", "audio/mp3", "2026-03-22")
        pages = repo.get_entry_pages(entry.id)
        assert len(pages) == 0


class TestChunkPersistence:
    """Chunks produced during ingestion must land in the entry_chunks table
    with the offsets the chunker computed, so the webapp overlay can read
    them back without re-running the chunker."""

    def test_ingest_image_persists_chunks(self, ingestion_service, repo):
        entry = ingestion_service.ingest_image(b"page data", "image/jpeg", "2026-03-22")
        stored = repo.get_chunks(entry.id)
        assert len(stored) == entry.chunk_count
        assert len(stored) > 0
        # Every persisted chunk must have its source range contained
        # within the entry's text.
        for chunk in stored:
            assert 0 <= chunk.char_start <= chunk.char_end <= len(entry.final_text)
            assert chunk.token_count > 0

    def test_ingest_voice_persists_chunks(self, ingestion_service, repo):
        entry = ingestion_service.ingest_voice(b"audio data", "audio/mp3", "2026-03-22")
        stored = repo.get_chunks(entry.id)
        assert len(stored) == entry.chunk_count
        assert len(stored) > 0

    def test_update_entry_text_replaces_chunks(self, ingestion_service, repo):
        entry = ingestion_service.ingest_image(b"page data", "image/jpeg", "2026-03-22")
        original_chunks = repo.get_chunks(entry.id)
        assert len(original_chunks) > 0

        new_text = "Completely different corrected text for the entry."
        ingestion_service.update_entry_text(entry.id, new_text)

        updated_chunks = repo.get_chunks(entry.id)
        # New chunks reflect the new text.
        assert len(updated_chunks) > 0
        assert updated_chunks[0].text != original_chunks[0].text
        # All chunk offsets fit within the new text.
        for chunk in updated_chunks:
            assert chunk.char_end <= len(new_text)

    def test_delete_entry_removes_chunks(self, ingestion_service, repo):
        entry = ingestion_service.ingest_image(b"page data", "image/jpeg", "2026-03-22")
        assert len(repo.get_chunks(entry.id)) > 0
        ingestion_service.delete_entry(entry.id)
        assert repo.get_chunks(entry.id) == []


class TestUncertainSpansIngestion:
    """Uncertainty spans produced by the OCR provider must land in the
    repository at the right offsets into the stored raw_text. These tests
    are the feature's contract — if they go red, the Review toggle in the
    webapp will highlight the wrong characters (or nothing at all)."""

    def test_single_page_persists_uncertain_spans(
        self, ingestion_service, repo, mock_ocr
    ):
        mock_ocr.extract.return_value = _ocr_result(
            "Hello Ritsya from Vienna.",
            [(6, 12), (18, 24)],  # "Ritsya" and "Vienna"
        )
        entry = ingestion_service.ingest_image(
            b"page data", "image/jpeg", "2026-03-22"
        )
        spans = repo.get_uncertain_spans(entry.id)
        assert spans == [(6, 12), (18, 24)]
        # Verify that the stored offsets actually land on the right words.
        assert entry.raw_text[6:12] == "Ritsya"
        assert entry.raw_text[18:24] == "Vienna"

    def test_single_page_no_uncertain_spans(self, ingestion_service, repo, mock_ocr):
        mock_ocr.extract.return_value = _ocr_result("All confident.", [])
        entry = ingestion_service.ingest_image(
            b"clean data", "image/jpeg", "2026-03-22"
        )
        assert repo.get_uncertain_spans(entry.id) == []

    def test_multi_page_shifts_spans_into_entry_coordinates(
        self, ingestion_service, repo, mock_ocr
    ):
        """The parser gives us per-page spans. After the join, spans
        from page 2 must be offset by len(page1_stripped) + 1 (the
        "\\n" separator) so they address the right characters in the
        combined entry.raw_text."""
        mock_ocr.extract.side_effect = [
            _ocr_result("First Ritsya line.", [(6, 12)]),       # "Ritsya" on page 1
            _ocr_result("Second Vienna line.", [(7, 13)]),      # "Vienna" on page 2
        ]
        entry = ingestion_service.ingest_multi_page_entry(
            images=[(b"img1", "image/jpeg"), (b"img2", "image/jpeg")],
            date="2026-03-22",
        )
        # Combined text: "First Ritsya line.\nSecond Vienna line."
        expected_text = "First Ritsya line.\nSecond Vienna line."
        assert entry.raw_text == expected_text

        spans = repo.get_uncertain_spans(entry.id)
        # Page 1 span unshifted: still at (6, 12) → "Ritsya"
        # Page 2 span shifted: original (7, 13), offset = len("First Ritsya line.") + 1 = 19
        #   new start = 7 + 19 = 26, new end = 13 + 19 = 32 → "Vienna"
        assert spans == [(6, 12), (26, 32)]
        # Cross-check that the shifted offsets land on the right words
        # in the combined raw_text.
        assert entry.raw_text[6:12] == "Ritsya"
        assert entry.raw_text[26:32] == "Vienna"

    def test_multi_page_strips_leading_whitespace_and_clips_spans(
        self, ingestion_service, repo, mock_ocr
    ):
        """An OCR page that starts with whitespace gets lstripped
        before joining. A span that was in the leading whitespace
        must be discarded (or clipped) — not silently offset into
        the wrong word."""
        mock_ocr.extract.side_effect = [
            # Page 1 has 3 leading spaces. A span at (4, 9) in the
            # pre-strip coordinates covers "world" — after lstripping
            # 3 chars, it should land at (1, 6) in the stripped page,
            # which equals (1, 6) in the combined text (page 1 is at
            # cumulative_offset=0).
            _ocr_result("   hello world", [(4, 9)]),
            _ocr_result("second", []),
        ]
        entry = ingestion_service.ingest_multi_page_entry(
            images=[(b"img1", "image/jpeg"), (b"img2", "image/jpeg")],
            date="2026-03-22",
        )
        # The combined text is "hello world\nsecond" (page 1 stripped).
        assert entry.raw_text == "hello world\nsecond"
        spans = repo.get_uncertain_spans(entry.id)
        # Wait — (4, 9) in "   hello world" is "ello ". After stripping
        # 3 leading chars, that's (1, 6) = "ello ". So the span shifts,
        # not the word it points at.
        assert spans == [(1, 6)]
        assert entry.raw_text[1:6] == "ello "

    def test_multi_page_drops_span_entirely_in_trimmed_whitespace(
        self, ingestion_service, repo, mock_ocr
    ):
        """A span that falls within whitespace stripped from the page
        edges must be discarded. The `_strip_and_shift_page_spans`
        helper returns an empty list for such spans."""
        mock_ocr.extract.side_effect = [
            # Span (0, 2) is inside the leading whitespace and should
            # be dropped entirely. Span (6, 11) covers "world".
            _ocr_result("  hello world  ", [(0, 2), (8, 13)]),
            _ocr_result("next", []),
        ]
        entry = ingestion_service.ingest_multi_page_entry(
            images=[(b"img1", "image/jpeg"), (b"img2", "image/jpeg")],
            date="2026-03-22",
        )
        assert entry.raw_text == "hello world\nnext"
        spans = repo.get_uncertain_spans(entry.id)
        # Only the second span survives; shifted by -2 (leading strip).
        assert spans == [(6, 11)]
        assert entry.raw_text[6:11] == "world"

    def test_multi_page_only_one_page_has_uncertainty(
        self, ingestion_service, repo, mock_ocr
    ):
        mock_ocr.extract.side_effect = [
            _ocr_result("All clean page one.", []),
            _ocr_result("Page two Atlas here.", [(9, 14)]),  # "Atlas"
        ]
        entry = ingestion_service.ingest_multi_page_entry(
            images=[(b"img1", "image/jpeg"), (b"img2", "image/jpeg")],
            date="2026-03-22",
        )
        expected_text = "All clean page one.\nPage two Atlas here."
        assert entry.raw_text == expected_text
        spans = repo.get_uncertain_spans(entry.id)
        # Page 2 offset = len("All clean page one.") + 1 = 20
        # Shifted span: (9 + 20, 14 + 20) = (29, 34)
        assert spans == [(29, 34)]
        assert entry.raw_text[29:34] == "Atlas"

    def test_patch_does_not_touch_uncertain_spans(
        self, ingestion_service, repo, mock_ocr
    ):
        """Editing final_text must not clear or modify the uncertainty
        spans, because they live in raw_text coordinates and raw_text
        is immutable."""
        mock_ocr.extract.return_value = _ocr_result(
            "Hello Ritsya.", [(6, 12)]
        )
        entry = ingestion_service.ingest_image(
            b"page data", "image/jpeg", "2026-03-22"
        )
        spans_before = repo.get_uncertain_spans(entry.id)
        assert spans_before == [(6, 12)]

        ingestion_service.update_entry_text(entry.id, "Completely different text.")

        spans_after = repo.get_uncertain_spans(entry.id)
        assert spans_after == [(6, 12)]


class TestVoiceUncertainSpans:
    """Uncertain spans from transcription logprobs must be stored in the
    same entry_uncertain_spans table as OCR doubts, so the webapp's Review
    toggle highlights low-confidence words for voice entries too."""

    def test_ingest_voice_persists_uncertain_spans(
        self, ingestion_service, repo, mock_transcription, mock_embeddings,
    ):
        mock_transcription.transcribe.return_value = TranscriptionResult(
            text="Hello wrld today.",
            uncertain_spans=[(6, 10)],  # "wrld"
        )
        entry = ingestion_service.ingest_voice(
            b"audio data", "audio/mp3", "2026-03-22",
        )
        spans = repo.get_uncertain_spans(entry.id)
        assert spans == [(6, 10)]
        assert entry.raw_text[6:10] == "wrld"

    def test_ingest_voice_no_uncertain_spans(
        self, ingestion_service, repo, mock_transcription, mock_embeddings,
    ):
        mock_transcription.transcribe.return_value = TranscriptionResult(
            text="All confident text.",
            uncertain_spans=[],
        )
        entry = ingestion_service.ingest_voice(
            b"audio data", "audio/mp3", "2026-03-22",
        )
        assert repo.get_uncertain_spans(entry.id) == []

    def test_multi_voice_shifts_spans_into_combined_coordinates(
        self, ingestion_service, repo, mock_transcription, mock_embeddings,
    ):
        mock_transcription.transcribe.side_effect = [
            TranscriptionResult(
                text="First wrld text.",
                uncertain_spans=[(6, 10)],  # "wrld"
            ),
            TranscriptionResult(
                text="Second badd text.",
                uncertain_spans=[(7, 11)],  # "badd"
            ),
        ]
        entry = ingestion_service.ingest_multi_voice(
            [(b"a1", "audio/webm"), (b"a2", "audio/webm")],
            "2026-03-22",
        )
        # Combined: "First wrld text.\n\nSecond badd text."
        # Recording 1: "wrld" at (6, 10) → stays (6, 10)
        # Recording 2: "badd" at (7, 11), offset = len("First wrld text.") + 2 = 18
        #   → (7+18, 11+18) = (25, 29)
        spans = repo.get_uncertain_spans(entry.id)
        assert spans == [(6, 10), (25, 29)]
        assert entry.raw_text[6:10] == "wrld"
        assert entry.raw_text[25:29] == "badd"

    def test_multi_voice_one_recording_uncertain(
        self, ingestion_service, repo, mock_transcription, mock_embeddings,
    ):
        mock_transcription.transcribe.side_effect = [
            TranscriptionResult(text="Clean text.", uncertain_spans=[]),
            TranscriptionResult(
                text="Has doubt here.",
                uncertain_spans=[(4, 9)],  # "doubt"
            ),
        ]
        entry = ingestion_service.ingest_multi_voice(
            [(b"a1", "audio/webm"), (b"a2", "audio/webm")],
            "2026-03-22",
        )
        # Recording 2 offset = len("Clean text.") + 2 = 13
        spans = repo.get_uncertain_spans(entry.id)
        assert spans == [(17, 22)]
        assert entry.raw_text[17:22] == "doubt"


class TestMultiPageIngestion:
    def test_ingest_multi_page(self, ingestion_service, mock_ocr, mock_embeddings):
        mock_ocr.extract.side_effect = [
            _ocr_result("Page one text."),
            _ocr_result("Page two text."),
        ]
        entry = ingestion_service.ingest_multi_page_entry(
            images=[(b"img1", "image/jpeg"), (b"img2", "image/jpeg")],
            date="2026-03-22",
        )

        assert entry.entry_date == "2026-03-22"
        assert entry.source_type == "photo"
        assert "Page one text." in entry.raw_text
        assert "Page two text." in entry.raw_text
        # Pages are joined with a single newline (not "\n\n") so page
        # boundaries don't force paragraph splits in the chunker. See
        # the join comment in `ingest_multi_page_entry` for the full
        # rationale.
        assert entry.raw_text == "Page one text.\nPage two text."
        assert entry.final_text == entry.raw_text
        assert entry.chunk_count > 0

    def test_ingest_multi_page_creates_pages(self, ingestion_service, repo, mock_ocr):
        mock_ocr.extract.side_effect = [
            _ocr_result("First page."),
            _ocr_result("Second page."),
        ]
        entry = ingestion_service.ingest_multi_page_entry(
            images=[(b"img1", "image/jpeg"), (b"img2", "image/jpeg")],
            date="2026-03-22",
        )

        pages = repo.get_entry_pages(entry.id)
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
        mock_ocr.extract.side_effect = [
            _ocr_result("Page one.\n"),
            _ocr_result("Page two.\n"),
        ]
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
        mock_ocr.extract.side_effect = [
            _ocr_result(page),
            _ocr_result(page),
            _ocr_result(page),
        ]
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
        mock_ocr.extract.return_value = _ocr_result("Page text.")
        ingestion_service.ingest_image(b"same", "image/jpeg", "2026-03-22")

        mock_ocr.extract.return_value = _ocr_result("Another page.")
        with pytest.raises(ValueError, match="already been uploaded"):
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
    def service_with_prefix(self, factory, mock_ocr, mock_transcription, mock_embeddings):
        repo = SQLiteEntryRepository(factory)
        vector_store = InMemoryVectorStore()
        return IngestionService(
            repository=repo,
            vector_store=vector_store,
            ocr_provider=mock_ocr,
            transcription_provider=mock_transcription,
            embeddings_provider=mock_embeddings,
            chunker=FixedTokenChunker(max_tokens=150, overlap_tokens=40),
            embed_metadata_prefix=True,
            preprocess_images=False,
        )

    @pytest.fixture
    def service_without_prefix(self, factory, mock_ocr, mock_transcription, mock_embeddings):
        repo = SQLiteEntryRepository(factory)
        vector_store = InMemoryVectorStore()
        return IngestionService(
            repository=repo,
            vector_store=vector_store,
            ocr_provider=mock_ocr,
            transcription_provider=mock_transcription,
            embeddings_provider=mock_embeddings,
            chunker=FixedTokenChunker(max_tokens=150, overlap_tokens=40),
            embed_metadata_prefix=False,
            preprocess_images=False,
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
        mock_ocr.extract.return_value = _ocr_result("This is the raw journal text.")
        service_with_prefix.ingest_image(b"fake image", "image/jpeg", "2026-02-15")

        # Fetch what was stored in the vector store for this entry.
        stored = service_with_prefix.vector_store.get_chunks_for_entry(1) \
            if hasattr(service_with_prefix.vector_store, "get_chunks_for_entry") else None

        # Fall back to searching — InMemoryVectorStore doesn't yet expose
        # get_chunks_for_entry (that's added in WU-H). Assert via a search.
        if stored is None:
            results = service_with_prefix.vector_store.search(
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

    def test_rechunk_replaces_existing_vectors(self, ingestion_service, repo, mock_embeddings):
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
        refreshed = repo.get_entry(entry.id)
        assert refreshed.chunk_count == new_count

    def test_rechunk_missing_entry_raises(self, ingestion_service):
        with pytest.raises(ValueError, match="not found"):
            ingestion_service.rechunk_entry(999)

    def test_rechunk_dry_run_does_not_touch_embeddings_or_db(
        self, ingestion_service, repo, mock_embeddings
    ):
        entry = ingestion_service.ingest_image(
            b"fake image", "image/jpeg", "2026-03-22"
        )
        original_count = entry.chunk_count

        mock_embeddings.embed_texts.reset_mock()
        # Also snapshot the vector store size so we can verify nothing
        # was deleted or added.
        before_count = ingestion_service.vector_store.count()

        new_count = ingestion_service.rechunk_entry(entry.id, dry_run=True)

        # Dry run still returns a chunk count.
        assert new_count >= 1
        # But it did NOT call embed_texts.
        mock_embeddings.embed_texts.assert_not_called()
        # Vector store unchanged.
        assert ingestion_service.vector_store.count() == before_count
        # SQLite chunk_count unchanged.
        refreshed = repo.get_entry(entry.id)
        assert refreshed.chunk_count == original_count

    def test_rechunk_empty_text_returns_zero(self, ingestion_service, repo):
        # Manually create an entry with no text (bypasses ingest_image
        # which would have rejected empty OCR output).
        entry = repo.create_entry("2026-03-22", "photo", "", 0)
        repo.update_chunk_count(entry.id, 0)

        result = ingestion_service.rechunk_entry(entry.id)
        assert result == 0


class TestPreprocessingIntegration:
    """Verify preprocessing is called/skipped based on the flag."""

    def test_preprocessing_called_when_enabled(
        self, factory, mock_ocr, mock_transcription, mock_embeddings, monkeypatch,
    ):
        from journal.services import preprocessing

        spy = MagicMock(return_value=(b"processed", "image/jpeg"))
        monkeypatch.setattr(preprocessing, "preprocess_image", spy)

        repo = SQLiteEntryRepository(factory)
        svc = IngestionService(
            repository=repo,
            vector_store=InMemoryVectorStore(),
            ocr_provider=mock_ocr,
            transcription_provider=mock_transcription,
            embeddings_provider=mock_embeddings,
            chunker=FixedTokenChunker(max_tokens=150, overlap_tokens=40),
            preprocess_images=True,
        )
        svc.ingest_image(b"raw image", "image/jpeg", "2026-04-20")
        spy.assert_called_once_with(b"raw image", "image/jpeg")
        # OCR should receive the preprocessed bytes (third arg is PageRole)
        ocr_call = mock_ocr.extract.call_args
        assert ocr_call.args[0] == b"processed"
        assert ocr_call.args[1] == "image/jpeg"

    def test_preprocessing_skipped_when_disabled(
        self, factory, mock_ocr, mock_transcription, mock_embeddings, monkeypatch,
    ):
        from journal.services import preprocessing

        spy = MagicMock()
        monkeypatch.setattr(preprocessing, "preprocess_image", spy)

        repo = SQLiteEntryRepository(factory)
        svc = IngestionService(
            repository=repo,
            vector_store=InMemoryVectorStore(),
            ocr_provider=mock_ocr,
            transcription_provider=mock_transcription,
            embeddings_provider=mock_embeddings,
            chunker=FixedTokenChunker(max_tokens=150, overlap_tokens=40),
            preprocess_images=False,
        )
        svc.ingest_image(b"raw image", "image/jpeg", "2026-04-20")
        spy.assert_not_called()
        # OCR receives original bytes (third arg is PageRole)
        ocr_call = mock_ocr.extract.call_args
        assert ocr_call.args[0] == b"raw image"
        assert ocr_call.args[1] == "image/jpeg"

    def test_multi_page_preprocessing_per_page(
        self, factory, mock_ocr, mock_transcription, mock_embeddings, monkeypatch,
    ):
        from journal.services import preprocessing

        spy = MagicMock(side_effect=[
            (b"processed-1", "image/jpeg"),
            (b"processed-2", "image/jpeg"),
        ])
        monkeypatch.setattr(preprocessing, "preprocess_image", spy)

        mock_ocr.extract.side_effect = [
            _ocr_result("Page one text."),
            _ocr_result("Page two text."),
        ]

        repo = SQLiteEntryRepository(factory)
        svc = IngestionService(
            repository=repo,
            vector_store=InMemoryVectorStore(),
            ocr_provider=mock_ocr,
            transcription_provider=mock_transcription,
            embeddings_provider=mock_embeddings,
            chunker=FixedTokenChunker(max_tokens=150, overlap_tokens=40),
            preprocess_images=True,
        )
        svc.ingest_multi_page_entry(
            [(b"page1", "image/jpeg"), (b"page2", "image/png")],
            "2026-04-20",
        )
        assert spy.call_count == 2
        spy.assert_any_call(b"page1", "image/jpeg")
        spy.assert_any_call(b"page2", "image/png")


class TestTranscriptFormatting:
    """Tests for LLM paragraph formatting integration in voice ingestion."""

    @pytest.fixture
    def mock_formatter(self):
        formatter = MagicMock()
        formatter.format_paragraphs.side_effect = lambda text: text.replace(
            ". ", ".\n\n"
        )
        return formatter

    @pytest.fixture
    def formatting_service(
        self, factory, mock_ocr, mock_transcription, mock_embeddings, mock_formatter,
    ):
        repo = SQLiteEntryRepository(factory)
        vector_store = InMemoryVectorStore()
        return IngestionService(
            repository=repo,
            vector_store=vector_store,
            ocr_provider=mock_ocr,
            transcription_provider=mock_transcription,
            embeddings_provider=mock_embeddings,
            chunker=FixedTokenChunker(max_tokens=150, overlap_tokens=40),
            preprocess_images=False,
            formatter=mock_formatter,
        )

    def test_ingest_voice_formats_final_text(
        self, formatting_service, mock_transcription, mock_embeddings, mock_formatter,
    ):
        mock_transcription.transcribe.return_value = TranscriptionResult(
            text="Hello world. Goodbye world."
        )
        entry = formatting_service.ingest_voice(
            b"audio", "audio/webm", "2026-04-22",
        )
        assert entry.raw_text == "Hello world. Goodbye world."
        assert entry.final_text == "Hello world.\n\nGoodbye world."
        mock_formatter.format_paragraphs.assert_called_once_with(
            "Hello world. Goodbye world."
        )

    def test_ingest_multi_voice_formats_final_text(
        self, formatting_service, mock_transcription, mock_embeddings, mock_formatter,
    ):
        mock_transcription.transcribe.side_effect = [
            TranscriptionResult(text="First part. More text."),
            TranscriptionResult(text="Second part. Even more."),
        ]
        entry = formatting_service.ingest_multi_voice(
            [(b"a1", "audio/webm"), (b"a2", "audio/webm")],
            "2026-04-22",
        )
        # raw_text uses \n\n between recordings
        assert "First part. More text.\n\nSecond part." in entry.raw_text
        # final_text has LLM-inserted paragraph breaks within recordings too
        assert entry.final_text != entry.raw_text
        mock_formatter.format_paragraphs.assert_called_once()

    def test_formatter_failure_falls_back_to_raw(
        self, formatting_service, mock_transcription, mock_embeddings, mock_formatter,
    ):
        mock_formatter.format_paragraphs.side_effect = RuntimeError("LLM down")
        mock_transcription.transcribe.return_value = TranscriptionResult(
            text="Some text here."
        )
        entry = formatting_service.ingest_voice(
            b"audio", "audio/webm", "2026-04-22",
        )
        # Fallback: final_text == raw_text
        assert entry.final_text == entry.raw_text

    def test_uncertainty_spans_anchored_to_raw_text(
        self, formatting_service, mock_transcription, mock_embeddings, mock_formatter,
    ):
        mock_transcription.transcribe.return_value = TranscriptionResult(
            text="Hello wrld today.",
            uncertain_spans=[(6, 10)],  # "wrld"
        )
        entry = formatting_service.ingest_voice(
            b"audio", "audio/webm", "2026-04-22",
        )
        spans = formatting_service._repo.get_uncertain_spans(entry.id)
        assert spans == [(6, 10)]
        # Spans reference raw_text, not final_text
        assert entry.raw_text[6:10] == "wrld"

    def test_no_formatter_leaves_final_text_equal(
        self, ingestion_service, mock_transcription, mock_embeddings,
    ):
        """Without a formatter, final_text == raw_text (existing behaviour)."""
        entry = ingestion_service.ingest_voice(
            b"audio", "audio/webm", "2026-04-22",
        )
        assert entry.final_text == entry.raw_text


class TestHeadingDetection:
    """Tests for date-heading detector integration in voice + OCR ingestion."""

    @pytest.fixture
    def mock_detector(self):
        from journal.services.heading_detector import HeadingDetectionResult

        detector = MagicMock()
        detector.detect.return_value = HeadingDetectionResult(
            heading_text="", body=""
        )
        return detector

    @pytest.fixture
    def detection_service(
        self, factory, mock_ocr, mock_transcription, mock_embeddings, mock_detector,
    ):
        repo = SQLiteEntryRepository(factory)
        vector_store = InMemoryVectorStore()
        return IngestionService(
            repository=repo,
            vector_store=vector_store,
            ocr_provider=mock_ocr,
            transcription_provider=mock_transcription,
            embeddings_provider=mock_embeddings,
            chunker=FixedTokenChunker(max_tokens=150, overlap_tokens=40),
            preprocess_images=False,
            heading_detector=mock_detector,
        )

    def test_voice_with_detected_heading_strips_date_from_final_text(
        self, detection_service, mock_transcription, mock_detector,
    ):
        from journal.services.heading_detector import HeadingDetectionResult

        raw = "April 28th. Today I went for a long run."
        mock_transcription.transcribe.return_value = TranscriptionResult(text=raw)
        mock_detector.detect.return_value = HeadingDetectionResult(
            heading_text="28 April 2026",
            body="Today I went for a long run.",
        )

        entry = detection_service.ingest_voice(
            b"audio", "audio/webm", "2026-04-28",
        )

        # raw_text preserves the verbatim transcription (audit trail).
        assert entry.raw_text == raw
        # final_text drops the leading date entirely — no markdown heading,
        # because the entry's title is already the date and a duplicate
        # heading would just be redundant.
        assert entry.final_text == "Today I went for a long run."
        assert "28 April 2026" not in entry.final_text
        assert not entry.final_text.startswith("#")
        mock_detector.detect.assert_called_once_with(raw, entry_date="2026-04-28")

    def test_voice_with_no_detected_heading_leaves_final_text_unchanged(
        self, detection_service, mock_transcription, mock_detector,
    ):
        from journal.services.heading_detector import HeadingDetectionResult

        raw = "I went to Berlin on April 28th."
        mock_transcription.transcribe.return_value = TranscriptionResult(text=raw)
        mock_detector.detect.return_value = HeadingDetectionResult(
            heading_text="", body=raw,
        )

        entry = detection_service.ingest_voice(
            b"audio", "audio/webm", "2026-04-28",
        )

        assert entry.raw_text == raw
        assert entry.final_text == raw

    def test_ocr_with_detected_heading_strips_date_from_final_text(
        self, detection_service, mock_ocr, mock_detector,
    ):
        from journal.services.heading_detector import HeadingDetectionResult

        ocr_text = "April 28th\nWoke up early and watched the sunrise."
        mock_ocr.extract.return_value = _ocr_result(ocr_text)
        mock_detector.detect.return_value = HeadingDetectionResult(
            heading_text="28 April 2026",
            body="Woke up early and watched the sunrise.",
        )

        entry = detection_service.ingest_image(
            b"page bytes", "image/jpeg", "2026-04-28",
        )

        assert entry.raw_text == ocr_text
        assert entry.final_text == "Woke up early and watched the sunrise."
        assert not entry.final_text.startswith("#")

    def test_multi_page_ocr_with_detected_heading_strips_date(
        self, detection_service, mock_ocr, mock_detector,
    ):
        from journal.services.heading_detector import HeadingDetectionResult

        mock_ocr.extract.side_effect = [
            _ocr_result("April 28th\nFirst page text."),
            _ocr_result("Second page text."),
        ]
        mock_detector.detect.return_value = HeadingDetectionResult(
            heading_text="28 April 2026",
            body="First page text.\nSecond page text.",
        )

        entry = detection_service.ingest_multi_page_entry(
            [(b"p1", "image/jpeg"), (b"p2", "image/jpeg")],
            "2026-04-28",
        )

        # final_text contains the body verbatim, with no markdown heading.
        assert entry.final_text == "First page text.\nSecond page text."
        assert not entry.final_text.startswith("#")
        # raw_text is the verbatim combined OCR text (still contains the date).
        assert "April 28th" in entry.raw_text
        assert "Second page text." in entry.raw_text

    def test_multi_voice_with_detected_heading(
        self, detection_service, mock_transcription, mock_detector,
    ):
        from journal.services.heading_detector import HeadingDetectionResult

        mock_transcription.transcribe.side_effect = [
            TranscriptionResult(text="April 28th. First clip."),
            TranscriptionResult(text="Second clip."),
        ]
        mock_detector.detect.return_value = HeadingDetectionResult(
            heading_text="28 April 2026",
            body="First clip.\n\nSecond clip.",
        )

        entry = detection_service.ingest_multi_voice(
            [(b"a1", "audio/webm"), (b"a2", "audio/webm")],
            "2026-04-28",
        )

        assert entry.final_text == "First clip.\n\nSecond clip."
        assert not entry.final_text.startswith("#")

    def test_detector_exception_falls_back_to_no_heading(
        self, detection_service, mock_transcription, mock_detector,
    ):
        mock_detector.detect.side_effect = RuntimeError("LLM down")
        raw = "April 28th. Today I went out."
        mock_transcription.transcribe.return_value = TranscriptionResult(text=raw)

        entry = detection_service.ingest_voice(
            b"audio", "audio/webm", "2026-04-28",
        )

        # Ingestion does not crash — final_text just falls back to raw_text.
        assert entry.raw_text == raw
        assert entry.final_text == raw

    def test_no_detector_does_not_call_detection(
        self, ingestion_service, mock_transcription,
    ):
        """The default service has no detector; voice ingestion must not change behaviour."""
        raw = "April 28th. Today I went out."
        mock_transcription.transcribe.return_value = TranscriptionResult(text=raw)
        entry = ingestion_service.ingest_voice(
            b"audio", "audio/webm", "2026-04-28",
        )
        assert entry.raw_text == raw
        assert entry.final_text == raw

    def test_voice_with_regex_extractable_leading_date_sets_entry_date(
        self, ingestion_service, mock_transcription,
    ):
        """Reproduces the prod bug: a backdated voice note that begins with a
        regex-extractable date (e.g. dictating today an entry from 3 days ago,
        starting with "Friday 1 January 2026") must end up with entry_date set
        to the dictated date — not today's date the caller passed in. Mirrors
        the OCR paths' use of extract_date_from_text."""
        raw = "Friday 1 January 2026. Today I went to the burrow and read."
        mock_transcription.transcribe.return_value = TranscriptionResult(text=raw)

        entry = ingestion_service.ingest_voice(
            b"audio", "audio/webm", date="2026-05-04",
        )

        assert entry.entry_date == "2026-01-01"

    def test_multi_voice_with_regex_extractable_leading_date_sets_entry_date(
        self, ingestion_service, mock_transcription,
    ):
        """Same parity for multi-voice: leading date in the first clip must
        propagate to the entry's entry_date."""
        mock_transcription.transcribe.side_effect = [
            TranscriptionResult(text="Friday 1 January 2026. Morning thoughts."),
            TranscriptionResult(text="More from later in the day."),
        ]

        entry = ingestion_service.ingest_multi_voice(
            [(b"a1", "audio/webm"), (b"a2", "audio/webm")], date="2026-05-04",
        )

        assert entry.entry_date == "2026-01-01"

    def test_voice_with_detector_iso_date_sets_entry_date(
        self, detection_service, mock_transcription, mock_detector,
    ):
        """When the heading detector returns date_iso (e.g. resolved from a
        spelled-out or relative phrase the regex can't parse), ingestion must
        use it as the entry's entry_date."""
        from journal.services.heading_detector import HeadingDetectionResult

        raw = "The first of January twenty twenty six. I cleaned the kitchen."
        mock_transcription.transcribe.return_value = TranscriptionResult(text=raw)
        mock_detector.detect.return_value = HeadingDetectionResult(
            heading_text="1 January 2026",
            body="I cleaned the kitchen.",
            date_iso="2026-01-01",
        )

        entry = detection_service.ingest_voice(
            b"audio", "audio/webm", date="2026-05-04",
        )

        assert entry.entry_date == "2026-01-01"
        assert entry.final_text == "I cleaned the kitchen."

    def test_multi_voice_with_detector_iso_date_sets_entry_date(
        self, detection_service, mock_transcription, mock_detector,
    ):
        from journal.services.heading_detector import HeadingDetectionResult

        mock_transcription.transcribe.side_effect = [
            TranscriptionResult(text="The first of January. Morning."),
            TranscriptionResult(text="Afternoon."),
        ]
        mock_detector.detect.return_value = HeadingDetectionResult(
            heading_text="1 January 2026",
            body="Morning.\n\nAfternoon.",
            date_iso="2026-01-01",
        )

        entry = detection_service.ingest_multi_voice(
            [(b"a1", "audio/webm"), (b"a2", "audio/webm")], date="2026-05-04",
        )

        assert entry.entry_date == "2026-01-01"

    def test_image_with_detector_iso_date_overrides_caller_date(
        self, detection_service, mock_ocr, mock_detector,
    ):
        """OCR path: the detector's date_iso should override the caller-passed
        date when set, just like the regex extractor already does."""
        from journal.services.heading_detector import HeadingDetectionResult

        ocr_text = "Yesterday\nWoke up early and read."
        mock_ocr.extract.return_value = _ocr_result(ocr_text)
        mock_detector.detect.return_value = HeadingDetectionResult(
            heading_text="3 May 2026",
            body="Woke up early and read.",
            date_iso="2026-05-03",
        )

        entry = detection_service.ingest_image(
            b"page", "image/jpeg", date="2026-05-04",
        )

        assert entry.entry_date == "2026-05-03"

    def test_multi_page_ocr_with_detector_iso_date_overrides_caller_date(
        self, detection_service, mock_ocr, mock_detector,
    ):
        from journal.services.heading_detector import HeadingDetectionResult

        mock_ocr.extract.side_effect = [
            _ocr_result("Yesterday\nFirst page."),
            _ocr_result("Second page."),
        ]
        mock_detector.detect.return_value = HeadingDetectionResult(
            heading_text="3 May 2026",
            body="First page.\nSecond page.",
            date_iso="2026-05-03",
        )

        entry = detection_service.ingest_multi_page_entry(
            [(b"p1", "image/jpeg"), (b"p2", "image/jpeg")], date="2026-05-04",
        )

        assert entry.entry_date == "2026-05-03"

    def test_detector_combines_with_formatter_on_body_only(
        self, factory, mock_ocr, mock_transcription, mock_embeddings,
    ):
        """When BOTH detector and formatter are wired, formatter must only see the body."""
        from journal.services.heading_detector import HeadingDetectionResult

        detector = MagicMock()
        detector.detect.return_value = HeadingDetectionResult(
            heading_text="28 April 2026",
            body="Hello world. Goodbye world.",
        )
        formatter = MagicMock()
        formatter.format_paragraphs.side_effect = lambda text: text.replace(
            ". ", ".\n\n"
        )

        repo = SQLiteEntryRepository(factory)
        service = IngestionService(
            repository=repo,
            vector_store=InMemoryVectorStore(),
            ocr_provider=mock_ocr,
            transcription_provider=mock_transcription,
            embeddings_provider=mock_embeddings,
            chunker=FixedTokenChunker(max_tokens=150, overlap_tokens=40),
            preprocess_images=False,
            formatter=formatter,
            heading_detector=detector,
        )

        raw = "April 28th. Hello world. Goodbye world."
        mock_transcription.transcribe.return_value = TranscriptionResult(text=raw)

        entry = service.ingest_voice(b"audio", "audio/webm", "2026-04-28")

        # Formatter must have received only the body, never the heading text.
        formatter.format_paragraphs.assert_called_once_with(
            "Hello world. Goodbye world."
        )
        # The leading date is stripped entirely; final_text is just the
        # formatter's output on the body — no markdown heading.
        assert entry.final_text == "Hello world.\n\nGoodbye world."
        assert not entry.final_text.startswith("#")


# ---------------------------------------------------------------------------
# WU7: integration tests that wire the real provider stack (Retrying / Shadow)
# through the real IngestionService.ingest_voice path. The point is to catch
# contract mismatches that the per-unit tests can't see — e.g. an exception
# class change, a TranscriptionResult shape drift, or an uncertain_spans
# attribute getting lost when a wrapper in the chain forwards the call.
# ---------------------------------------------------------------------------


def _openai_request() -> httpx.Request:
    """Construct a dummy httpx.Request — required by openai exception ctors."""
    return httpx.Request("POST", "https://api.openai.com/v1/audio/transcriptions")


def _api_timeout() -> openai.APITimeoutError:
    """Instantiate APITimeoutError; the SDK requires a `request` kwarg."""
    return openai.APITimeoutError(request=_openai_request())


class TestIngestionWithProviderStack:
    """End-to-end tests for IngestionService.ingest_voice with real wrappers.

    These tests instantiate the real RetryingTranscriptionProvider /
    ShadowTranscriptionProvider classes around mock primary/fallback/shadow
    adapters, then run them through the actual ingestion code path. They
    exercise the contract surface between wrappers and the rest of the
    pipeline — chunking, embedding, persistence, uncertain-span handling.
    """

    @pytest.fixture
    def primary_provider(self) -> MagicMock:
        provider = MagicMock(spec=TranscriptionProvider)
        provider.transcribe.return_value = TranscriptionResult(
            text="primary text", uncertain_spans=[],
        )
        return provider

    @pytest.fixture
    def fallback_provider(self) -> MagicMock:
        provider = MagicMock(spec=TranscriptionProvider)
        provider.transcribe.return_value = TranscriptionResult(
            text="fallback text", uncertain_spans=[],
        )
        return provider

    @pytest.fixture
    def shadow_provider(self) -> MagicMock:
        provider = MagicMock(spec=TranscriptionProvider)
        provider.transcribe.return_value = TranscriptionResult(
            text="shadow text", uncertain_spans=[],
        )
        return provider

    def _build_service(
        self,
        factory: ConnectionFactory,
        transcription: TranscriptionProvider,
        mock_embeddings: MagicMock,
    ) -> IngestionService:
        """Build a real IngestionService wrapping *transcription*.

        OCR and embeddings are mocked at the same boundaries as the rest
        of the test file's fixtures.
        """
        return IngestionService(
            repository=SQLiteEntryRepository(factory),
            vector_store=InMemoryVectorStore(),
            ocr_provider=MagicMock(),
            transcription_provider=transcription,
            embeddings_provider=mock_embeddings,
            chunker=FixedTokenChunker(max_tokens=150, overlap_tokens=40),
            preprocess_images=False,
        )

    # ------------------------------------------------------------------
    # (a) Retrying: primary fails once, succeeds the second time.
    # ------------------------------------------------------------------
    def test_ingest_voice_with_retrying_wrapper(
        self,
        factory: ConnectionFactory,
        mock_embeddings: MagicMock,
        primary_provider: MagicMock,
    ) -> None:
        primary_provider.transcribe.side_effect = [
            _api_timeout(),
            TranscriptionResult(
                text="success after retry", uncertain_spans=[],
            ),
        ]
        wrapper = RetryingTranscriptionProvider(
            primary=primary_provider, max_attempts=3,
        )
        service = self._build_service(factory, wrapper, mock_embeddings)

        with patch("journal.providers.transcription.time.sleep") as mock_sleep:
            entry = service.ingest_voice(
                audio_data=b"fake audio",
                media_type="audio/mp3",
                date="2026-04-01",
            )

        assert entry.raw_text == "success after retry"
        assert entry.final_text == "success after retry"
        assert entry.source_type == "voice"
        assert primary_provider.transcribe.call_count == 2
        # Exactly one sleep between attempt 1 (failed) and attempt 2 (succeeded).
        assert mock_sleep.call_count == 1

    # ------------------------------------------------------------------
    # (b) Retrying: primary exhausts; fallback (whisper-1 stand-in) succeeds.
    # ------------------------------------------------------------------
    def test_ingest_voice_falls_back_to_whisper(
        self,
        factory: ConnectionFactory,
        mock_embeddings: MagicMock,
        primary_provider: MagicMock,
        fallback_provider: MagicMock,
    ) -> None:
        primary_provider.transcribe.side_effect = _api_timeout()
        fallback_provider.transcribe.return_value = TranscriptionResult(
            text="from whisper fallback", uncertain_spans=[],
        )
        wrapper = RetryingTranscriptionProvider(
            primary=primary_provider,
            fallback=fallback_provider,
            max_attempts=3,
        )
        service = self._build_service(factory, wrapper, mock_embeddings)

        with patch("journal.providers.transcription.time.sleep"):
            entry = service.ingest_voice(
                audio_data=b"fake audio",
                media_type="audio/mp3",
                date="2026-04-01",
            )

        assert entry.raw_text == "from whisper fallback"
        assert entry.source_type == "voice"
        assert primary_provider.transcribe.call_count == 3
        fallback_provider.transcribe.assert_called_once()

    # ------------------------------------------------------------------
    # (c) Shadow: primary text wins; shadow text never reaches the entry.
    # ------------------------------------------------------------------
    def test_ingest_voice_with_shadow_returns_primary(
        self,
        factory: ConnectionFactory,
        mock_embeddings: MagicMock,
        primary_provider: MagicMock,
        shadow_provider: MagicMock,
    ) -> None:
        primary_provider.transcribe.return_value = TranscriptionResult(
            text="primary text", uncertain_spans=[],
        )
        shadow_provider.transcribe.return_value = TranscriptionResult(
            text="shadow text", uncertain_spans=[],
        )
        wrapper = ShadowTranscriptionProvider(
            primary=primary_provider, shadow=shadow_provider,
        )
        service = self._build_service(factory, wrapper, mock_embeddings)

        entry = service.ingest_voice(
            audio_data=b"fake audio",
            media_type="audio/mp3",
            date="2026-04-02",
        )

        assert entry.raw_text == "primary text"
        assert entry.final_text == "primary text"
        # And the persisted entry agrees with the in-memory one.
        stored = service._repo.get_entry(entry.id)
        assert stored is not None
        assert stored.raw_text == "primary text"

    # ------------------------------------------------------------------
    # (d) Shadow failure: ingestion succeeds; warning is logged.
    # ------------------------------------------------------------------
    def test_ingest_voice_shadow_failure_does_not_break_ingestion(
        self,
        factory: ConnectionFactory,
        mock_embeddings: MagicMock,
        primary_provider: MagicMock,
        shadow_provider: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        primary_provider.transcribe.return_value = TranscriptionResult(
            text="primary survives", uncertain_spans=[],
        )
        shadow_provider.transcribe.side_effect = RuntimeError(
            "shadow exploded",
        )
        wrapper = ShadowTranscriptionProvider(
            primary=primary_provider,
            shadow=shadow_provider,
            shadow_label="gemini/gemini-2.5-pro",
        )
        service = self._build_service(factory, wrapper, mock_embeddings)

        with caplog.at_level(
            logging.WARNING, logger="journal.providers.transcription",
        ):
            entry = service.ingest_voice(
                audio_data=b"fake audio",
                media_type="audio/mp3",
                date="2026-04-03",
            )

        assert entry.raw_text == "primary survives"
        # A WARNING log line names the failed shadow.
        assert any(
            record.levelno == logging.WARNING
            and "gemini/gemini-2.5-pro" in record.getMessage()
            for record in caplog.records
        ), "Expected a WARNING naming the failed shadow"

    # ------------------------------------------------------------------
    # (e) Shadow logs a `transcription_shadow_diff` record with diffs.
    # ------------------------------------------------------------------
    def test_ingest_voice_shadow_logs_diff(
        self,
        factory: ConnectionFactory,
        mock_embeddings: MagicMock,
        primary_provider: MagicMock,
        shadow_provider: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        primary_provider.transcribe.return_value = TranscriptionResult(
            text="alpha beta gamma", uncertain_spans=[],
        )
        shadow_provider.transcribe.return_value = TranscriptionResult(
            text="alpha BETA gamma", uncertain_spans=[],
        )
        wrapper = ShadowTranscriptionProvider(
            primary=primary_provider, shadow=shadow_provider,
        )
        service = self._build_service(factory, wrapper, mock_embeddings)

        with caplog.at_level(
            logging.INFO, logger="journal.providers.transcription",
        ):
            service.ingest_voice(
                audio_data=b"fake audio",
                media_type="audio/mp3",
                date="2026-04-04",
            )

        diff_records = [
            r for r in caplog.records
            if r.getMessage() == "transcription_shadow_diff"
        ]
        assert len(diff_records) == 1, (
            "Expected exactly one transcription_shadow_diff log record"
        )
        record = diff_records[0]
        diffs = record.diffs  # type: ignore[attr-defined]
        assert isinstance(diffs, list)
        assert len(diffs) >= 1
        # The disagreement covers the middle word.
        assert any(
            d["primary"] == "beta" and d["shadow"] == "BETA" for d in diffs
        )

    # ------------------------------------------------------------------
    # (f) Uncertain spans must survive the wrapper chain (retry + shadow).
    # ------------------------------------------------------------------
    def test_ingest_voice_uncertain_spans_persist_through_stack(
        self,
        factory: ConnectionFactory,
        mock_embeddings: MagicMock,
        primary_provider: MagicMock,
        shadow_provider: MagicMock,
    ) -> None:
        primary_provider.transcribe.return_value = TranscriptionResult(
            text="hello world", uncertain_spans=[(0, 5)],
        )
        shadow_provider.transcribe.return_value = TranscriptionResult(
            text="hello world", uncertain_spans=[],
        )
        # Stack: Shadow(Retrying(primary), shadow)
        retry_wrapper = RetryingTranscriptionProvider(
            primary=primary_provider, max_attempts=3,
        )
        full_stack = ShadowTranscriptionProvider(
            primary=retry_wrapper, shadow=shadow_provider,
        )
        service = self._build_service(factory, full_stack, mock_embeddings)

        with patch("journal.providers.transcription.time.sleep"):
            entry = service.ingest_voice(
                audio_data=b"fake audio",
                media_type="audio/mp3",
                date="2026-04-05",
            )

        assert entry.raw_text == "hello world"
        # The primary's uncertain span must have survived both wrappers.
        spans = service._repo.get_uncertain_spans(entry.id)
        assert spans == [(0, 5)]
        assert entry.raw_text[0:5] == "hello"


class TestIngestionPublicAPI:
    """Public methods added in Unit 1b so api/ingestion.py doesn't reach
    into ``ingestion_svc._repo`` or call the private ``_store_source_file``.
    """

    def test_get_page_count_delegates(self, ingestion_service):
        entry = ingestion_service.ingest_image(
            image_data=b"only-page", media_type="image/jpeg", date="2026-03-22",
        )
        # Single-image ingest creates one page.
        assert ingestion_service.get_page_count(entry.id) == 1

    def test_get_page_count_for_text_entry_is_zero(self, ingestion_service):
        # Text ingestion creates no pages (pages are an OCR artefact).
        entry = ingestion_service.ingest_text(
            text="hello world", date="2026-03-22", source_type="text_entry",
        )
        assert ingestion_service.get_page_count(entry.id) == 0

    def test_store_source_file_inserts_row(self, ingestion_service, repo):
        entry = ingestion_service.ingest_text(
            text="any text", date="2026-03-22", source_type="text_entry",
        )
        source_id = ingestion_service.store_source_file(
            entry.id, "upload:notes.md", "text/markdown", "deadbeef",
        )
        assert isinstance(source_id, int) and source_id > 0
        # And it shows up in the source_files table for that entry.
        row = repo.connection.execute(
            "SELECT file_path, file_type, file_hash FROM source_files WHERE id = ?",
            (source_id,),
        ).fetchone()
        assert row["file_path"] == "upload:notes.md"
        assert row["file_type"] == "text/markdown"
        assert row["file_hash"] == "deadbeef"

    def test_update_entry_date_changes_date(self, ingestion_service):
        entry = ingestion_service.ingest_text(
            text="some text", date="2026-03-22", source_type="text_entry",
        )
        updated, released = ingestion_service.update_entry_date(entry.id, "2026-04-15")
        assert updated is not None
        assert updated.entry_date == "2026-04-15"
        assert released is False

    def test_update_entry_date_returns_none_for_missing_entry(
        self, ingestion_service,
    ):
        updated, released = ingestion_service.update_entry_date(999_999, "2026-04-15")
        assert updated is None
        assert released is False

    def test_update_entry_date_refreshes_chunk_metadata(self, ingestion_service):
        """Motivating bug (2026-07-13): a date edit left per-chunk
        ``entry_date`` metadata stale in the vector store."""
        entry = ingestion_service.ingest_text(
            text="hello world body", date="2026-07-01", source_type="text_entry",
        )
        updated, released = ingestion_service.update_entry_date(entry.id, "2026-07-02")
        assert updated is not None and released is False
        results = [
            r
            for r in ingestion_service.vector_store.search([0.1, 0.2, 0.3], limit=50)
            if r.entry_id == entry.id
        ]
        assert results
        assert all(r.metadata["entry_date"] == "2026-07-02" for r in results)

    def test_update_entry_date_releases_quarantined_entry(
        self, ingestion_service, repo,
    ):
        held = repo.create_entry(
            "2019-07-09", "photo", "raw", 1, user_id=1, date_confirmed=False,
        )
        updated, released = ingestion_service.update_entry_date(
            held.id, "2026-07-09", user_id=1,
        )
        assert updated is not None
        assert released is True
        assert repo.get_entry(held.id, 1).date_confirmed is True

    def test_verify_doubts_marks_entry_verified(self, ingestion_service):
        entry = ingestion_service.ingest_text(
            text="some text", date="2026-03-22", source_type="text_entry",
        )
        assert ingestion_service.verify_doubts(entry.id) is True

    def test_verify_doubts_returns_false_for_missing_entry(
        self, ingestion_service,
    ):
        assert ingestion_service.verify_doubts(999_999) is False


def _year_off_fixture() -> tuple[str, str, str]:
    """(heading, correct_iso, wrong_iso): a heading whose weekday matches
    TODAY's date but whose written year is last year — the entries
    112/116 incident shape, computed live so the test never rots."""
    import calendar
    import datetime as dt

    today = dt.date.today()
    try:
        wrong = today.replace(year=today.year - 1)
    except ValueError:  # 29 Feb
        today = today - dt.timedelta(days=1)
        wrong = today.replace(year=today.year - 1)
    heading = (
        f"{calendar.day_name[today.weekday()]} {today.day}"
        f" {calendar.month_name[today.month]} {wrong.year} 9:40"
    )
    return heading, today.isoformat(), wrong.isoformat()


class TestIngestDateRepair:
    """Weekday auto-repair + quarantine at image ingest (spec 2026-07-13)."""

    def test_repairs_year_off_heading(self, ingestion_service, mock_ocr, repo):
        heading, correct_iso, _wrong_iso = _year_off_fixture()
        mock_ocr.extract.return_value = _ocr_result(
            f"{heading}\nWe played football in the park today."
        )
        entry = ingestion_service.ingest_image(
            image_data=b"repair-img", media_type="image/jpeg", date="2026-01-15",
        )
        assert entry.entry_date == correct_iso
        assert entry.date_confirmed is True
        assert entry.chunk_count > 0  # processed normally
        assert repo.get_uncertain_spans(entry.id)  # reviewable audit marker

    def test_quarantines_unrepairable_date(self, ingestion_service, mock_ocr, repo):
        mock_ocr.extract.return_value = _ocr_result(
            "9 July 2019\nAn old page with no weekday word at all."
        )
        entry = ingestion_service.ingest_image(
            image_data=b"quarantine-img", media_type="image/jpeg", date="2026-01-15",
        )
        assert entry.date_confirmed is False
        assert entry.entry_date == "2019-07-09"  # provisional display value
        assert entry.chunk_count == 0  # held from all derived pipelines

    def test_in_range_heading_unchanged(self, ingestion_service, mock_ocr):
        import calendar
        import datetime as dt

        today = dt.date.today()
        heading = (
            f"{calendar.day_name[today.weekday()]} {today.day}"
            f" {calendar.month_name[today.month]} {today.year} 9:40"
        )
        mock_ocr.extract.return_value = _ocr_result(f"{heading}\nA normal day.")
        entry = ingestion_service.ingest_image(
            image_data=b"ok-img", media_type="image/jpeg", date="2026-01-15",
        )
        assert entry.entry_date == today.isoformat()
        assert entry.date_confirmed is True
