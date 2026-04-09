# Rename repo from journal-agent to journal-server

Renamed the GitHub remote from `journal-agent` to `journal-server` to better reflect what
this project is — a server, not an agent.

## Changes

- Updated git remote URL to `https://github.com/johnmathews/journal-server.git`
- Replaced all `journal-agent` references across 9 files:
  - CI workflow (`IMAGE_NAME`), docker-compose (`image:`), README, docs, project brief
  - User-Agent header in `ingestion.py`
  - Historical journal entries that referenced the old ghcr.io image name
