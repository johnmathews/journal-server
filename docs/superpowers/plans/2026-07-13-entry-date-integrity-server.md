# Entry Date Integrity (Server) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enforce a hard entry-date floor, auto-repair year-off handwritten dates at ingest, quarantine unrepairable dates out of all pipelines, and make date edits propagate to ChromaDB metadata and affected storylines automatically.

**Architecture:** One new pure module (`services/entry_dates.py`) owns bounds + weekday repair. Ingestion orchestrators call it before `create_entry`; a new `entries.date_confirmed` column gates all derived processing; `update_entry_date` becomes a real service operation that refreshes vector metadata, and the PATCH handler queues storyline re-bootstraps via the existing Pool B job.

**Tech Stack:** Python 3.13, uv, pytest, SQLite (migrations via PRAGMA user_version), ChromaDB.

**Spec:** `docs/superpowers/specs/2026-07-13-entry-date-integrity-design.md` (approved 2026-07-13).

## Global Constraints

- `MIN_ENTRY_DATE` env var, ISO date string, default `2026-01-01`.
- Date ceiling is `today + 1 day` at validation time (server clock; prod runs CEST).
- Quarantined entries: row exists with provisional (invalid) date, `date_confirmed = 0`, **no** chunks/embeddings/mood/entity-extraction/storyline candidacy.
- Existing rows are all confirmed: column default is `1`.
- The reserved `storyline_panels_legacy` drop stays a separate later migration — do NOT fold it into 0037.
- TDD throughout: every task writes its failing test first. Run tests from inside `server/`.
- All new functions carry full type annotations.

---

### Task 1: Config setting + `entry_dates` module (validator, weekday finder, repair)

**Files:**
- Modify: `src/journal/config.py` (fields ~line 514 area; validation in `__post_init__` ~line 546)
- Create: `src/journal/services/entry_dates.py`
- Test: `tests/test_services/test_entry_dates.py`, `tests/test_config.py` (append)

**Interfaces:**
- Produces: `Config.min_entry_date: str`; `EntryDateError(ValueError)`; `validate_entry_date(date_iso: str, *, min_date: str, today: dt.date | None = None) -> None`; `find_weekday_token(text: str) -> tuple[str, tuple[int, int]] | None`; `DateRepairResult(status, date_iso, original, note)`; `repair_entry_date(date_iso: str, weekday: str | None, *, min_date: str, today: dt.date | None = None) -> DateRepairResult`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_services/test_entry_dates.py
"""Bounds validation + weekday auto-repair (spec 2026-07-13, components 1-2)."""
import datetime as dt

import pytest

from journal.services.entry_dates import (
    DateRepairResult,
    EntryDateError,
    find_weekday_token,
    repair_entry_date,
    validate_entry_date,
)

TODAY = dt.date(2026, 7, 13)
MIN = "2026-01-01"


class TestValidateEntryDate:
    def test_accepts_floor_and_today_plus_one(self) -> None:
        validate_entry_date("2026-01-01", min_date=MIN, today=TODAY)
        validate_entry_date("2026-07-14", min_date=MIN, today=TODAY)

    def test_rejects_below_floor(self) -> None:
        with pytest.raises(EntryDateError, match="2025-12-31"):
            validate_entry_date("2025-12-31", min_date=MIN, today=TODAY)

    def test_rejects_beyond_ceiling(self) -> None:
        with pytest.raises(EntryDateError):
            validate_entry_date("2026-07-15", min_date=MIN, today=TODAY)

    def test_rejects_malformed(self) -> None:
        with pytest.raises(EntryDateError):
            validate_entry_date("not-a-date", min_date=MIN, today=TODAY)


class TestFindWeekdayToken:
    def test_finds_weekday_with_span(self) -> None:
        token = find_weekday_token("Thursday 9 July 2025 9:40\n\nBody...")
        assert token is not None
        word, (start, end) = token
        assert word == "thursday"
        assert (start, end) == (0, 8)

    def test_none_when_absent(self) -> None:
        assert find_weekday_token("9 July 2025\n\nBody") is None

    def test_only_scans_head_of_text(self) -> None:
        assert find_weekday_token("x" * 300 + " Monday") is None


