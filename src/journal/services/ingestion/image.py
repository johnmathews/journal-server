"""Image (OCR) ingest paths for ``IngestionService``.

Mixin holding the single- and multi-page entry points. Both public methods
delegate to the private ``_ingest_pages`` core, which assigns each page a
``PageRole``, OCRs with its role, combines pages, resolves the content window,
and persists one entry — no multi-entry fan-out.
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
        """OCR a single page image into one entry (see _ingest_pages)."""
        return self._ingest_pages(
            [(image_data, media_type)], date,
            skip_mood=skip_mood, user_id=user_id,
        )

    def ingest_multi_page_entry(
        self,
        images: list[tuple[bytes, str]],
        date: str,
        *,
        skip_mood: bool = False,
        on_progress: Callable[[int, int], None] | None = None,
        user_id: int = 1,
    ) -> Entry:
        """OCR multiple page images into one entry (see _ingest_pages)."""
        return self._ingest_pages(
            images, date, skip_mood=skip_mood,
            on_progress=on_progress, user_id=user_id,
        )

    def _ingest_pages(
        self,
        images: list[tuple[bytes, str]],
        date: str,
        *,
        skip_mood: bool = False,
        on_progress: Callable[[int, int], None] | None = None,
        user_id: int = 1,
    ) -> Entry:
        from journal.services.date_extraction import extract_date_from_text
        from journal.services.ingestion.boundaries import (
            assign_roles,
            extract_content_window,
        )

        if not images:
            raise ValueError("At least one image is required")
        roles = assign_roles(len(images))

        page_results: list[OCRResult] = []
        page_hashes: list[str] = []
        page_media_types: list[str] = []
        for i, (image_data, media_type) in enumerate(images):
            file_hash = hashlib.sha256(image_data).hexdigest()
            if self._is_duplicate(file_hash):  # type: ignore[attr-defined]
                raise ValueError(
                    f"Page {i + 1} has already been uploaded in another entry. "
                    f"Delete the existing entry first if you want to re-upload."
                )
            image_data, media_type = self._maybe_preprocess(  # type: ignore[attr-defined]
                image_data, media_type,
            )
            ocr_result = self._ocr.extract(  # type: ignore[attr-defined]
                image_data, media_type, roles[i],
            )
            if not ocr_result.text.strip():
                raise ValueError(f"OCR extracted no text from page {i + 1}")
            page_results.append(ocr_result)
            page_hashes.append(file_hash)
            page_media_types.append(media_type)
            if on_progress is not None:
                on_progress(i + 1, len(images))

        # Combine pages (single-\n join; see chunking rationale) carrying
        # markers, then resolve the content window.
        combined_with_markers, combined_spans = self._combine_pages(page_results)
        window = extract_content_window(combined_with_markers, combined_spans)
        raw_text = window.text
        content = raw_text[window.start:window.end]
        word_count = len(content.split())

        extracted = extract_date_from_text(content)
        if extracted:
            date = extracted
        det = self._detect_heading(content, date)  # type: ignore[attr-defined]
        if det.date_iso:
            date = det.date_iso
        final_text = det.body if det.has_heading else content

        trimmed = window.start != 0 or window.end != len(raw_text)
        entry = self._repo.create_entry(  # type: ignore[attr-defined]
            date, "photo", raw_text, word_count, user_id=user_id,
            final_text=final_text,
            content_start_char=window.start if trimmed else None,
            content_end_char=window.end if trimmed else None,
        )

        for i, (_image_data, _) in enumerate(images):
            source_file_id = self.store_source_file(  # type: ignore[attr-defined]
                entry.id, f"image_{date}_p{i + 1}",
                page_media_types[i], page_hashes[i],
            )
            self._repo.add_entry_page(  # type: ignore[attr-defined]
                entry.id, i + 1, page_results[i].text, source_file_id,
            )

        self._repo.add_uncertain_spans(entry.id, window.spans)  # type: ignore[attr-defined]
        chunk_count = self._process_text(  # type: ignore[attr-defined]
            entry.id, entry.final_text, date,
            skip_mood=skip_mood, user_id=user_id,
        )
        self._repo.update_chunk_count(entry.id, chunk_count)  # type: ignore[attr-defined]
        log.info(
            "Ingested entry %d: %d page(s), %d words, date %s, window=%s",
            entry.id, len(images), word_count, date,
            (entry.content_start_char, entry.content_end_char),
        )
        return self._repo.get_entry(entry.id)  # type: ignore[attr-defined,return-value]

    def _combine_pages(
        self, page_results: list[OCRResult],
    ) -> tuple[str, list[tuple[int, int]]]:
        """Strip + single-\\n join pages; shift uncertain spans to combined coords."""
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
                cumulative_offset += 1  # the "\n" separator
        return "\n".join(stripped_parts), combined_spans
