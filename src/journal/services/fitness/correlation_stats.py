"""Pure-Python Pearson correlation for the fitness↔mood correlation tools.

The three MCP correlation tools (Q1/Q2/Q3 in ``docs/fitness-schema.md`` §8)
return per-day / per-week ``rows``; this module turns the complete pairs in
those rows into a single Pearson coefficient so callers get a headline "how
correlated are these two series" number alongside the raw grid.

No numpy/scipy on purpose: the sample sizes are personal-scale (≤365 daily
rows per user-year), so a straight two-pass Python computation is more than
fast enough and keeps the dependency surface minimal.
"""

from __future__ import annotations

PearsonResult = dict[str, float | int | None]


def pearson(pairs: list[tuple[float, float]]) -> PearsonResult:
    """Pearson product-moment correlation coefficient of ``pairs``.

    Args:
        pairs: complete ``(x, y)`` observation pairs. Callers must drop
            any pair with a missing value (``None``) before calling —
            this function does not filter.

    Returns:
        ``{"r": float | None, "n": int}``. ``r`` is ``None`` when
        ``n < 3`` (too few points for the coefficient to be meaningful)
        or when either series has zero variance (correlation undefined —
        a constant series has no linear relationship to define). ``n`` is
        always the number of pairs supplied.
    """
    n = len(pairs)
    if n < 3:
        return {"r": None, "n": n}
    mean_x = sum(x for x, _ in pairs) / n
    mean_y = sum(y for _, y in pairs) / n
    sxx = sum((x - mean_x) ** 2 for x, _ in pairs)
    syy = sum((y - mean_y) ** 2 for _, y in pairs)
    if sxx == 0.0 or syy == 0.0:
        return {"r": None, "n": n}
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in pairs)
    return {"r": sxy / (sxx * syy) ** 0.5, "n": n}