class TestRepairEntryDate:
    def test_incident_116_thursday_9_july(self) -> None:
        r = repair_entry_date("2025-07-09", "thursday", min_date=MIN, today=TODAY)
        assert r == DateRepairResult(
            status="repaired", date_iso="2026-07-09",
            original="2025-07-09", note="date auto-corrected from 2025-07-09",
        )

    def test_incident_112_monday_29_june(self) -> None:
        r = repair_entry_date("2025-06-29", "monday", min_date=MIN, today=TODAY)
        assert r.status == "repaired"
        assert r.date_iso == "2026-06-29"

    def test_in_range_matching_weekday_is_ok(self) -> None:
        # 2026-07-09 is a Thursday.
        r = repair_entry_date("2026-07-09", "thursday", min_date=MIN, today=TODAY)
        assert r.status == "ok" and r.date_iso == "2026-07-09"

    def test_in_range_mismatch_without_unique_candidate_is_doubtful(self) -> None:
        # 2026-07-09 is a Thursday, heading claims Monday; no year in
        # [2026, 2027] puts 9 July on a Monday (2026: Thu, 2027: Fri).
        r = repair_entry_date("2026-07-09", "monday", min_date=MIN, today=TODAY)
        assert r.status == "doubtful" and r.date_iso == "2026-07-09"

    def test_out_of_range_no_weekday_is_unrepairable(self) -> None:
        r = repair_entry_date("2025-07-09", None, min_date=MIN, today=TODAY)
        assert r.status == "unrepairable" and r.date_iso == "2025-07-09"

    def test_in_range_no_weekday_is_ok(self) -> None:
        r = repair_entry_date("2026-07-09", None, min_date=MIN, today=TODAY)
        assert r.status == "ok"

    def test_out_of_range_no_matching_year_is_unrepairable(self) -> None:
        # 2025-03-03 was a Monday; heading says Wednesday; 3 March is
        # Tue in 2026 and Wed in... 2027-03-03 is a Wednesday — so pick a
        # weekday matching NO candidate year: 2026: Tue, 2027: Wed → use
        # "friday" (matches neither).
        r = repair_entry_date("2025-03-03", "friday", min_date=MIN, today=TODAY)
        assert r.status == "unrepairable"

    def test_ambiguous_multiple_candidates_is_unrepairable(self) -> None:
        # Ceiling year included: with today=2026-12-30 the window spans
        # 2026-01-01..2026-12-31; single-year window can't be ambiguous,
        # so force ambiguity via a wide window: min 2026, today in 2032
        # gives repeated weekday/year alignments (e.g. 9 July is Thursday
        # in both 2026 and 2037 — outside; but 2026/2033 within a window
        # that wide). Use today=2033-07-13: 9 July is Thu in 2026 AND 2033.
        r = repair_entry_date(
            "2025-07-09", "thursday", min_date=MIN, today=dt.date(2033, 7, 13)
        )
        assert r.status == "unrepairable"

    def test_feb_29_candidate_years_skipped_safely(self) -> None:
        r = repair_entry_date("2025-02-29", "saturday", min_date=MIN, today=TODAY)
        assert r.status == "unrepairable"  # invalid original date, no crash
```

Append to `tests/test_config.py` (follow the file's existing style):

```python
def test_min_entry_date_default_and_env(monkeypatch):
    from journal.config import load_config

    monkeypatch.delenv("MIN_ENTRY_DATE", raising=False)
    assert load_config().min_entry_date == "2026-01-01"
    monkeypatch.setenv("MIN_ENTRY_DATE", "2026-03-01")
    assert load_config().min_entry_date == "2026-03-01"


def test_min_entry_date_invalid_rejected(monkeypatch):
    import pytest
    from journal.config import load_config

    monkeypatch.setenv("MIN_ENTRY_DATE", "March 2026")
    with pytest.raises(ValueError, match="MIN_ENTRY_DATE"):
        load_config()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_services/test_entry_dates.py tests/test_config.py -q`
Expected: FAIL — `ModuleNotFoundError: journal.services.entry_dates`, `AttributeError: min_entry_date`.

- [ ] **Step 3: Implement**

`src/journal/config.py` — add field next to the other date-string setting (`fitness_backfill_start`, ~line 514):

```python
min_entry_date: str = field(
    default_factory=lambda: os.environ.get("MIN_ENTRY_DATE", "2026-01-01")
)
```

In `__post_init__` (with the other validations, ~line 546):

```python
import datetime as _dt
try:
    _dt.date.fromisoformat(self.min_entry_date)
except ValueError as exc:
    raise ValueError(
        f"MIN_ENTRY_DATE must be an ISO date (YYYY-MM-DD), got {self.min_entry_date!r}"
    ) from exc
