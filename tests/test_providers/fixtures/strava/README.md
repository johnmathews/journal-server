# Strava fixtures

**FIXTURE SOURCE: hand-crafted; replace at W13.**

Until P0.1 is exercised end-to-end (W13 — first live smoke test), there is no real
recorded response we can use. The JSON in this directory is hand-written to match the
shape of `stravalib.model.SummaryActivity` (Pydantic 2.x) field-for-field. At W13,
record one anonymised real-account response per file and replace these.

If a test fails after the W13 swap, treat it as a real bug — the hand-crafted fixture
diverging from the real API shape is exactly the failure mode this discipline catches.
