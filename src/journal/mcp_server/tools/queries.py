"""Read-only query tools (search, list, statistics, trends)."""

import logging

from mcp.server.fastmcp import Context

from journal.mcp_server.app import mcp
from journal.mcp_server.tools._ctx import _get_query, _user_id

log = logging.getLogger(__name__)

# Fallback range for the ASCII trend bar when a dimension's scale is not
# in the loaded config (e.g. a retired dimension still present in
# historical mood_scores). Bipolar [-1, +1] preserves the pre-fix
# rendering for those rows.
_DEFAULT_SCALE: tuple[float, float] = (-1.0, 1.0)
_BAR_WIDTH = 10


def _mood_bar(avg_score: float, score_min: float, score_max: float) -> str:
    """Render a fixed-width ASCII bar for a mood score, scaled to the
    dimension's own range so unipolar and bipolar dimensions read
    correctly.

    The score is mapped from ``[score_min, score_max]`` onto
    ``0.._BAR_WIDTH`` ``+`` characters:

    - bipolar ([-1, +1]): a neutral 0.0 lands at half width (5 of 10),
      matching the intuition that 0 is the midpoint.
    - unipolar ([0, +1]): 0.0 renders an empty bar (absence of the
      pole) and +1.0 a full one — so a unipolar 0 is visibly distinct
      from a bipolar neutral instead of both showing a half-bar.
    """
    span = score_max - score_min
    if span <= 0:
        return ""
    fraction = (avg_score - score_min) / span
    fraction = max(0.0, min(1.0, fraction))
    return "+" * round(fraction * _BAR_WIDTH)