```

(If `config.py` already imports `datetime`, reuse that import.)

Create `src/journal/services/entry_dates.py`:

```python
"""Entry-date bounds and year-off auto-repair.

Handwritten headings sometimes carry the previous year ("Thursday 9 July
2025" written in July 2026 — entries 112/116 incidents). The weekday word
is a reliable cross-check: when the weekday contradicts the date, exactly
one nearby year usually makes it consistent. Spec:
docs/superpowers/specs/2026-07-13-entry-date-integrity-design.md.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Literal

_WEEKDAYS = [
    "monday", "tuesday", "wednesday", "thursday",
    "friday", "saturday", "sunday",
]
_WEEKDAY_RE = re.compile(
    r"\b(" + "|".join(_WEEKDAYS) + r")\b", re.IGNORECASE
)
# Weekday must appear near the top of the page to count as a heading token.
_HEAD_WINDOW = 200


class EntryDateError(ValueError):
    """An entry date outside the allowed [MIN_ENTRY_DATE, today+1] range."""


def _bounds(min_date: str, today: dt.date | None) -> tuple[dt.date, dt.date]:
    effective_today = today if today is not None else dt.date.today()
    return dt.date.fromisoformat(min_date), effective_today + dt.timedelta(days=1)


def validate_entry_date(
    date_iso: str, *, min_date: str, today: dt.date | None = None
) -> None:
    try:
        candidate = dt.date.fromisoformat(date_iso)
    except ValueError as exc:
        raise EntryDateError(
            f"'{date_iso}' is not a valid ISO 8601 date (YYYY-MM-DD)"
        ) from exc
    lower, upper = _bounds(min_date, today)
    if not (lower <= candidate <= upper):
        raise EntryDateError(
            f"entry date {date_iso} is outside the allowed range"
            f" {lower.isoformat()} – {upper.isoformat()}"
        )


def find_weekday_token(text: str) -> tuple[str, tuple[int, int]] | None:
    match = _WEEKDAY_RE.search(text[:_HEAD_WINDOW])
    if match is None:
        return None
    return match.group(1).lower(), match.span()


@dataclass(frozen=True)
class DateRepairResult:
    status: Literal["ok", "repaired", "doubtful", "unrepairable"]
    date_iso: str
    original: str
    note: str | None = None


def repair_entry_date(
    date_iso: str,
    weekday: str | None,
    *,
    min_date: str,
    today: dt.date | None = None,
) -> DateRepairResult:
    lower, upper = _bounds(min_date, today)
    try:
        candidate = dt.date.fromisoformat(date_iso)
    except ValueError:
        return DateRepairResult("unrepairable", date_iso, date_iso)
    in_range = lower <= candidate <= upper

    if weekday is None:
        status: Literal["ok", "unrepairable"] = "ok" if in_range else "unrepairable"
        return DateRepairResult(status, date_iso, date_iso)

    target = _WEEKDAYS.index(weekday.lower())
    if in_range and candidate.weekday() == target:
        return DateRepairResult("ok", date_iso, date_iso)

    matches: list[dt.date] = []
    for year in range(lower.year, upper.year + 1):
        try:
            shifted = candidate.replace(year=year)
        except ValueError:  # 29 Feb in a non-leap candidate year
            continue
        if lower <= shifted <= upper and shifted.weekday() == target:
            matches.append(shifted)

    if len(matches) == 1:
        repaired = matches[0].isoformat()
        return DateRepairResult(
            "repaired", repaired, date_iso,
            note=f"date auto-corrected from {date_iso}",
        )
    if in_range:
        return DateRepairResult("doubtful", date_iso, date_iso)
    return DateRepairResult("unrepairable", date_iso, date_iso)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_services/test_entry_dates.py tests/test_config.py -q`
Expected: PASS.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/ tests/
git add src/journal/config.py src/journal/services/entry_dates.py tests/test_services/test_entry_dates.py tests/test_config.py
git commit -m "feat(dates): MIN_ENTRY_DATE config + bounds validator + weekday auto-repair"
```

---

### Task 2: Enforce bounds on PATCH date edits and explicit ingestion dates (motivating bug #1)

**Files:**
- Modify: `src/journal/services/ingestion/service.py` (`__init__`; `update_entry_date` ~line 434; `ingest_text`)
- Modify: `src/journal/mcp_server/bootstrap.py` (where `IngestionService(...)` is constructed — pass `min_entry_date=config.min_entry_date`)
- Modify: `src/journal/api/entries.py` (`_patch_entry` date branch, lines 182–197)
- Test: `tests/test_api/test_entries_patch_boundary.py` (append), `tests/test_services/test_ingestion_text.py` (append)

**Interfaces:**
- Consumes: `validate_entry_date`, `EntryDateError` (Task 1).
- Produces: `IngestionService(..., min_entry_date: str = "2026-01-01")` stored as `self._min_entry_date`. `update_entry_date` raises `EntryDateError` on out-of-range dates (return type unchanged for now — Task 6 extends it). PATCH returns 400 with the validator's message.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_api/test_entries_patch_boundary.py`, following that file's existing client/fixture pattern (it already PATCHes entries — reuse its setup helpers):

```python
def test_patch_rejects_date_below_floor(client_and_entry):
    client, entry_id = client_and_entry  # adapt to the file's real fixture names
    resp = client.patch(f"/api/entries/{entry_id}", json={"entry_date": "2025-07-09"})
    assert resp.status_code == 400
    assert "allowed range" in resp.json()["error"]


def test_patch_rejects_far_future_date(client_and_entry):
    client, entry_id = client_and_entry
    resp = client.patch(f"/api/entries/{entry_id}", json={"entry_date": "2031-01-01"})
    assert resp.status_code == 400
```

Append to `tests/test_services/test_ingestion_text.py` (reuse its service fixture):

```python
def test_ingest_text_rejects_out_of_range_date(service):
    import pytest
    from journal.services.entry_dates import EntryDateError

    with pytest.raises(EntryDateError):
        service.ingest_text("some body text", date="2025-07-09")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_api/test_entries_patch_boundary.py tests/test_services/test_ingestion_text.py -q`
Expected: the new tests FAIL (PATCH currently returns 200; ingest_text accepts the date).

- [ ] **Step 3: Implement**

`service.py` — accept and store the floor (add keyword-only param to `__init__` with default `"2026-01-01"`, assign `self._min_entry_date = min_entry_date`). Wire the real value in `mcp_server/bootstrap.py` at the `IngestionService(` construction site: `min_entry_date=config.min_entry_date`.

`update_entry_date` (service.py:434) — validate before delegating:

```python
def update_entry_date(
    self, entry_id: int, entry_date: str, *, user_id: int | None = None,
) -> Entry | None:
    validate_entry_date(entry_date, min_date=self._min_entry_date)
    return self._repo.update_entry_date(entry_id, entry_date, user_id=user_id)
```

(import at top: `from journal.services.entry_dates import EntryDateError, validate_entry_date`)

`ingest_text` — validate the caller-supplied `date` the same way at the top of the method. The URL/text orchestrators funnel through it; voice/image detected dates are handled by Task 8/9 repair instead.

`api/entries.py` `_patch_entry` — wrap the `update_entry_date` call (line 197):

```python
try:
    updated = ingestion_svc.update_entry_date(entry_id, new_date, user_id=user_id)
except EntryDateError as exc:
    return JSONResponse({"error": str(exc)}, status_code=400)
```

(import `EntryDateError` at the function-local import block used by the handler, matching the file's lazy-import style at lines 49–51.)

- [ ] **Step 4: Run the full suite**

Run: `uv run pytest -q -m "not integration"`
Expected: PASS. If any existing fixture PATCHes/ingests old dates, set `MIN_ENTRY_DATE` (or pass `min_entry_date="2000-01-01"`) in that fixture rather than weakening the default.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/ tests/
git add -A src tests
git commit -m "feat(dates): enforce MIN_ENTRY_DATE on PATCH date edits and explicit ingest dates"
```

---

### Task 3: Migration 0037 `date_confirmed` + model + serialization

**Files:**
- Create: `src/journal/db/migrations/0037_entry_date_confirmed.sql`
- Modify: `src/journal/models.py:36` (Entry), `src/journal/db/repository/protocol.py` (`_row_to_entry` ~line 300), `src/journal/db/repository/core.py` (`create_entry` INSERT), `src/journal/api/_shared.py` (`_entry_to_dict` :87–130, `_entry_summary` :227–247)
- Test: `tests/test_db/test_migration_0037.py`, `tests/test_db/test_repository.py` (append)

**Interfaces:**
- Produces: `entries.date_confirmed INTEGER NOT NULL DEFAULT 1`; `Entry.date_confirmed: bool = True`; `create_entry(..., date_confirmed: bool = True)`; both serializers emit `"date_confirmed"`.

- [ ] **Step 1: Write the failing tests**

`tests/test_db/test_migration_0037.py` — copy the structure of `tests/test_db/test_migration_0036.py` (`_run_migrations_up_to` helper, `PRAGMA table_info` column assertions):

```python
# tests/test_db/test_migration_0037.py — reuse the helpers the sibling
# migration tests import (see tests/test_migration_0031_chapter_sectioning.py:20-60
# for _run_migrations_up_to / _columns; copy them or import if shared).
def test_0037_adds_date_confirmed_default_confirmed(tmp_path):
    conn = get_connection(tmp_path / "m.db")
    _run_migrations_up_to(conn, 36)
    conn.execute(
        "INSERT INTO users (id, email, password_hash) VALUES (1, 'a@b.c', 'x')"
    )
    conn.execute(
        "INSERT INTO entries (user_id, entry_date, source_type, raw_text, word_count)"
        " VALUES (1, '2026-07-01', 'photo', 'body', 1)"
    )
    conn.commit()

    _run_migrations_up_to(conn, 37)

    cols = {r[1] for r in conn.execute("PRAGMA table_info(entries)")}
    assert "date_confirmed" in cols
    row = conn.execute("SELECT date_confirmed FROM entries").fetchone()
    assert row[0] == 1  # pre-existing rows are confirmed
```

(Adapt the `users` INSERT columns to the real schema — copy whatever `test_migration_0036.py` inserts.)

Append to `tests/test_db/test_repository.py`:

```python
def test_create_entry_date_confirmed_flag(repo):
    confirmed = repo.create_entry("2026-07-01", "photo", "raw", 1, user_id=1)
    held = repo.create_entry(
        "2025-07-09", "photo", "raw", 1, user_id=1, date_confirmed=False
    )
    assert repo.get_entry(confirmed.id).date_confirmed is True
    assert repo.get_entry(held.id).date_confirmed is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_db/test_migration_0037.py tests/test_db/test_repository.py -q`
Expected: FAIL (no migration file; `create_entry` has no such kwarg).

- [ ] **Step 3: Implement**

`0037_entry_date_confirmed.sql`:

```sql
-- Quarantine flag for entries whose detected date failed bounds validation
-- and could not be auto-repaired (spec 2026-07-13). Existing rows are all
-- confirmed. NOTE: the storyline_panels_legacy drop is deliberately NOT in
-- this migration — it ships separately, later.
ALTER TABLE entries ADD COLUMN date_confirmed INTEGER NOT NULL DEFAULT 1;
```

`models.py` — after `doubts_verified` (line 36): `date_confirmed: bool = True`.
`protocol.py` `_row_to_entry` — mirror the `doubts_verified` mapping: `date_confirmed=bool(row["date_confirmed"])`.
`core.py` `create_entry` — add keyword-only `date_confirmed: bool = True`, include the column + `int(date_confirmed)` in the INSERT column/value lists.
`_shared.py` — add `"date_confirmed": entry.date_confirmed,` to BOTH `_entry_to_dict` and `_entry_summary`, adjacent to `doubts_verified`.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_db -q && uv run pytest -q -m "not integration"`
Expected: PASS (row-mapping tests across the suite exercise the new column).

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/ tests/
git add -A src tests
git commit -m "feat(db): entries.date_confirmed quarantine flag (migration 0037) + model/serialization"
```

---

### Task 4: `VectorStore.update_entry_metadata`

**Files:**
- Modify: `src/journal/vectorstore/store.py` (Protocol :30–51; ChromaDB impl; InMemory impl)
- Test: `tests/test_vectorstore/test_store.py` (append)

**Interfaces:**
- Produces: `def update_entry_metadata(self, entry_id: int, metadata: dict[str, str | int | float | bool]) -> None` on the Protocol and both implementations. Merges the given keys into every chunk of the entry; no-op when the entry has no chunks.

- [ ] **Step 1: Write the failing test** (in-memory store; follow the file's existing add/search test style)

```python
def test_update_entry_metadata_merges_into_all_chunks(store):
    store.add_entry(1, ["a", "b"], [[0.1] * 4, [0.2] * 4],
                    {"entry_date": "2025-07-09", "user_id": 1})
    store.update_entry_metadata(1, {"entry_date": "2026-07-09"})
    chunks = store.get_chunks_for_entry(1)
    assert all(c.metadata["entry_date"] == "2026-07-09" for c in chunks)
    assert all(c.metadata["user_id"] == 1 for c in chunks)  # untouched keys survive


def test_update_entry_metadata_missing_entry_is_noop(store):
    store.update_entry_metadata(999, {"entry_date": "2026-01-01"})  # no raise
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_vectorstore/test_store.py -q`
Expected: FAIL — `AttributeError: update_entry_metadata`.

- [ ] **Step 3: Implement**

Protocol addition (after `delete_entry`):

```python
def update_entry_metadata(
    self, entry_id: int, metadata: dict[str, str | int | float | bool]
) -> None: ...
```

ChromaDB implementation:

```python
def update_entry_metadata(
    self, entry_id: int, metadata: dict[str, str | int | float | bool]
) -> None:
    existing = self._collection.get(where={"entry_id": entry_id})
    ids = existing.get("ids") or []
    if not ids:
        return
    merged = [{**m, **metadata} for m in existing["metadatas"]]
    self._collection.update(ids=ids, metadatas=merged)
```

InMemory implementation: iterate its internal chunk records for `entry_id` and `record.metadata.update(metadata)` (match the class's actual storage attribute).

Also append an integration-marked test in `tests/integration/` mirroring Step 1 against real Chroma if the suite has a store integration file; skip if none exists (the in-memory + Protocol conformance tests are the gate).

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_vectorstore -q`
Expected: PASS.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/ tests/
git add -A src tests
git commit -m "feat(vectorstore): update_entry_metadata for in-place chunk metadata refresh"
```

---

### Task 5: `find_storyline_ids_for_entry` reverse lookup

**Files:**
- Modify: `src/journal/db/storyline_repository.py`
- Test: `tests/test_storyline_repository.py` (append)

**Interfaces:**
- Produces: `def find_storyline_ids_for_entry(self, entry_id: int) -> list[int]` — distinct storyline ids whose chapters contain the entry, ascending.

- [ ] **Step 1: Write the failing test** (reuse the file's existing fixtures that create storylines/chapters/memberships)

```python
def test_find_storyline_ids_for_entry(storyline_repo_with_data):
    repo = storyline_repo_with_data  # adapt: create 2 storylines, put entry 42 in a chapter of each, entry 43 in neither
    assert repo.find_storyline_ids_for_entry(42) == [1, 2]
    assert repo.find_storyline_ids_for_entry(43) == []
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_storyline_repository.py -q` → FAIL (`AttributeError`).

- [ ] **Step 3: Implement** (uses the existing `idx_storyline_chapter_entries_entry` index)

```python
def find_storyline_ids_for_entry(self, entry_id: int) -> list[int]:
    """Distinct storylines whose chapters (draft or published) contain
    the entry — the reverse lookup used by date-edit propagation."""
    rows = self._conn().execute(
        "SELECT DISTINCT c.storyline_id"
        " FROM storyline_chapter_entries ce"
        " JOIN storyline_chapters c ON c.id = ce.chapter_id"
        " WHERE ce.entry_id = ?"
        " ORDER BY c.storyline_id ASC",
        (entry_id,),
    ).fetchall()
    return [int(r["storyline_id"]) for r in rows]
```

- [ ] **Step 4: Run tests** → PASS.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/ tests/
git add -A src tests
git commit -m "feat(storylines): reverse lookup find_storyline_ids_for_entry"
```

---

### Task 6: Service-level date-edit propagation (motivating bug #2)

**Files:**
- Modify: `src/journal/services/ingestion/service.py` (`update_entry_date`), `src/journal/db/repository/core.py` (new `set_date_confirmed`), `src/journal/db/repository/protocol.py` (protocol entries)
- Modify: `src/journal/api/entries.py:197` (unpack the new return)
- Test: `tests/test_services/test_ingestion.py` (append), `tests/test_db/test_repository.py` (append)

**Interfaces:**
- Consumes: `update_entry_metadata` (Task 4), `date_confirmed` (Task 3), validator (Task 1).
- Produces: `update_entry_date(self, entry_id, entry_date, *, user_id=None) -> tuple[Entry | None, bool]` — `(updated_entry, released)`; `released` is True when the entry was quarantined and this edit confirmed it. Repo: `set_date_confirmed(self, entry_id: int, user_id: int | None = None) -> None`.

- [ ] **Step 1: Write the failing test** — this is the bug that bit on 2026-07-13: a date edit left ChromaDB `entry_date` metadata stale.

```python
# tests/test_services/test_ingestion.py (append; use the file's existing
# service fixture that wires an InMemoryVectorStore)
def test_update_entry_date_refreshes_chunk_metadata(service, vector_store):
    entry = service.ingest_text("hello world body", date="2026-07-01")
    updated, released = service.update_entry_date(entry.id, "2026-07-02")
    assert updated is not None and released is False
    chunks = vector_store.get_chunks_for_entry(entry.id)
    assert chunks and all(c.metadata["entry_date"] == "2026-07-02" for c in chunks)


def test_update_entry_date_releases_quarantined_entry(service, repo):
    held = repo.create_entry("2025-07-09", "photo", "raw", 1,
                             user_id=1, date_confirmed=False)
    updated, released = service.update_entry_date(held.id, "2026-07-09", user_id=1)
    assert released is True
    assert repo.get_entry(held.id, 1).date_confirmed is True
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_services/test_ingestion.py -q`
Expected: FAIL — current method returns a bare `Entry | None` and never touches the vector store.

- [ ] **Step 3: Implement**

Repo (`core.py`, mirror `verify_doubts` at `pages.py:124`):

```python
def set_date_confirmed(self, entry_id: int, user_id: int | None = None) -> None:
    conn = self._conn()
    with conn:
        if user_id is not None:
            conn.execute(
                "UPDATE entries SET date_confirmed = 1 WHERE id = ? AND user_id = ?",
                (entry_id, user_id),
            )
        else:
            conn.execute(
                "UPDATE entries SET date_confirmed = 1 WHERE id = ?", (entry_id,)
            )
```

Service:

```python
def update_entry_date(
    self, entry_id: int, entry_date: str, *, user_id: int | None = None,
) -> tuple[Entry | None, bool]:
    validate_entry_date(entry_date, min_date=self._min_entry_date)
    prior = self._repo.get_entry(entry_id, user_id)
    if prior is None:
        return None, False
    self._repo.update_entry_date(entry_id, entry_date, user_id=user_id)
    released = not prior.date_confirmed
    if released:
        self._repo.set_date_confirmed(entry_id, user_id=user_id)
    if prior.chunk_count > 0:
        self._vector_store.update_entry_metadata(
            entry_id, {"entry_date": entry_date}
        )
    return self._repo.get_entry(entry_id, user_id), released
```

Update the one existing caller (`api/entries.py:197`): `updated, released = ingestion_svc.update_entry_date(...)` — hold `released` for Task 7; for now it may be unused (`_released`). Fix any tests that call the old single-value signature.

- [ ] **Step 4: Run the full suite**

Run: `uv run pytest -q -m "not integration"` → PASS.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/ tests/
git add -A src tests
git commit -m "fix(dates): date edits refresh Chroma metadata and release quarantined entries"
```

---

### Task 7: API orchestration — storyline re-bootstrap + release pipeline

**Files:**
- Modify: `src/journal/api/entries.py` (`_patch_entry` date branch + response payload :329–336)
- Test: `tests/test_api/test_entries_patch_boundary.py` or `tests/test_api.py` (append, following how existing PATCH tests fake `job_runner`)

**Interfaces:**
- Consumes: `(entry, released)` (Task 6), `find_storyline_ids_for_entry` (Task 5), `JobRunner.submit_storyline_update(storyline_id, *, user_id, bootstrap=True)` (existing, runner.py:412), `submit_save_entry_pipeline` (existing, entries.py:301).
- Produces: PATCH response gains `storyline_bootstrap_job_ids: list[str]`; a released quarantined entry also gets the standard `pipeline_job_id` fields.

- [ ] **Step 1: Write the failing tests**

```python
def test_patch_date_queues_bootstrap_for_affected_storylines(client_with_storyline):
    # fixture: entry 1 is a member of storyline 4's chapter; fake job_runner records calls
    client, fake_runner = client_with_storyline
    resp = client.patch("/api/entries/1", json={"entry_date": "2026-07-02"})
    assert resp.status_code == 200
    assert fake_runner.storyline_update_calls == [
        {"storyline_id": 4, "user_id": 1, "bootstrap": True}
    ]
    assert resp.json()["storyline_bootstrap_job_ids"]


def test_patch_date_release_queues_save_pipeline(client_with_quarantined_entry):
    client, fake_runner = client_with_quarantined_entry
    resp = client.patch("/api/entries/1", json={"entry_date": "2026-07-09"})
    assert resp.status_code == 200
    assert resp.json()["date_confirmed"] is True
    assert fake_runner.save_pipeline_calls  # release runs chunk/embed/extract


def test_patch_date_no_storylines_no_jobs(client_and_entry_no_jobs):
    client, fake_runner = client_and_entry_no_jobs
    resp = client.patch("/api/entries/1", json={"entry_date": "2026-07-02"})
    assert resp.status_code == 200
    assert resp.json()["storyline_bootstrap_job_ids"] == []
```

Build the fixtures on the file's existing PATCH-test scaffolding (it already fakes services; extend the fake job runner with `submit_storyline_update` recording, and register a real `SQLiteStorylineRepository` over the test DB with one chapter membership row).

- [ ] **Step 2: Run to verify failure** — FAIL (no `storyline_bootstrap_job_ids` key; no jobs queued).

- [ ] **Step 3: Implement** — in `_patch_entry` after a successful date update:

```python
storyline_bootstrap_job_ids: list[str] = []
if released:
    should_queue_pipeline = True  # reuses the existing block at :292-323
job_runner = services.get("job_runner")
storyline_repo = services.get("storyline_repository")
if updated is not None and job_runner is not None and storyline_repo is not None:
    for sid in storyline_repo.find_storyline_ids_for_entry(entry_id):
        try:
            job = job_runner.submit_storyline_update(
                sid, user_id=user_id, bootstrap=True,
            )
        except RuntimeError:  # storyline engine not configured
            break
        storyline_bootstrap_job_ids.append(job.id)
```

Add `"storyline_bootstrap_job_ids": storyline_bootstrap_job_ids` to the response dict (:329–336). Duplicate-suppression across requests is deliberately NOT implemented (date edits are rare; Pool B is single-worker; bootstrap is idempotent) — note this in the docstring.

- [ ] **Step 4: Run the full suite** → `uv run pytest -q -m "not integration"` PASS.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/ tests/
git add -A src tests
git commit -m "feat(dates): PATCH date edits auto-queue storyline re-bootstraps; release queues save pipeline"
```

---

### Task 8: Image-ingest repair gate + quarantine creation

**Files:**
- Modify: `src/journal/services/ingestion/image.py` (`_ingest_pages`, after date resolution at :144–147, gate at :166–171)
- Modify: `src/journal/services/jobs/workers/image_ingestion.py:96` (skip post-ingestion jobs for quarantined entries)
- Test: `tests/test_services/test_ingestion.py` (append), `tests/test_services/test_jobs/` (worker test, append to the image worker's test file)

**Interfaces:**
- Consumes: `find_weekday_token`, `repair_entry_date` (Task 1); `create_entry(..., date_confirmed=)` (Task 3).
- Produces: quarantined entries have `date_confirmed=False`, `chunk_count == 0`, an uncertain span over the heading's weekday+date region; repaired entries carry the corrected date + the same reviewable span.

- [ ] **Step 1: Write the failing tests** — compute incident-shaped dates relative to the real clock so the tests never rot:

```python
import calendar
import datetime as dt


def _year_off_heading() -> tuple[str, str, str]:
    """Return (heading, wrong_iso, correct_iso) where the weekday word
    matches TODAY's year but the written year is last year."""
    today = dt.date.today()
    correct = today - dt.timedelta(days=7)
    wrong = correct.replace(year=correct.year - 1)
    weekday = calendar.day_name[correct.weekday()]
    heading = f"{weekday} {correct.day} {calendar.month_name[correct.month]} {wrong.year} 9:40"
    return heading, wrong.isoformat(), correct.isoformat()


