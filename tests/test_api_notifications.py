"""REST API tests for the /api/notifications/* routes (W15).

Exercises ``register_notifications_routes`` on a real Starlette test
client with a MagicMock notification service and user repo, mirroring
the ``_FakeAuthMiddleware`` pattern from ``test_api_storylines.py``.

Pinned behaviours:

- ``GET /topics`` passes ``(user_id, is_admin)`` through to
  ``get_topics_for_user`` and 503s when the notification service is
  absent from the services dict.
- ``GET /status`` is deliberately asymmetric with topics: a missing
  notification service yields ``200 {"configured": false}``, not 503.
- ``POST /validate`` persists both Pushover preference keys only when
  validation succeeds, and persists nothing on failure.
- ``POST /test`` maps the no-credentials failure to 400.
- All four routes 503 when the services getter returns None.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, call

import pytest
from mcp.server.fastmcp import FastMCP
from starlette.testclient import TestClient

from journal.api.notifications import register_notifications_routes
from journal.auth import AuthenticatedUser, _current_user_id
from journal.services.notifications import NotificationResult

if TYPE_CHECKING:
    from collections.abc import Callable

_TEST_USER_ID = 1


class _FakeAuthMiddleware:
    """ASGI middleware that injects a test user (admin flag configurable)."""

    is_admin: bool = False

    def __init__(self, app: Any) -> None:  # noqa: ANN401
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401
        if scope["type"] in ("http", "websocket"):
            scope["user"] = AuthenticatedUser(
                user_id=_TEST_USER_ID,
                email="test@example.com",
                display_name="Test User",
                is_admin=self.is_admin,
                is_active=True,
                email_verified=True,
            )
            token = _current_user_id.set(_TEST_USER_ID)
            try:
                await self.app(scope, receive, send)
            finally:
                _current_user_id.reset(token)
        else:
            await self.app(scope, receive, send)


class _FakeAdminAuthMiddleware(_FakeAuthMiddleware):
    is_admin = True


def _make_client(
    services_getter: Callable[[], dict | None],
    *,
    is_admin: bool = False,
) -> TestClient:
    mcp = FastMCP("test-notifications")
    register_notifications_routes(mcp, services_getter)
    middleware = _FakeAdminAuthMiddleware if is_admin else _FakeAuthMiddleware
    app = middleware(mcp.streamable_http_app())
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def notif() -> MagicMock:
    """Notification service double with realistic return values."""
    service = MagicMock()
    service.get_topics_for_user.return_value = [
        {"key": "notif_job_complete", "enabled": True},
    ]
    service.has_credentials.return_value = False
    service.validate_credentials.return_value = NotificationResult(sent=True)
    service.send_test_notification.return_value = NotificationResult(sent=True)
    return service


@pytest.fixture
def user_repo() -> MagicMock:
    return MagicMock()


@pytest.fixture
def services(notif: MagicMock, user_repo: MagicMock) -> dict[str, Any]:
    return {"notification_service": notif, "user_repo": user_repo}


@pytest.fixture
def client(services: dict[str, Any]) -> TestClient:
    return _make_client(lambda: services)


class TestGetTopics:
    def test_topics_non_admin_passes_is_admin_false(
        self, client: TestClient, notif: MagicMock,
    ) -> None:
        resp = client.get("/api/notifications/topics")
        assert resp.status_code == 200
        assert resp.json() == {
            "topics": [{"key": "notif_job_complete", "enabled": True}],
        }
        notif.get_topics_for_user.assert_called_once_with(_TEST_USER_ID, False)

    def test_topics_admin_passes_is_admin_true(
        self, services: dict[str, Any], notif: MagicMock,
    ) -> None:
        admin_client = _make_client(lambda: services, is_admin=True)
        resp = admin_client.get("/api/notifications/topics")
        assert resp.status_code == 200
        notif.get_topics_for_user.assert_called_once_with(_TEST_USER_ID, True)

    def test_topics_without_service_returns_503(
        self, user_repo: MagicMock,
    ) -> None:
        client = _make_client(lambda: {"user_repo": user_repo})
        resp = client.get("/api/notifications/topics")
        assert resp.status_code == 503
        assert resp.json() == {"error": "Notification service not configured"}


class TestGetStatus:
    @pytest.mark.parametrize("configured", [True, False])
    def test_status_reflects_has_credentials(
        self, client: TestClient, notif: MagicMock, configured: bool,
    ) -> None:
        notif.has_credentials.return_value = configured
        resp = client.get("/api/notifications/status")
        assert resp.status_code == 200
        assert resp.json() == {"configured": configured}
        notif.has_credentials.assert_called_once_with(_TEST_USER_ID)

    def test_status_without_service_is_200_not_configured(
        self, user_repo: MagicMock,
    ) -> None:
        """Asymmetry with /topics: a missing service is not an error here —
        the route reports the user simply has nothing configured."""
        client = _make_client(lambda: {"user_repo": user_repo})
        resp = client.get("/api/notifications/status")
        assert resp.status_code == 200
        assert resp.json() == {"configured": False}


class TestValidateCredentials:
    def test_invalid_json_returns_400(self, client: TestClient) -> None:
        resp = client.post(
            "/api/notifications/validate",
            content=b"not json",
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 400
        assert resp.json() == {"error": "Invalid JSON"}

    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"user_key": "uk"},
            {"app_token": "at"},
            {"user_key": "", "app_token": "at"},
            {"user_key": "uk", "app_token": ""},
        ],
    )
    def test_missing_fields_returns_400(
        self, client: TestClient, notif: MagicMock, body: dict[str, str],
    ) -> None:
        resp = client.post("/api/notifications/validate", json=body)
        assert resp.status_code == 400
        assert resp.json() == {
            "valid": False,
            "error": "Both user_key and app_token are required",
        }
        notif.validate_credentials.assert_not_called()

    def test_valid_credentials_saved_as_preferences(
        self, client: TestClient, notif: MagicMock, user_repo: MagicMock,
    ) -> None:
        notif.validate_credentials.return_value = NotificationResult(sent=True)
        resp = client.post(
            "/api/notifications/validate",
            json={"user_key": "uk-123", "app_token": "at-456"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"valid": True, "error": None}
        notif.validate_credentials.assert_called_once_with("uk-123", "at-456")
        assert user_repo.set_preference.call_count == 2
        user_repo.set_preference.assert_has_calls([
            call(_TEST_USER_ID, "pushover_user_key", "uk-123"),
            call(_TEST_USER_ID, "pushover_app_token", "at-456"),
        ])

    def test_invalid_credentials_not_saved(
        self, client: TestClient, notif: MagicMock, user_repo: MagicMock,
    ) -> None:
        notif.validate_credentials.return_value = NotificationResult(
            sent=False, error="user key is invalid",
        )
        resp = client.post(
            "/api/notifications/validate",
            json={"user_key": "uk-bad", "app_token": "at-bad"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"valid": False, "error": "user key is invalid"}
        user_repo.set_preference.assert_not_called()

    def test_validate_without_service_returns_503(
        self, user_repo: MagicMock,
    ) -> None:
        client = _make_client(lambda: {"user_repo": user_repo})
        resp = client.post(
            "/api/notifications/validate",
            json={"user_key": "uk", "app_token": "at"},
        )
        assert resp.status_code == 503


class TestSendTestNotification:
    def test_no_credentials_returns_400(
        self, client: TestClient, notif: MagicMock,
    ) -> None:
        notif.send_test_notification.return_value = NotificationResult(
            sent=False, error="No Pushover credentials configured",
        )
        resp = client.post("/api/notifications/test")
        assert resp.status_code == 400
        assert resp.json() == {
            "sent": False,
            "error": "No Pushover credentials configured",
        }

    def test_with_credentials_returns_sent_true(
        self, client: TestClient, notif: MagicMock,
    ) -> None:
        notif.send_test_notification.return_value = NotificationResult(sent=True)
        resp = client.post("/api/notifications/test")
        assert resp.status_code == 200
        assert resp.json() == {"sent": True, "error": None}
        notif.send_test_notification.assert_called_once_with(_TEST_USER_ID)

    def test_other_send_failures_return_200_with_error(
        self, client: TestClient, notif: MagicMock,
    ) -> None:
        """Only the no-credentials case maps to 400; transport failures
        surface as a 200 with sent=false and the error message."""
        notif.send_test_notification.return_value = NotificationResult(
            sent=False, status_code=500, error="Pushover unavailable",
        )
        resp = client.post("/api/notifications/test")
        assert resp.status_code == 200
        assert resp.json() == {"sent": False, "error": "Pushover unavailable"}

    def test_test_without_service_returns_503(
        self, user_repo: MagicMock,
    ) -> None:
        client = _make_client(lambda: {"user_repo": user_repo})
        resp = client.post("/api/notifications/test")
        assert resp.status_code == 503


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/notifications/topics"),
        ("GET", "/api/notifications/status"),
        ("POST", "/api/notifications/validate"),
        ("POST", "/api/notifications/test"),
    ],
)
def test_services_none_returns_503(method: str, path: str) -> None:
    client = _make_client(lambda: None)
    resp = client.request(method, path)
    assert resp.status_code == 503
    assert resp.json() == {"error": "Server not initialized"}
