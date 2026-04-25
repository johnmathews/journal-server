"""Tests for PushoverNotificationService."""

import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from journal.models import User
from journal.services.notifications import (
    PRIORITY_NORMAL,
    TOPICS,
    PushoverNotificationService,
)


@pytest.fixture
def mock_user_repo() -> MagicMock:
    repo = MagicMock()
    repo.get_preference.return_value = None
    repo.list_users.return_value = []
    return repo


@pytest.fixture
def svc(mock_user_repo: MagicMock) -> PushoverNotificationService:
    return PushoverNotificationService(
        user_repo=mock_user_repo,
        default_user_key="default-user-key",
        default_app_token="default-app-token",
    )


def _make_urlopen_response(body: dict, status: int = 200) -> MagicMock:
    """Create a mock urlopen response with the given JSON body."""
    resp = MagicMock()
    resp.status = status
    resp.read.return_value = json.dumps(body).encode()
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


class TestCredentialResolution:
    def test_falls_back_to_defaults(
        self, svc: PushoverNotificationService, mock_user_repo: MagicMock
    ) -> None:
        mock_user_repo.get_preference.return_value = None
        key, token = svc._resolve_credentials(1)
        assert key == "default-user-key"
        assert token == "default-app-token"

    def test_per_user_overrides_defaults(
        self, svc: PushoverNotificationService, mock_user_repo: MagicMock
    ) -> None:
        def pref_side_effect(user_id: int, key: str):
            return {"pushover_user_key": "user-key", "pushover_app_token": "user-token"}.get(key)
        mock_user_repo.get_preference.side_effect = pref_side_effect
        key, token = svc._resolve_credentials(1)
        assert key == "user-key"
        assert token == "user-token"

    def test_empty_defaults_return_empty(self, mock_user_repo: MagicMock) -> None:
        svc = PushoverNotificationService(
            user_repo=mock_user_repo,
            default_user_key="",
            default_app_token="",
        )
        key, token = svc._resolve_credentials(1)
        assert key == ""
        assert token == ""


class TestTopicEnabled:
    def test_returns_default_when_no_preference(
        self, svc: PushoverNotificationService, mock_user_repo: MagicMock
    ) -> None:
        mock_user_repo.get_preference.return_value = None
        # notif_job_failed defaults to True
        assert svc._is_topic_enabled(1, "notif_job_failed") is True
        # notif_admin_job_failed defaults to True
        assert svc._is_topic_enabled(1, "notif_admin_job_failed") is True

    def test_respects_explicit_false(
        self, svc: PushoverNotificationService, mock_user_repo: MagicMock
    ) -> None:
        mock_user_repo.get_preference.return_value = False
        assert svc._is_topic_enabled(1, "notif_job_failed") is False

    def test_respects_explicit_true(
        self, svc: PushoverNotificationService, mock_user_repo: MagicMock
    ) -> None:
        mock_user_repo.get_preference.return_value = True
        assert svc._is_topic_enabled(1, "notif_job_retrying") is True

    def test_unknown_topic_returns_false(
        self, svc: PushoverNotificationService
    ) -> None:
        assert svc._is_topic_enabled(1, "notif_nonexistent") is False


class TestPostToPushover:
    @patch("journal.services.notifications.urllib.request.urlopen")
    def test_success(
        self, mock_urlopen: MagicMock, svc: PushoverNotificationService
    ) -> None:
        mock_urlopen.return_value = _make_urlopen_response({"status": 1})
        result = svc._post_to_pushover(
            "uk", "at", "Title", "Message", PRIORITY_NORMAL,
        )
        assert result.sent is True
        assert result.status_code == 200
        assert result.error is None

    @patch("journal.services.notifications.urllib.request.urlopen")
    def test_invalid_credentials(
        self, mock_urlopen: MagicMock, svc: PushoverNotificationService
    ) -> None:
        mock_urlopen.return_value = _make_urlopen_response(
            {"status": 0, "errors": ["user key is invalid"]}
        )
        result = svc._post_to_pushover(
            "bad-key", "at", "Title", "Msg", PRIORITY_NORMAL,
        )
        assert result.sent is False
        assert "invalid" in (result.error or "")

    @patch("journal.services.notifications.urllib.request.urlopen")
    def test_network_error(
        self, mock_urlopen: MagicMock, svc: PushoverNotificationService
    ) -> None:
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")
        result = svc._post_to_pushover(
            "uk", "at", "Title", "Msg", PRIORITY_NORMAL,
        )
        assert result.sent is False
        assert "Connection refused" in (result.error or "")

    @patch("journal.services.notifications.urllib.request.urlopen")
    def test_truncates_long_title_and_message(
        self, mock_urlopen: MagicMock, svc: PushoverNotificationService
    ) -> None:
        mock_urlopen.return_value = _make_urlopen_response({"status": 1})
        svc._post_to_pushover("uk", "at", "T" * 300, "M" * 2000, 0)
        # Verify the request was made (no exception from truncation)
        mock_urlopen.assert_called_once()