def test_image_ingest_repairs_year_off_heading(image_service, repo):
    heading, _wrong, correct = _year_off_heading()
    # fixture: fake OCR returns f"{heading}\n\nBody text..." (follow the
    # file's existing fake-OCR pattern)
    entry = image_service.ingest_image(b"...", "page.jpg", user_id=1)
    assert entry.entry_date == correct
    assert repo.get_uncertain_spans(entry.id)  # reviewable audit marker
    assert entry.date_confirmed is True
    assert entry.chunk_count > 0  # processed normally


def test_image_ingest_quarantines_unrepairable_date(image_service, repo):
    # heading with an out-of-range date and NO weekday word
    entry = image_service.ingest_image(b"...", "page.jpg", user_id=1)  # OCR: "9 July 2019\n\nBody"
    assert entry.date_confirmed is False
    assert entry.entry_date == "2019-07-09"  # provisional display value
    assert entry.chunk_count == 0  # no derived data


def test_worker_skips_post_ingestion_jobs_for_quarantined_entry(worker_harness):
    # Follow the image-worker test file's existing harness (fake ctx with a
    # recording queue_post_ingestion_jobs; fake ingestion service returning
    # an Entry with date_confirmed=False). Assert:
    ctx, run_worker = worker_harness
    run_worker(entry_date_confirmed=False)
    assert ctx.post_ingestion_calls == []


