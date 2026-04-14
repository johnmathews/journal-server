# Phase 2: Health Endpoint & Web Dashboard

> **Superseded 2026-04-11 by [`roadmap.md`](./roadmap.md).** The `/health` endpoint and dashboard plans described below
> are now Tier 1 items 2 and 3 in the consolidated roadmap. Kept for history; do not add new work here.

## 1. Health & Stats Endpoint

Add a `/health` endpoint to the MCP server (or a parallel HTTP server) that returns detailed operational statistics.

### Ingestion Stats

- Total entries ingested (all time, last 7d, last 30d)
- Entries by source type (image vs voice)
- Average words per entry
- Last ingestion timestamp
- Chunking stats: total chunks, avg chunks per entry, avg tokens per chunk
- ChromaDB collection size and status
- SQLite database size, row counts

### Query & Usage Stats

- Total queries served (all time, last 7d, last 30d)
- Queries by type (semantic search, date lookup, statistics, mood, topic frequency)
- Average query latency (p50, p95, p99)
- Most frequent search terms / topics
- Cache hit rates (if caching is added)
- Uptime and last restart timestamp

### Format

- JSON response at `GET /health`
- Include a `status` field (`ok` / `degraded` / `error`) with checks for SQLite connectivity, ChromaDB connectivity, and
  API key validity

---

## 2. Web Dashboard

A browser-based dashboard for visualising journal trends over time. Served from the same process or as a lightweight
companion app.

### Core Charts

#### Happiness Over Time

- Infer a happiness score (0-10) from each journal entry using sentiment analysis or LLM-based scoring
- Bin entries by week (configurable: day / week / month)
- Plot average happiness per bin as a line chart over a selectable time range (default: last 12 months)
- Show confidence bands or entry count per bin to indicate data density

#### People Mentions Over Time

- Extract key people mentioned in entries (proper nouns, recurring names)
- Plot mentions per person per week as a stacked area or multi-line chart
- Default: top 5-10 most mentioned people over the last 12 months
- Example: "Week 17: Alice 4, Bob 6, Carol 8"
- Allow filtering/selecting which people to display

#### Mood Dimensions Over Time

- Extend beyond happiness to other dimensions: energy, anxiety, gratitude, productivity
- Each dimension scored 0-10 per entry, averaged per time bin
- Multi-line chart with toggleable dimensions

#### Activity & Topics

- Frequency of key topics/themes over time (exercise, work, travel, family, etc.)
- Heatmap or bar chart showing which topics dominate which periods

### Dashboard Features

- Date range selector (last month / 3 months / 6 months / 1 year / all time)
- Bin width selector (day / week / month)
- Responsive layout, works on desktop and tablet
- Data sourced from the query service and stats endpoint

### Tech Considerations

- Lightweight frontend (static HTML + JS with a charting library like Chart.js or D3)
- Backend API endpoints to serve aggregated chart data
- Scoring/inference can be done at ingestion time (store scores in SQLite) or on-demand
- Ingestion-time scoring is preferred to avoid repeated LLM calls for dashboard loads
