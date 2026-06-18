"""Tests for PATCH /api/entries/{id} content-window (boundary) changes.

Fixtures (api_factory, client, repo, services, mock_vector_store,
mock_embeddings) are provided by tests/test_api/conftest.py which
re-exports them from tests/test_api.py.
"""

from unittest.mock import MagicMock

import pytest
from starlette.testclient import TestClient

from journal.db.repository import SQLiteEntryRepository

# ---------------------------------------------------------------------------
# Test-local fixtures
# ---------------------------------------------------------------------------

_TEST_USER_ID = 1

# Raw text for seeded entries: "tail body next" (len 14)
_RAW_TEXT = "tail body next"


@pytest.fixture
def seeded_entry(repo: SQLiteEntryRepository) -> object:
    """Entry with raw_text='tail body next', no content window set."""
    return repo.create_entry(
        "2026-03-22",
        "photo",
        _RAW_TEXT,
        word_count=3,
        user_id=_TEST_USER_ID,
    )


@pytest.fixture
def seeded_windowed_entry(repo: SQLiteEntryRepository) -> object:
    """Entry with a content window already set (chars 5–9 = 'body')."""
    return repo.create_entry(
        "2026-03-22",
        "photo",
        _RAW_TEXT,
        word_count=1,
        content_start_char=5,
        content_end_char=9,
        user_id=_TEST_USER_ID,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPatchBoundary:
    def test_patch_sets_window_and_rederives_final_text(
        self, client: TestClient, seeded_entry: object
    ) -> None:
        """Setting a window slices raw_text and updates final_text + boundary."""
        resp = client.patch(
            f"/api/entries/{seeded_entry.id}",
            json={"content_start_char": 5, "content_end_char": 9},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["content_boundary"] == {"char_start": 5, "char_end": 9}
        # "tail body next"[5:9] == "body"
        assert body["final_text"] == "body"

    def test_patch_clears_window_with_nulls(
        self, client: TestClient, seeded_windowed_entry: object
    ) -> None:
        """Sending null/null clears the boundary and restores full raw_text."""
        resp = client.patch(
            f"/api/entries/{seeded_windowed_entry.id}",
            json={"content_start_char": None, "content_end_char": None},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["content_boundary"] is None

    def test_patch_rejects_out_of_range_window(
        self, client: TestClient, seeded_entry: object
    ) -> None:
        """end > len(raw_text) must return 400."""
        resp = client.patch(
            f"/api/entries/{seeded_entry.id}",
            json={"content_start_char": 5, "content_end_char": 999},
        )
        assert resp.status_code == 400

    def test_patch_rejects_partial_window(
        self, client: TestClient, seeded_entry: object
    ) -> None:
        """Providing only one of the two boundary fields must return 400."""
        resp = client.patch(
            f"/api/entries/{seeded_entry.id}",
            json={"content_start_char": 5},
        )
        assert resp.status_code == 400

    def test_patch_window_queues_pipeline(
        self,
        client: TestClient,
        seeded_entry: object,
        services: dict,
    ) -> None:
        """Window change triggers the same save-entry pipeline as final_text."""
        mock_parent = MagicMock()
        mock_parent.id = "pipeline-parent-id"
        mock_runner = MagicMock()
        mock_runner.submit_save_entry_pipeline = MagicMock(
            return_value=(
                mock_parent,
                {
                    "reprocess_embeddings": "reprocess-job-id",
                    "entity_extraction": "entity-job-id",
                    "mood_scoring": "mood-job-id",
                },
            ),
        )
        services["job_runner"] = mock_runner

        resp = client.patch(
            f"/api/entries/{seeded_entry.id}",
            json={"content_start_char": 5, "content_end_char": 9},
        )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["pipeline_job_id"] == "pipeline-parent-id"
        mock_runner.submit_save_entry_pipeline.assert_called_once()
        kwargs = mock_runner.submit_save_entry_pipeline.call_args.kwargs
        assert kwargs["entry_id"] == seeded_entry.id
        assert kwargs["user_id"] == _TEST_USER_ID

    def test_patch_final_text_and_window_does_not_double_submit(
        self,
        client: TestClient,
        seeded_entry: object,
        services: dict,
    ) -> None:
        """A request with BOTH final_text and a window change submits only ONE pipeline."""
        mock_parent = MagicMock()
        mock_parent.id = "pipeline-parent-id"
        mock_runner = MagicMock()
        mock_runner.submit_save_entry_pipeline = MagicMock(
            return_value=(mock_parent, {}),
        )
        services["job_runner"] = mock_runner

        resp = client.patch(
            f"/api/entries/{seeded_entry.id}",
            json={
                "final_text": "manual override",
                "content_start_char": 5,
                "content_end_char": 9,
            },
        )

        assert resp.status_code == 200, resp.text
        # Pipeline submitted exactly once, not twice.
        assert mock_runner.submit_save_entry_pipeline.call_count == 1

    def test_patch_window_only_counts_as_valid_field(
        self, client: TestClient, seeded_entry: object
    ) -> None:
        """Window fields alone satisfy the 'at least one field' requirement."""
        resp = client.patch(
            f"/api/entries/{seeded_entry.id}",
            json={"content_start_char": 5, "content_end_char": 9},
        )
        assert resp.status_code == 200

    def test_patch_rejects_inverted_range(
        self, client: TestClient, seeded_entry: object
    ) -> None:
        """start >= end must return 400."""
        resp = client.patch(
            f"/api/entries/{seeded_entry.id}",
            json={"content_start_char": 9, "content_end_char": 5},
        )
        assert resp.status_code == 400

    def test_patch_rejects_only_end_char(
        self, client: TestClient, seeded_entry: object
    ) -> None:
        """Providing only content_end_char must return 400."""
        resp = client.patch(
            f"/api/entries/{seeded_entry.id}",
            json={"content_end_char": 9},
        )
        assert resp.status_code == 400
