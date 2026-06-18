"""Extract dates from OCR text and filenames.

Handwritten journal pages typically begin with a date in formats like:
  - TUES 17 FEB 2026
  - Tuesday 17 February 2026
  - 17 Feb 2026
  - Feb 17, 2026
  - 17/02/2026
  - 2026-02-17

Uploaded filenames may also contain dates:
  - 2026-03-28_at_the_burrow.md
  - 2026-03-28.txt

This module searches for such patterns and returns an ISO 8601 date
string (YYYY-MM-DD) if found.
"""

from __future__ import annotations

import datetime
import logging
import re

log = logging.getLogger(__name__)

# Month name -> number mapping (case-insensitive, abbreviations OK)
_MONTHS: dict[str, int] = {}
for _i, _names in enumerate(
    [
        ("jan", "january"),
        ("feb", "february"),
        ("mar", "march"),
        ("apr", "april"),
        ("may",),
        ("jun", "june"),
        ("jul", "july"),
        ("aug", "august"),
        ("sep", "sept", "september"),
        ("oct", "october"),
        ("nov", "november"),
        ("dec", "december"),
    ],
    start=1,
):
    for _name in _names:
        _MONTHS[_name] = _i

# Weekday name (first three letters) -> Python weekday() index (Mon=0..Sun=6).
_WEEKDAYS: dict[str, int] = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}

# How many years back to search when inferring a missing year. 28 years is one
# full day-of-week cycle, so for any real calendar date a matching weekday is
# guaranteed to exist within the window. We always return the MOST RECENT past
# match, so widening the window never changes a result — it only lets the rare
# 29-February case resolve.
_YEAR_SEARCH_RANGE = 28

# Pattern 1: day-first named month — "17 Feb 2026", "17 February 2026",
# "9 June", "Thursday 18 June", "10 June 23:35". The leading weekday and the
# trailing year are BOTH optional; a year-less date has its year inferred (see
# _infer_missing_year). A trailing time (e.g. "23:35") is left unmatched and
# ignored — only the calendar date is captured.
_PAT_DMY_NAMED = re.compile(
    r"(?:(?P<weekday>mon|tue|wed|thu|fri|sat|sun)\w*[\s,.-]*)?"
    r"(?P<day>\d{1,2})\s+"
    r"(?P<month>[a-z]{3,9})"
    r"(?:\s+(?P<year>\d{4}))?",
    re.IGNORECASE,
)

# Pattern 2: "Feb 17, 2026" or "February 17 2026"
_PAT_MDY_NAMED = re.compile(
    r"(?:(?:mon|tue|wed|thu|fri|sat|sun)\w*[\s,.-]*)?([a-z]{3,9})\s+"
    r"(\d{1,2})[,\s]+(\d{4})",
    re.IGNORECASE,
)

# Pattern 3: ISO-ish "2026-02-17"
_PAT_ISO = re.compile(r"(\d{4})-(\d{2})-(\d{2})")

# Pattern 4: "17/02/2026" or "17.02.2026" (DD/MM/YYYY)
_PAT_DMY_NUMERIC = re.compile(r"(\d{1,2})[/.](\d{1,2})[/.](\d{4})")


def _safe_date(year: int, month: int, day: int) -> str | None:
    """Validate and return ISO date string, or None if invalid."""
    try:
        return datetime.date(year, month, day).isoformat()
    except ValueError:
        return None


def _infer_missing_year(
    month: int,
    day: int,
    weekday_idx: int | None,
    today: datetime.date,
) -> str | None:
    """Infer the year for a year-less ``(month, day)`` and return ISO, or None.

    A journal entry can never be dated in the future, so we walk back from the
    current year and take the most recent occurrence that falls on or before
    ``today``.

    When the heading included a weekday name (``weekday_idx`` is 0=Mon..6=Sun),
    we prefer the most recent past year whose weekday actually matches — this
    disambiguates an old entry whose nearest-past year has the wrong weekday
    (e.g. "Wednesday 18 June" resolves to 2025, not the current-year Thursday).
    If no year within the search window matches the weekday (a misread weekday,
    or an impossible date), we fall back to the plain most-recent-past
    occurrence. Returns None only when no valid past date exists at all
    (e.g. "31 June").
    """
    fallback: datetime.date | None = None
    for year in range(today.year, today.year - _YEAR_SEARCH_RANGE, -1):
        try:
            candidate = datetime.date(year, month, day)
        except ValueError:
            continue  # e.g. 29 Feb in a non-leap year
        if candidate > today:
            continue
        if fallback is None:
            fallback = candidate
        if weekday_idx is None or candidate.weekday() == weekday_idx:
            return candidate.isoformat()
    if weekday_idx is not None and fallback is not None:
        log.info(
            "No past %02d-%02d matched weekday %d within %d years — "
            "using most recent occurrence %s",
            month, day, weekday_idx, _YEAR_SEARCH_RANGE, fallback.isoformat(),
        )
    return fallback.isoformat() if fallback else None


