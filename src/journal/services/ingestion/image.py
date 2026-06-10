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


ENTRY_DELIMITER = "<<<NEW ENTRY>>>"
"""Marker the OCR vision model emits between consecutive journal entries
on a single photographed page. See ``SYSTEM_PROMPT`` in
``journal.providers.ocr`` for the exact instruction sent to the model."""


def split_text_into_entries(
    text: str,
    spans: list[tuple[int, int]],
) -> list[tuple[str, list[tuple[int, int]]]]:
    """Split OCR text on ``ENTRY_DELIMITER``; discard the orphan tail.

    A single photographed page may contain the start of more than one
    journal entry — most commonly, the last few lines of yesterday's
    entry sitting above a fresh date heading. The OCR prompt asks the
    vision model to emit ``ENTRY_DELIMITER`` on its own line between
    each pair of consecutive entries. This helper splits on the marker
    and discards the text BEFORE the first marker (the orphan tail) per
    the project's "discard orphan tail" policy.

    Behaviour:

    - **No delimiter present:** the page is a single entry; return
      ``[(text, spans)]`` unchanged.
    - **Delimiter present with at least one non-empty segment after it:**
      drop the orphan tail (segment before the first delimiter); return
      one ``(segment_text, segment_spans)`` tuple per remaining non-empty
      segment. Each segment is stripped of leading/trailing whitespace
      and the marker itself is removed. Spans are re-anchored into
      segment-local coordinates; spans that lay inside the orphan tail
      or that straddled a segment boundary are dropped.
    - **Trailing delimiter with empty segment after it:** the model
      marked something but the only real content is BEFORE the marker.
      Discarding it would lose everything, so the helper falls back to
      ``[(text_with_delimiter_stripped, spans)]`` — single entry. Spans
      that overlap the stripped delimiter are dropped; surviving spans
      are shifted to account for the removed marker bytes.
    """
    if ENTRY_DELIMITER not in text:
        return [(text, list(spans))]

    # Walk the text and record (segment_start, segment_end) byte ranges
    # for each piece between delimiter occurrences. Segment_end is the
    # char index of the next delimiter (exclusive); segment_start of the
    # piece after the delimiter is delim_end (inclusive).
    raw_segments: list[tuple[int, int]] = []
    cursor = 0
    delim_len = len(ENTRY_DELIMITER)
    while True:
        idx = text.find(ENTRY_DELIMITER, cursor)
        if idx == -1:
            raw_segments.append((cursor, len(text)))
            break
        raw_segments.append((cursor, idx))
        cursor = idx + delim_len

    # Helper to materialise a segment with span re-anchoring.
    def _materialise(
        seg_start: int, seg_end: int,
    ) -> tuple[str, list[tuple[int, int]]] | None:
        seg_text = text[seg_start:seg_end]
        lstripped = seg_text.lstrip()
        leading = len(seg_text) - len(lstripped)
        stripped = lstripped.rstrip()
        if not stripped:
            return None
        offset = seg_start + leading
        seg_len = len(stripped)
        seg_spans: list[tuple[int, int]] = []
        for s, e in spans:
            ns = s - offset
            ne = e - offset
            if ns >= 0 and ne <= seg_len and ne > ns:
                seg_spans.append((ns, ne))
        return stripped, seg_spans

    # Skip the first segment (orphan tail). Materialise the rest.
    entries: list[tuple[str, list[tuple[int, int]]]] = []
    for seg_start, seg_end in raw_segments[1:]:
        materialised = _materialise(seg_start, seg_end)
        if materialised is not None:
            entries.append(materialised)

    if entries:
        return entries

    # Fallback: the page had a delimiter (probably trailing) but no
    # non-empty segment after it. Don't drop content — return the text
    # before the first delimiter as a single entry, with the marker
    # stripped. Shift spans to account for any removed delimiter bytes
    # that preceded them.
    first_delim_end = raw_segments[0][1]
    body_text = text[:first_delim_end].rstrip()
    body_spans: list[tuple[int, int]] = []
    for s, e in spans:
        if e <= len(body_text):
            body_spans.append((s, e))
    return [(body_text, body_spans)]


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
        """Ingest a journal page image: OCR -> chunk -> embed -> store.

        If the OCR output contains the ``ENTRY_DELIMITER`` marker (i.e.
        the page contains more than one journal entry, e.g. the tail of
        a previous entry above a fresh dated entry), the page is split
        into segments. The orphan tail above the first delimiter is
        discarded; each remaining segment becomes its own entry. The
        most recently dated entry — typically the new one the user just
        photographed — is returned. Callers that need every entry
        created from the page (e.g. the image-ingestion job worker,
        which queues follow-up jobs per entry) should use
        :meth:`ingest_image_entries` instead.
        """
        return self.ingest_image_entries(
            image_data, media_type, date,
            skip_mood=skip_mood, user_id=user_id,
        )[-1]

    def ingest_image_entries(
        self,
        image_data: bytes,
        media_type: str,
        date: str,
        *,
        skip_mood: bool = False,
        user_id: int = 1,
    ) -> list[Entry]:
        """Same as :meth:`ingest_image` but returns ALL created entries.

        The list is in segment order (top of the page first), so the
        last element is the most recently dated entry — the one
        :meth:`ingest_image` returns. The common single-entry page
        yields a one-element list.
        """
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
        # ocr_result.text coordinates.
        ocr_result = self._ocr.extract(image_data, media_type)  # type: ignore[attr-defined]
        if not ocr_result.text.strip():
            raise ValueError("OCR extracted no text from image")

        # Split into segments if the model marked any entry boundaries.
        # The common case (single-entry page) returns one segment with
        # text and spans unchanged.
        segments = split_text_into_entries(
            ocr_result.text, ocr_result.uncertain_spans,
        )
        if len(segments) > 1:
            log.info(
                "OCR output contained %d entry boundaries; "
                "splitting page into %d entries (orphan tail discarded)",
                len(segments),
                len(segments),
            )

        created_entries: list[Entry] = []
        for seg_idx, (segment_text, segment_spans) in enumerate(segments, 1):
            entry = self._create_entry_from_image_segment(
                segment_text=segment_text,
                segment_spans=segment_spans,
                fallback_date=date,
                media_type=media_type,
                file_hash=file_hash,
                user_id=user_id,
                skip_mood=skip_mood,
                segment_index=seg_idx,
            )
            created_entries.append(entry)

        return created_entries

    def _create_entry_from_image_segment(
        self,
        *,
        segment_text: str,
        segment_spans: list[tuple[int, int]],
        fallback_date: str,
        media_type: str,
        file_hash: str,
        user_id: int,
        skip_mood: bool,
        segment_index: int,
    ) -> Entry:
        """Persist one segment of a (possibly multi-entry) image upload.

        Runs date extraction and heading detection on the segment text
        in isolation, so a page containing two dated entries lands each
        under its own date rather than collapsing to a single date for
        the whole page.
        """
        # Try to extract date from the segment text if caller used a default.
        from journal.services.date_extraction import extract_date_from_text

        date = fallback_date
        extracted = extract_date_from_text(segment_text)
        if extracted:
            date = extracted

        # Optional date-heading detection per segment. See the original
        # comment block below for why we don't strip the date from the
        # body: keeps the entry reading as the user wrote it; raw_text
        # stays verbatim so the OCR overlay / audit trail still points
        # at exactly what the model returned.
        det = self._detect_heading(segment_text, date)  # type: ignore[attr-defined]
        if det.date_iso:
            date = det.date_iso
        final_text = det.body if det.has_heading else None

        word_count = len(segment_text.split())
        entry = self._repo.create_entry(  # type: ignore[attr-defined]
            date, "photo", segment_text, word_count, user_id=user_id,
            final_text=final_text,
        )
        # Each entry gets its own source_files row referencing the same
        # image hash. source_files.file_hash is indexed but not unique,
        # so the same image can be the source for N entries — the
        # upload-time dedup check at the top of ingest_image is the only
        # guard against re-uploads.
        source_file_id = self.store_source_file(  # type: ignore[attr-defined]
            entry.id, f"image_{date}_{segment_index}", media_type, file_hash,
        )

        self._repo.add_entry_page(  # type: ignore[attr-defined]
            entry.id, 1, segment_text, source_file_id,
        )
        self._repo.add_uncertain_spans(entry.id, segment_spans)  # type: ignore[attr-defined]

        chunk_count = self._process_text(  # type: ignore[attr-defined]
            entry.id, entry.final_text, date,
            skip_mood=skip_mood, user_id=user_id,
        )
        self._repo.update_chunk_count(entry.id, chunk_count)  # type: ignore[attr-defined]

        log.info(
            "Ingested image entry %d: %d words, date %s (segment %d)",
            entry.id, word_count, date, segment_index,
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

        # Optional date-heading detection — same contract as single-page
        # OCR. The detected date drives the entry's filing date but is
        # NOT removed from the body; the body keeps the date phrase as
        # the user wrote it. The detector's ISO date (if any) overrides
        # the regex result for the same reason as the single-page path.
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
