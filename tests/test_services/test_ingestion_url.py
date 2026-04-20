"""Tests for URL-based ingestion.

The module-scoped autouse fixture `_skip_ssrf_validation` stubs out
`_validate_public_url` so the tests can use literal hostnames like
`example.com` and `files.slack.com` without triggering real DNS
resolution (which is unreliable in CI and irrelevant to what these
tests are checking). SSRF validation itself is covered by
`tests/test_services/test_ssrf.py`.
"""

from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import pytest

from journal.db.repository import SQLiteEntryRepository
from journal.providers.ocr import OCRResult
from journal.services.chunking import FixedTokenChunker
from journal.services.ingestion import IngestionService
from journal.vectorstore.store import InMemoryVectorStore


def _ocr_result(text: str, spans: list[tuple[int, int]] | None = None) -> OCRResult:
    return OCRResult(text=text, uncertain_spans=list(spans) if spans else [])


@pytest.fixture(autouse=True)
def _skip_ssrf_validation():
    with patch("journal.services.ingestion._validate_public_url"):
        yield


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
        preprocess_images=False,
    )


@pytest.fixture
def ingestion_service_with_slack(
    db_conn, mock_ocr, mock_transcription, mock_embeddings,
):
    repo = SQLiteEntryRepository(db_conn)
    vector_store = InMemoryVectorStore()
    return IngestionService(
        repository=repo,
        vector_store=vector_store,
        ocr_provider=mock_ocr,
        transcription_provider=mock_transcription,
        embeddings_provider=mock_embeddings,
        chunker=FixedTokenChunker(max_tokens=150, overlap_tokens=40),
        slack_bot_token="xoxb-test-token-123",
        preprocess_images=False,
    )


def _mock_urlopen(data: bytes, content_type: str = "image/jpeg"):
    """Create a mock urllib response."""
    response = MagicMock()
    response.read.return_value = data
    response.headers = {"Content-Type": content_type}
    response.__enter__ = lambda s: s
    response.__exit__ = MagicMock(return_value=False)
    return response


class TestIngestImageFromUrl:
    @patch("journal.services.ingestion.urlopen")
    def test_downloads_and_ingests(self, mock_url, ingestion_service, mock_ocr):
        mock_url.return_value = _mock_urlopen(b"fake image bytes")

        entry = ingestion_service.ingest_image_from_url(
            url="https://files.slack.com/image.jpg",
            date="2026-03-22",
        )

        assert entry.entry_date == "2026-03-22"
        assert entry.source_type == "photo"
        mock_ocr.extract.assert_called_once_with(b"fake image bytes", "image/jpeg")

    @patch("journal.services.ingestion.urlopen")
    def test_uses_explicit_media_type(self, mock_url, ingestion_service, mock_ocr):
        mock_url.return_value = _mock_urlopen(b"png data", content_type="application/octet-stream")

        ingestion_service.ingest_image_from_url(
            url="https://example.com/photo.png",
            date="2026-03-22",
            media_type="image/png",
        )

        mock_ocr.extract.assert_called_once_with(b"png data", "image/png")

    @patch("journal.services.ingestion.urlopen")
    def test_infers_media_type_from_response(self, mock_url, ingestion_service, mock_ocr):
        mock_url.return_value = _mock_urlopen(b"data", content_type="image/webp")

        ingestion_service.ingest_image_from_url(
            url="https://example.com/photo",
            date="2026-03-22",
        )

        mock_ocr.extract.assert_called_once_with(b"data", "image/webp")

    @patch("journal.services.ingestion.urlopen")
    def test_download_failure_raises(self, mock_url, ingestion_service):
        mock_url.side_effect = URLError("Connection refused")

        with pytest.raises(ValueError, match="Failed to download"):
            ingestion_service.ingest_image_from_url(
                url="https://example.com/broken",
                date="2026-03-22",
            )

    @patch("journal.services.ingestion.urlopen")
    def test_http_error_raises(self, mock_url, ingestion_service):
        mock_url.side_effect = HTTPError(
            url="https://example.com/forbidden",
            code=403,
            msg="Forbidden",
            hdrs=MagicMock(),
            fp=None,
        )

        with pytest.raises(ValueError, match="Failed to download.*403"):
            ingestion_service.ingest_image_from_url(
                url="https://example.com/forbidden",
                date="2026-03-22",
            )

    @patch("journal.services.ingestion.urlopen")
    def test_duplicate_detection(self, mock_url, ingestion_service):
        mock_url.return_value = _mock_urlopen(b"same image data")

        ingestion_service.ingest_image_from_url(
            url="https://example.com/page1.jpg",
            date="2026-03-22",
        )

        mock_url.return_value = _mock_urlopen(b"same image data")

        with pytest.raises(ValueError, match="already been uploaded"):
            ingestion_service.ingest_image_from_url(
                url="https://example.com/page1.jpg",
                date="2026-03-23",
            )


