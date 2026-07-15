"""Free-form mood-dimension → canonical facet resolution."""

from __future__ import annotations

import pytest

from journal.services.conversations.dimensions import resolve_dimension

_FACETS = [
    "joy_sadness",
    "energy_vigor",
    "tension_calm",
    "physical_fatigue",
    "mental_fatigue",
    "agency",
    "fulfillment",
    "connection",
    "frustration",
    "proactive_reactive",
]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("energy_vigor", "energy_vigor"),  # exact
        ("Energy_Vigor", "energy_vigor"),  # case-insensitive
        ("energy vigor", "energy_vigor"),  # separator-insensitive
        ("energy", "energy_vigor"),  # single-token near-miss
        ("vigor", "energy_vigor"),
        ("joy", "joy_sadness"),
        ("sad", "joy_sadness"),  # synonym → sadness token
        ("happiness", "joy_sadness"),  # synonym → joy token
        ("stress", "tension_calm"),  # synonym → tension token
        ("frustration", "frustration"),
        ("agency", "agency"),
        ("mental fatigue", "mental_fatigue"),  # multi-token exact-ish
    ],
)
def test_resolves_to_expected_facet(raw: str, expected: str) -> None:
    assert resolve_dimension(raw, _FACETS) == expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "mood",  # generic — no facet token
        "overall",
        "fatigue",  # ambiguous: physical_ vs mental_fatigue
        "tiredness",  # synonym → fatigue → ambiguous
        "exhausted",
        "nonsense_xyz",
    ],
)
def test_unresolved_or_ambiguous_returns_none(raw: str | None) -> None:
    assert resolve_dimension(raw, _FACETS) is None
