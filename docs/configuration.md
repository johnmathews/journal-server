# Configuration

All configuration is via environment variables. No config files are needed.

## Required

| Variable             | Description                                                                                                                                                                                                                                                |
| -------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `JOURNAL_SECRET_KEY` | Server secret used to sign session cookies and password-reset tokens. **The server refuses to start without it** (fail-closed check in `mcp_server/runserver.py`). Generate with `python -c "import secrets; print(secrets.token_urlsafe(32))"`.           |
| `ANTHROPIC_API_KEY`  | Anthropic API key — required when `OCR_PROVIDER=anthropic` or under `OCR_DUAL_PASS=true`. Also used for entity extraction (Opus), mood scoring (Sonnet 4.5), and search reranking (Haiku 4.5).                                                             |
| `OPENAI_API_KEY`     | OpenAI API key. Used for embeddings (`text-embedding-3-large`) and any OpenAI transcription adapter (primary, shadow, or `whisper-1` fallback).                                                                                                            |
| `GOOGLE_API_KEY`     | Google API key — required when `OCR_PROVIDER=gemini` (the prod default) or when Gemini is the primary/shadow transcription provider.                                                                                                                       |

> **Migration note (2026-04-15, finalized 2026-06-10):** the legacy `JOURNAL_API_TOKEN` single bearer-token was retired
> when multi-user auth shipped. Auth is now session cookies (web) + per-user API keys (programmatic). The vestigial
> `api_bearer_token` field has since been deleted from `config.py` entirely — setting `JOURNAL_API_TOKEN` has no effect.
> See [`auth.md`](auth.md) for the current model.

See `docs/security.md` for the threat model and how auth fits in.

## Required — auth & email (multi-user)

| Variable                | Default                  | Description                                                                                                                                                                            |
| ----------------------- | ------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `REGISTRATION_ENABLED`  | `false`                  | When `true`, `/api/auth/register` accepts new users. Toggleable at runtime via `runtime_settings`. Set to `true` for first-time setup, then flip back to `false`.                      |
| `SESSION_EXPIRY_DAYS`   | `7`                      | Session cookie lifetime. Sessions are stored hashed in `user_sessions`; expired rows are pruned by the (currently un-scheduled) cleanup helper.                                        |
| `APP_BASE_URL`          | `http://localhost:5173`  | Base URL used inside email-verification and password-reset links. Set to the public origin for prod (`https://journal.itsa-pizza.com`).                                              |
| `SMTP_HOST`             | `smtp.gmail.com`         | SMTP server hostname. Email-verification and password-reset emails fail silently if `SMTP_USERNAME`/`SMTP_PASSWORD` are unset — required for the user-self-registration flow.          |
| `SMTP_PORT`             | `465`                    | SMTP port (default is implicit-TLS / SSL).                                                                                                                                             |
| `SMTP_USERNAME`         |                          | SMTP auth username.                                                                                                                                                                    |
| `SMTP_PASSWORD`         |                          | SMTP auth password.                                                                                                                                                                    |
| `SMTP_FROM_EMAIL`       |                          | `From:` header used for outgoing verification and reset emails.                                                                                                                        |

## Optional — deployment