class TestSlackUrlAuth:
    @patch("journal.services.ingestion.urlopen")
    def test_adds_bearer_header_for_slack_urls(
        self, mock_url, ingestion_service_with_slack,
    ):
        mock_url.return_value = _mock_urlopen(b"slack image")

        ingestion_service_with_slack.ingest_image_from_url(
            url="https://files.slack.com/files-pri/T0X-F0X/journal.jpg",
            date="2026-03-22",
        )

        req = mock_url.call_args[0][0]
        assert req.get_header("Authorization") == "Bearer xoxb-test-token-123"

    @patch("journal.services.ingestion.urlopen")
    def test_no_auth_header_for_non_slack_urls(
        self, mock_url, ingestion_service_with_slack,
    ):
        mock_url.return_value = _mock_urlopen(b"other image")

        ingestion_service_with_slack.ingest_image_from_url(
            url="https://example.com/photo.jpg",
            date="2026-03-22",
        )

        req = mock_url.call_args[0][0]
        assert req.get_header("Authorization") is None

    @patch("journal.services.ingestion.urlopen")
    def test_no_auth_header_when_token_not_configured(
        self, mock_url, ingestion_service,
    ):
        mock_url.return_value = _mock_urlopen(b"slack image")

        ingestion_service.ingest_image_from_url(
            url="https://files.slack.com/files-pri/T0X-F0X/journal.jpg",
            date="2026-03-22",
        )

        req = mock_url.call_args[0][0]
        assert req.get_header("Authorization") is None


