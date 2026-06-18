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
    build_success_message,
    post_to_pushover,
    resolve_credentials,
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
    """resolve_credentials is a module-level helper exercised directly."""

    def test_falls_back_to_defaults(self, mock_user_repo: MagicMock) -> None:
        mock_user_repo.get_preference.return_value = None
        key, token = resolve_credentials(
            mock_user_repo, 1,
            default_user_key="default-user-key",
            default_app_token="default-app-token",
        )
        assert key == "default-user-key"
        assert token == "default-app-token"

    def test_per_user_overrides_defaults(self, mock_user_repo: MagicMock) -> None:
        def pref_side_effect(user_id: int, key: str):
            return {
                "pushover_user_key": "user-key",
                "pushover_app_token": "user-token",
            }.get(key)
        mock_user_repo.get_preference.side_effect = pref_side_effect
        key, token = resolve_credentials(
            mock_user_repo, 1,
            default_user_key="default-user-key",
            default_app_token="default-app-token",
        )
        assert key == "user-key"
        assert token == "user-token"

    def test_empty_defaults_return_empty(self, mock_user_repo: MagicMock) -> None:
        key, token = resolve_credentials(
            mock_user_repo, 1,
            default_user_key="",
            default_app_token="",
        )
        assert key == ""
        assert token == ""


class TestTopicGating:
    """Topic-preference gating, exercised through the public notify_* API.

    Each notify_* method calls the internal topic-enabled predicate
    against a fixed topic key; if the user has opted out, no Pushover
    POST happens. We assert the side effect (POST attempted or not)
    instead of poking the predicate directly.
    """

    @patch("journal.services.notifications.urllib.request.urlopen")
    def test_default_true_topic_posts_when_no_preference(
        self,
        mock_urlopen: MagicMock,
        svc: PushoverNotificationService,
        mock_user_repo: MagicMock,
    ) -> None:
        # notif_job_failed defaults to True; with no preference set the
        # gating must allow the post.
        mock_user_repo.get_preference.return_value = None
        mock_urlopen.return_value = _make_urlopen_response({"status": 1})

        svc.notify_job_failed(
            user_id=1, job_type="ingest_images", error_message="boom",
        )

        # urlopen called → topic was enabled (no preference, fell back to default).
        # Filter to the POST that carries credentials so we don't trip
        # on incidental urlopen calls from other layers.
        post_calls = [c for c in mock_urlopen.call_args_list if c.args]
        assert post_calls, "expected notify_job_failed to POST to Pushover"

    @patch("journal.services.notifications.urllib.request.urlopen")
    def test_explicit_false_skips_post(
        self,
        mock_urlopen: MagicMock,
        svc: PushoverNotificationService,
        mock_user_repo: MagicMock,
    ) -> None:
        mock_user_repo.get_preference.return_value = False

        svc.notify_job_failed(
            user_id=1, job_type="ingest_images", error_message="boom",
        )

        assert not mock_urlopen.called, (
            "user opted out of notif_job_failed but a POST was attempted"
        )

    @patch("journal.services.notifications.urllib.request.urlopen")
    def test_explicit_true_posts_even_for_default_off_topic(
        self,
        mock_urlopen: MagicMock,
        svc: PushoverNotificationService,
        mock_user_repo: MagicMock,
    ) -> None:
        # notif_job_success_entity_reembed defaults to False. Explicit
        # True should flip the gate open and let the POST through. This
        # exercises both "default off" and "explicit override" branches
        # in one assertion.
        reembed_default = next(
            t for t in TOPICS
            if t["key"] == "notif_job_success_entity_reembed"
        )["default"]
        assert reembed_default is False, (
            "fixture invalidated — notif_job_success_entity_reembed "
            "default changed; pick another default-False topic"
        )

        # Stub credentials so the POST path doesn't bail on missing key.
        def _pref(_user_id: int, key: str) -> object:
            if key == "notif_job_success_entity_reembed":
                return True
            if key in ("pushover_user_key", "pushover_app_token"):
                # Fall through to the service's defaults so credentials resolve.
                return None
            return None

        mock_user_repo.get_preference.side_effect = _pref
        mock_urlopen.return_value = _make_urlopen_response({"status": 1})

        svc.notify_job_success(
            user_id=1, job_type="entity_reembed",
            result={"entity_id": 7, "name": "Alice"},
        )

        assert mock_urlopen.called, (
            "user explicitly enabled notif_job_success_entity_reembed "
            "but no POST was attempted"
        )


