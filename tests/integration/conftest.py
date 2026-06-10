"""Integration-test fixtures and reachability probe.

Without this conftest, plain ``uv run pytest`` (no marker filter) would
*collect* the integration suite, attempt to construct a
``ChromaVectorStore`` per test fixture, and fail at fixture-setup time
with a stack trace. The hook below runs once at collection time, opens
a TCP socket to the configured Chroma host:port, and if unreachable
marks every integration item as skipped with a clear, actionable
reason.

CI is unaffected: the integration job in ``ci-and-deploy.yml`` runs
ChromaDB as a service on port 8000 and explicitly sets
``CHROMADB_PORT=8000``, so the probe succeeds and the tests run.

Local default is **port 8401**, not 8000 — that's the port
``docker-compose.dev.yml`` exposes for the dev stack. CI overrides via
the env var when it needs 8000. The earlier default of 8000 was a
silent footgun for local devs who'd brought up Chroma via the dev
compose only to see all integration tests still fail.

The canonical env vars are ``CHROMADB_HOST`` / ``CHROMADB_PORT``,
matching the runtime config in ``journal.config``. The legacy
``CHROMA_HOST`` / ``CHROMA_PORT`` names are still honoured as
fallbacks for one release — see :func:`chroma_endpoint`.
"""

from __future__ import annotations

import os
import socket
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterable


def chroma_endpoint() -> tuple[str, int]:
    """Resolve the Chroma endpoint from env vars.

    Canonical vars are ``CHROMADB_HOST`` / ``CHROMADB_PORT`` (aligned
    with the runtime config). Default port is 8401 (matches
    ``docker-compose.dev.yml``); CI sets ``CHROMADB_PORT=8000``
    explicitly to use the service container.

    Deprecated: the legacy ``CHROMA_HOST`` / ``CHROMA_PORT`` names are
    honoured as fallbacks for one release and will be removed after
    that. Switch any local scripts to the ``CHROMADB_*`` names.
    """
    host = os.getenv("CHROMADB_HOST") or os.getenv("CHROMA_HOST", "localhost")
    port = int(os.getenv("CHROMADB_PORT") or os.getenv("CHROMA_PORT", "8401"))
    return host, port


def _is_reachable(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def pytest_collection_modifyitems(
    config: pytest.Config, items: Iterable[pytest.Item],
) -> None:
    """Skip every integration item if Chroma is unreachable.

    Runs once at collection, after items are collected but before any
    fixture is set up. Items in this folder all carry the
    ``integration`` marker (set module-level in the test files); we
    only act on those, so a wider future ``tests/integration/`` tree
    that holds non-Chroma integration tests would still need its own
    probe — but at the moment Chroma is the only external dependency
    here.
    """
    host, port = chroma_endpoint()
    if _is_reachable(host, port):
        return
    skip = pytest.mark.skip(
        reason=(
            f"ChromaDB not reachable at {host}:{port}. Bring it up with "
            "`docker compose -f docker-compose.dev.yml up -d` (dev port 8401), "
            "then re-run. CI sets CHROMADB_PORT=8000 against its service container."
        ),
    )
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)
