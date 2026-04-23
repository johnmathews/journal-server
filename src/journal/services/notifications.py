"""Pushover notification service.

Sends push notifications via the Pushover API when jobs complete,
fail, or enter retry backoff. Credentials are resolved per-user
from ``user_preferences``, falling back to server-wide defaults
from environment variables.

Notification failures are logged and swallowed — a notification
failure must never affect job execution or server operation.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from journal.services.jobs import _is_transient

if TYPE_CHECKING:
    from journal.db.user_repository import SQLiteUserRepository

log = logging.getLogger(__name__)

_PUSHOVER_MESSAGES_URL = "https://api.pushover.net/1/messages.json"
_PUSHOVER_VALIDATE_URL = "https://api.pushover.net/1/users/validate.json"
_REQUEST_TIMEOUT = 7  # seconds

# Priority levels
PRIORITY_LOW = -1
PRIORITY_NORMAL = 0
PRIORITY_HIGH = 1

# ── Topic definitions ────────────────────────────────────────────────
#
# Each topic has:
#   key         — preference key stored in user_preferences
#   label       — human-readable label for the UI
#   group       — UI grouping: "success", "failure", or "admin"
#   admin_only  — only shown to / enabled for admin users
#   default     — default enabled state when user has no preference set

TOPICS: list[dict[str, Any]] = [
    {
        "key": "notif_job_success_ingest_images",
        "label": "Image ingestion succeeded",
        "group": "success",
        "admin_only": False,
        "default": True,
    },
    {
        "key": "notif_job_success_ingest_audio",
        "label": "Audio ingestion succeeded",
        "group": "success",
        "admin_only": False,
        "default": True,
    },
    {
        "key": "notif_job_retrying",
        "label": "Job retrying (backoff started)",
        "group": "failure",
        "admin_only": False,
        "default": True,
    },
    {
        "key": "notif_job_failed",
        "label": "Job failed permanently",
        "group": "failure",
        "admin_only": False,
        "default": True,
    },
    {
        "key": "notif_admin_job_failed",
        "label": "Any user's job failed (admin)",
        "group": "admin",
        "admin_only": True,
        "default": True,
    },
    {
        "key": "notif_admin_health_alert",
        "label": "System health degraded (admin)",
        "group": "admin",
        "admin_only": True,
        "default": True,
    },
]

# Map job_type -> topic key for success notifications.
# Only ingestion jobs have user-facing success topics.
_SUCCESS_TOPIC_MAP: dict[str, str] = {
    "ingest_images": "notif_job_success_ingest_images",
    "ingest_audio": "notif_job_success_ingest_audio",
}

# Human-readable labels for job types.
_JOB_TYPE_LABELS: dict[str, str] = {
    "ingest_images": "Image ingestion",
    "ingest_audio": "Audio ingestion",
    "entity_extraction": "Entity extraction",
    "mood_backfill": "Mood analysis",
    "mood_score_entry": "Mood scoring",
    "reprocess_embeddings": "Embedding reprocessing",
}


@dataclass
class NotificationResult:
    """Outcome of a Pushover API call."""

    sent: bool
    status_code: int | None = None
    error: str | None = None


class PushoverNotificationService:
    """Send Pushover notifications scoped to individual users.

    Credentials are resolved per-user: preferences override server
    defaults. All public ``notify_*`` methods are fire-and-forget —
    exceptions are caught and logged internally.
    """

    def __init__(
        self,
        user_repo: SQLiteUserRepository,
        default_user_key: str = "",
        default_app_token: str = "",
    ) -> None:
        self._user_repo = user_repo
        self._default_user_key = default_user_key
        self._default_app_token = default_app_token

    # ── Core send ────────────────────────────────────────────────────

    def send_notification(
        self,
        user_key: str,
        app_token: str,
        *,
        title: str,
        message: str,
        priority: int = PRIORITY_NORMAL,
    ) -> NotificationResult:
        """Send a single Pushover notification."""
        return self._post_to_pushover(
            user_key, app_token, title, message, priority,
        )

    # ── Job lifecycle notifications ──────────────────────────────────

    def notify_job_success(
        self,
        user_id: int,
        job_type: str,
        result: dict[str, Any],
    ) -> None:
        """Notify a user that their job succeeded.

        Ingestion jobs (image/audio) have dedicated toggleable topics.
        Other job types (entity_extraction, mood_backfill) always fire
        a lightweight notification when triggered manually.
        """
        try:
            topic_key = _SUCCESS_TOPIC_MAP.get(job_type)
            if topic_key is not None and not self._is_topic_enabled(user_id, topic_key):
                return
            # else: standalone batch job — always notify

            user_key, app_token = self._resolve_credentials(user_id)
            if not user_key or not app_token:
                return

            label = _JOB_TYPE_LABELS.get(job_type, job_type)
            title = f"{label} complete"
            message = self._build_success_message(job_type, result)

            self._post_to_pushover(
                user_key, app_token, title, message, PRIORITY_NORMAL,
            )
        except Exception:  # noqa: BLE001
            log.warning(
                "Failed to send success notification for user %d, job %s",
                user_id, job_type, exc_info=True,
            )

    def notify_job_retrying(
        self,
        user_id: int,
        job_type: str,
        attempt: int,
        delay_seconds: int,
        error_message: str,
        exc: Exception | None = None,
    ) -> None:
        """Notify a user that their job entered retry backoff."""
        try:
            if not self._is_topic_enabled(user_id, "notif_job_retrying"):
                return

            user_key, app_token = self._resolve_credentials(user_id)
            if not user_key or not app_token:
                return

            label = _JOB_TYPE_LABELS.get(job_type, job_type)
            is_external = _is_transient(exc) if exc is not None else False
            cause = "external API issue" if is_external else "transient error"
            delay_min = delay_seconds // 60

            title = f"{label} retrying"
            message = (
                f"{error_message}\n"
                f"Cause: {cause}\n"
                f"Retry attempt {attempt}, next try in {delay_min} min"
            )

            self._post_to_pushover(
                user_key, app_token, title, message, PRIORITY_NORMAL,
            )
        except Exception:  # noqa: BLE001
            log.warning(
                "Failed to send retry notification for user %d, job %s",
                user_id, job_type, exc_info=True,
            )

    def notify_job_failed(
        self,
        user_id: int,
        job_type: str,
        error_message: str | None,
        exc: Exception | None = None,
    ) -> None:
        """Notify a user that their job failed permanently."""
        try:
            if not self._is_topic_enabled(user_id, "notif_job_failed"):
                return

            user_key, app_token = self._resolve_credentials(user_id)
            if not user_key or not app_token:
                return

            label = _JOB_TYPE_LABELS.get(job_type, job_type)
            is_external = _is_transient(exc) if exc is not None else False
            cause_tag = "External API issue" if is_external else "Internal error"

            title = f"{label} failed"
            message = (
                f"{cause_tag}: {error_message or 'Unknown error'}"
            )

            self._post_to_pushover(
                user_key, app_token, title, message, PRIORITY_HIGH,
            )
        except Exception:  # noqa: BLE001
            log.warning(
                "Failed to send failure notification for user %d, job %s",
                user_id, job_type, exc_info=True,
            )

    def notify_admin_job_failed(
        self,
        job_owner_user_id: int,
        job_type: str,
        error_message: str | None,
        exc: Exception | None = None,
    ) -> None:
        """Notify all admin users about a failed job (any user's).

        Skips the job owner to avoid duplicate notifications — they
        already receive one from ``notify_job_failed``.
        """
        try:
            label = _JOB_TYPE_LABELS.get(job_type, job_type)
            is_external = _is_transient(exc) if exc is not None else False
            cause_tag = "External API issue" if is_external else "Internal error"

            title = f"[Admin] {label} failed for user {job_owner_user_id}"
            message = f"{cause_tag}: {error_message or 'Unknown error'}"

            for admin_id in self._get_admin_user_ids():
                if admin_id == job_owner_user_id:
                    continue  # already notified via notify_job_failed
                if not self._is_topic_enabled(admin_id, "notif_admin_job_failed"):
                    continue
                user_key, app_token = self._resolve_credentials(admin_id)
                if not user_key or not app_token:
                    continue
                self._post_to_pushover(
                    user_key, app_token, title, message, PRIORITY_HIGH,
                )
        except Exception:  # noqa: BLE001
            log.warning(
                "Failed to send admin failure notification for job %s",
                job_type, exc_info=True,
            )

    # ── Health alerts ────────────────────────────────────────────────

    def notify_health_alert(
        self,
        component: str,
        detail: str,
    ) -> None:
        """Notify admin users about a health status degradation."""
        try:
            title = f"[Health] {component} degraded"
            message = detail

            for admin_id in self._get_admin_user_ids():
                if not self._is_topic_enabled(admin_id, "notif_admin_health_alert"):
                    continue
                user_key, app_token = self._resolve_credentials(admin_id)
                if not user_key or not app_token:
                    continue
                self._post_to_pushover(
                    user_key, app_token, title, message, PRIORITY_HIGH,
                )
        except Exception:  # noqa: BLE001
            log.warning(
                "Failed to send health alert for %s", component,
                exc_info=True,
            )

    # ── Credential validation ────────────────────────────────────────

    def validate_credentials(
        self,
        user_key: str,
        app_token: str,
    ) -> NotificationResult:
        """Validate a Pushover user key + app token pair."""
        try:
            data = urllib.parse.urlencode({
                "token": app_token,
                "user": user_key,
            }).encode()
            req = urllib.request.Request(
                _PUSHOVER_VALIDATE_URL,
                data=data,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
                body = json.loads(resp.read())
                if body.get("status") == 1:
                    return NotificationResult(sent=True, status_code=resp.status)
                return NotificationResult(
                    sent=False,
                    status_code=resp.status,
                    error="; ".join(body.get("errors", ["Unknown error"])),
                )
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read())
                error = "; ".join(body.get("errors", [str(e)]))
            except Exception:
                error = str(e)
            return NotificationResult(sent=False, status_code=e.code, error=error)
        except Exception as e:
            return NotificationResult(sent=False, error=str(e))

    # ── Topic metadata ───────────────────────────────────────────────

    def get_topics_for_user(
        self,
        user_id: int,
        is_admin: bool,
    ) -> list[dict[str, Any]]:
        """Return topics visible to this user with their current enabled state."""
        result = []
        for topic in TOPICS:
            if topic["admin_only"] and not is_admin:
                continue
            pref = self._user_repo.get_preference(user_id, topic["key"])
            enabled = pref if isinstance(pref, bool) else topic["default"]
            result.append({**topic, "enabled": enabled})
        return result

    def has_credentials(self, user_id: int) -> bool:
        """Check whether a user has Pushover credentials configured."""
        user_key, app_token = self._resolve_credentials(user_id)
        return bool(user_key and app_token)

    def send_test_notification(self, user_id: int) -> NotificationResult:
        """Send a test notification using the user's resolved credentials."""
        user_key, app_token = self._resolve_credentials(user_id)
        if not user_key or not app_token:
            return NotificationResult(
                sent=False, error="No Pushover credentials configured",
            )
        return self.send_notification(
            user_key, app_token,
            title="Journal Insights",
            message="Test notification — your Pushover integration is working!",
        )

    # ── Private helpers ──────────────────────────────────────────────

    def _resolve_credentials(self, user_id: int) -> tuple[str, str]:
        """Resolve Pushover credentials for a user.

        Per-user preferences override server-wide defaults.
        """
        user_key = (
            self._user_repo.get_preference(user_id, "pushover_user_key")
            or self._default_user_key
        )
        app_token = (
            self._user_repo.get_preference(user_id, "pushover_app_token")
            or self._default_app_token
        )
        return str(user_key) if user_key else "", str(app_token) if app_token else ""

    def _is_topic_enabled(self, user_id: int, topic_key: str) -> bool:
        """Check if a notification topic is enabled for a user."""
        pref = self._user_repo.get_preference(user_id, topic_key)
        if isinstance(pref, bool):
            return pref
        # Fall back to topic default
        for topic in TOPICS:
            if topic["key"] == topic_key:
                return topic["default"]
        return False

    def _get_admin_user_ids(self) -> list[int]:
        """Return IDs of all active admin users."""
        return [
            u.id for u in self._user_repo.list_users()
            if u.is_admin and u.is_active
        ]

    def _post_to_pushover(
        self,
        user_key: str,
        app_token: str,
        title: str,
        message: str,
        priority: int,
    ) -> NotificationResult:
        """POST a message to the Pushover API."""
        try:
            data = urllib.parse.urlencode({
                "token": app_token,
                "user": user_key,
                "title": title[:250],
                "message": message[:1024],
                "priority": priority,
            }).encode()
            req = urllib.request.Request(
                _PUSHOVER_MESSAGES_URL,
                data=data,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
                body = json.loads(resp.read())
                if body.get("status") == 1:
                    return NotificationResult(sent=True, status_code=resp.status)
                return NotificationResult(
                    sent=False,
                    status_code=resp.status,
                    error="; ".join(body.get("errors", ["Unknown error"])),
                )
        except urllib.error.HTTPError as e:
            log.warning("Pushover API error %d: %s", e.code, e.reason)
            return NotificationResult(sent=False, status_code=e.code, error=str(e))
        except Exception as e:
            log.warning("Pushover request failed: %s", e)
            return NotificationResult(sent=False, error=str(e))

    def _build_success_message(
        self,
        job_type: str,
        result: dict[str, Any],
    ) -> str:
        """Build a concise success notification message body."""
        parts: list[str] = []

        if job_type in ("ingest_images", "ingest_audio"):
            entry_id = result.get("entry_id")
            if entry_id:
                parts.append(f"Entry {entry_id} created")
            parts.append("All processing complete")
        elif job_type == "entity_extraction":
            processed = result.get("entries_processed", 0)
            created = result.get("entities_created", 0)
            mentions = result.get("mentions_created", 0)
            parts.append(f"{processed} entries processed")
            parts.append(f"{created} entities, {mentions} mentions")
        elif job_type == "mood_backfill":
            scored = result.get("scored", 0)
            skipped = result.get("skipped", 0)
            parts.append(f"{scored} entries scored, {skipped} skipped")
        elif job_type == "mood_score_entry":
            count = result.get("scores_written", 0)
            parts.append(f"{count} mood scores written")
        elif job_type == "reprocess_embeddings":
            count = result.get("chunk_count", 0)
            parts.append(f"{count} chunks reprocessed")

        return "\n".join(parts) if parts else "Job completed successfully"