class TestPostToPushover:
    @patch("journal.services.notifications.urllib.request.urlopen")
    def test_success(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _make_urlopen_response({"status": 1})
        result = post_to_pushover(
            "uk", "at", "Title", "Message", PRIORITY_NORMAL,
        )
        assert result.sent is True
        assert result.status_code == 200
        assert result.error is None

    @patch("journal.services.notifications.urllib.request.urlopen")
    def test_invalid_credentials(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _make_urlopen_response(
            {"status": 0, "errors": ["user key is invalid"]}
        )
        result = post_to_pushover(
            "bad-key", "at", "Title", "Msg", PRIORITY_NORMAL,
        )
        assert result.sent is False
        assert "invalid" in (result.error or "")

    @patch("journal.services.notifications.urllib.request.urlopen")
    def test_network_error(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")
        result = post_to_pushover(
            "uk", "at", "Title", "Msg", PRIORITY_NORMAL,
        )
        assert result.sent is False
        assert "Connection refused" in (result.error or "")

    @patch("journal.services.notifications.urllib.request.urlopen")
    def test_truncates_long_title_and_message(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _make_urlopen_response({"status": 1})
        post_to_pushover("uk", "at", "T" * 300, "M" * 2000, 0)
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
    def test_save_entry_pipeline_success_gated_by_dedicated_topic(
        self, mock_urlopen: MagicMock, svc: PushoverNotificationService,
        mock_user_repo: MagicMock,
    ) -> None:
        """The save-entry pipeline (entity extraction + mood analysis +
        embedding reprocessing after an edit) gets its own dedicated
        success toggle, separate from ingestion success notifications."""
        def pref_side_effect(user_id: int, key: str):
            if key == "notif_job_success_save_entry":
                return False
            return None

        mock_user_repo.get_preference.side_effect = pref_side_effect
        svc.notify_job_success(
            1, "save_entry_pipeline", {"entry_id": 42, "chunk_count": 3},
        )
        mock_urlopen.assert_not_called()

    @patch("journal.services.notifications.urllib.request.urlopen")
    def test_save_entry_pipeline_success_fires_when_enabled(
        self, mock_urlopen: MagicMock, svc: PushoverNotificationService,
        mock_user_repo: MagicMock,
    ) -> None:
        """When the save-entry success topic is left at default (True),
        the notification fires after a save."""
        mock_urlopen.return_value = _make_urlopen_response({"status": 1})
        # Default: get_preference returns None → topic uses default (True)
        svc.notify_job_success(
            1, "save_entry_pipeline", {"entry_id": 42, "chunk_count": 3},
        )
        mock_urlopen.assert_called_once()

    @patch("journal.services.notifications.urllib.request.urlopen")
    def test_save_entry_pipeline_success_independent_of_ingest_topics(
        self, mock_urlopen: MagicMock, svc: PushoverNotificationService,
        mock_user_repo: MagicMock,
    ) -> None:
        """Disabling image ingestion success must not silence save-entry
        pipeline success (or vice versa) — the toggles are independent."""
        def pref_side_effect(user_id: int, key: str):
            if key == "notif_job_success_ingest_images":
                return False
            return None

        mock_user_repo.get_preference.side_effect = pref_side_effect
        mock_urlopen.return_value = _make_urlopen_response({"status": 1})
        svc.notify_job_success(
            1, "save_entry_pipeline", {"entry_id": 42, "chunk_count": 3},
        )
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
        # 5 success (ingest_images, ingest_audio, save_entry,
        #            entity_reembed, fitness_sync_success)
        # 5 failure (job_retrying, job_failed, job_failed_save_entry,
        #            fitness_auth_broken, fitness_sync_failure)
        assert len(topics) == 10

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
    def test_ingestion_message_without_followups(self) -> None:
        msg = build_success_message("ingest_images", {"entry_id": 42})
        assert "- Created Entry 42" in msg
        # Bullet-formatted: every line starts with "- "
        assert all(line.startswith("- ") for line in msg.splitlines())

    def test_ingestion_message_with_pipeline_results(self) -> None:
        """Combined pipeline result includes mood + entity summaries."""
        result = {
            "entry_id": 76,
            "mood_scoring_result": {"scores_written": 7},
            "entity_extraction_result": {
                "entities_created": 8,
                "mentions_created": 18,
            },
        }
        msg = build_success_message("ingest_audio", result)
        assert "- Created Entry 76" in msg
        assert "- Created 8 entities" in msg
        assert "- Recorded 18 mentions" in msg
        # Mood scores are constant (7 dimensions) — only the verb-led
        # bullet is shown, no count.
        assert "- Calculated mood scores" in msg
        assert "7 mood scores" not in msg
        # Should NOT contain the generic fallback when follow-up results exist
        assert "completed all processing" not in msg.lower()

    def test_entity_extraction_message(self) -> None:
        msg = build_success_message(
            "entity_extraction",
            {"entries_processed": 5, "entities_created": 3, "mentions_created": 10},
        )
        assert "- Processed 5 entries" in msg
        assert "- Created 3 entities" in msg
        assert "- Recorded 10 mentions" in msg

    def test_mood_backfill_message(self) -> None:
        msg = build_success_message(
            "mood_backfill", {"scored": 10, "skipped": 2},
        )
        assert "- Scored 10 entries" in msg
        assert "- Skipped 2 entries" in msg

    def test_mood_score_entry_message_omits_constant_count(self) -> None:
        """Per-entry mood scoring always produces the same fixed number
        of scores (one per mood dimension), so the count is not shown."""
        msg = build_success_message(
            "mood_score_entry", {"scores_written": 7},
        )
        assert msg == "- Calculated mood scores"

    def test_reprocess_embeddings_message(self) -> None:
        msg = build_success_message(
            "reprocess_embeddings", {"chunk_count": 4},
        )
        assert msg == "- Reprocessed 4 chunks"

    def test_fallback_message(self) -> None:
        msg = build_success_message("unknown_type", {})
        assert msg == "- Completed successfully"

    def test_save_entry_pipeline_success_message(self) -> None:
        """Save-entry pipeline (edit flow) success summary covers all 3 stages
        with explicit per-line bullets."""
        result = {
            "entry_id": 76,
            "follow_up_jobs": {
                "reprocess_embeddings": "r1",
                "entity_extraction": "e1",
                "mood_scoring": "m1",
            },
            "reprocess_embeddings_result": {
                "entry_id": 76, "chunk_count": 4,
            },
            "entity_extraction_result": {
                "entries_processed": 1,
                "entities_created": 2,
                "entities_matched": 7,
                "entities_deleted": 1,
                "mentions_created": 19,
            },
            "mood_scoring_result": {"entry_id": 76, "scores_written": 3},
        }
        msg = build_success_message("save_entry_pipeline", result)
        assert "- Updated Entry 76" in msg
        assert "- Reprocessed 4 chunks" in msg
        assert "- Created 2 entities" in msg
        assert "- Deleted 1 entities" in msg
        # Total = created (2) + matched (7) = 9
        assert "- Total: 9 entities" in msg
        assert "- Recorded 19 mentions" in msg
        # Mood-scores count is constant per entry — only the verb is shown
        assert "- Calculated mood scores" in msg
        assert "Mood scores: 3" not in msg

    def test_save_entry_pipeline_message_handles_missing_entities_deleted(self) -> None:
        """Older-format payloads without entities_deleted default the line
        to 0 rather than crashing."""
        result = {
            "entry_id": 76,
            "reprocess_embeddings_result": {"chunk_count": 4},
            "entity_extraction_result": {
                "entries_processed": 1,
                "entities_created": 2,
                "entities_matched": 0,
                # entities_deleted missing
                "mentions_created": 5,
            },
            "mood_scoring_result": {"scores_written": 3},
        }
        msg = build_success_message("save_entry_pipeline", result)
        assert "- Deleted 0 entities" in msg
        assert "- Total: 2 entities" in msg

    def test_save_entry_pipeline_message_omits_missing_stages(self) -> None:
        """If a stage has no result (e.g. mood disabled), it's omitted."""
        result = {
            "entry_id": 76,
            "reprocess_embeddings_result": {"chunk_count": 4},
            "entity_extraction_result": {
                "entries_processed": 1,
                "entities_created": 2,
                "entities_matched": 0,
                "entities_deleted": 0,
                "mentions_created": 5,
            },
            # No mood_scoring_result
        }
        msg = build_success_message("save_entry_pipeline", result)
        assert "- Updated Entry 76" in msg
        assert "mood" not in msg.lower()


class TestBuildPipelineFailureBody:
    """Module-level build_pipeline_failure_body helper used by JobRunner."""

    def test_partial_failure_lists_successes_and_failures(self) -> None:
        from journal.services.notifications import build_pipeline_failure_body

        combined = {
            "entry_id": 76,
            "reprocess_embeddings_result": {"chunk_count": 4},
            "entity_extraction_result": {
                "entries_processed": 1,
                "entities_created": 2,
                "entities_matched": 7,
                "entities_deleted": 1,
                "mentions_created": 19,
            },
        }
        failures = {"mood_scoring": "LLM overloaded"}

        body = build_pipeline_failure_body(
            "save_entry_pipeline", combined, failures,
        )
        assert "Entry 76 update" in body
        assert "partial failure" in body
        # Successes prefixed with ✓, failures with ✗
        assert "✓ Reprocessed 4 chunks" in body
        assert "✓ Created 2 entities" in body
        assert "✓ Deleted 1 entities" in body
        assert "✓ Total: 9 entities" in body
        assert "✓ Recorded 19 mentions" in body
        assert "✗ Mood scoring: LLM overloaded" in body

    def test_total_failure_uses_failed_header(self) -> None:
        from journal.services.notifications import build_pipeline_failure_body

        combined = {"entry_id": 76}
        failures = {
            "reprocess_embeddings": "reprocess broke",
            "entity_extraction": "extraction broke",
            "mood_scoring": "mood broke",
        }
        body = build_pipeline_failure_body(
            "save_entry_pipeline", combined, failures,
        )
        assert "Entry 76 update failed" in body
        assert "partial failure" not in body
        assert "✗ Reprocess: reprocess broke" in body
        assert "✗ Entity extraction: extraction broke" in body
        assert "✗ Mood scoring: mood broke" in body

    def test_unknown_parent_type_uses_generic_header(self) -> None:
        from journal.services.notifications import build_pipeline_failure_body

        body = build_pipeline_failure_body(
            "weird_pipeline", {"entry_id": 1}, {"reprocess_embeddings": "boom"},
        )
        assert "weird_pipeline" in body
        assert "partial failure" in body


class TestNotifyPipelineFailed:
    """notify_pipeline_failed posts a single high-priority Pushover with
    the caller-built body (no automatic 'Internal error: ' prefix)."""

    def test_posts_high_priority_with_caller_body(
        self,
        svc: PushoverNotificationService,
        mock_user_repo: MagicMock,
    ) -> None:
        body = "Entry 76 update — partial failure\n✓ Reprocessed 4 chunks"
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _make_urlopen_response({"status": 1})
            svc.notify_pipeline_failed(
                user_id=1, parent_job_type="save_entry_pipeline", body=body,
            )

            assert mock_urlopen.called
            req = mock_urlopen.call_args[0][0]
            posted_data = req.data.decode()
            # Title uses the parent job's label
            assert "Entry+update+failed" in posted_data
            # Body is exactly what we passed (no cause-tag prefix).
            # "Reprocessed 4 chunks" serialises to "Reprocessed+4+chunks"
            # in the form-encoded request body.
            assert "Reprocessed+4+chunks" in posted_data
            assert "Internal+error" not in posted_data
            # High priority
            assert "priority=1" in posted_data

    def test_skips_when_save_entry_failure_topic_disabled(
        self,
        svc: PushoverNotificationService,
        mock_user_repo: MagicMock,
    ) -> None:
        """Save-entry pipeline failures are gated by their own dedicated
        topic — toggling it off mutes save-entry failures specifically,
        leaving other failure notifications alone."""
        def pref_side_effect(user_id: int, key: str):
            if key == "notif_job_failed_save_entry":
                return False
            return None

        mock_user_repo.get_preference.side_effect = pref_side_effect
        with patch("urllib.request.urlopen") as mock_urlopen:
            svc.notify_pipeline_failed(1, "save_entry_pipeline", "body")
            mock_urlopen.assert_not_called()

    def test_save_entry_failure_not_gated_by_global_failed_topic(
        self,
        svc: PushoverNotificationService,
        mock_user_repo: MagicMock,
    ) -> None:
        """Disabling the global notif_job_failed must not silence
        save-entry pipeline failures — those have their own dedicated
        toggle. (Otherwise the user could silence save-entry pipeline
        failures only by silencing every job failure.)"""
        def pref_side_effect(user_id: int, key: str):
            if key == "notif_job_failed":
                return False
            return None  # save-entry-specific topic stays at default (True)

        mock_user_repo.get_preference.side_effect = pref_side_effect
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _make_urlopen_response({"status": 1})
            svc.notify_pipeline_failed(1, "save_entry_pipeline", "body")
            mock_urlopen.assert_called_once()

    def test_skips_when_no_credentials(
        self,
        mock_user_repo: MagicMock,
    ) -> None:
        svc = PushoverNotificationService(
            user_repo=mock_user_repo,
            default_user_key="",
            default_app_token="",
        )
        with patch("urllib.request.urlopen") as mock_urlopen:
            svc.notify_pipeline_failed(1, "save_entry_pipeline", "body")
            mock_urlopen.assert_not_called()


class TestFitnessNotificationTopics:
    """Verifies the four fitness topics added in W3 are wired into the
    TOPICS list and into the success-routing map (per W8 — without the
    map update, notify_job_success would always-notify and ignore the
    user's opt-in default for `notif_fitness_sync_success`)."""

    def test_fitness_topics_present(self) -> None:
        keys = {t["key"] for t in TOPICS}
        assert "notif_fitness_auth_broken" in keys
        assert "notif_fitness_sync_failure" in keys
        assert "notif_fitness_normalize_drift" in keys
        assert "notif_fitness_sync_success" in keys

    def test_auth_broken_user_visible_default_on(self) -> None:
        topic = next(t for t in TOPICS if t["key"] == "notif_fitness_auth_broken")
        assert topic["default"] is True
        assert topic["admin_only"] is False
        assert topic["group"] == "failure"

    def test_normalize_drift_admin_only(self) -> None:
        topic = next(t for t in TOPICS if t["key"] == "notif_fitness_normalize_drift")
        assert topic["admin_only"] is True
        assert topic["group"] == "admin"

    def test_sync_success_defaults_off_opt_in(self) -> None:
        topic = next(t for t in TOPICS if t["key"] == "notif_fitness_sync_success")
        assert topic["default"] is False

    def test_success_topic_map_routes_both_sources(self) -> None:
        from journal.services.notifications import _SUCCESS_TOPIC_MAP
        assert _SUCCESS_TOPIC_MAP["fitness_sync_strava"] == "notif_fitness_sync_success"
        assert _SUCCESS_TOPIC_MAP["fitness_sync_garmin"] == "notif_fitness_sync_success"

    def test_job_type_labels_present(self) -> None:
        from journal.services.notifications import _JOB_TYPE_LABELS
        assert "fitness_sync_strava" in _JOB_TYPE_LABELS
        assert "fitness_sync_garmin" in _JOB_TYPE_LABELS


class TestNotifyFitnessAuthBroken:
    """notify_fitness_auth_broken is a fire-once topic — caller is
    responsible for invoking only on transition. The notification
    service just gates on the topic toggle."""

    def test_posts_high_priority_with_source_label(
        self,
        svc: PushoverNotificationService,
        mock_user_repo: MagicMock,
    ) -> None:
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _make_urlopen_response({"status": 1})
            svc.notify_fitness_auth_broken(user_id=1, source="strava")

            assert mock_urlopen.called
            req = mock_urlopen.call_args[0][0]
            posted_data = req.data.decode()
            assert "Strava+re-auth+needed" in posted_data
            assert "priority=1" in posted_data  # PRIORITY_HIGH

    def test_skips_when_topic_disabled(
        self,
        svc: PushoverNotificationService,
        mock_user_repo: MagicMock,
    ) -> None:
        def pref_side_effect(user_id: int, key: str):
            return False if key == "notif_fitness_auth_broken" else None

        mock_user_repo.get_preference.side_effect = pref_side_effect
        with patch("urllib.request.urlopen") as mock_urlopen:
            svc.notify_fitness_auth_broken(1, "strava")
            mock_urlopen.assert_not_called()


class TestNotifyFitnessSyncFailure:
    """notify_fitness_sync_failure fires after N consecutive failures."""

    def test_posts_normal_priority_with_attempts_count(
        self,
        svc: PushoverNotificationService,
        mock_user_repo: MagicMock,
    ) -> None:
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _make_urlopen_response({"status": 1})
            svc.notify_fitness_sync_failure(user_id=1, source="garmin", attempts=3)

            assert mock_urlopen.called
            req = mock_urlopen.call_args[0][0]
            posted_data = req.data.decode()
            assert "Garmin+sync+failing" in posted_data
            assert "failed+3+times" in posted_data
            assert f"priority={PRIORITY_NORMAL}" in posted_data

    def test_skips_when_topic_disabled(
        self,
        svc: PushoverNotificationService,
        mock_user_repo: MagicMock,
    ) -> None:
        def pref_side_effect(user_id: int, key: str):
            return False if key == "notif_fitness_sync_failure" else None

        mock_user_repo.get_preference.side_effect = pref_side_effect
        with patch("urllib.request.urlopen") as mock_urlopen:
            svc.notify_fitness_sync_failure(1, "garmin", 3)
            mock_urlopen.assert_not_called()


class TestNotifyFitnessNormalizeDrift:
    """notify_fitness_normalize_drift is admin-only and fires once per batch."""

    def test_posts_to_admin_when_topic_enabled(
        self,
        svc: PushoverNotificationService,
        mock_user_repo: MagicMock,
    ) -> None:
        admin = User(
            id=42, email="admin@local",
            display_name="admin", is_admin=True, is_active=True,
        )
        mock_user_repo.list_users.return_value = [admin]
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _make_urlopen_response({"status": 1})
            svc.notify_fitness_normalize_drift(source="strava", drift_count=4)

            assert mock_urlopen.called
            req = mock_urlopen.call_args[0][0]
            posted_data = req.data.decode()
            assert "Strava+normalize+drift" in posted_data
            assert "4+Strava+raw+row" in posted_data

    def test_skips_when_admin_topic_disabled(
        self,
        svc: PushoverNotificationService,
        mock_user_repo: MagicMock,
    ) -> None:
        admin = User(
            id=42, email="admin@local",
            display_name="admin", is_admin=True, is_active=True,
        )
        mock_user_repo.list_users.return_value = [admin]

        def pref_side_effect(user_id: int, key: str):
            return False if key == "notif_fitness_normalize_drift" else None

        mock_user_repo.get_preference.side_effect = pref_side_effect
        with patch("urllib.request.urlopen") as mock_urlopen:
            svc.notify_fitness_normalize_drift("strava", 4)
            mock_urlopen.assert_not_called()