def test_worker_queues_post_ingestion_jobs_for_confirmed_entry(worker_harness):
    ctx, run_worker = worker_harness
    run_worker(entry_date_confirmed=True)
    assert len(ctx.post_ingestion_calls) == 1
```

- [ ] **Step 2: Run to verify failure** — FAIL (entry keeps the wrong year; no quarantine flag).

- [ ] **Step 3: Implement**

`image.py`, immediately after the existing date resolution (:144–147):

```python
weekday_token = find_weekday_token(content)
repair = repair_entry_date(
    date,
    weekday_token[0] if weekday_token else None,
    min_date=self._min_entry_date,
)
date = repair.date_iso
quarantined = repair.status == "unrepairable"
if repair.status in ("repaired", "doubtful") and weekday_token is not None:
    # Mark the heading (weekday token → end of its line) as a reviewable
    # doubt so the UI highlights the suspicious/corrected date.
    line_end = content.find("\n", weekday_token[1][1])
    span_end = line_end if line_end != -1 else len(content)
    window.spans.append((weekday_token[1][0], span_end))
if repair.status == "repaired":
    log.info(
        "Entry date auto-corrected %s -> %s (%s)",
        repair.original, repair.date_iso, filename,
    )
```

(`window.spans` is the uncertain-span list persisted at :166 — match the actual local variable name at that site.)

Pass the flag into creation (:150) — `date_confirmed=not quarantined` — and gate derived processing (:166–171):

```python
self._repo.add_uncertain_spans(entry.id, window.spans)
if not quarantined:
    chunk_count = self._process_text(...)   # unchanged existing call
    self._repo.update_chunk_count(entry.id, chunk_count)
