"""Bootstrap wires the conversation service into the services dict."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import journal.mcp_server.bootstrap as mcp_module
from journal.db.conversation_repository import SQLiteConversationRepository
from journal.services.conversations import ConversationService


@pytest.fixture(autouse=True)
def _reset_services():
    """Reset the global services singleton between tests."""
    mcp_module._services = None
    yield
    mcp_module._services = None


@pytest.fixture
def _mock_chromadb():
    """Patch ChromaVectorStore so tests don't need a running ChromaDB."""
    with patch("journal.mcp_server.bootstrap.ChromaVectorStore") as mock_cls:
        mock_cls.return_value = MagicMock()
        yield mock_cls


def test_bootstrap_wires_conversation_service(
    config, monkeypatch, _mock_chromadb
) -> None:
    """conversation and conversation_repository must land in the services dict.

    This mirrors the existing wiring tests in test_lifespan.py: we
    monkeypatch load_config to inject a test Config (tmp SQLite, fake keys,
    no ChromaDB socket needed) so no external services are touched.
    """
    monkeypatch.setattr("journal.mcp_server.bootstrap.load_config", lambda: config)

    services = mcp_module._init_services()
    try:
        assert services.get("conversation") is not None
        assert isinstance(services.get("conversation"), ConversationService)
        assert services.get("conversation_repository") is not None
        assert isinstance(
            services.get("conversation_repository"), SQLiteConversationRepository
        )
    finally:
        runner = services.get("job_runner")
        if runner is not None:
            runner.shutdown(wait=True, cancel_futures=False)
