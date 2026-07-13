"""Bounds validation + weekday auto-repair (spec 2026-07-13, components 1-2)."""

import datetime as dt

import pytest

from journal.services.entry_dates import (
    DateRepairResult,
    EntryDateError,
    find_weekday_token,
    repair_entry_date,
    validate_entry_date,
)

TODAY = dt.date(2026, 7, 13)
MIN = "2026-01-01"


class TestValidateEntryDate:
    def test_accepts_floor_and_today_plus_one(self) -> None:
        validate_entry_date("2026-01-01", min_date=MIN, today=TODAY)
        validate_entry_date("2026-07-14", min_date=MIN, today=TODAY)

    def test_rejects_below_floor(self) -> None:
        with pytest.raises(EntryDateError, match="2025-12-31"):
            validate_entry_date("2025-12-31", min_date=MIN, today=TODAY)

    def test_rejects_beyond_ceiling(self) -> None:
        with pytest.raises(EntryDateError):
            validate_entry_date("2026-07-15", min_date=MIN, today=TODAY)

    def test_rejects_malformed(self) -> None:
        with pytest.raises(EntryDateError):
            validate_entry_date("not-a-date", min_date=MIN, today=TODAY)


class TestFindWeekdayToken:
    def test_finds_weekday_with_span(self) -> None:
        token = find_weekday_token("Thursday 9 July 2025 9:40\n\nBody...")
        assert token is not None
        word, (start, end) = token
        assert word == "thursday"
        assert (start, end) == (0, 8)

    def test_none_when_absent(self) -> None:
        assert find_weekday_token("9 July 2025\n\nBody") is None

    def test_only_scans_head_of_text(self) -> None:
        assert find_weekday_token("x" * 300 + " Monday") is None


class TestRepairEntryDate:
    def test_incident_116_thursday_9_july(self) -> None:
        r = repair_entry_date("2025-07-09", "thursday", min_date=MIN, today=TODAY)
        assert r == DateRepairResult(
            status="repaired",
            date_iso="2026-07-09",
            original="2025-07-09",
            note="date auto-corrected from 2025-07-09",
        )

    def test_incident_112_monday_29_june(self) -> None:
        r = repair_entry_date("2025-06-29", "monday", min_date=MIN, today=TODAY)
        assert r.status == "repaired"
        assert r.date_iso == "2026-06-29"

    def test_in_range_matching_weekday_is_ok(self) -> None:
        # 2026-07-09 is a Thursday.
        r = repair_entry_date("2026-07-09", "thursday", min_date=MIN, today=TODAY)
        assert r.status == "ok"
        assert r.date_iso == "2026-07-09"

    def test_in_range_mismatch_without_unique_candidate_is_doubtful(self) -> None:
        # 2026-07-09 is a Thursday, heading claims Monday; no year in the
        # [2026, 2027] window puts 9 July on a Monday (2026: Thu, 2027: Fri).
        r = repair_entry_date("2026-07-09", "monday", min_date=MIN, today=TODAY)
        assert r.status == "doubtful"
        assert r.date_iso == "2026-07-09"

    def test_out_of_range_no_weekday_is_unrepairable(self) -> None:
        r = repair_entry_date("2025-07-09", None, min_date=MIN, today=TODAY)
        assert r.status == "unrepairable"
        assert r.date_iso == "2025-07-09"

    def test_in_range_no_weekday_is_ok(self) -> None:
        r = repair_entry_date("2026-07-09", None, min_date=MIN, today=TODAY)
        assert r.status == "ok"

    def test_out_of_range_no_matching_year_is_unrepairable(self) -> None:
        # 3 March: Tuesday in 2026, Wednesday in 2027 — "friday" matches
        # no candidate year in the window.
        r = repair_entry_date("2025-03-03", "friday", min_date=MIN, today=TODAY)
        assert r.status == "unrepairable"

    def test_ambiguous_multiple_candidates_is_unrepairable(self) -> None:
        # A wide window (today in 2037) makes 9 July a Thursday in both
        # 2026 and 2037 → ambiguous → unrepairable.
        r = repair_entry_date(
            "2025-07-09", "thursday", min_date=MIN, today=dt.date(2037, 7, 13)
        )
        assert r.status == "unrepairable"

    def test_feb_29_candidate_years_skipped_safely(self) -> None:
        r = repair_entry_date("2025-02-29", "saturday", min_date=MIN, today=TODAY)
        assert r.status == "unrepairable"  # invalid original date, no crash
