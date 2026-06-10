"""Tests for the uniform API handler decorator (``journal.api._handler``).

Covers the W20 contract:

- canonical 503 when services are not initialized (+ override hook)
- canonical 400s for invalid JSON / non-dict JSON bodies
- lenient and raw body modes
- the body executes OFF the event loop on a worker thread and can complete
  a real SQLite call through the per-thread ``ConnectionFactory``
- exceptions raised in the body propagate to the server-error path
- a slow (blocking ``time.sleep``) body does NOT block a concurrent request
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.testclient import TestClient

if TYPE_CHECKING:
    from pathlib import Path

    from starlette.requests import Request

from journal.api._handler import JsonBody, handler
from journal.db.factory import ConnectionFactory
from journal.db.migrations import run_migrations
from journal.db.repository import SQLiteEntryRepository

_SERVICES: dict[str, Any] = {"marker": "wired"}


def _client(routes: list[Route]) -> TestClient:
    app = Starlette(routes=routes)
    return TestClient(app, raise_server_exceptions=False)


class TestServicesGate:
    def test_uninitialized_services_return_canonical_503(self) -> None:
        @handler(lambda: None)
        def body(request: Request, services: dict, body: None) -> Response:
            raise AssertionError("body must not run")  # pragma: no cover

        client = _client([Route("/t", body, methods=["GET"])])
        resp = client.get("/t")
        assert resp.status_code == 503
        assert resp.json() == {"error": "Server not initialized"}

    def test_on_uninitialized_override(self) -> None:
        @handler(
            lambda: None,
            on_uninitialized=lambda: JSONResponse(
                {"status": "error", "message": "Server not initialized"},
                status_code=503,
            ),
        )
        def body(request: Request, services: dict, body: None) -> Response:
            raise AssertionError("body must not run")  # pragma: no cover

        client = _client([Route("/t", body, methods=["GET"])])
        resp = client.get("/t")
        assert resp.status_code == 503
        assert resp.json() == {
            "status": "error",
            "message": "Server not initialized",
        }

    def test_services_passed_to_body(self) -> None:
        @handler(lambda: _SERVICES)
        def body(request: Request, services: dict, body: None) -> Response:
            return JSONResponse({"marker": services["marker"]})

        client = _client([Route("/t", body, methods=["GET"])])
        assert client.get("/t").json() == {"marker": "wired"}


class TestJsonParsing:
    def test_invalid_json_returns_canonical_400(self) -> None:
        @handler(lambda: _SERVICES, parse_json=True)
        def body(request: Request, services: dict, body: dict) -> Response:
            raise AssertionError("body must not run")  # pragma: no cover

        client = _client([Route("/t", body, methods=["POST"])])
        resp = client.post("/t", content=b"{not json")
        assert resp.status_code == 400
        assert resp.json() == {"error": "Invalid JSON body"}

    def test_non_dict_body_returns_canonical_400(self) -> None:
        @handler(lambda: _SERVICES, parse_json=True)
        def body(request: Request, services: dict, body: dict) -> Response:
            raise AssertionError("body must not run")  # pragma: no cover

        client = _client([Route("/t", body, methods=["POST"])])
        resp = client.post("/t", json=[1, 2, 3])
        assert resp.status_code == 400
        assert resp.json() == {"error": "Request body must be a JSON object"}

    def test_valid_json_passed_to_body(self) -> None:
        @handler(lambda: _SERVICES, parse_json=True)
        def body(request: Request, services: dict, body: dict) -> Response:
            return JSONResponse({"echo": body})

        client = _client([Route("/t", body, methods=["POST"])])
        resp = client.post("/t", json={"a": 1})
        assert resp.json() == {"echo": {"a": 1}}

    def test_lenient_mode_swallows_invalid_json(self) -> None:
        @handler(lambda: _SERVICES, parse_json=JsonBody(invalid_error=None))
        def body(request: Request, services: dict, body: dict) -> Response:
            return JSONResponse({"echo": body})

        client = _client([Route("/t", body, methods=["POST"])])
        resp = client.post("/t", content=b"{not json")
        assert resp.status_code == 200
        assert resp.json() == {"echo": {}}

    def test_lenient_mode_still_rejects_non_dict(self) -> None:
        @handler(lambda: _SERVICES, parse_json=JsonBody(invalid_error=None))
        def body(request: Request, services: dict, body: dict) -> Response:
            raise AssertionError("body must not run")  # pragma: no cover

        client = _client([Route("/t", body, methods=["POST"])])
        resp = client.post("/t", json="just a string")
        assert resp.status_code == 400
        assert resp.json() == {"error": "Request body must be a JSON object"}

    def test_custom_invalid_error_message(self) -> None:
        @handler(
            lambda: _SERVICES,
            parse_json=JsonBody(invalid_error="Invalid JSON", require_dict=False),
        )
        def body(request: Request, services: dict, body: Any) -> Response:
            return JSONResponse({"echo": body})

        client = _client([Route("/t", body, methods=["POST"])])
        resp = client.post("/t", content=b"{not json")
        assert resp.status_code == 400
        assert resp.json() == {"error": "Invalid JSON"}
        # require_dict=False passes non-dict bodies through unchanged
        resp = client.post("/t", json=[1, 2])
        assert resp.json() == {"echo": [1, 2]}

    def test_raw_mode_passes_bytes(self) -> None:
        @handler(lambda: _SERVICES, parse_json="raw")
        def body(request: Request, services: dict, body: bytes) -> Response:
            assert isinstance(body, bytes)
            return JSONResponse({"raw": body.decode()})

        client = _client([Route("/t", body, methods=["POST"])])
        resp = client.post("/t", content=b"anything at all")
        assert resp.json() == {"raw": "anything at all"}


class TestThreadBoundary:
    def test_body_runs_off_the_event_loop_thread(self) -> None:
        seen: dict[str, Any] = {}

        @handler(lambda: _SERVICES)
        def body(request: Request, services: dict, body: None) -> Response:
            seen["thread_id"] = threading.get_ident()
            try:
                asyncio.get_running_loop()
                seen["loop_in_body"] = True
            except RuntimeError:
                seen["loop_in_body"] = False
            return JSONResponse({"ok": True})

        loop_thread_id: dict[str, int] = {}

        async def record_loop_thread(request: Request) -> Response:
            loop_thread_id["id"] = threading.get_ident()
            return JSONResponse({"ok": True})

        client = _client(
            [
                Route("/t", body, methods=["GET"]),
                Route("/loop", record_loop_thread, methods=["GET"]),
            ]
        )
        assert client.get("/loop").status_code == 200
        assert client.get("/t").status_code == 200
        assert seen["loop_in_body"] is False
        assert seen["thread_id"] != loop_thread_id["id"]

    def test_smoke_real_sqlite_call_from_worker_thread(self, tmp_path: Path) -> None:
        """Thread-safety pre-check: a handler body on a worker thread
        completes a real SQLite call through the per-thread
        ``ConnectionFactory`` (fresh connection opened on the worker
        thread, not the loop thread)."""
        factory = ConnectionFactory(tmp_path / "handler_smoke.db")
        run_migrations(factory.get())  # main-thread connection
        repo = SQLiteEntryRepository(factory)
        repo.create_entry("2026-06-10", "text_entry", "smoke entry text", 3)

        @handler(lambda: {"repo": repo})
        def body(request: Request, services: dict, body: None) -> Response:
            count = services["repo"].count_entries()
            return JSONResponse(
                {"count": count, "thread_id": threading.get_ident()}
            )

        client = _client([Route("/t", body, methods=["GET"])])
        resp = client.get("/t")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 1
        assert data["thread_id"] != threading.get_ident()

    def test_exception_in_body_propagates_as_server_error(self) -> None:
        @handler(lambda: _SERVICES)
        def body(request: Request, services: dict, body: None) -> Response:
            raise RuntimeError("boom from the worker thread")

        client = _client([Route("/t", body, methods=["GET"])])
        resp = client.get("/t")
        assert resp.status_code == 500

    def test_exception_reraised_when_client_raises(self) -> None:
        @handler(lambda: _SERVICES)
        def body(request: Request, services: dict, body: None) -> Response:
            raise RuntimeError("boom from the worker thread")

        app = Starlette(routes=[Route("/t", body, methods=["GET"])])
        client = TestClient(app, raise_server_exceptions=True)
        with pytest.raises(RuntimeError, match="boom from the worker thread"):
            client.get("/t")


class TestConcurrency:
    async def test_slow_body_does_not_block_concurrent_request(self) -> None:
        """A handler body stuck in blocking ``time.sleep`` must not stall
        the event loop: a concurrent fast request (the /health analogue)
        completes while the slow body is still sleeping."""
        slow_seconds = 0.5

        @handler(lambda: _SERVICES)
        def slow(request: Request, services: dict, body: None) -> Response:
            time.sleep(slow_seconds)  # deliberately blocking
            return JSONResponse({"slow": True})

        async def fast(request: Request) -> Response:
            return JSONResponse({"fast": True})

        app = Starlette(
            routes=[
                Route("/slow", slow, methods=["GET"]),
                Route("/fast", fast, methods=["GET"]),
            ]
        )
        transport = httpx.ASGITransport(app=app)
        done_order: list[str] = []

        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:

            async def hit(path: str, tag: str) -> httpx.Response:
                resp = await client.get(path)
                done_order.append(tag)
                return resp

            slow_task = asyncio.create_task(hit("/slow", "slow"))
            # Let the slow request reach its handler before racing it.
            await asyncio.sleep(0.05)
            start = time.monotonic()
            fast_resp = await hit("/fast", "fast")
            fast_elapsed = time.monotonic() - start
            slow_resp = await slow_task

        assert fast_resp.status_code == 200
        assert slow_resp.status_code == 200
        assert done_order == ["fast", "slow"]
        # The fast request must not have waited out the slow body's sleep.
        assert fast_elapsed < slow_seconds
