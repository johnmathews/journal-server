"""Ingestion service — orchestrates OCR/transcription, chunking, embedding, and storage."""

import datetime
import hashlib
import ipaddress
import logging
import socket
from typing import TYPE_CHECKING
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from journal.db.repository import EntryRepository
from journal.models import Entry
from journal.providers.embeddings import EmbeddingsProvider
from journal.providers.ocr import OCRProvider, OCRResult
from journal.providers.transcription import TranscriptionProvider
from journal.services.chunking import ChunkingStrategy
from journal.vectorstore.store import VectorStore

if TYPE_CHECKING:
    from collections.abc import Callable

    from journal.providers.formatter import FormatterProtocol
    from journal.services.mood_scoring import MoodScoringService

log = logging.getLogger(__name__)


def _strip_and_shift_page_spans(
    page_text: str,
    page_spans: list[tuple[int, int]],
    cumulative_offset: int,
) -> tuple[str, list[tuple[int, int]]]:
    """Strip whitespace from `page_text` and shift spans into entry-level coords.

    Multi-page ingestion combines per-page OCR results with
    ``"\\n".join(p.strip() for p in pages)``. The sentinel parser
    returns spans in the *pre-strip* page coordinates; this helper
    applies the same strip, discards spans that land fully in the
    trimmed whitespace, clips spans that partially overlap the kept
    region, and shifts the surviving spans by ``cumulative_offset``
    so they address positions in the combined entry text.

    Returns ``(stripped_text, shifted_spans)``.
    """
    lstripped = page_text.lstrip()
    leading = len(page_text) - len(lstripped)
    stripped = lstripped.rstrip()
    stripped_len = len(stripped)

    shifted: list[tuple[int, int]] = []
    for start, end in page_spans:
        new_start = max(0, start - leading)
        new_end = min(stripped_len, end - leading)
        if new_end > new_start:
            shifted.append(
                (cumulative_offset + new_start, cumulative_offset + new_end)
            )
    return stripped, shifted


