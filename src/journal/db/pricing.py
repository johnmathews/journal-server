"""Read / write API pricing configuration."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3

log = logging.getLogger(__name__)


@dataclass
class PricingEntry:
    """A single row from the pricing table."""

    model: str
    category: str  # 'llm' | 'embedding' | 'transcription'
    input_cost_per_mtok: float | None
    output_cost_per_mtok: float | None
    cost_per_minute: float | None
    last_verified: str


def get_all_pricing(conn: sqlite3.Connection) -> list[PricingEntry]:
    """Return every pricing row, ordered by category then model."""
    rows = conn.execute(
        "SELECT model, category, input_cost_per_mtok, output_cost_per_mtok, "
        "cost_per_minute, last_verified FROM pricing ORDER BY category, model",
    ).fetchall()
    return [PricingEntry(**dict(r)) for r in rows]


def estimate_cost(
    conn: sqlite3.Connection,
    per_model: dict[str, dict[str, int]],
) -> float | None:
    """Best-effort USD cost for captured per-model token counts.

    Reuses the existing ``pricing`` table (0017 seed + later backfills):
    for each model in *per_model*, adds
    ``input_tokens/1e6 * input_cost_per_mtok`` and
    ``output_tokens/1e6 * output_cost_per_mtok``.

    A model is *excluded* (not an error) when it has no pricing row (a
    warning is logged) or when its row is ``category == 'transcription'``
    — those are priced per audio-minute, not per token, so they can't be
    costed from token counts. ``None`` cost fields (either direction) are
    skipped term-by-term.

    Returns ``None`` when nothing was priceable (so the caller can record
    tokens while leaving ``cost_usd`` NULL); otherwise the float total.
    """
    pricing = {entry.model: entry for entry in get_all_pricing(conn)}
    total = 0.0
    priced_any = False
    for model, bucket in per_model.items():
        entry = pricing.get(model)
        if entry is None:
            log.warning(
                "No pricing row for model %r; excluding from cost estimate", model,
            )
            continue
        if entry.category == "transcription":
            # Priced per audio-minute, not per token — out of scope here.
            continue
        input_tokens = bucket.get("input_tokens", 0)
        output_tokens = bucket.get("output_tokens", 0)
        if entry.input_cost_per_mtok is not None:
            total += input_tokens / 1e6 * entry.input_cost_per_mtok
            priced_any = True
        if entry.output_cost_per_mtok is not None:
            total += output_tokens / 1e6 * entry.output_cost_per_mtok
            priced_any = True
    return total if priced_any else None


def update_pricing(
    conn: sqlite3.Connection,
    model: str,
    updates: dict[str, object],
) -> PricingEntry | None:
    """Update pricing for a single model.

    Only cost fields and ``last_verified`` are writable.
    Returns the updated entry, or ``None`` if the model was not found
    or *updates* contained no allowed keys.
    """
    allowed = {"input_cost_per_mtok", "output_cost_per_mtok", "cost_per_minute", "last_verified"}
    filtered = {k: v for k, v in updates.items() if k in allowed}
    if not filtered:
        return None

    # Check model exists
    existing = conn.execute(
        "SELECT model FROM pricing WHERE model = ?", (model,)
    ).fetchone()
    if not existing:
        return None

    set_clause = ", ".join(f"{k} = ?" for k in filtered)
    values = list(filtered.values()) + [model]
    conn.execute(f"UPDATE pricing SET {set_clause} WHERE model = ?", values)  # noqa: S608
    conn.commit()

    row = conn.execute(
        "SELECT model, category, input_cost_per_mtok, output_cost_per_mtok, "
        "cost_per_minute, last_verified FROM pricing WHERE model = ?",
        (model,),
    ).fetchone()
    return PricingEntry(**dict(row)) if row else None
