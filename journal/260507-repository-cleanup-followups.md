# 2026-05-07 — Repository follow-ups: legacy method deletion + transaction standardisation

Two small, mechanical follow-ups to the round-3 db/repository.py
split. Both were filed as future work by the split's planning doc;
landing them together as a single worktree branch
(`eng-repo-cleanup`).

## What landed

1. **Delete `add_people` / `add_places` / `add_tags`** (`a9cb1ad`).
   Legacy entity-attach methods on `EntryRepository` that predate
   the modern `entitystore/` package. Verified by grep that no
   production code calls them — only their own three test methods
   in `TestPeopleAndPlaces`. Removed:
   - 3 implementations in `db/repository/core.py`
   - 3 Protocol declarations in `db/repository/protocol.py`
   - 3 test methods (the entire `TestPeopleAndPlaces` class) in
     `tests/test_db/test_repository.py`
   - The "legacy entity-attach methods" paragraph in `core.py`'s
     module docstring.

   The underlying SQLite tables (`people`, `places`, `tags`,
   `entry_people`, `entry_places`, `entry_tags`) are left in
   place — the schema can keep historical data without supporting
   new writes. Tests dropped 1799 → 1796.

2. **Standardise on context-manager transactions** (`12acee5`).
   The split was an explicit byte-for-byte structural move and
   carried forward the inconsistent transaction style: most write
   methods used `self._conn.execute(...); self._conn.commit()`
   while four (`replace_chunks`, `replace_mood_scores`,
   `add_uncertain_spans`, `verify_doubts`) already used
   `with self._conn:`. This commit closes the gap.

   Eight methods converted to the context-manager form:
   - `chunks.py: update_chunk_count`
   - `core.py: create_entry`, `update_final_text`,
     `update_entry_date`, `delete_entry`
   - `mood.py: add_mood_score`, `prune_retired_mood_scores`
     (two-branch case)
   - `pages.py: add_entry_page`

   After this commit, there are zero `self._conn.commit()` calls
   in the repository package.

## Why context manager is better than bare commit

`self._conn.execute(...); self._conn.commit()` silently leaves a
half-applied transaction open if `execute()` raises mid-call —
SQLite's autocommit semantics depend on whether you're in
"deferred", "immediate", or "exclusive" mode, and Python's
`sqlite3` module wraps writes in implicit transactions by default.
The exception-then-no-commit case means the next call on the same
connection inherits an open transaction it didn't open.

`with self._conn:` makes the boundary explicit: commit on clean
exit, rollback on exception. It also matches the pattern already
in use elsewhere in the package, removing one source of "is this
write atomic in failure?" ambiguity.

Note: this does **not** fix the round-2 item-1.1 cross-call
shared-connection race. That race is between threads holding
overlapping references to the same cursor; the context manager
only governs commit/rollback semantics within a single method
call. Reopen criteria for item 1.1 unchanged.

## Notable detail: line-length fallout

Two SQL strings in `core.py` (`update_final_text` and
`update_entry_date`) had to be split across an additional
concatenated string literal because the new 4-space indent under
`with self._conn:` pushed them past the 100-char ruff cap (from
99 → 103). Added a third concatenation point at the
`updated_at = strftime(...)` / `WHERE id = ?` boundary. Still
reads naturally.

## Cursor lifetime under context manager

Methods like `create_entry` (uses `cursor.lastrowid`) and
`delete_entry` (uses `cursor.rowcount`) keep the cursor reference
live across the `with self._conn:` exit:

```python
with self._conn:
    cursor = self._conn.execute(sql, params)
return cursor.lastrowid
```

The context manager only governs the implicit transaction —
the connection stays open, the cursor stays valid. Same pattern
as the existing `replace_chunks` method.

## Files touched

- `src/journal/db/repository/core.py` — 3 deleted methods, 5
  transaction conversions, 2 line-length fixes, docstring tidy.
- `src/journal/db/repository/protocol.py` — 3 deleted Protocol
  declarations.
- `src/journal/db/repository/chunks.py` — 1 conversion.
- `src/journal/db/repository/mood.py` — 2 conversions.
- `src/journal/db/repository/pages.py` — 1 conversion.
- `tests/test_db/test_repository.py` — TestPeopleAndPlaces
  class deleted.

## Standing facts after these commits

- 1796 unit tests + 8 integration tests pass (was 1799 + 8;
  dropped 3 deleted tests).
- `db/repository/` package: largest file is `stats.py` at 357
  lines (unchanged); `core.py` shrank from 185 → 145.
- Reach-in gates: api 0, tests 37 (unchanged).
- Zero `self._conn.commit()` calls anywhere in the repository
  package.

## What round 3 + follow-ups now looks like

Round 3's five recommendations are all closed (RESOLVED ×3,
deferred ×2). The two filed follow-ups from the repository split
are now also closed. Remaining natural follow-ups:

1. **`api/entities.py` split** (item-6 exception, ~2–3 hours).
   Already sketched in `journal/260507-api-py-split-unit-1a.md`.
2. **`auth_api.py`** (840 lines, item-6 exception). No planning
   work yet.
3. **`services/entity_extraction/service.py`** (808 lines,
   item-6 exception). Would need an `ExtractionContext` refactor
   per `journal/260507-unit-2-entity-extraction-split.md`.

None of these are urgent. The reach-in grep gate catches
regressions in the meantime.
