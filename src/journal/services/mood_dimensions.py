"""Mood dimension configuration loader.

Mood facets live in a TOML file (default:
`config/mood-dimensions.toml`) rather than in Python source, so the
user can add, remove, or edit facets by editing the file and
re-running the backfill CLI. No schema migration required — the
`mood_scores` table is sparse by `(entry_id, dimension)`, and
retired facets are preserved as historical scores until explicitly
pruned.

See `docs/mood-scoring.md` for the rationale (bipolar vs unipolar,
rebuild procedure, cadence, cost estimates).
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from pathlib import Path

log = logging.getLogger(__name__)

ScaleType = Literal["bipolar", "unipolar"]

_VALID_SCALE_TYPES: tuple[ScaleType, ...] = ("bipolar", "unipolar")


@dataclass(frozen=True)
class MoodDimension:
    """One facet in the mood-scoring schema.

    - `name` is the stable key stored in `mood_scores.dimension`.
      Must be snake_case and unique across the config file.
    - `positive_pole` and `negative_pole` are human-readable labels
      shown in the LLM prompt, the chart legend, and the dimension
      toggle UI.
    - `scale_type` is `"bipolar"` (scores in `[-1, +1]`, with 0 as a
      meaningful neutral) or `"unipolar"` (scores in `[0, +1]`, with
      0 meaning absence of the positive pole).
    - `notes` is 1-2 sentences of scoring criteria that go into the
      LLM prompt verbatim. Editing the notes and re-running
      `journal backfill-mood --force` reinterprets every entry
      against the new criteria.
    """

    name: str
    positive_pole: str
    negative_pole: str
    scale_type: ScaleType
    notes: str

    @property
    def score_min(self) -> float:
        """Inclusive lower bound of the scoring range, based on
        `scale_type`. Used to build the LLM tool schema."""
        return -1.0 if self.scale_type == "bipolar" else 0.0

    @property
    def score_max(self) -> float:
        """Inclusive upper bound of the scoring range."""
        return 1.0


class MoodDimensionConfigError(ValueError):
    """Raised when the TOML config is malformed or contains
    duplicate/invalid dimension definitions. The CLI catches this
    and prints a clean error message — a misconfigured file should
    never silently degrade to an empty dimension list."""


def load_mood_dimensions(path: Path) -> tuple[MoodDimension, ...]:
    """Load and validate the mood-dimension config from a TOML file.

    Raises `FileNotFoundError` if the file is missing (the CLI /
    server should fail loudly — an absent config with
    `JOURNAL_ENABLE_MOOD_SCORING=true` is almost certainly a
    deployment mistake).

    Raises `MoodDimensionConfigError` for:
    1. A top-level structure that is not a list of tables under
       `[[dimension]]`.
    2. Any dimension missing a required field.
    3. A duplicate `name`.
    4. A `scale_type` that is not `"bipolar"` or `"unipolar"`.
    5. A `name` that is not a valid snake_case identifier (which
       would break SQL queries that filter on `dimension`).

    Returns the dimensions as a tuple in file order — ordering is
    load-bearing only for chart display.
    """
    if not path.exists():
        raise FileNotFoundError(
            f"Mood dimensions config not found: {path}. Either create "
            f"the file or disable mood scoring by setting "
            f"JOURNAL_ENABLE_MOOD_SCORING=false."
        )

    with path.open("rb") as f:
        data = tomllib.load(f)

    raw_dimensions = data.get("dimension")
    if not isinstance(raw_dimensions, list) or not raw_dimensions:
        raise MoodDimensionConfigError(
            f"{path}: expected at least one [[dimension]] block at "
            f"the top level of the file."
        )

    seen_names: set[str] = set()
    dimensions: list[MoodDimension] = []
    for i, raw in enumerate(raw_dimensions):
        if not isinstance(raw, dict):
            raise MoodDimensionConfigError(
                f"{path}: dimension #{i} is not a table."
            )
        missing = [
            field
            for field in (
                "name",
                "positive_pole",
                "negative_pole",
                "scale_type",
                "notes",
            )
            if field not in raw
        ]
        if missing:
            raise MoodDimensionConfigError(
                f"{path}: dimension #{i} missing required fields: "
                f"{', '.join(missing)}"
            )

        name = str(raw["name"]).strip()
        if not name or not _is_snake_case(name):
            raise MoodDimensionConfigError(
                f"{path}: dimension #{i} has invalid name {name!r}; "
                f"must be a non-empty snake_case identifier."
            )
        if name in seen_names:
            raise MoodDimensionConfigError(
                f"{path}: duplicate dimension name {name!r}."
            )
        seen_names.add(name)

        scale_type = str(raw["scale_type"]).strip()
        if scale_type not in _VALID_SCALE_TYPES:
            raise MoodDimensionConfigError(
                f"{path}: dimension {name!r} has invalid scale_type "
                f"{scale_type!r}; must be 'bipolar' or 'unipolar'."
            )

        dimensions.append(
            MoodDimension(
                name=name,
                positive_pole=str(raw["positive_pole"]).strip(),
                negative_pole=str(raw["negative_pole"]).strip(),
                scale_type=scale_type,  # type: ignore[arg-type]
                notes=str(raw["notes"]).strip(),
            )
        )

    log.info(
        "Loaded %d mood dimensions from %s: %s",
        len(dimensions),
        path,
        ", ".join(d.name for d in dimensions),
    )
    return tuple(dimensions)


def _is_snake_case(s: str) -> bool:
    """Lower-case letters, digits, and underscores; first character
    must be a letter. Matches Python identifier conventions minus
    leading underscores (we don't want private-looking names in the
    user-facing config)."""
    if not s:
        return False
    if not s[0].isalpha() or not s[0].islower():
        return False
    return all(c.isalnum() and (c.isdigit() or c.islower()) or c == "_" for c in s)
