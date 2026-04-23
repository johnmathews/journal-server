"""Tests for the health poller."""

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from journal.services.health_poll import HealthPoller, check_disk


class TestCheckDisk:
    @patch("journal.services.health_poll.shutil.disk_usage")
    def test_ok_when_plenty_of_space(self, mock_usage: MagicMock) -> None:
        mock_usage.return_value = MagicMock(free=2 * 1024 * 1024 * 1024)  # 2 GB
        result = check_disk(Path("/tmp/test.db"))
        assert result.status == "ok"
        assert result.name == "disk"

    @patch("journal.services.health_poll.shutil.disk_usage")
    def test_degraded_when_low_space(self, mock_usage: MagicMock) -> None:
        mock_usage.return_value = MagicMock(free=300 * 1024 * 1024)  # 300 MB
        result = check_disk(Path("/tmp/test.db"))
        assert result.status == "degraded"

    @patch("journal.services.health_poll.shutil.disk_usage")
    def test_error_when_very_low_space(self, mock_usage: MagicMock) -> None:
        mock_usage.return_value = MagicMock(free=50 * 1024 * 1024)  # 50 MB
        result = check_disk(Path("/tmp/test.db"))
        assert result.status == "error"

    @patch("journal.services.health_poll.shutil.disk_usage")
    def test_handles_os_error(self, mock_usage: MagicMock) -> None:
        mock_usage.side_effect = OSError("Permission denied")
        result = check_disk(Path("/nonexistent"))
        assert result.status == "error"
        assert "Permission denied" in (result.error or "")


class TestHealthPoller:
    @pytest.fixture
    def mock_conn(self) -> MagicMock:
        conn = MagicMock()
        row = MagicMock()
        row.__getitem__ = MagicMock(return_value=1)
        conn.execute.return_value.fetchone.return_value = row
        return conn

    @pytest.fixture
    def mock_vector_store(self) -> MagicMock:
        vs = MagicMock()
        vs.count.return_value = 100
        return vs

    @pytest.fixture
    def mock_notif(self) -> MagicMock:
        return MagicMock()

    @pytest.fixture
    def poller(
        self,
        mock_conn: MagicMock,
        mock_vector_store: MagicMock,
        mock_notif: MagicMock,
        tmp_path: Path,
    ) -> HealthPoller:
        db_path = tmp_path / "test.db"
        db_path.touch()
        return HealthPoller(
            conn=mock_conn,
            vector_store=mock_vector_store,
            db_path=db_path,
            notification_service=mock_notif,
            poll_interval=1,
        )

    def test_poll_once_all_ok_no_notification(
        self, poller: HealthPoller, mock_notif: MagicMock
    ) -> None:
        poller.poll_once()
        mock_notif.notify_health_alert.assert_not_called()

    def test_poll_once_degradation_sends_notification(
        self, poller: HealthPoller, mock_notif: MagicMock, mock_conn: MagicMock,
    ) -> None:
        # First poll: everything ok
        poller.poll_once()
        mock_notif.notify_health_alert.assert_not_called()

        # SQLite goes down
        mock_conn.execute.side_effect = sqlite3.OperationalError("db locked")
        poller.poll_once()
        mock_notif.notify_health_alert.assert_called_once()
        args = mock_notif.notify_health_alert.call_args
        assert args[0][0] == "sqlite"

    def test_same_bad_status_no_repeat_notification(
        self, poller: HealthPoller, mock_notif: MagicMock, mock_conn: MagicMock,
    ) -> None:
        mock_conn.execute.side_effect = sqlite3.OperationalError("db locked")
        poller.poll_once()
        assert mock_notif.notify_health_alert.call_count == 1
        poller.poll_once()
        # Still just 1 — same bad status, no repeat
        assert mock_notif.notify_health_alert.call_count == 1

    def test_recovery_no_notification(
        self, poller: HealthPoller, mock_notif: MagicMock, mock_conn: MagicMock,
    ) -> None:
        # First: degrade
        mock_conn.execute.side_effect = sqlite3.OperationalError("db locked")
        poller.poll_once()
        assert mock_notif.notify_health_alert.call_count == 1

        # Recover
        row = MagicMock()
        row.__getitem__ = MagicMock(return_value=1)
        mock_conn.execute.side_effect = None
        mock_conn.execute.return_value.fetchone.return_value = row
        poller.poll_once()
        # No new notification on recovery
        assert mock_notif.notify_health_alert.call_count == 1

    def test_start_stop(self, poller: HealthPoller) -> None:
        poller.start()
        assert poller._thread is not None
        assert poller._thread.is_alive()
        poller.stop()
        poller._thread.join(timeout=3)
        assert not poller._thread.is_alive()