class TestNotifyJobSuccess:
    @patch("journal.services.notifications.urllib.request.urlopen")
    def test_ingestion_success_sends_notification(
        self, mock_urlopen: MagicMock, svc: PushoverNotificationService,
        mock_user_repo: MagicMock,
    ) -> None:
        mock_urlopen.return_value = _make_urlopen_response({"status": 1})
        svc.notify_job_success(1, "ingest_images", {"entry_id": 42})
        mock_urlopen.assert_called_once()

    @patch("journal.services.notifications.urllib.request.urlopen")
    def test_ingestion_disabled_topic_skips(
        self, mock_urlopen: MagicMock, svc: PushoverNotificationService,
        mock_user_repo: MagicMock,
    ) -> None:
        mock_user_repo.get_preference.return_value = False
        svc.notify_job_success(1, "ingest_images", {"entry_id": 42})
        mock_urlopen.assert_not_called()

    @patch("journal.services.notifications.urllib.request.urlopen")
    def test_standalone_batch_always_fires(
        self, mock_urlopen: MagicMock, svc: PushoverNotificationService,
    ) -> None:
        """Entity extraction has no toggleable topic — always notifies."""
        mock_urlopen.return_value = _make_urlopen_response({"status": 1})
        svc.notify_job_success(1, "entity_extraction", {"entries_processed": 5})
        mock_urlopen.assert_called_once()

    @patch("journal.services.notifications.urllib.request.urlopen")
    def test_no_credentials_skips(
        self, mock_urlopen: MagicMock, mock_user_repo: MagicMock,
    ) -> None:
        svc = PushoverNotificationService(
            user_repo=mock_user_repo,
            default_user_key="",
            default_app_token="",
        )
        svc.notify_job_success(1, "ingest_images", {"entry_id": 1})
        mock_urlopen.assert_not_called()


class TestNotifyJobRetrying:
    @patch("journal.services.notifications.urllib.request.urlopen")
    def test_sends_retry_notification(
        self, mock_urlopen: MagicMock, svc: PushoverNotificationService,
    ) -> None:
        mock_urlopen.return_value = _make_urlopen_response({"status": 1})
        exc = Exception("503 UNAVAILABLE high demand")
        svc.notify_job_retrying(1, "ingest_images", 1, 180, "OCR service overloaded", exc)
        mock_urlopen.assert_called_once()

    @patch("journal.services.notifications.urllib.request.urlopen")
    def test_disabled_topic_skips(
        self, mock_urlopen: MagicMock, svc: PushoverNotificationService,
        mock_user_repo: MagicMock,
    ) -> None:
        mock_user_repo.get_preference.return_value = False
        svc.notify_job_retrying(1, "ingest_images", 1, 180, "error", None)
        mock_urlopen.assert_not_called()


class TestNotifyJobFailed:
    @patch("journal.services.notifications.urllib.request.urlopen")
    def test_sends_failure_with_high_priority(
        self, mock_urlopen: MagicMock, svc: PushoverNotificationService,
    ) -> None:
        mock_urlopen.return_value = _make_urlopen_response({"status": 1})
        svc.notify_job_failed(1, "ingest_images", "API overloaded", None)
        mock_urlopen.assert_called_once()
        # Check priority=1 (high) in the POST data
        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        assert b"priority=1" in req.data

    @patch("journal.services.notifications.urllib.request.urlopen")
    def test_classifies_external_error(
        self, mock_urlopen: MagicMock, svc: PushoverNotificationService,
    ) -> None:
        mock_urlopen.return_value = _make_urlopen_response({"status": 1})
        exc = Exception("503 UNAVAILABLE high demand")
        svc.notify_job_failed(1, "ingest_images", "OCR overloaded", exc)
        req = mock_urlopen.call_args[0][0]
        assert b"External+API+issue" in req.data or b"External" in req.data


class TestNotifyAdminJobFailed:
    @patch("journal.services.notifications.urllib.request.urlopen")
    def test_fans_out_to_admins(
        self, mock_urlopen: MagicMock, svc: PushoverNotificationService,
        mock_user_repo: MagicMock,
    ) -> None:
        mock_urlopen.return_value = _make_urlopen_response({"status": 1})
        admin1 = User(id=1, email="a@b.com", display_name="Admin1", is_admin=True)
        admin2 = User(id=2, email="c@d.com", display_name="Admin2", is_admin=True)
        regular = User(id=3, email="e@f.com", display_name="Regular", is_admin=False)
        mock_user_repo.list_users.return_value = [admin1, admin2, regular]
        svc.notify_admin_job_failed(99, "ingest_images", "fail", None)
        # Should send to 2 admin users
        assert mock_urlopen.call_count == 2

    @patch("journal.services.notifications.urllib.request.urlopen")
    def test_no_admins_no_calls(
        self, mock_urlopen: MagicMock, svc: PushoverNotificationService,
        mock_user_repo: MagicMock,
    ) -> None:
        mock_user_repo.list_users.return_value = []
        svc.notify_admin_job_failed(99, "ingest_images", "fail", None)
        mock_urlopen.assert_not_called()


