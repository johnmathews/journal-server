"""Tests for `journal.services.entity_naming`.

Covers the smart-title-case algorithm and the TOML exception loader.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from journal.services.entity_naming import (
    load_entity_casing_exceptions,
    smart_title_case,
)

# ---------------------------------------------------------------------------
# Algorithm — basic title-casing
# ---------------------------------------------------------------------------


class TestTitleCaseBasics:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("running", "Running"),
            ("church", "Church"),
            ("prayer", "Prayer"),
            ("alice", "Alice"),
            ("john smith", "John Smith"),
        ],
    )
    def test_simple_words(self, raw: str, expected: str) -> None:
        assert smart_title_case(raw) == expected


class TestArticlePreservation:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            # Leading article always capitalized.
            ("the netherlands", "The Netherlands"),
            ("the lord of the rings", "The Lord of the Rings"),
            ("republic of korea", "Republic of Korea"),
            ("a tale of two cities", "A Tale of Two Cities"),
        ],
    )
    def test_articles_lowercased_in_non_leading_positions(
        self, raw: str, expected: str
    ) -> None:
        assert smart_title_case(raw) == expected


class TestIntraWordUppercasePreservation:
    """Per-word preservation: a word with a deliberate intra-word uppercase
    (uppercase after a lowercase in the same word, e.g. ``iOS``, ``DeepMind``)
    is passed through verbatim. A word that is fully uppercase and longer
    than one character (e.g. ``FC``, ``NASA``) is also preserved as an acronym.
    """

    @pytest.mark.parametrize(
        "name",
        [
            "iOS",
            "iPhone",
            "iPad",
            "macOS",
            "eBay",
            "GitHub",
            "LinkedIn",
            "FC Barcelona",
            "McDonald's",
            "DeepMind",
        ],
    )
    def test_passes_through_when_already_cased(self, name: str) -> None:
        assert smart_title_case(name) == name

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            # Mixed-case words alongside lowercase words: each handled per-word.
            ("Easter picnic", "Easter Picnic"),
            ("Kettlebell workouts", "Kettlebell Workouts"),
            ("Artist dates", "Artist Dates"),
            ("Easter sunday", "Easter Sunday"),
            ("Pull-up bar", "Pull-Up Bar"),
            # iOS-style word adjacent to a regular word.
            ("iOS app", "iOS App"),
            ("eBay listing", "eBay Listing"),
            # Unknown acronym (not in exceptions) preserved when fully uppercase.
            ("FOO bar", "FOO Bar"),
            ("BBQ tongs", "BBQ Tongs"),
        ],
    )
    def test_per_word_normalisation_with_mixed_casing(
        self, raw: str, expected: str
    ) -> None:
        assert smart_title_case(raw) == expected


class TestExceptionsLookup:
    @pytest.fixture
    def exceptions(self) -> dict[str, str]:
        return {
            "ios": "iOS",
            "nasa": "NASA",
            "ikea": "IKEA",
            "o'brien": "O'Brien",
            "the netherlands": "The Netherlands",
            "fc barcelona": "FC Barcelona",
        }

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("ios", "iOS"),
            ("IOS", "iOS"),
            ("Ios", "iOS"),
            ("nasa", "NASA"),
            ("NASA", "NASA"),
            ("ikea", "IKEA"),
            ("IKEA", "IKEA"),
            ("o'brien", "O'Brien"),
            ("O'BRIEN", "O'Brien"),
            ("the netherlands", "The Netherlands"),
        ],
    )
    def test_lookup_is_case_insensitive(
        self, exceptions: dict[str, str], raw: str, expected: str
    ) -> None:
        assert smart_title_case(raw, exceptions=exceptions) == expected

    def test_exception_overrides_midword_uppercase_passthrough(
        self, exceptions: dict[str, str]
    ) -> None:
        # Even if the input is already mixed-case, an exact (case-insensitive)
        # match in the exceptions table wins so the operator's preferred
        # casing is canonical.
        # Note: 'iOS' as input -> matches 'ios' key in exceptions -> 'iOS'.
        assert smart_title_case("iOS", exceptions=exceptions) == "iOS"

    def test_exception_with_already_correct_casing(
        self, exceptions: dict[str, str]
    ) -> None:
        assert smart_title_case("FC Barcelona", exceptions=exceptions) == "FC Barcelona"


class TestDutchParticles:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            # First-word particle still capitalized.
            ("den haag", "Den Haag"),
            # Middle-position particle stays lowercase.
            ("jan van halen", "Jan van Halen"),
            ("pieter de groot", "Pieter de Groot"),
            ("vincent van gogh", "Vincent van Gogh"),
            ("anne van der berg", "Anne van der Berg"),
        ],
    )
    def test_dutch_particles_lowercased_in_non_leading_positions(
        self, raw: str, expected: str
    ) -> None:
        assert smart_title_case(raw) == expected


class TestHyphens:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("anglo-saxon", "Anglo-Saxon"),
            ("co-pilot", "Co-Pilot"),
            ("jean-paul", "Jean-Paul"),
            ("self-help", "Self-Help"),
        ],
    )
    def test_each_hyphen_segment_capitalized(
        self, raw: str, expected: str
    ) -> None:
        assert smart_title_case(raw) == expected


class TestApostrophes:
    def test_possessive_s_keeps_lowercase(self) -> None:
        # The apostrophe is not a word separator: 'john's' is one word and
        # only the leading 'j' is uppercased.
        assert smart_title_case("john's bakery") == "John's Bakery"

    def test_oclock_style(self) -> None:
        assert smart_title_case("five o'clock") == "Five O'clock"


class TestEdgeCases:
    def test_empty_string(self) -> None:
        assert smart_title_case("") == ""

    def test_whitespace_only(self) -> None:
        assert smart_title_case("   ") == ""

    def test_single_character(self) -> None:
        assert smart_title_case("a") == "A"

    def test_internal_whitespace_collapsed(self) -> None:
        assert smart_title_case("  john   smith  ") == "John Smith"

    def test_idempotent(self) -> None:
        # Apply twice — should converge after the first pass.
        for raw in ("running", "the netherlands", "iOS", "anglo-saxon", ""):
            once = smart_title_case(raw)
            twice = smart_title_case(once)
            assert once == twice, f"Not idempotent for {raw!r}"

    def test_none_exceptions_treated_as_empty(self) -> None:
        # smart_title_case with exceptions=None must not crash.
        assert smart_title_case("running", exceptions=None) == "Running"


class TestNumerals:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("route 66", "Route 66"),
            ("boeing 747", "Boeing 747"),
            ("apollo 11", "Apollo 11"),
        ],
    )
    def test_numerals_pass_through(self, raw: str, expected: str) -> None:
        assert smart_title_case(raw) == expected


# ---------------------------------------------------------------------------
# TOML loader
# ---------------------------------------------------------------------------


class TestLoadEntityCasingExceptions:
    def test_loads_valid_file(self, tmp_path: Path) -> None:
        path = tmp_path / "exceptions.toml"
        path.write_text(
            '[meta]\n'
            'version = "test"\n'
            '\n'
            '[exceptions]\n'
            '"ios" = "iOS"\n'
            '"nasa" = "NASA"\n',
            encoding="utf-8",
        )
        result = load_entity_casing_exceptions(path)
        assert result == {"ios": "iOS", "nasa": "NASA"}

    def test_keys_lowercased_for_lookup(self, tmp_path: Path) -> None:
        # Even if a key is uppercase in the TOML, the loader normalises to lower.
        path = tmp_path / "exceptions.toml"
        path.write_text(
            '[exceptions]\n'
            '"IOS" = "iOS"\n',
            encoding="utf-8",
        )
        result = load_entity_casing_exceptions(path)
        assert result == {"ios": "iOS"}

    def test_missing_file_returns_empty_with_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = tmp_path / "does-not-exist.toml"
        with caplog.at_level(logging.WARNING, logger="journal.services.entity_naming"):
            result = load_entity_casing_exceptions(path)
        assert result == {}
        assert any(
            "not found" in r.message.lower() for r in caplog.records
        )

    def test_malformed_toml_returns_empty_with_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = tmp_path / "bad.toml"
        path.write_text("this is = not [valid] toml = [", encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger="journal.services.entity_naming"):
            result = load_entity_casing_exceptions(path)
        assert result == {}
        assert any(
            "failed to load" in r.message.lower() for r in caplog.records
        )

    def test_missing_exceptions_table_returns_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "no-table.toml"
        path.write_text('[meta]\nversion = "1"\n', encoding="utf-8")
        assert load_entity_casing_exceptions(path) == {}

    def test_exceptions_not_a_table_logs_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = tmp_path / "wrong-shape.toml"
        path.write_text('exceptions = "not a table"\n', encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger="journal.services.entity_naming"):
            result = load_entity_casing_exceptions(path)
        assert result == {}
        assert any("not a table" in r.message.lower() for r in caplog.records)

    def test_repo_default_file_loads(self) -> None:
        """The shipped config file at config/entity-casing-exceptions.toml should parse."""
        # Resolve relative to the repo root regardless of where pytest is invoked from.
        candidates = [
            Path("config/entity-casing-exceptions.toml"),
            Path(__file__).resolve().parent.parent.parent
            / "config"
            / "entity-casing-exceptions.toml",
        ]
        path = next((p for p in candidates if p.exists()), None)
        if path is None:
            pytest.skip("config/entity-casing-exceptions.toml not found")
        result = load_entity_casing_exceptions(path)
        # File contains tech / brand / Dutch entries — check a few we expect.
        assert result.get("ios") == "iOS"
        assert result.get("ikea") == "IKEA"
        assert result.get("the netherlands") == "The Netherlands"
