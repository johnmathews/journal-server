"""Tests for date extraction from OCR text."""

from __future__ import annotations

from journal.services.date_extraction import extract_date_from_text


class TestDMYNamedFormat:
    """Pattern: day-name day month-name year (e.g. 'TUES 17 FEB 2026')."""

    def test_abbreviated_day_and_month(self) -> None:
        assert extract_date_from_text("TUES 17 FEB 2026") == "2026-02-17"

    def test_full_day_and_month_names(self) -> None:
        assert extract_date_from_text("Tuesday 17 February 2026") == "2026-02-17"

    def test_no_day_name_prefix(self) -> None:
        assert extract_date_from_text("17 February 2026") == "2026-02-17"

    def test_abbreviated_month_no_day_name(self) -> None:
        assert extract_date_from_text("17 Feb 2026") == "2026-02-17"

    def test_case_insensitive(self) -> None:
        assert extract_date_from_text("tues 17 feb 2026") == "2026-02-17"

    def test_single_digit_day(self) -> None:
        assert extract_date_from_text("3 March 2026") == "2026-03-03"


class TestMDYNamedFormat:
    """Pattern: month-name day, year (e.g. 'Feb 17, 2026')."""

    def test_abbreviated_month_with_comma(self) -> None:
        assert extract_date_from_text("Feb 17, 2026") == "2026-02-17"

    def test_full_month_with_comma(self) -> None:
        assert extract_date_from_text("February 17, 2026") == "2026-02-17"

    def test_full_month_without_comma(self) -> None:
        assert extract_date_from_text("February 17 2026") == "2026-02-17"

    def test_single_digit_day(self) -> None:
        assert extract_date_from_text("Mar 3, 2026") == "2026-03-03"


class TestISOFormat:
    """Pattern: YYYY-MM-DD (e.g. '2026-02-17')."""

    def test_standard_iso(self) -> None:
        assert extract_date_from_text("2026-02-17") == "2026-02-17"

    def test_iso_embedded_in_text(self) -> None:
        assert extract_date_from_text("Entry date: 2026-02-17 notes") == "2026-02-17"


class TestDMYNumericFormat:
    """Pattern: DD/MM/YYYY or DD.MM.YYYY."""

    def test_slash_separator(self) -> None:
        assert extract_date_from_text("17/02/2026") == "2026-02-17"

    def test_dot_separator(self) -> None:
        assert extract_date_from_text("17.02.2026") == "2026-02-17"

    def test_single_digit_day_and_month(self) -> None:
        assert extract_date_from_text("3/2/2026") == "2026-02-03"


class TestNoDate:
    """Text that contains no recognisable date returns None."""

    def test_plain_text(self) -> None:
        assert extract_date_from_text("Just some random thoughts today") is None

    def test_empty_string(self) -> None:
        assert extract_date_from_text("") is None

    def test_numbers_but_not_a_date(self) -> None:
        assert extract_date_from_text("I walked 12 miles and ate 3 apples") is None


class TestPositionInText:
    """Date must appear within the first 500 characters."""

    def test_date_after_leading_text_within_500_chars(self) -> None:
        prefix = "Some journal preamble. " * 5  # ~115 chars
        text = prefix + "17 February 2026 — today was a good day."
        assert extract_date_from_text(text) == "2026-02-17"

    def test_date_at_exactly_char_boundary(self) -> None:
        # Place date so it starts right before the 500-char cutoff
        padding = "x" * 480
        text = padding + " 17 Feb 2026 and more text"
        assert extract_date_from_text(text) == "2026-02-17"

    def test_date_beyond_500_chars_returns_none(self) -> None:
        padding = "x" * 510
        text = padding + " 17 February 2026"
        assert extract_date_from_text(text) is None


class TestInvalidDates:
    """Calendar-invalid dates should return None."""

    def test_feb_30(self) -> None:
        assert extract_date_from_text("30 February 2026") is None

    def test_feb_30_numeric(self) -> None:
        assert extract_date_from_text("30/02/2026") is None

    def test_april_31(self) -> None:
        assert extract_date_from_text("31 April 2026") is None

    def test_month_13_numeric(self) -> None:
        assert extract_date_from_text("15/13/2026") is None