else:
    log.warning(
        "Entry %d quarantined: date %s failed bounds and repair; held from pipelines",
        entry.id, repair.date_iso,
    )
```

`image_ingestion.py:96`:

```python
if entry_id is not None and entry.date_confirmed:
    ctx.queue_post_ingestion_jobs(job_id, "Image", entry.id, resolved_user_id)
```

(adapt to the worker's actual local names; include a job-result note like `"quarantined": True` if the worker builds a result dict.)

- [ ] **Step 4: Run tests** → `uv run pytest tests/test_services -q` then full suite. PASS.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/ tests/
git add -A src tests
git commit -m "feat(ingest): weekday auto-repair + quarantine gate on image ingestion"
```

---### Task 9: Voice-ingest repair gate

**Files:**
- Modify: `src/journal/services/ingestion/voice.py` (both date-resolution sites: :62–88 and :202–217)
- Test: `tests/test_services/test_ingestion.py` or the voice tests' home (follow where voice ingest is tested; `grep -rn "ingest_voice" tests/`)

Same change as Task 8 applied to both voice call sites (they follow the identical `extract_date_from_text` → `_detect_heading` → `create_entry` shape). Voice transcripts rarely carry weekday headings, so the common quarantine path here is "out-of-range, no weekday → unrepairable".

