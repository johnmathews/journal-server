# 1. Garmin acute/chronic training load — wrong payload path

**Date:** 2026-07-17
**Branch:** `fix/garmin-training-load-acute`
**Scope:** server only (webapp already reads the field correctly)

## 1.1 Symptom

The "Training load vs. how I feel" chart on `/fitness` (`MoodFitnessChart.vue`)
has a "Training load (acute)" series that rendered **empty**. The frontend
requests and plots `training_load_acute` correctly; `spanGaps: true` means an
all-NULL series simply draws no line while the other series still show.

Confirmed against the live DB: `fitness_daily.training_load_acute` (and
`training_load_chronic`) were **NULL for 100% of days** — not intermittent
missing data, which pointed at a systematic extraction bug rather than a sync
gap.

## 1.2 Root cause

Both extraction sites read the acute/chronic load from the **wrong key** in
Garmin's `get_training_status` response:

```python
tlb = training_status.get("mostRecentTrainingLoadBalance") or {}
training_load_acute = _float_or_none(tlb.get("metricsTrainingLoadAcute"))
```

`mostRecentTrainingLoadBalance` exists but carries only *monthly*
aerobic/anaerobic balance (`metricsTrainingLoadBalanceDTOMap.<deviceId>.monthly*`).
It has no `metricsTrainingLoadAcute` key, so `.get(...)` → `None` every day.

The real values (verified from a live anonymised capture) live at:

```
mostRecentTrainingStatus
  .latestTrainingStatusData.<deviceId>.acuteTrainingLoadDTO
    .dailyTrainingLoadAcute    = 732
    .dailyTrainingLoadChronic  = 667
```

`latestTrainingStatusData` is keyed by device id and can hold more than one
device; the primary is flagged `primaryTrainingDevice: true`.

### 1.2.1 Why tests stayed green

The wrong path was baked into a **hand-crafted** fixture
(`tests/test_providers/fixtures/garmin/training_status.json`) whose README
explicitly said *"hand-crafted; replace at W13… If a test fails after the W13
swap, treat it as a real bug."* The W13 fixture swap never happened for this
endpoint (fitness verification stalled on the Strava paywall, 2026-07-13), so
code and fixture shared the same wrong assumption and the test passed against a
payload Garmin never actually sends.

## 1.3 Fix

- New single-source-of-truth helper `extract_training_load(training_status)` in
  `providers/garmin.py`: walks the real path, prefers the primary device, falls
  back to any device with an `acuteTrainingLoadDTO`, preserves a real `0` load
  (rest days report `dailyTrainingLoadAcute == 0`), returns `(None, None)` when
  absent.
- Both call sites now use it: `garmin.py:get_daily` (live sync) and
  `services/fitness/normalize.py:_garmin_daily_from_raws` (raw-payload
  re-normalise / backfill). Centralising kills the duplicated-logic trap that
  caused this bug.
- `training_status.json` replaced with a real anonymised capture (`userId` /
  `deviceId` scrubbed); README source note + extraction-contract table updated.
- Failing-test-first proof: with old `normalize.py` + the real-structure
  fixture, `training_load_acute` comes back `None` (reproduces prod); with the
  fix, `412.0`. Added focused `extract_training_load` unit tests (primary-device
  preference, zero-load preservation, fallback, and the old wrong path now
  yielding `(None, None)`).

## 1.4 Data recovery

The raw `get_training_status` payloads are archived in `fitness_raw_garmin`
(223 payloads, all with an extractable acute value going back to 2026-01-01),
so history can be fully repopulated by a **force re-normalise** — no re-fetch
from Garmin needed:

```bash
docker exec journal-server uv run python -c "
from journal.config import Config
from journal.db.connection import get_connection
from journal.db.fitness_repository import FitnessRepository
from journal.services.fitness.normalize import normalize_garmin
cfg = Config(); conn = get_connection(cfg.db_path)
repo = FitnessRepository(conn)
print(normalize_garmin(repo, user_id=1, since='2000-01-01'))
"
```

`since` bypasses the watermark; `INSERT OR REPLACE` makes it idempotent and it
only re-derives the same values for every other daily field.

## 1.5 Status

Code + tests + docs done, full unit suite green (3262 passed), ruff clean.
Remaining: deploy `journal-server` to prod and run the backfill above, then the
chart's acute series populates (no webapp change required).
