"""Tests for the pure-Python Pearson helper (fitness correlation tools)."""

import pytest

from journal.services.fitness.correlation_stats import pearson


def test_pearson_perfect_positive() -> None:
    pairs = [(1.0, 2.0), (2.0, 4.0), (3.0, 6.0), (4.0, 8.0)]
    out = pearson(pairs)
    assert out["n"] == 4
    assert out["r"] == pytest.approx(1.0)


def test_pearson_perfect_negative() -> None:
    pairs = [(1.0, 8.0), (2.0, 6.0), (3.0, 4.0), (4.0, 2.0)]
    out = pearson(pairs)
    assert out["n"] == 4
    assert out["r"] == pytest.approx(-1.0)


def test_pearson_zero_variance_returns_none() -> None:
    """A constant series has no linear relationship to define → r is None,
    but n still reflects the pair count."""
    pairs = [(5.0, 1.0), (5.0, 2.0), (5.0, 3.0)]
    out = pearson(pairs)
    assert out == {"r": None, "n": 3}


def test_pearson_too_few_points_returns_none() -> None:
    pairs = [(1.0, 2.0), (2.0, 4.0)]
    out = pearson(pairs)
    assert out == {"r": None, "n": 2}

    assert pearson([]) == {"r": None, "n": 0}


def test_pearson_known_nontrivial_value() -> None:
    """A hand-checkable moderate positive correlation.

    x = [1, 2, 3, 4, 5], y = [2, 1, 4, 3, 5]:
    mean_x = 3, mean_y = 3. sxx = 10, syy = 10,
    sxy = (-2)(-1)+(-1)(-2)+0+1*0+2*2 = 2+2+0+0+4 = 8.
    r = 8 / sqrt(10*10) = 0.8.
    """
    pairs = [(1.0, 2.0), (2.0, 1.0), (3.0, 4.0), (4.0, 3.0), (5.0, 5.0)]
    out = pearson(pairs)
    assert out["n"] == 5
    assert out["r"] == pytest.approx(0.8)
