"""Regression guard: the mood-dimension literals hardcoded in the
fitness correlation SQL must stay in sync with the shipped
`config/mood-dimensions.toml`.

The correlation queries in `journal.mcp_server.tools.fitness` filter
on `ms.dimension = '<name>'` with the facet name written as a string
literal (there is no compile-time link between the TOML config and the
SQL). If a facet is renamed in the config but not in the SQL — exactly
the failure mode the 2026-07-15 `energy_fatigue` → `energy_vigor` split
could have introduced — the query silently returns all-NULL columns
instead of erroring. These tests fail loudly instead.
"""

from __future__ import annotations

import re
from pathlib import Path

from journal.services.mood_dimensions import load_mood_dimensions

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CONFIG_PATH = _REPO_ROOT / "config" / "mood-dimensions.toml"
_FITNESS_SRC = (
    _REPO_ROOT / "src" / "journal" / "mcp_server" / "tools" / "fitness.py"
)


def _loaded_names() -> set[str]:
    return {d.name for d in load_mood_dimensions(_CONFIG_PATH)}


def test_correlation_sql_dimensions_are_configured() -> None:
    """The facet names the fitness correlation SQL depends on must all
    exist in the shipped config."""
    required = {"energy_vigor", "joy_sadness", "frustration"}
    assert required <= _loaded_names()


def test_every_fitness_sql_literal_is_a_loaded_dimension() -> None:
    """Extract every ``ms.dimension = '<name>'`` literal from the
    fitness source and assert each names a real, loaded facet — so a
    future rename can never leave a dangling literal behind."""
    source = _FITNESS_SRC.read_text()
    literals = set(re.findall(r"ms\.dimension\s*=\s*'([a-z0-9_]+)'", source))
    assert literals, "expected at least one ms.dimension literal in fitness.py"

    names = _loaded_names()
    unknown = literals - names
    assert not unknown, (
        f"fitness.py references mood dimensions absent from "
        f"{_CONFIG_PATH.name}: {sorted(unknown)}"
    )