class TestNotifyHealthAlert:
    @patch("journal.services.notifications.urllib.request.urlopen")
    def test_sends_to_admins(
        self, mock_urlopen: MagicMock, svc: PushoverNotificationService,
        mock_user_repo: MagicMock,
    ) -> None:
        mock_urlopen.return_value = _make_urlopen_response({"status": 1})
        admin = User(id=1, email="a@b.com", display_name="Admin", is_admin=True)
        mock_user_repo.list_users.return_value = [admin]
        svc.notify_health_alert("sqlite", "SELECT 1 failed")
        mock_urlopen.assert_called_once()


class TestValidateCredentials:
    @patch("journal.services.notifications.urllib.request.urlopen")
    def test_valid_credentials(
        self, mock_urlopen: MagicMock, svc: PushoverNotificationService,
    ) -> None:
        mock_urlopen.return_value = _make_urlopen_response({"status": 1})
        result = svc.validate_credentials("valid-key", "valid-token")
        assert result.sent is True

    @patch("journal.services.notifications.urllib.request.urlopen")
    def test_invalid_credentials(
        self, mock_urlopen: MagicMock, svc: PushoverNotificationService,
    ) -> None:
        mock_urlopen.return_value = _make_urlopen_response(
            {"status": 0, "errors": ["user key is invalid"]},
        )
        result = svc.validate_credentials("bad", "bad")
        assert result.sent is False
        assert "invalid" in (result.error or "")

    @patch("journal.services.notifications.urllib.request.urlopen")
    def test_http_error(
        self, mock_urlopen: MagicMock, svc: PushoverNotificationService,
    ) -> None:
        error_body = json.dumps({"status": 0, "errors": ["invalid token"]}).encode()
        http_err = urllib.error.HTTPError(
            url="", code=400, msg="Bad Request",
            hdrs=None, fp=MagicMock(read=MagicMock(return_value=error_body)),
        )
        mock_urlopen.side_effect = http_err
        result = svc.validate_credentials("bad", "bad")
        assert result.sent is False
        assert result.status_code == 400


class TestGetTopicsForUser:
    def test_non_admin_sees_no_admin_topics(
        self, svc: PushoverNotificationService,
    ) -> None:
        topics = svc.get_topics_for_user(1, is_admin=False)
        assert all(not t["admin_only"] for t in topics)
        assert len(topics) == 4  # 2 success + 2 failure

    def test_admin_sees_all_topics(
        self, svc: PushoverNotificationService,
    ) -> None:
        topics = svc.get_topics_for_user(1, is_admin=True)
        assert len(topics) == len(TOPICS)

    def test_enabled_state_from_preferences(
        self, svc: PushoverNotificationService, mock_user_repo: MagicMock,
    ) -> None:
        def pref_side_effect(user_id: int, key: str):
            if key == "notif_job_failed":
                return False
            return None
        mock_user_repo.get_preference.side_effect = pref_side_effect
        topics = svc.get_topics_for_user(1, is_admin=False)
        failed_topic = next(t for t in topics if t["key"] == "notif_job_failed")
        assert failed_topic["enabled"] is False


class TestHasCredentials:
    def test_has_credentials_with_defaults(
        self, svc: PushoverNotificationService,
    ) -> None:
        assert svc.has_credentials(1) is True

    def test_no_credentials(self, mock_user_repo: MagicMock) -> None:
        svc = PushoverNotificationService(
            user_repo=mock_user_repo,
            default_user_key="",
            default_app_token="",
        )
        assert svc.has_credentials(1) is False


class TestBuildSuccessMessage:
    def test_ingestion_message_without_followups(self, svc: PushoverNotificationService) -> None:
        msg = svc._build_success_message("ingest_images", {"entry_id": 42})
        assert "Entry 42" in msg
        assert "complete" in msg.lower()

    def test_ingestion_message_with_pipeline_results(
        self, svc: PushoverNotificationService,
    ) -> None:
        """Combined pipeline result includes mood + entity summaries."""
        result = {
            "entry_id": 76,
            "mood_scoring_result": {"scores_written": 7},
            "entity_extraction_result": {
                "entities_created": 8,
                "mentions_created": 18,
            },
        }
        msg = svc._build_success_message("ingest_audio", result)
        assert "Entry 76" in msg
        assert "7 mood scores" in msg
        assert "8 entities" in msg
        assert "18 mentions" in msg
        # Should NOT contain the generic fallback when follow-up results exist
        assert "all processing complete" not in msg.lower()

    def test_entity_extraction_message(self, svc: PushoverNotificationService) -> None:
        msg = svc._build_success_message(
            "entity_extraction",
            {"entries_processed": 5, "entities_created": 3, "mentions_created": 10},
        )
        assert "5 entries" in msg
        assert "3 entities" in msg

    def test_mood_backfill_message(self, svc: PushoverNotificationService) -> None:
        msg = svc._build_success_message(
            "mood_backfill", {"scored": 10, "skipped": 2},
        )
        assert "10 entries scored" in msg

    def test_fallback_message(self, svc: PushoverNotificationService) -> None:
        msg = svc._build_success_message("unknown_type", {})
        assert "completed successfully" in msg.lower()
