"""Background health poller for admin notifications.

Periodically checks internal components (SQLite, ChromaDB, disk space)
and sends Pushover notifications to admin users when a component
transitions from healthy to unhealthy. No external API calls are
made — the checks are purely local, incurring zero usage fees.

The poller runs as a daemon thread started at service initialization.
"""

from __future__ import annotations

import logging
import shutil
import threading
from pathlib import Path  # noqa: TC003
from typing import TYPE_CHECKING, Any

from journal.services.liveness import (
    ComponentCheck,
    check_chromadb,
    check_sqlite,
)

if TYPE_CHECKING:
    import sqlite3

    from journal.services.notifications import PushoverNotificationService

log = logging.getLogger(__name__)

_DEFAULT_POLL_INTERVAL = 300  # 5 minutes
_DISK_ERROR_MB = 100
_DISK_DEGRADED_MB = 500


def check_disk(db_path: Path) -> ComponentCheck:
    """Check free disk space on the partition containing the database.

    Returns ``error`` if < 100 MB free, ``degraded`` if < 500 MB,
    else ``ok``. Uses ``shutil.disk_usage`` — stdlib, no external
    dependencies.
    """
    try:
        usage = shutil.disk_usage(db_path.parent)
        free_mb = usage.free / (1024 * 1024)
        if free_mb < _DISK_ERROR_MB:
            return ComponentCheck(
                name="disk",
                status="error",
                detail=f"Only {free_mb:.0f} MB free (< {_DISK_ERROR_MB} MB)",
            )
        if free_mb < _DISK_DEGRADED_MB:
            return ComponentCheck(
                name="disk",
                status="degraded",
                detail=f"Only {free_mb:.0f} MB free (< {_DISK_DEGRADED_MB} MB)",
            )
        return ComponentCheck(
            name="disk",
            status="ok",
            detail=f"{free_mb:.0f} MB free",
        )
    except OSError as e:
        return ComponentCheck(
            name="disk",
            status="error",
            detail="disk_usage check failed",
            error=str(e),
        )


class HealthPoller:
    """Daemon thread that polls component health and notifies admins.

    Notifications fire only on status transitions from ``ok`` to a
    non-ok state. Recovery (back to ``ok``) is logged but does not
    trigger a notification.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        vector_store: Any,
        db_path: Path,
        notification_service: PushoverNotificationService,
        poll_interval: int = _DEFAULT_POLL_INTERVAL,
    ) -> None:
        self._conn = conn
        self._vector_store = vector_store
        self._db_path = db_path
        self._notifications = notification_service
        self._poll_interval = poll_interval
        self._stop_event = threading.Event()
        self._last_status: dict[str, str] = {}
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the polling daemon thread."""
        self._thread = threading.Thread(
            target=self._run,
            name="health-poller",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal the poller to stop."""
        self._stop_event.set()

    def poll_once(self) -> None:
        """Run a single poll cycle. Exposed for testing."""
        checks = [
            check_sqlite(self._conn),
            check_chromadb(self._vector_store),
            check_disk(self._db_path),
        ]

        for check in checks:
            prev = self._last_status.get(check.name, "ok")
            if check.status != "ok" and prev == "ok":
                log.warning(
                    "Health degradation: %s changed from %s to %s — %s",
                    check.name, prev, check.status, check.detail,
                )
                self._notifications.notify_health_alert(
                    check.name, check.detail,
                )
            elif check.status == "ok" and prev != "ok":
                log.info(
                    "Health recovery: %s changed from %s to ok",
                    check.name, prev,
                )
            self._last_status[check.name] = check.status

    def _run(self) -> None:
        """Thread body: poll on interval until stopped."""
        log.info("Health poller started (interval=%ds)", self._poll_interval)
        while not self._stop_event.is_set():
            try:
                self.poll_once()
            except Exception:  # noqa: BLE001
                log.exception("Health poll cycle failed")
            self._stop_event.wait(self._poll_interval)
        log.info("Health poller stopped")