| Variable                            | Default               | Description                                                                                                                                                            |
| ----------------------------------- | --------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `DB_PATH`                           | `journal.db`          | Path to SQLite database file                                                                                                                                           |
| `CHROMADB_HOST`                     | `localhost`           | ChromaDB server hostname                                                                                                                                               |
| `CHROMADB_PORT`                     | `8000`                | ChromaDB server port                                                                                                                                                   |
| `MCP_HOST`                          | `0.0.0.0`             | MCP server bind address (in-container). The host-side port in `docker-compose.yml` is bound to `127.0.0.1` — see `docs/security.md`.                                   |
| `MCP_PORT`                          | `8000`                | MCP server port (use 8400 on media VM to avoid Gluetun conflict)                                                                                                       |
| `MCP_ALLOWED_HOSTS`                 | `127.0.0.1,localhost` | Comma-separated hostnames that DNS rebinding protection will accept as Host headers. Add any externally-facing hostname if you front the service with a reverse proxy. |
| `SLACK_BOT_TOKEN`                   |                       | Slack bot token for downloading files from Slack URLs                                                                                                                  |
| `API_CORS_ORIGINS`                  |                       | Comma-separated list of allowed CORS origins for the REST API (e.g., `http://localhost:5173`). Empty disables CORS.                                                    |
| `JOURNAL_AUTHOR_NAME`               | `John`                | Name the entity extractor uses as the subject of first-person statements. See `docs/entity-tracking.md`.                                                               |
| `ENTITY_DEDUP_SIMILARITY_THRESHOLD` | `0.88`                | Cosine similarity threshold for the stage-c embedding dedup fallback. Raise to be stricter, lower to merge more aggressively.                                          |
| `ENTITY_LLM_CANDIDATE_TOP_K`        | `30`                  | Max number of curated user entities passed to the extraction LLM as `known_entities` per call. See `docs/entity-tracking.md` (stage 0).                                |
| `ENTITY_LLM_CANDIDATE_THRESHOLD`    | `0.4`                 | Minimum cosine similarity (entry-text vs entity embedding) required for an entity to be included in the per-call `known_entities` candidate set.                       |
| `ENTITY_LLM_MATCH_MIN_COSINE`       | `0.3`                 | Floor for guard D in the four-guard hybrid sanity check on LLM-asserted matches: cosine(new mention, asserted match's stored embedding) must be ≥ this to be accepted. |

## Optional — Pushover notifications

| Variable                 | Default | Description                                                                                                                                     |
| ------------------------ | ------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| `PUSHOVER_USER_KEY`      |         | Default Pushover user key. Per-user keys in Settings override this. Leave empty if all users configure their own keys.                          |
| `PUSHOVER_APP_API_TOKEN` |         | Default Pushover application API token. Per-user tokens in Settings override this. Leave empty if all users configure their own tokens.         |

Users configure their own Pushover credentials via the Settings page in the webapp. The environment variables serve as
server-wide defaults — useful for single-user deployments. See the webapp's Notifications section for topic configuration.

## Optional — chunking

See `docs/architecture.md` → "Chunking Strategies" for the algorithm and tradeoffs.

| Variable                         | Default    | Applies to    | Description                                                                                               |
| -------------------------------- | ---------- | ------------- | --------------------------------------------------------------------------------------------------------- |
| `CHUNKING_STRATEGY`              | `semantic` | both          | `"fixed"` or `"semantic"`                                                                                 |
| `CHUNKING_MAX_TOKENS`            | `150`      | both          | Upper bound for chunk size                                                                                |
| `CHUNKING_OVERLAP_TOKENS`        | `40`       | fixed only    | Tokens carried between adjacent chunks                                                                    |
| `CHUNKING_MIN_TOKENS`            | `30`       | semantic only | Minimum chunk size; smaller segments are merged                                                           |
| `CHUNKING_BOUNDARY_PERCENTILE`   | `25`       | semantic only | Adjacent similarities at/below this percentile are cut positions                                          |
| `CHUNKING_DECISIVE_PERCENTILE`   | `10`       | semantic only | Cuts at/below this are clean (no overlap); between 10 and 25 are weak cuts with adaptive tail overlap     |
| `CHUNKING_EMBED_METADATA_PREFIX` | `true`     | both          | Prepend `"Date: YYYY-MM-DD. Weekday."` to each chunk before embedding (stored document stays un-prefixed) |

## Optional — OCR provider

| Variable           | Default      | Description                                                                                                                                                                                                                              |
| ------------------ | ------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `OCR_PROVIDER`     | `anthropic`  | Which vision API to use for handwriting OCR. `"anthropic"` (Claude) or `"gemini"` (Google Gemini). **Prod runs `gemini`** (set in compose env).                                                                                          |
| `OCR_MODEL`        | per-provider | Model name sent to the selected provider. Defaults: `claude-opus-4-6` (anthropic), `gemini-2.5-pro` (gemini). Ignored in dual-pass mode — each provider always uses its own default.                                                     |
| `OCR_DUAL_PASS`    | `false`      | When `true`, run **both** providers on every page and persist a reconciled `final_text`. **Prod runs `true`.** Doubles per-page cost. Toggleable at runtime via `runtime_settings`.                                                       |
| `PREPROCESS_IMAGES`| `true`       | Apply Pillow-based deskew/contrast preprocessing before sending to the OCR provider. Toggleable at runtime via `runtime_settings`.                                                                                                       |

Both providers receive the context-priming glossary (`OCR_CONTEXT_DIR`): Anthropic puts the composed system text in the
top-level `system` block (with `cache_control` once the system text is large enough); Gemini puts the same composed
text into `system_instruction` on each call. Voice transcription priming (`TRANSCRIPTION_CONTEXT_ENABLED`) is independent
of the OCR backend — the glossary is wired into whichever transcription provider is active (Whisper-style `prompt` for
OpenAI adapters, full system instruction for the Gemini adapter).

## Optional — transcription provider

Voice transcription is provider-pluggable. The default behaviour is unchanged from the single-provider era — the OpenAI
`gpt-4o-transcribe` adapter runs first, with the only addition being an automatic `whisper-1` fallback after retries
when the primary keeps raising transient errors. To swap to Gemini, run two providers side-by-side, or tune the retry
policy, set the variables below. See `docs/transcription-providers.md` for the architecture and operational notes.

| Variable                              | Type    | Default             | Description                                                                                                                                                       |
| ------------------------------------- | ------- | ------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `TRANSCRIPTION_PROVIDER`              | string  | `openai`            | Primary transcription provider. `"openai"` or `"gemini"`. Validated at startup.                                                                                   |
| `TRANSCRIPTION_MODEL`                 | string  | per-provider        | Primary model. Defaults: `gpt-4o-transcribe` (openai), `gemini-2.5-pro` (gemini). Cross-provider model names (e.g. `gpt-4o-transcribe` with provider=gemini) are silently overridden to the provider's default with an INFO log. |
| `TRANSCRIPTION_FALLBACK_ENABLED`      | bool    | `true`              | Wrap the primary in a retry+fallback adapter. When `true`, transient failures trigger up to N retries, then route to an OpenAI fallback adapter.                  |
| `TRANSCRIPTION_FALLBACK_MODEL`        | string  | `whisper-1`         | OpenAI model used as the fallback adapter. Always uses `OPENAI_API_KEY`.                                                                                          |
| `TRANSCRIPTION_RETRY_MAX_ATTEMPTS`    | int     | `3`                 | Total attempts at the primary before falling through. Must be >= 1.                                                                                               |
| `TRANSCRIPTION_RETRY_BASE_DELAY`      | float   | `1.0`               | Seconds for the first backoff sleep. Doubles each retry (1s, 2s, 4s…). Must be >= 0.                                                                              |
| `TRANSCRIPTION_RETRY_MAX_DELAY`       | float   | `30.0`              | Upper cap on the per-retry sleep, in seconds. Must be >= 0.                                                                                                       |
| `TRANSCRIPTION_SHADOW_PROVIDER`       | string  |                     | When set (`"openai"` or `"gemini"`), runs the shadow provider in parallel with the primary on every request and logs a word-level diff. **Doubles per-audio cost.** Empty disables shadow mode. |
| `TRANSCRIPTION_SHADOW_MODEL`          | string  | shadow-default      | Model for the shadow adapter. Empty = the shadow provider's own default.                                                                                          |

## Optional — context files (OCR + voice)

Markdown context files in `OCR_CONTEXT_DIR` prime BOTH the OCR system prompt and the active transcription provider so
handwritten and spoken proper nouns get correct spellings. See `docs/context-files.md` for the unified reference,
`docs/ocr-context.md` for the OCR-side mechanism, and `docs/transcription-providers.md` for the per-provider wiring.

| Variable                        | Default          | Description                                                                                                                          |
| ------------------------------- | ---------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| `OCR_CONTEXT_DIR`               |                  | Directory of `*.md` files. Unset = no context priming on either pipeline.                                                            |
| `OCR_CONTEXT_CACHE_TTL`         | `1h`             | Anthropic cache TTL for the OCR system block (`5m` or `1h`).                                                                         |
| `TRANSCRIPTION_CONTEXT_ENABLED` | `true`           | Wire the glossary into the active transcription provider. OpenAI adapters get the stripped, ~200-token-capped text via the `prompt` parameter; the Gemini adapter gets the full glossary as a system instruction. Set to `false` for OCR priming without transcription priming. |

## Optional — voice transcription post-processing

| Variable                       | Default            | Description                                                                                                                                |
| ------------------------------ | ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------ |
| `TRANSCRIPT_FORMATTING`        | `false`            | Use Anthropic Haiku to insert paragraph breaks into voice transcripts. Word preservation is enforced; failures fall back to raw text.      |
| `TRANSCRIPT_FORMATTER_MODEL`   | `claude-haiku-4-5` | Model used for paragraph formatting.                                                                                                       |
| `DATE_HEADING_DETECTION`       | `true`             | When the transcript or OCR text begins with a date (numeric, written, or relative like "today"), strip it from the body. The entry's title already shows the date, so a leading date in the body is just a redundant duplicate. The detector also returns the resolved ISO date, which becomes the entry's `entry_date` — overriding the upload-time default for backdated dictations. |
| `DATE_HEADING_MODEL`           | `claude-haiku-4-5` | Model used for heading detection.                                                                                                          |

`raw_text` is preserved verbatim through both steps — the date strip and any LLM-inserted paragraph breaks land only on
`final_text`. `final_text` is what the chunker, embedder, and search index see.

For voice and multi-voice ingestion, the date stripped from the body also propagates to the entry's `entry_date`. This
mirrors the OCR paths: a regex pass over the start of the text catches numeric / abbreviated forms ("Friday 1 January
2026", "2026-01-01"), and the LLM detector's `iso_date` overrides when set so spelled-out and relative phrases ("the
first of January", "yesterday") also flow through. The result: a backdated dictation that begins with the actual date is
filed under that date, not under the upload day.

## Optional — hybrid search

`/api/search` and the `journal_search_entries` MCP tool both run a fixed hybrid pipeline: BM25 (FTS5) + dense retrieval, RRF fusion,
listwise rerank. There is no `mode` toggle — the parameter was retired and now returns `400 mode_removed`. See [docs/search.md](search.md)
for the full architecture and rationale.

| Variable                  | Default            | Description                                                                                            |
| ------------------------- | ------------------ | ------------------------------------------------------------------------------------------------------ |
| `HYBRID_BM25_CANDIDATES`  | `50`               | Top-N entries fetched from FTS5 in L1.                                                                 |
| `HYBRID_DENSE_CANDIDATES` | `50`               | Top-N chunks fetched from Chroma in L1.                                                                |
| `HYBRID_FUSION_TOP_M`     | `30`               | Entries kept after RRF fusion, before reranking.                                                       |
| `HYBRID_RRF_K`            | `60`               | RRF damping constant. Lower = sharper top-rank preference. Cormack et al. (2009) default.              |
| `HYBRID_RERANKER`         | `anthropic`        | L2 reranker. `anthropic` runs Claude listwise; `none` skips L2 and returns RRF-only ordering.          |
| `RERANKER_MODEL`          | `claude-haiku-4-5` | Model used by `AnthropicReranker`. Only consulted when `HYBRID_RERANKER=anthropic`.                    |

## Optional — mood scoring

See [`mood-scoring.md`](mood-scoring.md) for the pipeline and CLI.

| Variable                       | Default                            | Description                                                                                                                                                                                          |
| ------------------------------ | ---------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `JOURNAL_ENABLE_MOOD_SCORING`  | `true`                             | When `true`, ingestion submits a `mood_score_entry` job per entry. Toggleable at runtime via Settings → Features (writes the `enable_mood_scoring` row in `runtime_settings` and rebuilds the service in-place; no restart needed). |
| `MOOD_SCORER_MODEL`            | `claude-sonnet-4-5`                | Anthropic model used for tool-use mood scoring.                                                                                                                                                      |
| `MOOD_SCORER_MAX_TOKENS`       | `1024`                             | Token budget for the mood-scoring tool-use response.                                                                                                                                                 |
| `MOOD_DIMENSIONS_PATH`         | `config/mood-dimensions.toml`      | Path to the TOML file defining the 7 facets (joy_sadness, energy_fatigue, agency, fulfillment, connection, frustration, proactive_reactive). Reload with `POST /api/admin/reload/mood-dimensions`.   |

## Optional — entity extraction

See [`entity-tracking.md`](entity-tracking.md) for the pipeline.

| Variable                          | Default                                  | Description                                                                                                                          |
| --------------------------------- | ---------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| `ENTITY_CASING_EXCEPTIONS_PATH`   | `config/entity-casing-exceptions.toml`   | TOML file defining canonical-casing exceptions for `smart_title_case` (e.g. `iPhone`, `eBay`). Reload with `POST /api/admin/reload/entity-casing`. |

## Optional — fitness integration

See [`fitness-pipeline.md`](fitness-pipeline.md) for the data flow,
[`fitness-operations.md`](fitness-operations.md) for re-auth and backfill
runbooks, and [`external-services.md`](external-services.md#fitness-data-sources)
for the Strava + Garmin provider entries.

**Strava** counts as "wired on this server" when both `STRAVA_CLIENT_ID` and
`STRAVA_CLIENT_SECRET` are set (one OAuth app per server, shared across all
users). Without those, `submit_fitness_sync_strava` raises a `RuntimeError`
and `POST /api/fitness/sync/strava` returns `503` so operators can tell
*feature off* from *bug*.

**Garmin** is always wired (per W6 of the fitness multi-user plan) — there are
no global Garmin env vars. Each user connects their own Garmin account via the
webapp Settings panel (`POST /api/fitness/garmin/connect`) or via
`journal fitness-reauth-garmin --user-id N --username EMAIL` (operator
fallback). A user without a `fitness_auth_state` row produces a clean
`auth_broken` sync rather than a 503.

| Variable                                | Default                                | Description                                                                                                                                                  |
| --------------------------------------- | -------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `STRAVA_CLIENT_ID`                      |                                        | Strava API app client id from <https://www.strava.com/settings/api>. Required for Strava re-auth and sync.                                                   |
| `STRAVA_CLIENT_SECRET`                  |                                        | Strava API app client secret. Required for Strava re-auth and sync.                                                                                          |
| `STRAVA_REDIRECT_URI`                   | `http://localhost:8400/strava/callback`| OAuth callback URL embedded in the authorize URL. In prod (post-multi-user-plan W13), this points at the webapp callback route (`https://<webapp>/settings/fitness/strava/callback`) so the per-user webapp connect flow works; the Strava developer app's Authorization Callback Domain must match. The default value still drives the CLI listener path used for dev/laptop bootstrap (see [`fitness-operations.md` §2e](fitness-operations.md#2e-strava--cli-operator-fallback-headless-deployment-server-on-8400) for the headless workaround). |
| `FITNESS_BACKFILL_START`                | `2026-01-01`                           | Default `--start` for `journal fitness-backfill` when no flag is passed. Activities and daily wellness rows from before this date are not retroactively pulled even if they exist upstream. |
| `FITNESS_TRANSIENT_FAILURE_THRESHOLD`   | `3`                                    | Number of consecutive transient failures before W6 transitions a source to `auth_status="broken"`. Also the streak ceiling backfill aborts at. Must be ≥ 1.  |
| `FITNESS_HEALTH_BROKEN_DEGRADED_HOURS`  | `48`                                   | Hours a source can be `auth_status="broken"` before `/api/health` downgrades the overall `status` to `degraded`. Must be ≥ 1.                                |
| `FITNESS_SYNC_ENABLED`                  | `true`                                 | When `true`, start the in-process `FitnessSyncScheduler` daemon thread that enqueues per-user incremental syncs once daily at 17:00 server-local time. Set to `0`, `false`, `no`, or `off` to disable. See [`fitness-operations.md` §4](fitness-operations.md#daily-auto-sync). |


## Optional — background jobs

The in-process job runner (`services/jobs/runner.py`) uses two thread pools: a parallel ingestion/fast pool (Pool A)
and a single-worker storyline pool (Pool B). Only Pool A is sized by an env var. See [`jobs.md`](jobs.md#jobrunner)
for the full model.

| Variable            | Default | Description                                                                                                                                                          |
| ------------------- | ------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `JOB_WORKER_COUNT`  | `4`     | Number of parallel workers in Pool A (ingestion / mood / fitness / extraction jobs). Storyline jobs always run on a separate single-worker pool. Must be ≥ 1; set to `1` for a fully-serial Pool A. |

## Optional — storylines

Storylines (cross-entry narratives) are gated on `ANTHROPIC_API_KEY`; when it is unset the feature is disabled and
these vars are inert. All are optional with working defaults. See [`storylines.md`](storylines.md#configuration) for the
full model (providers, the judge-driven chapter engine, the extension classifier pipeline).

| Variable                                   | Default            | Description                                                                                       |
| ------------------------------------------ | ------------------ | ------------------------------------------------------------------------------------------------ |
| `STORYLINE_NARRATOR_MODEL`                 | `claude-opus-4-7`  | Model for chapter narration (Anthropic Citations API).                                           |
| `STORYLINE_NARRATOR_MAX_TOKENS`            | `4096`             | Max output tokens for narration.                                                                  |
| `STORYLINE_JUDGE_MODEL`                    | `claude-haiku-4-5` | Model for the chapter-boundary judge (`judge_extension` / `partition`).                          |
| `STORYLINE_EXTENSION_DECIDER_MODEL`        | `claude-haiku-4-5` | Model for the extension classifier's LLM decider stage.                                          |
| `STORYLINE_EXTENSION_RELEVANCE_THRESHOLD`  | `0.5`              | Cosine at/above which the classifier's embedding fallback (vs. the draft chapter's embedding) escalates a no-match entry to the decider. |
| `STORYLINE_MIN_PUBLISH_ENTRIES`            | `3`                | Guard: a would-be publish below this entry count defers instead of producing a thin chapter.      |

## Runtime-toggleable settings

The following env vars seed the initial value at startup, but are overlaid at runtime by rows in the `runtime_settings`
table (admins toggle them from the webapp's Settings / Admin pages without a server restart): `OCR_PROVIDER`,
`OCR_DUAL_PASS`, `PREPROCESS_IMAGES`, `JOURNAL_ENABLE_MOOD_SCORING`, `REGISTRATION_ENABLED`, `TRANSCRIPT_FORMATTING`,
`DATE_HEADING_DETECTION`, `TRANSCRIPTION_CONTEXT_ENABLED`. The current effective value is exposed at
`GET /api/settings/runtime` (admin) and merged into `GET /api/settings` (all users) for the relevant feature flags.

## Models (defaults, overridable via env vars or config.py)

| Variable / Setting                   | Default                              | Description                                                                       |
| ------------------------------------ | ------------------------------------ | --------------------------------------------------------------------------------- |
| `OCR_MODEL`                          | `claude-opus-4-6` / `gemini-2.5-pro` | Vision model for OCR (depends on `OCR_PROVIDER`)                                  |
| `transcription_model`                | `gpt-4o-transcribe`                  | OpenAI model for transcription                                                    |
| `TRANSCRIPTION_CONFIDENCE_THRESHOLD` | `-0.5`                               | Logprob threshold for flagging uncertain words (≈60% confidence). More negative = fewer flags |
| `embedding_model`                    | `text-embedding-3-large`             | OpenAI model for embeddings                                                       |
| `embedding_dimensions`               | `1024`                               | Embedding vector dimensions (reduced from 3072)                                   |

## Docker Compose

When running via Docker Compose, set API keys in a `.env` file in the project root or export them as environment
variables:

```bash
export JOURNAL_SECRET_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
export ANTHROPIC_API_KEY=sk-ant-...
export OPENAI_API_KEY=sk-...
export GOOGLE_API_KEY=...           # required when OCR_PROVIDER=gemini (the prod default)
export OCR_PROVIDER=gemini          # or "anthropic"
export OCR_DUAL_PASS=true           # optional second-pass with the other provider
export SLACK_BOT_TOKEN=xoxb-...     # optional, for Slack file URL ingestion
docker compose up
```

## Reloading file-backed config

Four resources are read from disk only at startup and otherwise stay
cached in memory: the OCR glossary directory (`OCR_CONTEXT_DIR/*.md`),
the transcription context (same files, different formatter), the
mood-dimensions TOML (`MOOD_DIMENSIONS_PATH`), and the entity-casing
exceptions TOML (`ENTITY_CASING_EXCEPTIONS_PATH`, defaults to
`config/entity-casing-exceptions.toml`). When you edit one of those
files in production, the server will not see the change until you
either restart it or hit one of these admin-only endpoints.

| Endpoint                                       | Reloads                                                                                |
| ---------------------------------------------- | -------------------------------------------------------------------------------------- |
| `POST /api/admin/reload/ocr-context`           | OCR provider (rebuilds with the current glossary)                                      |
| `POST /api/admin/reload/transcription-context` | Transcription provider stack (Whisper / Gemini, with current context)                  |
| `POST /api/admin/reload/mood-dimensions`       | `MoodScoringService` (rebuilds from the TOML); 409 if `JOURNAL_ENABLE_MOOD_SCORING` is unset |
| `POST /api/admin/reload/entity-casing`         | Entity-casing exceptions (rebinds the table on `SQLiteEntityStore` for new writes)     |

All four require an admin session (or admin API key). They take no
body, return a small JSON summary describing what was reloaded, and
don't disturb in-flight requests — callers that already resolved the
old provider keep using it until they finish; new requests pick up the
fresh one. See `docs/security.md` for the auth posture.

```bash
# After editing OCR_CONTEXT_DIR/*.md
curl -X POST -b "session_id=$ADMIN_SESSION" \
  http://localhost:8400/api/admin/reload/ocr-context

# After editing the same files but caring about transcription
curl -X POST -b "session_id=$ADMIN_SESSION" \
  http://localhost:8400/api/admin/reload/transcription-context

# After editing MOOD_DIMENSIONS_PATH
curl -X POST -b "session_id=$ADMIN_SESSION" \
  http://localhost:8400/api/admin/reload/mood-dimensions

# After editing config/entity-casing-exceptions.toml
curl -X POST -b "session_id=$ADMIN_SESSION" \
  http://localhost:8400/api/admin/reload/entity-casing
```

The entity-casing TOML is purely additive: it adjusts how new entity
canonical names are normalised at write time (`smart_title_case`).
Existing rows are not rewritten on reload — see
`docs/entity-tracking.md` for the algorithm and the no-backfill rule.

OCR and transcription deliberately do not share a reload — although both
read `OCR_CONTEXT_DIR`, the formatter chains differ and a single reload
would create an implicit coupling. After editing the glossary, hit both
endpoints.

The webapp surfaces these as buttons under `/admin/server` for users
with `is_admin=true`.

## Media VM Deployment

The `docker-compose.yml` is configured for the media VM stack:

- **MCP server** on port 8400 (avoids Gluetun's port 8000)
- **ChromaDB** on port 8401 (internal 8000)
- Bind mounts to `/srv/media/config/journal/{data,chromadb}`
- Image pulled from `ghcr.io/johnmathews/journal-server:latest`

MCP endpoint: `http://<media-vm-ip>:8400/mcp`

Create data directories before first run:

```bash
mkdir -p /srv/media/config/journal/{data,chromadb}
```
