"""Tests for ``ConnectionFactory``.

Cover the invariants W2+ rely on: distinct connection per thread,
PRAGMAs applied identically to every connection, the built-in
``check_same_thread`` guard remains armed, idempotent ``get()``,
re-open after close, and concurrent writers serialise correctly
under the WAL file-level lock + ``busy_timeout`` rather than
producing the shared-state OperationalErrors that motivated this
refactor.
"""

from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TYPE_CHECKING

import pytest

from journal.db.factory import ConnectionFactory

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def factory(tmp_path: Path) -> ConnectionFactory:
    return ConnectionFactory(tmp_path / "factory.db")


class TestSingleThreadSemantics:
    def test_get_returns_same_connection_on_repeated_calls(self, factory):
        a = factory.get()
        b = factory.get()
        assert a is b

    def test_db_path_property(self, tmp_path):
        path = tmp_path / "exposed.db"
        factory = ConnectionFactory(path)
        assert factory.db_path == path

    def test_close_current_releases_and_reopens(self, factory):
        first = factory.get()
        factory.close_current()
        second = factory.get()
        assert first is not second
        # Closed connection should refuse further use.
        with pytest.raises(sqlite3.ProgrammingError):
            first.execute("SELECT 1")
        # New one is alive.
        second.execute("SELECT 1").fetchone()

    def test_close_current_when_unopened_is_noop(self, factory):
        # No exception, no side effects.
        factory.close_current()
        factory.close_current()


class TestPragmasApplied:
    """Each connection must come up with the standard PRAGMAs already
    set. The shipped values live in ``db/connection.py`` and are
    reused via ``get_connection``; the test asserts the *observable*
    effect rather than the literal SQL so a future refactor that
    moves PRAGMA application is still covered."""

    def test_wal_mode_active(self, factory):
        conn = factory.get()
        (mode,) = conn.execute("PRAGMA journal_mode").fetchone()
        assert mode.lower() == "wal"

    def test_foreign_keys_on(self, factory):
        conn = factory.get()
        (fk,) = conn.execute("PRAGMA foreign_keys").fetchone()
        assert fk == 1

    def test_busy_timeout_is_at_least_5s(self, factory):
        conn = factory.get()
        (timeout_ms,) = conn.execute("PRAGMA busy_timeout").fetchone()
        assert timeout_ms >= 5000

    def test_synchronous_mode_normal(self, factory):
        conn = factory.get()
        (sync,) = conn.execute("PRAGMA synchronous").fetchone()
        # SQLite returns the numeric value: 1 = NORMAL.
        assert sync == 1

    def test_row_factory_is_sqlite3_row(self, factory):
        conn = factory.get()
        assert conn.row_factory is sqlite3.Row


class TestCrossThread:
    def test_distinct_connection_per_thread(self, factory):
        results: list[int] = []

        def worker() -> None:
            results.append(id(factory.get()))

        a_id = id(factory.get())
        t = threading.Thread(target=worker)
        t.start()
        t.join()
        assert len(results) == 1
        assert results[0] != a_id

    def test_check_same_thread_guard_is_armed(self, factory):
        """If the factory's invariants ever regressed and a connection
        leaked across threads, Python's built-in guard must surface
        the problem loudly. This test would have caught the prod race
        immediately."""
        conn = factory.get()
        captured: list[BaseException] = []

        def use_from_other_thread() -> None:
            try:
                conn.execute("SELECT 1")
            except BaseException as exc:  # noqa: BLE001
                captured.append(exc)

        t = threading.Thread(target=use_from_other_thread)
        t.start()
        t.join()
        assert len(captured) == 1
        assert isinstance(captured[0], sqlite3.ProgrammingError)

    def test_concurrent_writers_serialise_at_file_level(
        self, factory, tmp_path,
    ):
        """Two writer threads on independent connections must succeed
        without raising. WAL + busy_timeout = 5s handles the
        contention at the SQLite file level; the per-connection
        transaction state never collides because connections are not
        shared. This is the scenario that produced the prod crash
        under the old shared-connection model.
        """
        # Schema once on the test thread.
        bootstrap = factory.get()
        bootstrap.execute("CREATE TABLE counters (id INTEGER PRIMARY KEY, n INTEGER NOT NULL)")
        bootstrap.execute("INSERT INTO counters (id, n) VALUES (1, 0)")
        bootstrap.commit()

        errors: list[BaseException] = []
        writes_per_thread = 25
        thread_count = 4

        def writer() -> None:
            try:
                conn = factory.get()
                for _ in range(writes_per_thread):
                    conn.execute("UPDATE counters SET n = n + 1 WHERE id = 1")
                    conn.commit()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                factory.close_current()

        with ThreadPoolExecutor(max_workers=thread_count) as ex:
            futures = [ex.submit(writer) for _ in range(thread_count)]
            for f in as_completed(futures):
                f.result()

        assert errors == []
        (total,) = bootstrap.execute(
            "SELECT n FROM counters WHERE id = 1",
        ).fetchone()
        assert total == thread_count * writes_per_thread

    def test_thread_can_reopen_after_close(self, factory):
        """A worker thread that calls ``close_current()`` and then
        does more work must transparently get a fresh connection."""
        captured: list[int] = []

        def worker() -> None:
            first = factory.get()
            first.execute("SELECT 1").fetchone()
            captured.append(id(first))
            factory.close_current()
            second = factory.get()
            second.execute("SELECT 1").fetchone()
            captured.append(id(second))

        t = threading.Thread(target=worker)
        t.start()
        t.join()
        assert len(captured) == 2
        assert captured[0] != captured[1]
