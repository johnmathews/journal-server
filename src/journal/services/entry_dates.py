"""Entry-date bounds and year-off auto-repair.

Handwritten headings sometimes carry the previous year ("Thursday 9 July
2025" written in July 2026 — the entries 112/116 incidents). The weekday
word is a reliable cross-check: when the weekday contradicts the date,
exactly one nearby year usually makes it consistent. Spec:
docs/superpowers/specs/2026-07-13-entry-date-integrity-design.md.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Literal

_WEEKDAYS = [
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
]
_WEEKDAY_RE = re.compile(r"\b(" + "|".join(_WEEKDAYS) + r")\b", re.IGNORECASE)
# A weekday must appear near the top of the page to count as a heading token.
_HEAD_WINDOW = 200


class EntryDateError(ValueError):
    """An entry date outside the allowed [MIN_ENTRY_DATE, today+1] range."""


def _bounds(min_date: str, today: dt.date | None) -> tuple[dt.date, dt.date]:
    effective_today = today if today is not None else dt.date.today()
    return dt.date.fromisoformat(min_date), effective_today + dt.timedelta(days=1)


def validate_entry_date(
    date_iso: str, *, min_date: str, today: dt.date | None = None
) -> None:
    """Raise :class:`EntryDateError` unless ``date_iso`` is a valid ISO date
    inside ``[min_date, today + 1 day]``.
    """
    try:
        candidate = dt.date.fromisoformat(date_iso)
    except (TypeError, ValueError) as exc:
        raise EntryDateError(
            f"'{date_iso}' is not a valid ISO 8601 date (YYYY-MM-DD)"
        ) from exc
    lower, upper = _bounds(min_date, today)
    if not (lower <= candidate <= upper):
        raise EntryDateError(
            f"entry date {date_iso} is outside the allowed range"
            f" {lower.isoformat()} – {upper.isoformat()}"
        )


def find_weekday_token(text: str) -> tuple[str, tuple[int, int]] | None:
    """Return ``(weekday_lowercase, (start, end))`` for the first weekday
    word in the head of ``text``, or ``None``. The span is in the original
    text's coordinates, suitable for an uncertain-span audit marker.
    """
    match = _WEEKDAY_RE.search(text[:_HEAD_WINDOW])
    if match is None:
        return None
    return match.group(1).lower(), match.span()


@dataclass(frozen=True)
class DateRepairResult:
    """Outcome of :func:`repair_entry_date`.

    ``ok``           — date in range, weekday (if any) consistent.
    ``repaired``     — exactly one candidate year fixed a contradiction.
    ``doubtful``     — in-range date contradicts the weekday but no unique
                       repair exists; keep the date, flag for review.
    ``unrepairable`` — out-of-range date with no unique repair; the entry
                       must be quarantined.
    """

    status: Literal["ok", "repaired", "doubtful", "unrepairable"]
    date_iso: str
    original: str
    note: str | None = None


def repair_entry_date(
    date_iso: str,
    weekday: str | None,
    *,
    min_date: str,
    today: dt.date | None = None,
) -> DateRepairResult:
    """Cross-check ``date_iso`` against a heading ``weekday`` and the
    allowed range, repairing a year-off date when exactly one candidate
    year makes both constraints hold.
    """
    lower, upper = _bounds(min_date, today)
    try:
        candidate = dt.date.fromisoformat(date_iso)
    except (TypeError, ValueError):
        return DateRepairResult("unrepairable", date_iso, date_iso)
    in_range = lower <= candidate <= upper

    if weekday is None:
        if in_range:
            return DateRepairResult("ok", date_iso, date_iso)
        return DateRepairResult("unrepairable", date_iso, date_iso)

    target = _WEEKDAYS.index(weekday.lower())
    if in_range and candidate.weekday() == target:
        return DateRepairResult("ok", date_iso, date_iso)

    matches: list[dt.date] = []
    for year in range(lower.year, upper.year + 1):
        try:
            shifted = candidate.replace(year=year)
        except ValueError:  # 29 Feb in a non-leap candidate year
            continue
        if lower <= shifted <= upper and shifted.weekday() == target:
            matches.append(shifted)

    if len(matches) == 1:
        repaired = matches[0].isoformat()
        return DateRepairResult(
            "repaired",
            repaired,
            date_iso,
            note=f"date auto-corrected from {date_iso}",
        )
    if in_range:
        return DateRepairResult("doubtful", date_iso, date_iso)
    return DateRepairResult("unrepairable", date_iso, date_iso)
