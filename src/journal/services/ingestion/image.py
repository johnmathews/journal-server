"""Image (OCR) ingest paths for ``IngestionService``.

Mixin holding the single- and multi-page entry points. Methods stay
bound to ``self`` so they keep using the constructor-injected
collaborators (``_ocr``, ``_repo``, ``_detect_heading``,
``_maybe_preprocess``, …) without any context-passing churn.
"""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from journal.models import Entry
    from journal.providers.ocr import OCRResult

log = logging.getLogger(__name__)


def _strip_and_shift_page_spans(
    page_text: str,
    page_spans: list[tuple[int, int]],
    cumulative_offset: int,
) -> tuple[str, list[tuple[int, int]]]:
    """Strip whitespace from `page_text` and shift spans into entry coords.

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


class _ImageIngestMixin:
    """``ingest_image`` and ``ingest_multi_page_entry`` — OCR paths."""

    def ingest_image(
        self,
        image_data: bytes,
        media_type: str,
        date: str,
        *,
        skip_mood: bool = False,
        user_id: int = 1,
    ) -> Entry:
        """Ingest a journal page image: OCR -> chunk -> embed -> store."""
        log.info(
            "Ingesting image for date %s (%s, %d bytes)",
            date, media_type, len(image_data),
        )

        # Check for duplicate
        file_hash = hashlib.sha256(image_data).hexdigest()
        if self._is_duplicate(file_hash):  # type: ignore[attr-defined]
            raise ValueError(
                "This image has already been uploaded in another entry. "
                "Delete the existing entry first if you want to re-upload."
            )

        # Preprocess before OCR (auto-rotate, crop, downscale, contrast).
        image_data, media_type = self._maybe_preprocess(  # type: ignore[attr-defined]
            image_data, media_type,
        )

        # Extract text + uncertainty spans via OCR. Spans are in
        # ocr_result.text coordinates; since we store that text as-is
        # in entries.raw_text (no stripping in the single-page path),
        # no offset shifting is needed here.
        ocr_result = self._ocr.extract(image_data, media_type)  # type: ignore[attr-defined]
        raw_text = ocr_result.text
        if not raw_text.strip():
            raise ValueError("OCR extracted no text from image")

        # Try to extract date from the OCR text if caller used a default
        from journal.services.date_extraction import extract_date_from_text

        extracted = extract_date_from_text(raw_text)
        if extracted:
            date = extracted

        # Optional date-heading detection. When a leading date is found
        # we strip it from the body entirely — the entry's title already
        # shows the date, so reproducing it as a markdown heading or
        # leaving it as the first line would just be a redundant duplicate
        # of the title. raw_text is left verbatim so the OCR overlay /
        # audit trail still points at exactly what the model returned.
        # If the detector resolved an ISO date (covers spelled-out and
        # relative phrases the regex can't), it overrides — the LLM is
        # the more capable of the two extractors.
        det = self._detect_heading(raw_text, date)  # type: ignore[attr-defined]
        if det.date_iso:
            date = det.date_iso
        final_text = det.body if det.has_heading else None

        # Store entry (final_text defaults to raw_text when None)
        word_count = len(raw_text.split())
        entry = self._repo.create_entry(  # type: ignore[attr-defined]
            date, "photo", raw_text, word_count, user_id=user_id,
            final_text=final_text,
        )
        source_file_id = self.store_source_file(  # type: ignore[attr-defined]
            entry.id, f"image_{date}", media_type, file_hash,
        )

        # Add page record
        self._repo.add_entry_page(entry.id, 1, raw_text, source_file_id)  # type: ignore[attr-defined]

        # Record uncertain spans anchored to raw_text.
        self._repo.add_uncertain_spans(entry.id, ocr_result.uncertain_spans)  # type: ignore[attr-defined]

        # Chunk, embed, and store in vector DB
        chunk_count = self._process_text(  # type: ignore[attr-defined]
            entry.id, entry.final_text, date,
            skip_mood=skip_mood, user_id=user_id,
        )
        self._repo.update_chunk_count(entry.id, chunk_count)  # type: ignore[attr-defined]

        log.info(
            "Ingested image entry %d: %d words, date %s",
            entry.id, word_count, date,
        )
        return self._repo.get_entry(entry.id)  # type: ignore[attr-defined,return-value]

    def ingest_multi_page_entry(
        self,
        images: list[tuple[bytes, str]],
        date: str,
        *,
        skip_mood: bool = False,
        on_progress: Callable[[int, int], None] | None = None,
        user_id: int = 1,
    ) -> Entry:
        """Ingest multiple page images as a single journal entry.

        Args:
            images: List of (image_data, media_type) tuples, one per page in order.
            date: Journal entry date (ISO 8601).
        """
        if not images:
            raise ValueError("At least one image is required")

        log.info(
            "Ingesting multi-page entry for date %s (%d pages)",
            date, len(images),
        )

        # OCR each page and check for duplicates
        page_results: list[OCRResult] = []
        page_hashes: list[str] = []
        page_media_types: list[str] = []
        for i, (image_data, media_type) in enumerate(images):
            file_hash = hashlib.sha256(image_data).hexdigest()
            if self._is_duplicate(file_hash):  # type: ignore[attr-defined]
                raise ValueError(
                    f"Page {i + 1} has already been uploaded in another "
                    f"entry. Delete the existing entry first if you want "
                    f"to re-upload."
                )
            image_data, media_type = self._maybe_preprocess(  # type: ignore[attr-defined]
                image_data, media_type,
            )
            ocr_result = self._ocr.extract(image_data, media_type)  # type: ignore[attr-defined]
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
        # at the page boundary and wastes nearly half the budget.
        # Joining with a single newline keeps the paragraph splitter
        # blind to page boundaries, letting the packer combine pages up
        # to the real budget. The verbatim per-page OCR output is still
        # stored in `entry_pages.raw_text` below.
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
                r.text, r.uncertain_spans, cumulative_offset,
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

        # Optional date-heading detection — same as single-page OCR. A
        # detected leading date is stripped from the body entirely,
        # never promoted to a markdown heading: the entry's title
        # already shows the date, so a heading would just duplicate the
        # title. The detector's ISO date (if any) overrides the regex
        # result for the same reason as the single-page path.
        det = self._detect_heading(combined_text, date)  # type: ignore[attr-defined]
        if det.date_iso:
            date = det.date_iso
        final_text = det.body if det.has_heading else None

        # Create single entry
        entry = self._repo.create_entry(  # type: ignore[attr-defined]
            date, "photo", combined_text, word_count, user_id=user_id,
            final_text=final_text,
        )

        # Store source files and pages
        for i, (_image_data, _) in enumerate(images):
            source_file_id = self.store_source_file(  # type: ignore[attr-defined]
                entry.id, f"image_{date}_p{i + 1}",
                page_media_types[i], page_hashes[i],
            )
            # Per-page raw_text preserves the verbatim extracted text
            # (after sentinel stripping) — what the model gave us for
            # that image, un-stripped. Useful for per-page review.
            self._repo.add_entry_page(  # type: ignore[attr-defined]
                entry.id, i + 1, page_results[i].text, source_file_id,
            )

        # Record uncertain spans anchored to entries.raw_text.
        self._repo.add_uncertain_spans(entry.id, combined_spans)  # type: ignore[attr-defined]

        # Chunk, embed, and store
        chunk_count = self._process_text(  # type: ignore[attr-defined]
            entry.id, entry.final_text, date,
            skip_mood=skip_mood, user_id=user_id,
        )
        self._repo.update_chunk_count(entry.id, chunk_count)  # type: ignore[attr-defined]

        log.info(
            "Ingested multi-page entry %d: %d pages, %d words, date %s",
            entry.id, len(images), word_count, date,
        )
        return self._repo.get_entry(entry.id)  # type: ignore[attr-defined,return-value]
