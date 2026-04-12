"""Extract dates from OCR text.

Handwritten journal pages typically begin with a date in formats like:
  - TUES 17 FEB 2026
  - Tuesday 17 February 2026
  - 17 Feb 2026
  - Feb 17, 2026
  - 17/02/2026
  - 2026-02-17

This module searches the first few lines of OCR text for such patterns
and returns an ISO 8601 date string (YYYY-MM-DD) if found.
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

# Pattern 1: "17 Feb 2026" or "17 February 2026" (with optional leading day name)
_PAT_DMY_NAMED = re.compile(
    r"(?:(?:mon|tue|wed|thu|fri|sat|sun)\w*[\s,.-]*)?(\d{1,2})\s+"
    r"([a-z]{3,9})\s+(\d{4})",
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


def extract_date_from_text(text: str) -> str | None:
    """Try to extract a date from the first few lines of OCR text.

    Returns an ISO 8601 date string (YYYY-MM-DD) or None if no date
    is found. Only searches the first 500 characters to avoid false
    positives deeper in the text.
    """
    head = text[:500]

    # Try named-month patterns first (most common in handwritten journals)
    m = _PAT_DMY_NAMED.search(head)
    if m:
        day, month_str, year = int(m.group(1)), m.group(2).lower(), int(m.group(3))
        month = _MONTHS.get(month_str[:3])
        if month:
            result = _safe_date(year, month, day)
            if result:
                log.info("Extracted date %s from OCR text (DMY named)", result)
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
