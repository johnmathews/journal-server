# Garmin fixtures

**FIXTURE SOURCE: hand-crafted; replace at W13.**

Until P0.2 + W6 are exercised end-to-end (W13 — first live smoke test), there is no
real recorded response we can use. The JSON in this directory is hand-written to
match the shape of what `garminconnect`'s endpoint methods actually return — based
on inspection of the SDK source at version 0.3.3 — field for field. At W13, record
one anonymised real-account response per file (one per endpoint) and replace these.

If a test fails after the W13 swap, treat it as a real bug — the hand-crafted
fixture diverging from the real API shape is exactly the failure mode this
discipline catches. (Same convention as `../strava/README.md`.)

## Field-extraction contract (what the adapter reads from each payload)

The fixture shapes are deliberately the subset the adapter actually consumes —
nothing more. Whole payloads are still preserved as `raw_payloads_per_endpoint`,
so adding more extracted fields later doesn't require fixture changes.

| Source endpoint | Adapter field | Path inside payload |
| --- | --- | --- |
| `get_sleep_data` | `sleep_score` | `dailySleepDTO.sleepScores.overall.value` |
| `get_sleep_data` | `sleep_duration_s` | `dailySleepDTO.sleepTimeSeconds` |
| `get_sleep_data` | `sleep_efficiency_pct` | `dailySleepDTO.sleepEfficiencyPercentage` |
| `get_sleep_data` | `resting_hr_bpm` | `restingHeartRate` (top-level) |
| `get_hrv_data` | `hrv_overnight_ms` | `hrvSummary.lastNightAvg` |
| `get_body_battery` | `body_battery_high` | `[0].charged` (max charge over the day) |
| `get_body_battery` | `body_battery_low` | `[0].drained` (max drain over the day) |
| `get_stress_data` | `stress_avg` | `avgStressLevel` |
| `get_training_status` | `training_load_acute` | `mostRecentTrainingLoadBalance.metricsTrainingLoadAcute` |
| `get_training_status` | `training_load_chronic` | `mostRecentTrainingLoadBalance.metricsTrainingLoadChronic` |
| `get_training_readiness` | `training_readiness` | `[0].score` |
| `get_activities_by_date` | each list element → `GarminActivitySummary` | see `list_activities_response.json` |