- [ ] **Step 1: Write failing tests** — one repaired case (transcript starting with a year-off weekday heading, built with the `_year_off_heading()` helper from Task 8 — move it to a shared `tests/test_services/conftest.py` helper), one quarantine case (out-of-range date, no weekday: entry created `date_confirmed=False`, `chunk_count == 0`).
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement** — identical block to Task 8 at both sites; extract a small shared helper `_apply_date_repair(content, date, spans) -> tuple[str, bool]` on the ingestion service if the duplication across image/voice exceeds ~15 lines (DRY, but only after the third copy — image, voice site 1, voice site 2 — exists).
- [ ] **Step 4: Run the full suite** → PASS.
- [ ] **Step 5: Commit**

```bash
git add -A src tests
git commit -m "feat(ingest): date repair + quarantine gate on voice ingestion"
```

---

### Task 10: Storyline candidacy excludes unconfirmed entries (defense in depth)

**Files:**
- Modify: `src/journal/db/storyline_repository.py` (`find_entries_mentioning` :597 — the WHERE at :620–621)
- Test: `tests/test_storyline_repository.py` (append)

- [ ] **Step 1: Write the failing test**

```python
def test_find_entries_mentioning_excludes_unconfirmed(storyline_repo, entry_repo):
    entry_repo.create_entry("2026-07-01", "photo", "Atlas played football", 4, user_id=1)
    entry_repo.create_entry("2019-07-01", "photo", "Atlas at the beach", 4,
                            user_id=1, date_confirmed=False)
    hits = storyline_repo.find_entries_mentioning(1, "Atlas")
    assert len(hits) == 1
```

