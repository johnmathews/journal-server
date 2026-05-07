"""Ingestion tools — create / update entries from media, URLs, or text."""

import logging

from mcp.server.fastmcp import Context

from journal.mcp_server.app import mcp
from journal.mcp_server.tools._ctx import _get_ingestion, _user_id

log = logging.getLogger(__name__)


@mcp.tool()
def journal_ingest_media_from_url(
    source_type: str,
    url: str,
    media_type: str | None = None,
    date: str | None = None,
    language: str = "en",
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Ingest a SINGLE journal page image or voice note by URL.

    Use this for media files (images for OCR, audio for transcription).
    For plain text entries, use `journal_ingest_text` instead.

    Preferred over journal_ingest_media when the file is available at a URL,
    since it avoids base64-encoding large files as tool parameters.

    IMPORTANT: If you have multiple photos that are pages of the *same* journal
    entry (e.g. a two-page handwritten entry split across two images), do NOT
    call this tool once per image — that creates a separate entry per photo.
    Use `journal_ingest_multi_page_from_url` instead so all pages are combined
    into one entry.

    Args:
        source_type: Either "image" (for handwritten page OCR) or "voice" (for audio).
        url: URL to download the file from (must be accessible from the server).
        media_type: MIME type override. If omitted, inferred from the response header.
        date: Date of the journal entry (ISO 8601, e.g. "2026-03-22"). Defaults to today.
        language: Language code for voice transcription (default "en"). Ignored for images.
    """
    from datetime import date as date_type

    log.info(
        "Tool call: journal_ingest_media_from_url(source_type=%s, url=%s, date=%s)",
        source_type, url, date,
    )
    service = _get_ingestion(ctx)
    user_id = _user_id(ctx)
    entry_date = date or date_type.today().isoformat()

    if source_type == "image":
        entry = service.ingest_image_from_url(url, entry_date, media_type, user_id=user_id)
    elif source_type == "voice":
        entry = service.ingest_voice_from_url(
            url, entry_date, media_type, language, user_id=user_id,
        )
    else:
        return f"Invalid source_type '{source_type}'. Must be 'image' or 'voice'."

    return (
        f"Entry ingested successfully.\n"
        f"  ID: {entry.id}\n"
        f"  Date: {entry.entry_date}\n"
        f"  Source: {entry.source_type}\n"
        f"  Words: {entry.word_count}\n"
        f"  Chunks: {entry.chunk_count}\n"
        f"  Preview: {entry.final_text[:200]}..."
    )


@mcp.tool()
def journal_ingest_media(
    source_type: str,
    data_base64: str,
    media_type: str,
    date: str | None = None,
    language: str = "en",
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Ingest a journal entry from a base64-encoded image or voice note.

    Use this for media files (images for OCR, audio for transcription).
    For plain text entries, use `journal_ingest_text` instead.
    When the file is available at a URL, prefer `journal_ingest_media_from_url`.

    Args:
        source_type: Either "image" (for handwritten page OCR) or "voice" (for audio transcription).
        data_base64: Base64-encoded file data.
        media_type: MIME type (e.g. "image/jpeg", "image/png", "audio/mp3", "audio/m4a").
        date: Date of the journal entry (ISO 8601, e.g. "2026-03-22"). Defaults to today.
        language: Language code for voice transcription (default "en"). Ignored for images.
    """
    import base64
    from datetime import date as date_type

    log.info(
        "Tool call: journal_ingest_media(source_type=%s, media_type=%s, date=%s, size=%d)",
        source_type, media_type, date, len(data_base64),
    )
    service = _get_ingestion(ctx)
    user_id = _user_id(ctx)
    data = base64.b64decode(data_base64)
    entry_date = date or date_type.today().isoformat()

    if source_type == "image":
        entry = service.ingest_image(data, media_type, entry_date, user_id=user_id)
    elif source_type == "voice":
        entry = service.ingest_voice(data, media_type, entry_date, language, user_id=user_id)
    else:
        return f"Invalid source_type '{source_type}'. Must be 'image' or 'voice'."

    return (
        f"Entry ingested successfully.\n"
        f"  ID: {entry.id}\n"
        f"  Date: {entry.entry_date}\n"
        f"  Source: {entry.source_type}\n"
        f"  Words: {entry.word_count}\n"
        f"  Chunks: {entry.chunk_count}\n"
        f"  Preview: {entry.final_text[:200]}..."
    )


@mcp.tool()
def journal_ingest_text(
    text: str,
    date: str | None = None,
    source_type: str = "text_entry",
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Create a journal entry from plain text.

    Use this when you already have the text content (typed, dictated, or
    pasted). No OCR or transcription is performed — the text is stored
    directly, chunked, embedded, and indexed.

    For handwritten page images use `journal_ingest_media` or
    `journal_ingest_media_from_url`. For audio recordings use those same
    tools with source_type="voice".

    Args:
        text: The journal entry text content.
        date: Date of the journal entry (ISO 8601, e.g. "2026-03-22").
            Defaults to today.
        source_type: Entry source type. Defaults to "text_entry".
    """
    from datetime import date as date_type

    log.info(
        "Tool call: journal_ingest_text(date=%s, source_type=%s, chars=%d)",
        date, source_type, len(text),
    )
    service = _get_ingestion(ctx)
    user_id = _user_id(ctx)
    entry_date = date or date_type.today().isoformat()

    try:
        entry = service.ingest_text(
            text, entry_date, source_type, skip_mood=True, user_id=user_id,
        )
    except ValueError as e:
        return f"Error: {e}"

    return (
        f"Text entry created successfully.\n"
        f"  ID: {entry.id}\n"
        f"  Date: {entry.entry_date}\n"
        f"  Source: {entry.source_type}\n"
        f"  Words: {entry.word_count}\n"
        f"  Chunks: {entry.chunk_count}\n"
        f"  Preview: {entry.final_text[:200]}..."
    )


@mcp.tool()
def journal_ingest_multi_page(
    images_base64: list[str],
    media_types: list[str],
    date: str | None = None,
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Ingest multiple page images as a single journal entry.

    Args:
        images_base64: List of base64-encoded image data, one per page in order.
        media_types: List of MIME types, one per image (e.g. ["image/jpeg", "image/jpeg"]).
        date: Date of the journal entry (ISO 8601). Defaults to today.
    """
    import base64
    from datetime import date as date_type

    log.info(
        "Tool call: journal_ingest_multi_page(pages=%d, date=%s)",
        len(images_base64), date,
    )
    service = _get_ingestion(ctx)
    user_id = _user_id(ctx)
    entry_date = date or date_type.today().isoformat()

    if len(images_base64) != len(media_types):
        return "Error: images_base64 and media_types must have the same length."

    images = [
        (base64.b64decode(img), mt)
        for img, mt in zip(images_base64, media_types, strict=True)
    ]

    entry = service.ingest_multi_page_entry(images, entry_date, user_id=user_id)

    return (
        f"Multi-page entry ingested successfully.\n"
        f"  ID: {entry.id}\n"
        f"  Date: {entry.entry_date}\n"
        f"  Pages: {len(images)}\n"
        f"  Words: {entry.word_count}\n"
        f"  Chunks: {entry.chunk_count}\n"
        f"  Preview: {entry.final_text[:200]}..."
    )


@mcp.tool()
def journal_ingest_multi_page_from_url(
    urls: list[str],
    media_types: list[str] | None = None,
    date: str | None = None,
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Ingest multiple page images (by URL) as a single multi-page journal entry.

    Use this tool when a single journal entry spans multiple photos — for
    example, a two-page handwritten entry where each page was photographed
    separately. All images are downloaded, OCR'd page-by-page, and combined
    into ONE entry with one page record per image. This is the preferred
    way to ingest multi-page entries from MCP clients like Slack-driven
    agents, since it avoids base64-encoding large files.

    Slack file URLs (files.slack.com) are automatically authenticated via
    the server's SLACK_BOT_TOKEN.

    Args:
        urls: Ordered list of page image URLs, one per page.
        media_types: Optional per-URL MIME type overrides. If provided,
            must be the same length as `urls`. Omit entirely to infer
            each page's MIME type from the response Content-Type header
            (usually correct for Slack and most CDNs).
        date: Date of the journal entry (ISO 8601, e.g. "2026-03-22").
            Defaults to today.
    """
    from datetime import date as date_type

    log.info(
        "Tool call: journal_ingest_multi_page_from_url(pages=%d, date=%s)",
        len(urls), date,
    )
    service = _get_ingestion(ctx)
    user_id = _user_id(ctx)
    entry_date = date or date_type.today().isoformat()

    if media_types is not None and len(media_types) != len(urls):
        return "Error: media_types and urls must have the same length when media_types is provided."

    # Service layer accepts list[str | None] | None; list[str] is a valid
    # instance of that type, so no conversion is needed.
    try:
        entry = service.ingest_multi_page_entry_from_urls(
            urls, entry_date, media_types, user_id=user_id,  # type: ignore[arg-type]
        )
    except ValueError as e:
        return f"Error: {e}"

    return (
        f"Multi-page entry ingested successfully.\n"
        f"  ID: {entry.id}\n"
        f"  Date: {entry.entry_date}\n"
        f"  Pages: {len(urls)}\n"
        f"  Words: {entry.word_count}\n"
        f"  Chunks: {entry.chunk_count}\n"
        f"  Preview: {entry.final_text[:200]}..."
    )


@mcp.tool()
def journal_update_entry_text(
    entry_id: int,
    final_text: str,
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Update the corrected text of a journal entry, triggering re-embedding.

    Use this to fix OCR errors. The original raw text is preserved; only the
    corrected version (final_text) is updated. All downstream features (search,
    analytics) will use the corrected text.

    Args:
        entry_id: The ID of the entry to update.
        final_text: The corrected text content.
    """
    log.info("Tool call: journal_update_entry_text(entry_id=%d)", entry_id)
    service = _get_ingestion(ctx)
    user_id = _user_id(ctx)
    try:
        entry = service.update_entry_text(entry_id, final_text, user_id=user_id)
    except ValueError as e:
        return f"Error: {e}"

    return (
        f"Entry {entry.id} updated successfully.\n"
        f"  Words: {entry.word_count}\n"
        f"  Chunks: {entry.chunk_count}\n"
        f"  Preview: {entry.final_text[:200]}..."
    )
