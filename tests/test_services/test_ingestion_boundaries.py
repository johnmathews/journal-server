"""Tests for the unified _ingest_pages path with content-window boundaries.

TDD — these tests were written RED before the implementation and go GREEN
once _ingest_pages, _combine_pages, and the updated public wrappers land in
image.py.
"""

import pytest

from journal.db.repository import SQLiteEntryRepository
from journal.providers.ocr import ENTRY_BEGINS, ENTRY_ENDS, OCRResult, PageRole
from journal.services.chunking import FixedTokenChunker
from journal.services.ingestion import IngestionService
from journal.vectorstore.store import InMemoryVectorStore


class _RoleOCR:
    """Returns canned text per page; records the roles it was given."""

    def __init__(self, pages: list[str]):
        self._pages = pages
        self._i = 0
        self.roles: list[PageRole | None] = []

    def extract(  # noqa: E501
        self, image_data: bytes, media_type: str, page_role: PageRole | None = None,
    ) -> OCRResult:
        self.roles.append(page_role)
        text = self._pages[self._i]
        self._i += 1
        return OCRResult(text=text, uncertain_spans=[])


@pytest.fixture
def make_ingestion(factory):
    """Return a factory function that builds IngestionService with the given OCR provider."""
    from unittest.mock import MagicMock

    def _make(ocr_provider):
        repo = SQLiteEntryRepository(factory)
        mock_embeddings = MagicMock()
        mock_embeddings.embed_texts.return_value = [[0.1, 0.2, 0.3]]
        mock_embeddings.embed_query.return_value = [0.1, 0.2, 0.3]
        mock_transcription = MagicMock()
        return IngestionService(
            repository=repo,
            vector_store=InMemoryVectorStore(),
            ocr_provider=ocr_provider,
            transcription_provider=mock_transcription,
            embeddings_provider=mock_embeddings,
            chunker=FixedTokenChunker(max_tokens=150, overlap_tokens=40),
            preprocess_images=False,
        )

    return _make


def test_single_image_only_role_trims_tail_and_next(make_ingestion):
    ocr = _RoleOCR([f"prev tail\n{ENTRY_BEGINS}\nMy entry body\n{ENTRY_ENDS}\nnext"])
    svc = make_ingestion(ocr)
    entry = svc.ingest_image(b"img", "image/png", "2026-01-01")
    assert ocr.roles == [PageRole.ONLY]
    # raw_text keeps everything (markers stripped), window isolates the body
    assert "prev tail" in entry.raw_text and "next" in entry.raw_text
    assert ENTRY_BEGINS not in entry.raw_text
    assert entry.raw_text[entry.content_start_char:entry.content_end_char] == "My entry body\n"
    # final_text (reading view) is the in-bounds slice only
    assert "prev tail" not in entry.final_text and "next" not in entry.final_text


def test_multi_page_roles_and_first_last_trim(make_ingestion):
    ocr = _RoleOCR([
        f"yesterday tail\n{ENTRY_BEGINS}\nPage one body",   # FIRST
        "page two body",                                     # MIDDLE
        f"page three end\n{ENTRY_ENDS}\ntomorrow heading",   # LAST
    ])
    svc = make_ingestion(ocr)
    entry = svc.ingest_multi_page_entry(
        [(b"a", "image/png"), (b"b", "image/png"), (b"c", "image/png")],
        "2026-01-01",
    )
    assert ocr.roles == [PageRole.FIRST, PageRole.MIDDLE, PageRole.LAST]
    content = entry.raw_text[entry.content_start_char:entry.content_end_char]
    assert "yesterday tail" not in content
    assert "tomorrow heading" not in content
    assert "Page one body" in content and "page three end" in content


def test_no_markers_leaves_window_null(make_ingestion):
    ocr = _RoleOCR(["just one clean entry"])
    svc = make_ingestion(ocr)
    entry = svc.ingest_image(b"img", "image/png", "2026-01-01")
    assert entry.content_start_char is None
    assert entry.content_end_char is None
