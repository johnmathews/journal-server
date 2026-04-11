"""Tests for the mood dimensions config loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from journal.services.mood_dimensions import (
    MoodDimension,
    MoodDimensionConfigError,
    load_mood_dimensions,
)


def _write(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "mood-dimensions.toml"
    path.write_text(content)
    return path


class TestLoadValidConfig:
    def test_single_bipolar_dimension(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            """
[[dimension]]
name = "joy_sadness"
positive_pole = "joy"
negative_pole = "sadness"
scale_type = "bipolar"
notes = "Joyful vs sad"
""",
        )
        dims = load_mood_dimensions(path)
        assert len(dims) == 1
        d = dims[0]
        assert d.name == "joy_sadness"
        assert d.positive_pole == "joy"
        assert d.negative_pole == "sadness"
        assert d.scale_type == "bipolar"
        assert d.notes == "Joyful vs sad"
        assert d.score_min == -1.0
        assert d.score_max == 1.0

    def test_unipolar_dimension_has_zero_min(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            """
[[dimension]]
name = "agency"
positive_pole = "agency"
negative_pole = "apathy"
scale_type = "unipolar"
notes = "Agency (1) vs apathy (0)"
""",
        )
        dims = load_mood_dimensions(path)
        assert dims[0].score_min == 0.0
        assert dims[0].score_max == 1.0

    def test_multiple_dimensions_preserve_file_order(
        self, tmp_path: Path
    ) -> None:
        path = _write(
            tmp_path,
            """
[[dimension]]
name = "first"
positive_pole = "p"
negative_pole = "n"
scale_type = "bipolar"
notes = "."

[[dimension]]
name = "second"
positive_pole = "p"
negative_pole = "n"
scale_type = "unipolar"
notes = "."

[[dimension]]
name = "third"
positive_pole = "p"
negative_pole = "n"
scale_type = "bipolar"
notes = "."
""",
        )
        dims = load_mood_dimensions(path)
        assert [d.name for d in dims] == ["first", "second", "third"]
        assert [d.scale_type for d in dims] == ["bipolar", "unipolar", "bipolar"]

    def test_multiline_notes_are_trimmed(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            '''
[[dimension]]
name = "joy_sadness"
positive_pole = "joy"
negative_pole = "sadness"
scale_type = "bipolar"
notes = """
Multi-line
notes here.
"""
''',
        )
        dims = load_mood_dimensions(path)
        assert dims[0].notes.startswith("Multi-line")
        assert dims[0].notes.endswith("notes here.")


class TestInvalidConfig:
    def test_file_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="not found"):
            load_mood_dimensions(tmp_path / "missing.toml")

    def test_empty_file_rejects(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "")
        with pytest.raises(
            MoodDimensionConfigError, match="at least one"
        ):
            load_mood_dimensions(path)

    def test_no_dimension_key_rejects(self, tmp_path: Path) -> None:
        path = _write(tmp_path, 'other = "value"')
        with pytest.raises(MoodDimensionConfigError):
            load_mood_dimensions(path)

    def test_missing_required_field(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            """
[[dimension]]
name = "joy_sadness"
positive_pole = "joy"
scale_type = "bipolar"
notes = "."
""",
        )
        with pytest.raises(
            MoodDimensionConfigError, match="missing required fields"
        ):
            load_mood_dimensions(path)

    def test_duplicate_name(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            """
[[dimension]]
name = "joy_sadness"
positive_pole = "joy"
negative_pole = "sadness"
scale_type = "bipolar"
notes = "."

[[dimension]]
name = "joy_sadness"
positive_pole = "j"
negative_pole = "s"
scale_type = "bipolar"
notes = "."
""",
        )
        with pytest.raises(
            MoodDimensionConfigError, match="duplicate dimension name"
        ):
            load_mood_dimensions(path)

    def test_invalid_scale_type(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            """
[[dimension]]
name = "x"
positive_pole = "p"
negative_pole = "n"
scale_type = "trimodal"
notes = "."
""",
        )
        with pytest.raises(
            MoodDimensionConfigError, match="invalid scale_type"
        ):
            load_mood_dimensions(path)

    def test_invalid_name_camel_case(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            """
[[dimension]]
name = "joySadness"
positive_pole = "p"
negative_pole = "n"
scale_type = "bipolar"
notes = "."
""",
        )
        with pytest.raises(
            MoodDimensionConfigError, match="invalid name"
        ):
            load_mood_dimensions(path)

    def test_invalid_name_starts_with_digit(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            """
[[dimension]]
name = "1st_facet"
positive_pole = "p"
negative_pole = "n"
scale_type = "bipolar"
notes = "."
""",
        )
        with pytest.raises(
            MoodDimensionConfigError, match="invalid name"
        ):
            load_mood_dimensions(path)


class TestShippedConfigFile:
    """Smoke test the real `config/mood-dimensions.toml` shipped
    with the repo — catches breakage from future edits."""

    def test_shipped_config_loads(self) -> None:
        # Walk up from this test file to the repo root, same way
        # the server resolves `MOOD_DIMENSIONS_PATH` at startup.
        repo_root = Path(__file__).resolve().parents[2]
        path = repo_root / "config" / "mood-dimensions.toml"
        dims = load_mood_dimensions(path)
        assert len(dims) >= 1
        # Each name appears only once.
        assert len({d.name for d in dims}) == len(dims)
        # Each dimension has a non-empty note so the LLM prompt is
        # not degenerate.
        for d in dims:
            assert d.notes
            assert d.positive_pole
            assert d.negative_pole

    def test_shipped_config_has_mixed_scale_types(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        path = repo_root / "config" / "mood-dimensions.toml"
        dims = load_mood_dimensions(path)
        scale_types = {d.scale_type for d in dims}
        # The starting set should include at least one unipolar
        # facet so the tool-use schema path gets exercised by real
        # config on every run.
        assert "unipolar" in scale_types
        assert "bipolar" in scale_types


class TestMoodDimensionDataclass:
    def test_frozen(self) -> None:
        import dataclasses as _dc

        d = MoodDimension("x", "p", "n", "bipolar", "note")
        with pytest.raises(_dc.FrozenInstanceError):
            d.name = "y"  # type: ignore[misc]

    def test_score_range_bipolar(self) -> None:
        d = MoodDimension("x", "p", "n", "bipolar", ".")
        assert (d.score_min, d.score_max) == (-1.0, 1.0)

    def test_score_range_unipolar(self) -> None:
        d = MoodDimension("x", "p", "n", "unipolar", ".")
        assert (d.score_min, d.score_max) == (0.0, 1.0)