def _validate_public_url(url: str) -> None:
    """Reject URLs that would expose the server to SSRF.

    Resolves the hostname via DNS and refuses to continue if any of its
    addresses are loopback (127.0.0.0/8, ::1), private (RFC1918 + RFC
    4193), link-local (169.254.0.0/16 — includes cloud metadata
    endpoints), multicast, reserved, or unspecified. Non-HTTP(S) schemes
    are also rejected, so `file://`, `gopher://`, and friends are
    blocked wholesale.

    This is called from `_download()` before any network traffic, so
    an attacker cannot use a journal-server ingest endpoint to pivot
    into internal services on the host VM or the cloud metadata IP.
    It does NOT defend against DNS rebinding between resolution and
    connection — an attacker with control of DNS could return a public
    IP to this check and a private IP to urlopen — but closing that is
    a socket-level fix that requires patching urllib's connection
    pathway, which is out of scope for a personal tool. Loopback and
    RFC1918 are the realistic threat surface, and they are closed.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            f"URL scheme must be http or https, got {parsed.scheme!r}"
        )
    if not parsed.hostname:
        raise ValueError(f"URL has no hostname: {url!r}")

    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except OSError as e:
        raise ValueError(
            f"Failed to resolve {parsed.hostname!r}: {e}"
        ) from e

    for info in infos:
        sockaddr = info[4]
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            # getaddrinfo returned something that isn't an IP — skip it.
            # The socket layer will refuse to connect to it anyway.
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise ValueError(
                f"Refusing to fetch {url!r} — host {parsed.hostname} "
                f"resolved to non-public address {ip_str}"
            )


class IngestionService:
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

    def ingest_image(
        self, image_data: bytes, media_type: str, date: str, *,
        skip_mood: bool = False, user_id: int = 1,
    ) -> Entry:
        """Ingest a journal page image: OCR -> chunk -> embed -> store."""
        log.info("Ingesting image for date %s (%s, %d bytes)", date, media_type, len(image_data))

        # Check for duplicate
        file_hash = hashlib.sha256(image_data).hexdigest()
        if self._is_duplicate(file_hash):
            raise ValueError(
                "This image has already been uploaded in another entry. "
                "Delete the existing entry first if you want to re-upload."
            )

        # Preprocess before OCR (auto-rotate, crop, downscale, contrast).
        image_data, media_type = self._maybe_preprocess(image_data, media_type)

        # Extract text + uncertainty spans via OCR. Spans are in
        # ocr_result.text coordinates; since we store that text as-is
        # in entries.raw_text (no stripping in the single-page path),
        # no offset shifting is needed here.
        ocr_result = self._ocr.extract(image_data, media_type)
        raw_text = ocr_result.text
        if not raw_text.strip():
            raise ValueError("OCR extracted no text from image")

        # Try to extract date from the OCR text if caller used a default
        from journal.services.date_extraction import extract_date_from_text

        extracted = extract_date_from_text(raw_text)
        if extracted:
            date = extracted

        # Store entry (final_text defaults to raw_text)
        word_count = len(raw_text.split())
        entry = self._repo.create_entry(date, "photo", raw_text, word_count, user_id=user_id)
        source_file_id = self._store_source_file(
            entry.id, f"image_{date}", media_type, file_hash,
        )

        # Add page record
        self._repo.add_entry_page(entry.id, 1, raw_text, source_file_id)

        # Record uncertain spans anchored to raw_text.
        self._repo.add_uncertain_spans(entry.id, ocr_result.uncertain_spans)

        # Chunk, embed, and store in vector DB
        chunk_count = self._process_text(
            entry.id, entry.final_text, date, skip_mood=skip_mood, user_id=user_id,
        )
        self._repo.update_chunk_count(entry.id, chunk_count)

        log.info("Ingested image entry %d: %d words, date %s", entry.id, word_count, date)
        return self._repo.get_entry(entry.id)  # type: ignore[return-value]

    def ingest_voice(
        self, audio_data: bytes, media_type: str, date: str, language: str = "en",
        *, source_type: str = "voice", skip_mood: bool = False, user_id: int = 1,
    ) -> Entry:
        """Ingest a voice note: transcribe -> chunk -> embed -> store."""
        log.info(
            "Ingesting voice note for date %s (%s, %d bytes)", date, media_type, len(audio_data)
        )

        file_hash = hashlib.sha256(audio_data).hexdigest()
        if self._is_duplicate(file_hash):
            raise ValueError(
                "This audio file has already been uploaded in another entry. "
                "Delete the existing entry first if you want to re-upload."
            )

        # Transcribe
        result = self._transcription.transcribe(audio_data, media_type, language)
        raw_text = result.text if hasattr(result, "text") else result  # type: ignore[assignment]
        if not raw_text.strip():
            raise ValueError("Transcription produced no text from audio")

        # Optionally format with LLM paragraph breaks (final_text only;
        # raw_text stays as the original transcription).
        final_text = self._maybe_format_transcript(raw_text)

        word_count = len(raw_text.split())
        entry = self._repo.create_entry(
            date, source_type, raw_text, word_count, user_id=user_id,
            final_text=final_text if final_text != raw_text else None,
        )
        self._store_source_file(entry.id, f"voice_{date}", media_type, file_hash)

        # Record uncertain spans from transcription confidence data.
        uncertain_spans = getattr(result, "uncertain_spans", [])
        if uncertain_spans:
            self._repo.add_uncertain_spans(entry.id, uncertain_spans)

        # Chunk, embed, and store in vector DB
        chunk_count = self._process_text(
            entry.id, entry.final_text, date, skip_mood=skip_mood, user_id=user_id,
        )
        self._repo.update_chunk_count(entry.id, chunk_count)

        log.info("Ingested voice entry %d: %d words, date %s", entry.id, word_count, date)
        return self._repo.get_entry(entry.id)  # type: ignore[return-value]

    def ingest_multi_voice(
        self,
        recordings: list[tuple[bytes, str]],
        date: str,
        language: str = "en",
        *,
        source_type: str = "voice",
        skip_mood: bool = False,
        on_progress: "Callable[[int, int], None] | None" = None,
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
            if self._is_duplicate(file_hash):
                raise ValueError(
                    f"Recording {i + 1} has already been uploaded in another entry. "
                    f"Delete the existing entry first if you want to re-upload."
                )
            result = self._transcription.transcribe(audio_data, media_type, language)
            text = result.text if hasattr(result, "text") else result  # type: ignore[assignment]
            if not text.strip():
                raise ValueError(f"Transcription produced no text from recording {i + 1}")
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

        # Optionally format with LLM paragraph breaks (final_text only).
        final_text = self._maybe_format_transcript(raw_text)

        word_count = len(raw_text.split())
        entry = self._repo.create_entry(
            date, source_type, raw_text, word_count, user_id=user_id,
            final_text=final_text if final_text != raw_text else None,
        )

        # Store source file records for each recording
        for i, (file_hash, media_type) in enumerate(
            zip(file_hashes, file_media_types, strict=True)
        ):
            self._store_source_file(
                entry.id, f"voice_{date}_part{i + 1}", media_type, file_hash
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
            self._repo.add_uncertain_spans(entry.id, combined_spans)

        # Chunk, embed, and store in vector DB
        chunk_count = self._process_text(
            entry.id, entry.final_text, date, skip_mood=skip_mood, user_id=user_id,
        )
        self._repo.update_chunk_count(entry.id, chunk_count)

        log.info(
            "Ingested multi-voice entry %d: %d recordings, %d words, date %s",
            entry.id, len(recordings), word_count, date,
        )
        return self._repo.get_entry(entry.id)  # type: ignore[return-value]

    def ingest_text(
        self, text: str, date: str, source_type: str = "text_entry", *, skip_mood: bool = False,
        user_id: int = 1,
    ) -> Entry:
        """Ingest a plain-text entry (no OCR, no transcription).

        Used for manually typed entries and imported text/markdown files.
        The text is stored as both raw_text and final_text, then chunked,
        embedded, and stored in the vector DB.

        Args:
            text: The entry text content.
            date: Journal entry date (ISO 8601).
            source_type: Entry source type (e.g. "text_entry",
                "imported_text_file", "imported_audio_file").
            skip_mood: When True, skip inline mood scoring (caller will
                handle it separately, e.g. via an async job).
        """
        text = text.strip()
        if not text:
            raise ValueError("Text must not be empty")

        log.info(
            "Ingesting text entry for date %s (source=%s, %d chars)",
            date, source_type, len(text),
        )

        word_count = len(text.split())
        entry = self._repo.create_entry(
            date, source_type, text, word_count, user_id=user_id,
        )

        chunk_count = self._process_text(
            entry.id, entry.final_text, date, skip_mood=skip_mood,
            user_id=user_id,
        )
        self._repo.update_chunk_count(entry.id, chunk_count)

        log.info("Ingested text entry %d: %d words, date %s", entry.id, word_count, date)
        return self._repo.get_entry(entry.id)  # type: ignore[return-value]

    def ingest_image_from_url(
        self,
        url: str,
        date: str,
        media_type: str | None = None,
        *,
        user_id: int = 1,
    ) -> Entry:
        """Download an image from a URL and ingest it."""
        data, resolved_type = self._download(url, media_type)
        return self.ingest_image(data, resolved_type, date, user_id=user_id)

    def ingest_multi_page_entry_from_urls(
        self,
        urls: list[str],
        date: str,
        media_types: list[str | None] | None = None,
        *,
        user_id: int = 1,
    ) -> Entry:
        """Download a list of page images from URLs and ingest them as one entry.

        Each URL is downloaded (with Slack bearer auth where applicable),
        then the raw bytes are handed to `ingest_multi_page_entry` which
        OCRs each page individually and combines them into a single
        entry with one page record per image.

        Args:
            urls: Ordered list of image URLs, one per page.
            date: Journal entry date (ISO 8601).
            media_types: Optional per-URL MIME type overrides. If provided,
                must have the same length as `urls`; `None` entries fall
                back to the Content-Type returned by the server.
        """
        if not urls:
            raise ValueError("At least one URL is required")
        if media_types is not None and len(media_types) != len(urls):
            raise ValueError(
                "media_types must have the same length as urls when provided"
            )

        log.info("Downloading %d pages for multi-page entry (date=%s)", len(urls), date)
        images: list[tuple[bytes, str]] = []
        for i, url in enumerate(urls):
            override = media_types[i] if media_types is not None else None
            data, resolved_type = self._download(url, override)
            images.append((data, resolved_type))

        return self.ingest_multi_page_entry(images, date, user_id=user_id)

    def ingest_voice_from_url(
        self,
        url: str,
        date: str,
        media_type: str | None = None,
        language: str = "en",
        *,
        user_id: int = 1,
    ) -> Entry:
        """Download audio from a URL and ingest it."""
        data, resolved_type = self._download(url, media_type)
        return self.ingest_voice(data, resolved_type, date, language, user_id=user_id)

    def _download(
        self, url: str, media_type: str | None = None
    ) -> tuple[bytes, str]:
        """Download a file from a URL, return (data, media_type).

        SSRF protection: the URL is validated against `_validate_public_url`
        before any socket is opened, so loopback/private/link-local targets
        (including cloud metadata endpoints like 169.254.169.254) are
        refused regardless of the caller.
        """
        _validate_public_url(url)
        log.info("Downloading from %s", url)
        try:
            req = Request(url, headers={"User-Agent": "journal-server/0.1"})
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
        row = self._repo._conn.execute(  # type: ignore[attr-defined]
            "SELECT id FROM source_files WHERE file_hash = ?", (file_hash,)
        ).fetchone()
        return row is not None

    def ingest_multi_page_entry(
        self,
        images: list[tuple[bytes, str]],
        date: str,
        *,
        skip_mood: bool = False,
        on_progress: "Callable[[int, int], None] | None" = None,
        user_id: int = 1,
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
        page_results: list[OCRResult] = []
        page_hashes: list[str] = []
        page_media_types: list[str] = []
        for i, (image_data, media_type) in enumerate(images):
            file_hash = hashlib.sha256(image_data).hexdigest()
            if self._is_duplicate(file_hash):
                raise ValueError(
                    f"Page {i + 1} has already been uploaded in another entry. "
                    f"Delete the existing entry first if you want to re-upload."
                )
            image_data, media_type = self._maybe_preprocess(image_data, media_type)
            ocr_result = self._ocr.extract(image_data, media_type)
            if not ocr_result.text.strip():
                raise ValueError(f"OCR extracted no text from page {i + 1}")
            page_results.append(ocr_result)
            page_hashes.append(file_hash)
            page_media_types.append(media_type)
            if on_progress is not None:
                on_progress(i + 1, len(images))

        # Combine page texts. Each page is stripped of leading/trailing
        # whitespace (OCR output frequently has trailing newlines) and
        # joined with a SINGLE newline rather than a blank line. This
        # matters for chunking: `FixedTokenChunker` splits paragraphs
        # on `"\n\n"`, so a blank-line join turns every page boundary
        # into a paragraph break. The packer doesn't flush *at* a
        # paragraph break — it flushes when the next paragraph would
        # overflow the token budget — but once each page is a single
        # ~80-token paragraph, any two-page combination (80 + 80 = 160)
        # already exceeds the 150-token budget, so the packer flushes
        # at the page boundary and wastes nearly half the budget. Joining
        # with a single newline keeps the paragraph splitter blind to
        # page boundaries, letting the packer combine pages up to the
        # real budget. The verbatim per-page OCR output is still stored
        # in `entry_pages.raw_text` below.
        #
        # Uncertain spans come back per-page in pre-strip coordinates.
        # `_strip_and_shift_page_spans` re-anchors them to the combined
        # entry text so the API can serve them without any further
        # coordinate arithmetic.
        stripped_parts: list[str] = []
        combined_spans: list[tuple[int, int]] = []
        cumulative_offset = 0
        for i, r in enumerate(page_results):
            stripped, shifted = _strip_and_shift_page_spans(
                r.text, r.uncertain_spans, cumulative_offset
            )
            stripped_parts.append(stripped)
            combined_spans.extend(shifted)
            cumulative_offset += len(stripped)
            if i < len(page_results) - 1:
                cumulative_offset += 1  # the "\n" separator between pages
        combined_text = "\n".join(stripped_parts)
        word_count = len(combined_text.split())

        # Try to extract date from the first page's OCR text
        from journal.services.date_extraction import extract_date_from_text

        extracted = extract_date_from_text(combined_text)
        if extracted:
            date = extracted

        # Create single entry
        entry = self._repo.create_entry(date, "photo", combined_text, word_count, user_id=user_id)

        # Store source files and pages
        for i, (_image_data, _) in enumerate(images):
            source_file_id = self._store_source_file(
                entry.id, f"image_{date}_p{i + 1}", page_media_types[i], page_hashes[i],
            )
            # Per-page raw_text preserves the verbatim extracted text
            # (after sentinel stripping) — what the model gave us for
            # that image, un-stripped. Useful for per-page review.
            self._repo.add_entry_page(
                entry.id, i + 1, page_results[i].text, source_file_id
            )

        # Record uncertain spans anchored to entries.raw_text.
        self._repo.add_uncertain_spans(entry.id, combined_spans)

        # Chunk, embed, and store
        chunk_count = self._process_text(
            entry.id, entry.final_text, date, skip_mood=skip_mood, user_id=user_id,
        )
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