class TestIngestMultiPageFromUrls:
    @patch("journal.services.ingestion.urlopen")
    def test_downloads_all_pages_and_creates_single_entry(
        self, mock_url, ingestion_service, mock_ocr,
    ):
        # Each call to urlopen returns a different mock response so the
        # duplicate-hash check (which hashes raw bytes) passes for all pages.
        mock_url.side_effect = [
            _mock_urlopen(b"page one bytes"),
            _mock_urlopen(b"page two bytes"),
        ]
        mock_ocr.extract.side_effect = [
            _ocr_result("First page text about Vienna."),
            _ocr_result("Second page text about Atlas."),
        ]

        entry = ingestion_service.ingest_multi_page_entry_from_urls(
            urls=[
                "https://example.com/page1.jpg",
                "https://example.com/page2.jpg",
            ],
            date="2026-04-10",
        )

        assert entry.entry_date == "2026-04-10"
        assert entry.source_type == "photo"
        # Both page texts should be present in the combined entry.
        assert "First page text about Vienna." in entry.raw_text
        assert "Second page text about Atlas." in entry.raw_text
        assert mock_ocr.extract.call_count == 2
        assert mock_url.call_count == 2

    @patch("journal.services.ingestion.urlopen")
    def test_respects_per_url_media_type_override(
        self, mock_url, ingestion_service, mock_ocr,
    ):
        mock_url.side_effect = [
            _mock_urlopen(b"first", content_type="application/octet-stream"),
            _mock_urlopen(b"second", content_type="image/jpeg"),
        ]

        ingestion_service.ingest_multi_page_entry_from_urls(
            urls=["https://example.com/p1", "https://example.com/p2.jpg"],
            date="2026-04-10",
            media_types=["image/png", None],
        )

        # First call: explicit override wins.
        first_call = mock_ocr.extract.call_args_list[0]
        assert first_call[0][1] == "image/png"
        # Second call: inferred from response Content-Type.
        second_call = mock_ocr.extract.call_args_list[1]
        assert second_call[0][1] == "image/jpeg"

    @patch("journal.services.ingestion.urlopen")
    def test_empty_urls_raises(self, mock_url, ingestion_service):
        with pytest.raises(ValueError, match="At least one URL"):
            ingestion_service.ingest_multi_page_entry_from_urls(
                urls=[], date="2026-04-10",
            )
        mock_url.assert_not_called()

    @patch("journal.services.ingestion.urlopen")
    def test_mismatched_media_types_length_raises(
        self, mock_url, ingestion_service,
    ):
        with pytest.raises(ValueError, match="same length"):
            ingestion_service.ingest_multi_page_entry_from_urls(
                urls=["https://example.com/p1", "https://example.com/p2"],
                date="2026-04-10",
                media_types=["image/jpeg"],
            )
        mock_url.assert_not_called()

    @patch("journal.services.ingestion.urlopen")
    def test_previously_ingested_page_raises(
        self, mock_url, ingestion_service,
    ):
        # Ingest a single image first so its hash is recorded in source_files.
        mock_url.return_value = _mock_urlopen(b"page one bytes")
        ingestion_service.ingest_image_from_url(
            url="https://example.com/first.jpg",
            date="2026-04-09",
        )

        # Now a multi-page batch that includes the same bytes should fail
        # because the hash is already in source_files.
        mock_url.return_value = None
        mock_url.side_effect = [
            _mock_urlopen(b"page one bytes"),
            _mock_urlopen(b"page two bytes"),
        ]

        with pytest.raises(ValueError, match="already been uploaded"):
            ingestion_service.ingest_multi_page_entry_from_urls(
                urls=[
                    "https://example.com/p1.jpg",
                    "https://example.com/p2.jpg",
                ],
                date="2026-04-10",
            )

    @patch("journal.services.ingestion.urlopen")
    def test_slack_urls_get_bearer_header(
        self, mock_url, ingestion_service_with_slack,
    ):
        mock_url.side_effect = [
            _mock_urlopen(b"slack page one"),
            _mock_urlopen(b"slack page two"),
        ]

        ingestion_service_with_slack.ingest_multi_page_entry_from_urls(
            urls=[
                "https://files.slack.com/files-pri/T0X-F0X/p1.jpg",
                "https://files.slack.com/files-pri/T0X-F0X/p2.jpg",
            ],
            date="2026-04-10",
        )

        for call in mock_url.call_args_list:
            req = call[0][0]
            assert req.get_header("Authorization") == "Bearer xoxb-test-token-123"


class TestIngestVoiceFromUrl:
    @patch("journal.services.ingestion.urlopen")
    def test_downloads_and_transcribes(self, mock_url, ingestion_service, mock_transcription):
        mock_url.return_value = _mock_urlopen(b"fake audio bytes", content_type="audio/mp3")

        entry = ingestion_service.ingest_voice_from_url(
            url="https://example.com/note.mp3",
            date="2026-03-22",
        )

        assert entry.entry_date == "2026-03-22"
        assert entry.source_type == "voice"
        mock_transcription.transcribe.assert_called_once_with(
            b"fake audio bytes", "audio/mp3", "en",
        )

    @patch("journal.services.ingestion.urlopen")
    def test_passes_language(self, mock_url, ingestion_service, mock_transcription):
        mock_url.return_value = _mock_urlopen(b"audio", content_type="audio/m4a")

        ingestion_service.ingest_voice_from_url(
            url="https://example.com/note.m4a",
            date="2026-03-22",
            language="nl",
        )

        mock_transcription.transcribe.assert_called_once_with(b"audio", "audio/m4a", "nl")
