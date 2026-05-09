"""Soft-pointer integrity check for the fitness pipeline.

Per fitness-schema.md §6, normalized rows carry `(source, source_id)` and
a soft `raw_ref_id` (or `raw_ref_ids_json` array for daily) into the
per-source raw tables — *not* a hard FOREIGN KEY. The trade-off is:

- Raw is sacred and append-only (D3), so a missing raw row should never
  happen. A hard FK with CASCADE would silently delete normalized
  evidence; with RESTRICT it would block a hypothetical raw cleanup.
- Soft pointer + this checker keeps the invariant a property of the
  ingestion code (which never deletes raw), rather than of the schema.

This module runs the checks documented in §6 and returns an
`IntegrityReport` describing any orphans. Callers (e.g. the W9
`/api/fitness/integrity` endpoint and the W10 MCP tool) get a
structured payload they can render or log.

Both raw tables have independent AUTOINCREMENT sequences, so a
source-blind join on `id` would silently match the wrong row across
tables. Every check below filters on `source` to avoid that.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3


@dataclass(frozen=True)
class ActivityOrphan:
    activity_id: int
    source: str
    raw_ref_id: int
    issue: str = "raw_row_missing"


@dataclass(frozen=True)
class DailyOrphan:
    daily_id: int
    source: str
    missing_raw_ids: list[int]


@dataclass(frozen=True)
class IntegrityReport:
    activities: list[ActivityOrphan] = field(default_factory=list)
    daily: list[DailyOrphan] = field(default_factory=list)

    @property
    def has_orphans(self) -> bool:
        return bool(self.activities or self.daily)


def check_fitness_integrity(conn: sqlite3.Connection) -> IntegrityReport:
    """Verify every normalized row's soft pointer resolves into the
    matching per-source raw table.

    Returns a report; does not raise. Callers decide what to do with
    orphans (alert via Pushover, surface in /health, etc.).
    """
    activities: list[ActivityOrphan] = []

    # Strava activities — soft pointer must resolve into fitness_raw_strava.
    strava_orphans = conn.execute(
        """
        SELECT fa.id, fa.raw_ref_id
        FROM fitness_activities fa
        LEFT JOIN fitness_raw_strava r ON r.id = fa.raw_ref_id
        WHERE fa.source = 'strava' AND r.id IS NULL
        """,
    ).fetchall()
    for row in strava_orphans:
        activities.append(
            ActivityOrphan(
                activity_id=row["id"],
                source="strava",
                raw_ref_id=row["raw_ref_id"],
            ),
        )

    # Garmin activities — soft pointer must resolve into fitness_raw_garmin.
    garmin_orphans = conn.execute(
        """
        SELECT fa.id, fa.raw_ref_id
        FROM fitness_activities fa
        LEFT JOIN fitness_raw_garmin r ON r.id = fa.raw_ref_id
        WHERE fa.source = 'garmin' AND r.id IS NULL
        """,
    ).fetchall()
    for row in garmin_orphans:
        activities.append(
            ActivityOrphan(
                activity_id=row["id"],
                source="garmin",
                raw_ref_id=row["raw_ref_id"],
            ),
        )

    daily: list[DailyOrphan] = []

    # Daily rollups — JSON array of raw row ids, expanded via json_each.
    # Garmin is the only source that contributes daily rows today, so the
    # join targets fitness_raw_garmin. If a future source (Strava-derived
    # daily, Whoop, etc.) is added, this query needs a per-source branch.
    daily_rows = conn.execute(
        """
        SELECT fd.id, fd.source, fd.raw_ref_ids_json
        FROM fitness_daily fd
        """,
    ).fetchall()
    for row in daily_rows:
        ref_ids = json.loads(row["raw_ref_ids_json"]) if row["raw_ref_ids_json"] else []
        if not ref_ids:
            continue
        # Cheap to do per-row when row count is small (≤365/year).
        placeholders = ",".join("?" * len(ref_ids))
        existing = {
            r["id"]
            for r in conn.execute(
                f"SELECT id FROM fitness_raw_garmin WHERE id IN ({placeholders})",
                ref_ids,
            ).fetchall()
        }
        missing = [rid for rid in ref_ids if rid not in existing]
        if missing:
            daily.append(
                DailyOrphan(
                    daily_id=row["id"],
                    source=row["source"],
                    missing_raw_ids=missing,
                ),
            )

    return IntegrityReport(activities=activities, daily=daily)