- [ ] **Step 2: Run to verify failure** — FAIL (both rows returned).

- [ ] **Step 3: Implement** — add `AND date_confirmed = 1` to the WHERE clause at :620–621.

- [ ] **Step 4: Run tests** → PASS.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/ tests/
git add -A src tests
git commit -m "feat(storylines): candidate scan excludes unconfirmed-date entries"
```

---

### Task 11: Docs + journal + full verification

**Files:**
- Create: `docs/entry-date-integrity.md` (living reference: the four components, env var, quarantine semantics, release flow, operational notes)
- Create: `journal/260713-entry-date-integrity.md` (dated entry: incidents, decisions incl. "quarantine over reject", links to spec/plan)
- Modify: `docs/storylines.md` (one paragraph: date edits auto-queue re-bootstraps; candidates require `date_confirmed`)

- [ ] **Step 1: Write the docs** — living reference structured like `docs/storylines.md` (short sections, exact env var table row for `MIN_ENTRY_DATE`).
- [ ] **Step 2: Full local verification**

```bash
uv run pytest -q            # all unit tests
uv run pytest --cov -q -m "not integration"
uv run ruff check src/ tests/
```

- [ ] **Step 3: Commit, push, watch CI**

```bash
git add docs journal
git commit -m "docs: entry-date integrity reference + journal entry"
git push
gh run watch --exit-status
```

---

## Self-review notes

- Spec coverage: component 1 → Tasks 1–2; component 2 → Tasks 1, 8, 9; component 3 → Tasks 3, 8, 9, 10 (+ release in 6–7); component 4 → Tasks 4–7. Webapp badge/confirm → separate webapp plan.
- Deviation from spec (approved rationale in Task 7): cross-request job coalescing dropped as YAGNI — `find_pending_storyline_update` only matches plain updates, Pool B is single-worker, bootstrap is idempotent, and date edits are rare.
- The `doubtful` status (in-range date, contradicting weekday, no unique repair) keeps the date and only records the reviewable span — it does NOT quarantine.
