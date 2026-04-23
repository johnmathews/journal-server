# Pushover Notification Service

Added push notifications via the Pushover API, giving users real-time alerts about job outcomes
without needing to keep the webapp open.

## What changed

### Notification service (`services/notifications.py`)
- `PushoverNotificationService` with per-user credential resolution (user preferences override env var defaults)
- 6 notification topics: image ingestion success, audio ingestion success, job retrying (backoff started),
  job failed permanently, admin job failed (fan-out to all admins), admin health alert
- Pushover API integration using stdlib `urllib.request` — no new dependencies
- Failure classification: internal error vs external API issue (reuses `_is_transient` from jobs.py)

### Health poller (`services/health_poll.py`)
- Daemon thread checking SQLite, ChromaDB, and disk space every 5 minutes
- Notifies admins only on status transitions (healthy -> unhealthy), no repeat alerts
- Zero external API calls — purely local checks, no usage fees

### JobRunner integration
- Added `notification_service` parameter to `JobRunner.__init__`
- Notification hooks in all 6 worker bodies (entity extraction, mood backfill, image/audio ingestion,
  mood score entry, reprocess embeddings)
- Retry notifications fire on first backoff only (not every retry)
- Injected `user_id` into params for `mood_score_entry` and `reprocess_embeddings` submissions

### API endpoints
- `GET /api/notifications/topics` — list topics with user's toggle state
- `GET /api/notifications/status` — check if user has credentials configured
- `POST /api/notifications/validate` — validate + save Pushover credentials
- `POST /api/notifications/test` — send test notification

### Environment variables
- `PUSHOVER_USER_KEY` — default Pushover user key (optional)
- `PUSHOVER_APP_API_TOKEN` — default Pushover app token (optional)

## Design decisions

- Each user provides both Pushover keys (app token + user key) — stored in `user_preferences` table
- Ingestion success notifications include entity extraction and mood analysis results in the message body
  (1 notification for the happy path, not 4 per pipeline stage)
- Standalone batch jobs (manual entity extraction, mood backfill) always notify on completion — no separate toggle
- Admin-only topic preferences are enforced server-side in the PATCH preferences endpoint (not just UI-hidden)
- Admin job failure fan-out skips the job owner to avoid duplicate notifications
- Notification failures are always caught and logged — never affect job execution

## Code review fixes
- Added admin-only guard to `PATCH /api/users/me/preferences` to prevent non-admin users setting admin topic prefs
- Deduplicated admin notifications for job owners who are also admins
- Replaced private `_resolve_credentials` call in test endpoint with public `send_test_notification` method
- Added error feedback for failed topic preference saves in the webapp store
