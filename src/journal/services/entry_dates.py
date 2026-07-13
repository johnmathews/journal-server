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
    """Return ``(weekday_lowercase, (start, end))`` for a weekday word in
    the entry's *heading line*, or ``None``. The span is in the original
    text's coordinates, suitable for an uncertain-span audit marker.

    Scoped to the first line (capped at ``_HEAD_WINDOW`` chars), and the
    line must contain a digit — a date heading always carries day/year
    digits, while body prose that merely mentions a weekday ("On Monday
    she flew to Paris…") usually doesn't. Without this scoping an
    incidental body weekday would contradict a perfectly good date and
    spuriously flag it (final-review finding, 2026-07-13).
    """
    newline = text.find("\n")
    end = min(newline if newline != -1 else len(text), _HEAD_WINDOW)
    heading_line = text[:end]
    if not any(ch.isdigit() for ch in heading_line):
        return None
    match = _WEEKDAY_RE.search(heading_line)
    if match is None:
        return None
    return match.group(1).lower(), match.span()


@dataclass(frozen=True)
class DateRepairResult:
    """Outcome of :func:`repair_entry_date`.

    ``ok``           — date in range, weekday (if any) consistent.
    ``repaired``     — out-of-range date; exactly one candidate year makes
                       the weekday consistent and lands in range.
    ``doubtful``     — in-range date contradicts the weekday; the date is
                       kept (never rewritten) and flagged for review.
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

    if in_range:
        # An in-range date is never rewritten: a weekday contradiction is
        # as likely a wrong weekday word as a wrong year, so keep the
        # plausible date and flag it for review (final-review finding —
        # a multi-year candidate window could otherwise silently re-year
        # a correct date off an incidental weekday match).
        return DateRepairResult("doubtful", date_iso, date_iso)

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
    return DateRepairResult("unrepairable", date_iso, date_iso)