@mcp.tool()
def journal_search_entries(
    query: str,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 10,
    offset: int = 0,
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Search journal entries by content.

    Combines keyword (BM25) and semantic (embedding) retrieval, then
    reranks the merged candidates. Use this for any question about
    what is *in* the journal — proper nouns, exact phrases, paraphrased
    themes, or open-ended questions all work. The query can be a
    quote ("the meeting in Vienna"), a name or term ("Atlas",
    "deadlift PR"), or a natural-language description of what you're
    looking for ("times I felt anxious about work").

    For browsing by date instead of content, use
    journal_get_entries_by_date or journal_list_entries.

    Args:
        query: What to search for. Free-form text — keywords, phrases,
            or natural language. Required, non-empty.
        start_date: ISO 8601 date (e.g. "2026-01-01"). Only return
            entries on or after this date. Optional.
        end_date: ISO 8601 date. Only return entries on or before this
            date. Optional.
        limit: Max entries to return (1-50, default 10).
        offset: Pagination offset. Combine with limit to page through
            results.
    """
    log.info(
        "Tool call: journal_search_entries(query=%r, start_date=%s, end_date=%s)",
        query, start_date, end_date,
    )
    service = _get_query(ctx)
    user_id = _user_id(ctx)
    results = service.search_entries(
        query, start_date, end_date, min(limit, 50), offset, user_id=user_id,
    )

    if not results:
        return f"No journal entries found matching '{query}'."

    lines = [f"Found {len(results)} entries matching '{query}':\n"]
    for r in results:
        lines.append(
            f"--- {r.entry_date} (top relevance: {r.score:.0%}, "
            f"{len(r.matching_chunks)} matching chunk"
            f"{'s' if len(r.matching_chunks) != 1 else ''}) ---"
        )
        # Show every matching chunk (not just the top one) so LLM consumers
        # see all passages in this entry that were relevant to the query.
        for i, cm in enumerate(r.matching_chunks, start=1):
            snippet = cm.text[:200]
            ellipsis = "..." if len(cm.text) > 200 else ""
            lines.append(f"  match {i} ({cm.score:.0%}): {snippet}{ellipsis}")
        # Then the full parent entry for context.
        lines.append("")
        lines.append(r.text[:500])
        if len(r.text) > 500:
            lines.append(f"... ({len(r.text)} chars total)")
        lines.append("")
    return "\n".join(lines)


@mcp.tool()
def journal_get_entries_by_date(
    date: str,
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Get all journal entries for a specific date.

    Args:
        date: Date in ISO 8601 format (e.g. "2026-03-22").
    """
    log.info("Tool call: journal_get_entries_by_date(date=%s)", date)
    service = _get_query(ctx)
    user_id = _user_id(ctx)
    entries = service.get_entries_by_date(date, user_id=user_id)

    if not entries:
        return f"No journal entries found for {date}."

    lines = [f"{len(entries)} entries for {date}:\n"]
    for e in entries:
        lines.append(f"--- Entry {e.id} ({e.source_type}, {e.word_count} words) ---")
        lines.append(e.final_text)
        lines.append("")
    return "\n".join(lines)


@mcp.tool()
def journal_list_entries(
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 20,
    offset: int = 0,
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """List journal entries in reverse chronological order.

    Args:
        start_date: Filter from this date (ISO 8601). Optional.
        end_date: Filter until this date (ISO 8601). Optional.
        limit: Max results (1-50, default 20).
        offset: Pagination offset.
    """
    log.info(
        "Tool call: journal_list_entries(start_date=%s, end_date=%s, limit=%d)",
        start_date, end_date, limit,
    )
    service = _get_query(ctx)
    user_id = _user_id(ctx)
    entries = service.list_entries(start_date, end_date, min(limit, 50), offset, user_id=user_id)

    if not entries:
        return "No journal entries found."

    lines = [f"Showing {len(entries)} entries:\n"]
    for e in entries:
        preview = e.final_text[:100].replace("\n", " ")
        lines.append(f"- {e.entry_date} | {e.source_type} | {e.word_count} words | {preview}...")
    return "\n".join(lines)


@mcp.tool()
def journal_get_statistics(
    start_date: str | None = None,
    end_date: str | None = None,
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Get journal statistics: entry count, frequency, average length, date range.

    Args:
        start_date: Start of period (ISO 8601). Defaults to all time.
        end_date: End of period (ISO 8601). Defaults to today.
    """
    log.info("Tool call: journal_get_statistics(start_date=%s, end_date=%s)", start_date, end_date)
    service = _get_query(ctx)
    user_id = _user_id(ctx)
    stats = service.get_statistics(start_date, end_date, user_id=user_id)

    lines = ["Journal Statistics:"]
    lines.append(f"  Total entries: {stats.total_entries}")
    start = stats.date_range_start or "N/A"
    end = stats.date_range_end or "N/A"
    lines.append(f"  Date range: {start} to {end}")
    lines.append(f"  Total words: {stats.total_words:,}")
    lines.append(f"  Average words per entry: {stats.avg_words_per_entry:.0f}")
    lines.append(f"  Entries per month: {stats.entries_per_month:.1f}")
    return "\n".join(lines)


@mcp.tool()
def journal_get_mood_trends(
    start_date: str | None = None,
    end_date: str | None = None,
    granularity: str = "week",
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Analyze mood trends over time from journal entries.

    Args:
        start_date: Start of period (ISO 8601). Optional; when omitted,
            trends cover all entries from the start of the journal (no
            default window is applied).
        end_date: End of period (ISO 8601). Optional; when omitted,
            trends run through the most recent entry.
        granularity: Time grouping - "day", "week", or "month" (default "week").
    """
    log.info(
        "Tool call: journal_get_mood_trends(start_date=%s, end_date=%s, granularity=%s)",
        start_date, end_date, granularity,
    )
    service = _get_query(ctx)
    user_id = _user_id(ctx)
    trends = service.get_mood_trends(start_date, end_date, granularity, user_id=user_id)

    if not trends:
        return "No mood data available for the specified period."

    # Look up each dimension's scale so the ASCII bar is scaled to its
    # own range (unipolar [0, 1] vs bipolar [-1, +1]) rather than
    # assuming every dimension is bipolar.
    dimensions = ctx.request_context.lifespan_context.get("mood_dimensions", ())
    scale_by_name = {d.name: (d.score_min, d.score_max) for d in dimensions}

    lines = [f"Mood trends by {granularity}:\n"]
    for t in trends:
        score_min, score_max = scale_by_name.get(t.dimension, _DEFAULT_SCALE)
        bar = _mood_bar(t.avg_score, score_min, score_max)
        line = f"  {t.period} | {t.dimension}: {t.avg_score:+.2f} ({t.entry_count} entries) {bar}"
        lines.append(line)
    return "\n".join(lines)


@mcp.tool()
def journal_get_topic_frequency(
    topic: str,
    start_date: str | None = None,
    end_date: str | None = None,
    ctx: Context = None,  # type: ignore[assignment]
) -> str:
    """Count how often a topic, person, or place appears in journal entries.

    Args:
        topic: Topic, person, place, or theme to search for (e.g. "Vienna", "Atlas", "work").
        start_date: Start of period (ISO 8601). Optional.
        end_date: End of period (ISO 8601). Optional.
    """
    log.info(
        "Tool call: journal_get_topic_frequency(topic=%r, start_date=%s, end_date=%s)",
        topic, start_date, end_date,
    )
    service = _get_query(ctx)
    user_id = _user_id(ctx)
    freq = service.get_topic_frequency(topic, start_date, end_date, user_id=user_id)

    if freq.count == 0:
        return f"'{topic}' was not found in any journal entries."

    lines = [f"'{topic}' appears in {freq.count} entries:"]
    for e in freq.entries[:10]:
        preview = e.final_text[:80].replace("\n", " ")
        lines.append(f"  - {e.entry_date}: {preview}...")
    if freq.count > 10:
        lines.append(f"  ... and {freq.count - 10} more entries")
    return "\n".join(lines)
