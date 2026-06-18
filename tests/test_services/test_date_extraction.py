"""Tests for date extraction from OCR text and filenames."""

from __future__ import annotations

from datetime import date

from journal.services.date_extraction import extract_date_from_filename, extract_date_from_text

# A fixed "today" so year-inference tests are deterministic. 2026-06-18 is a
# Thursday (see the weekday-disambiguation cases below).
_TODAY = date(2026, 6, 18)


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


class TestYearlessDate:
    """Day-first named-month dates with NO year — the year is inferred as the
    most recent occurrence that is not in the future."""

    def test_day_month_only(self) -> None:
        # 9 June 2026 is on/before today (18 June 2026) → current year.
        assert extract_date_from_text("9 June", today=_TODAY) == "2026-06-09"

    def test_today_itself(self) -> None:
        assert extract_date_from_text("18 June", today=_TODAY) == "2026-06-18"

    def test_future_month_day_rolls_back_a_year(self) -> None:
        # 20 June 2026 is after today → fall back to 20 June 2025.
        assert extract_date_from_text("20 June", today=_TODAY) == "2025-06-20"

    def test_abbreviated_month(self) -> None:
        assert extract_date_from_text("9 Jun", today=_TODAY) == "2026-06-09"

    def test_invalid_day_returns_none(self) -> None:
        assert extract_date_from_text("31 June", today=_TODAY) is None


class TestTimeSuffixIgnored:
    """A trailing time (HH:MM) must not break parsing or change the date."""

    def test_yearless_with_time(self) -> None:
        assert extract_date_from_text("10 June 23:35", today=_TODAY) == "2026-06-10"

    def test_year_with_time(self) -> None:
        assert (
            extract_date_from_text("11 June 2026 22:15", today=_TODAY) == "2026-06-11"
        )

    def test_weekday_year_time(self) -> None:
        assert (
            extract_date_from_text("Thursday 18 June 2026 14:30", today=_TODAY)
            == "2026-06-18"
        )


class TestWeekdayDisambiguation:
    """When a weekday name is present on a year-less date, it selects the most
    recent past year whose weekday matches."""

    def test_weekday_matches_current_year(self) -> None:
        # 18 June 2026 is a Thursday → current year wins.
        assert (
            extract_date_from_text("Thursday 18 June 22:55", today=_TODAY)
            == "2026-06-18"
        )

    def test_weekday_picks_previous_year(self) -> None:
        # 18 June 2026 is Thursday, 2025 is Wednesday → "Wednesday 18 June" = 2025.
        assert (
            extract_date_from_text("Wednesday 18 June", today=_TODAY) == "2025-06-18"
        )

    def test_weekday_picks_several_years_back(self) -> None:
        # Nearest past Friday-the-18th-of-June before/at today is 2021.
        assert extract_date_from_text("Friday 18 June", today=_TODAY) == "2021-06-18"

    def test_weekday_with_invalid_day_returns_none(self) -> None:
        assert extract_date_from_text("Monday 31 April", today=_TODAY) is None


class TestExplicitYearKeptEvenIfFuture:
    """An explicitly written year is trusted as-is — the no-future rule only
    governs INFERRED (missing) years."""

    def test_future_explicit_year_kept(self) -> None:
        assert extract_date_from_text("9 June 2030", today=_TODAY) == "2030-06-09"

    def test_future_day_in_current_year_kept(self) -> None:
        # 20 June 2026 is after today, but the year was written → keep it.
        assert extract_date_from_text("20 June 2026", today=_TODAY) == "2026-06-20"

    def test_historical_year_kept(self) -> None:
        assert extract_date_from_text("7 June 2016", today=_TODAY) == "2016-06-07"


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


class TestFilenameISOFormat:
    """Extract dates from ISO-style filenames like '2026-03-28_description.md'."""

    def test_iso_with_underscore_description(self) -> None:
        assert extract_date_from_filename("2026-03-28_at_the_burrow.md") == "2026-03-28"

    def test_iso_bare(self) -> None:
        assert extract_date_from_filename("2026-03-28.md") == "2026-03-28"

    def test_iso_with_underscores_as_separator(self) -> None:
        assert extract_date_from_filename("2026_03_28_notes.txt") == "2026-03-28"

    def test_iso_with_dots_as_separator(self) -> None:
        assert extract_date_from_filename("2026.03.28.md") == "2026-03-28"

    def test_iso_with_path(self) -> None:
        assert extract_date_from_filename("/uploads/2026-03-28_entry.md") == "2026-03-28"

    def test_iso_no_extension(self) -> None:
        assert extract_date_from_filename("2026-03-28") == "2026-03-28"


class TestFilenameNamedMonthFormat:
    """Extract dates from filenames with named months."""

    def test_dmy_named(self) -> None:
        assert extract_date_from_filename("28-March-2026.md") == "2026-03-28"

    def test_dmy_abbreviated(self) -> None:
        assert extract_date_from_filename("28_mar_2026_notes.txt") == "2026-03-28"

    def test_mdy_named(self) -> None:
        assert extract_date_from_filename("March-28-2026.md") == "2026-03-28"

    def test_mdy_abbreviated_underscores(self) -> None:
        assert extract_date_from_filename("mar_28_2026.txt") == "2026-03-28"


class TestFilenameNoDate:
    """Filenames without recognisable dates return None."""

    def test_plain_name(self) -> None:
        assert extract_date_from_filename("my_journal_entry.md") is None

    def test_short_numbers(self) -> None:
        assert extract_date_from_filename("entry_12.txt") is None

    def test_empty_string(self) -> None:
        assert extract_date_from_filename("") is None


class TestFilenameInvalidDate:
    """Calendar-invalid dates in filenames should return None."""

    def test_feb_30(self) -> None:
        assert extract_date_from_filename("2026-02-30_notes.md") is None

    def test_month_13(self) -> None:
        assert extract_date_from_filename("2026-13-01.md") is None