def extract_date_from_text(
    text: str, today: datetime.date | None = None
) -> str | None:
    """Try to extract a date from the first few lines of OCR text.

    Returns an ISO 8601 date string (YYYY-MM-DD) or None if no date
    is found. Only searches the first 500 characters to avoid false
    positives deeper in the text.

    Handwritten headings vary: they may include a weekday and a time, and may
    omit the year ("9 June", "Thursday 18 June 22:55"). A missing year is
    inferred as the most recent occurrence that is not in the future, using
    the weekday to disambiguate when present (see ``_infer_missing_year``). An
    explicitly written year is always trusted as-is, even if it lands in the
    future. ``today`` defaults to the current date and exists for testability.
    """
    if today is None:
        today = datetime.date.today()
    head = text[:500]

    # Try named-month patterns first (most common in handwritten journals)
    m = _PAT_DMY_NAMED.search(head)
    if m:
        day = int(m.group("day"))
        month = _MONTHS.get(m.group("month").lower()[:3])
        if month:
            year_group = m.group("year")
            if year_group is not None:
                # Explicit year: trust it verbatim, even if in the future.
                result = _safe_date(int(year_group), month, day)
                if result:
                    log.info("Extracted date %s from OCR text (DMY named)", result)
                    return result
            else:
                weekday_group = m.group("weekday")
                weekday_idx = (
                    _WEEKDAYS.get(weekday_group.lower()[:3])
                    if weekday_group
                    else None
                )
                result = _infer_missing_year(month, day, weekday_idx, today)
                if result:
                    log.info(
                        "Inferred date %s from OCR text (DMY named, year-less)",
                        result,
                    )
                    return result

    m = _PAT_MDY_NAMED.search(head)
    if m:
        month_str, day, year = m.group(1).lower(), int(m.group(2)), int(m.group(3))
        month = _MONTHS.get(month_str[:3])
        if month:
            result = _safe_date(year, month, day)
            if result:
                log.info("Extracted date %s from OCR text (MDY named)", result)
                return result

    m = _PAT_ISO.search(head)
    if m:
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        result = _safe_date(year, month, day)
        if result:
            log.info("Extracted date %s from OCR text (ISO)", result)
            return result

    m = _PAT_DMY_NUMERIC.search(head)
    if m:
        day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        result = _safe_date(year, month, day)
        if result:
            log.info("Extracted date %s from OCR text (DMY numeric)", result)
            return result

    return None


# Filename patterns — match YYYY-MM-DD or YYYY_MM_DD at the start of the
# basename (before extension), with any separator (-, _, .).
_PAT_FILENAME_ISO = re.compile(r"(\d{4})[-_.](\d{2})[-_.](\d{2})")

# Named-month filename patterns like "28-March-2026" or "28_march_2026"
_PAT_FILENAME_DMY = re.compile(
    r"(\d{1,2})[-_.\s]([a-z]{3,9})[-_.\s](\d{4})", re.IGNORECASE
)
_PAT_FILENAME_MDY = re.compile(
    r"([a-z]{3,9})[-_.\s](\d{1,2})[-_.\s](\d{4})", re.IGNORECASE
)


def extract_date_from_filename(filename: str) -> str | None:
    """Try to extract a date from a filename (with or without extension).

    Strips the directory path and extension, then looks for date patterns.
    Returns an ISO 8601 date string (YYYY-MM-DD) or None.
    """
    import os

    stem = os.path.splitext(os.path.basename(filename))[0]

    # Try ISO-style first (most common for programmatic filenames)
    m = _PAT_FILENAME_ISO.search(stem)
    if m:
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        result = _safe_date(year, month, day)
        if result:
            log.info("Extracted date %s from filename '%s' (ISO)", result, filename)
            return result

    # Try DMY named: "28-March-2026" or "28_mar_2026"
    m = _PAT_FILENAME_DMY.search(stem)
    if m:
        day, month_str, year = int(m.group(1)), m.group(2).lower(), int(m.group(3))
        month = _MONTHS.get(month_str[:3])
        if month:
            result = _safe_date(year, month, day)
            if result:
                log.info("Extracted date %s from filename '%s' (DMY named)", result, filename)
                return result

    # Try MDY named: "March-28-2026" or "mar_28_2026"
    m = _PAT_FILENAME_MDY.search(stem)
    if m:
        month_str, day, year = m.group(1).lower(), int(m.group(2)), int(m.group(3))
        month = _MONTHS.get(month_str[:3])
        if month:
            result = _safe_date(year, month, day)
            if result:
                log.info("Extracted date %s from filename '%s' (MDY named)", result, filename)
                return result

    return None
