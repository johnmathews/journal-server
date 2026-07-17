# Garmin fixtures

**FIXTURE SOURCE: mostly hand-crafted; `training_status.json` swapped for a
real anonymised capture (2026-07-17).**

Most JSON here is still hand-written to match the shape of what `garminconnect`'s
endpoint methods return — based on inspection of the SDK source at version 0.3.3 —
field for field. The remaining hand-crafted files should each be replaced with one
anonymised real-account response as the endpoints are exercised end-to-end.

`training_status.json` **has** been replaced: it is a real (anonymised — `userId`
and `deviceId` scrubbed) capture of `get_training_status`. The hand-crafted version
guessed the acute/chronic load lived under
`mostRecentTrainingLoadBalance.metricsTrainingLoad{Acute,Chronic}`, but the real
response nests them under
`mostRecentTrainingStatus.latestTrainingStatusData.<deviceId>.acuteTrainingLoadDTO`
as `dailyTrainingLoad{Acute,Chronic}`. That divergence silently produced NULL
training load for every synced day until it was caught — exactly the failure mode
this discipline warns about.

If a test fails after a fixture swap, treat it as a real bug — the hand-crafted
fixture diverging from the real API shape is exactly what this discipline catches.
(Same convention as `../strava/README.md`.)

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
| `get_training_status` | `training_load_acute` | `mostRecentTrainingStatus.latestTrainingStatusData.<deviceId>.acuteTrainingLoadDTO.dailyTrainingLoadAcute` |
| `get_training_status` | `training_load_chronic` | `mostRecentTrainingStatus.latestTrainingStatusData.<deviceId>.acuteTrainingLoadDTO.dailyTrainingLoadChronic` |
| `get_training_readiness` | `training_readiness` | `[0].score` |
| `get_activities_by_date` | each list element → `GarminActivitySummary` | see `list_activities_response.json` |

## `raw_payloads_per_endpoint` keys

The `GarminDailyMetrics.raw_payloads_per_endpoint` dict keys also appear as the
`endpoint` column value in `fitness_raw_garmin`, so they must match the schema's
CHECK constraint (`sleep, hrv, body_battery, training_load, training_readiness,
stress, activities, activity_detail`). Notably the dict key for the
`get_training_status` payload is `training_load` (not `training_status`) — the
schema's terminology takes precedence because it's load-bearing for inserts.
