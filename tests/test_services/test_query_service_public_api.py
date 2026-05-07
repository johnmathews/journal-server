"""QueryService public pass-through methods (Unit 1b).

These tests pin the API contract exposed to the api/ layer so that future
refactors of `EntryRepository` cannot silently break the routes that depend
on `query_svc.<method>`. Each test asserts that the public method delegates
to the corresponding repository method with the right keyword arguments.

Coverage of the actual SQL behaviour lives in `test_query.py` (which uses a
real SQLite repo) and `test_api.py` (end-to-end). The point here is the
*shape* of the service surface — names, kwargs, and return-value forwarding.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, sentinel

import pytest

from journal.services.query import QueryService


@pytest.fixture
def repo() -> MagicMock:
    return MagicMock(name="EntryRepository")


@pytest.fixture
def vector_store() -> MagicMock:
    return MagicMock(name="VectorStore")


@pytest.fixture
def service(repo: MagicMock, vector_store: MagicMock) -> QueryService:
    return QueryService(
        repository=repo,
        vector_store=vector_store,
        embeddings_provider=MagicMock(),
    )


# ── infra access (properties) ────────────────────────────────────────


def test_vector_store_property_returns_store(
    service: QueryService, vector_store: MagicMock,
) -> None:
    assert service.vector_store is vector_store


def test_connection_property_returns_repo_conn(
    service: QueryService, repo: MagicMock,
) -> None:
    repo.connection = sentinel.connection
    assert service.connection is sentinel.connection


# ── single-entry reads / writes ──────────────────────────────────────


def test_get_entry_delegates(service: QueryService, repo: MagicMock) -> None:
    repo.get_entry.return_value = sentinel.entry
    out = service.get_entry(42, user_id=7)
    assert out is sentinel.entry
    repo.get_entry.assert_called_once_with(42, user_id=7)


def test_get_entry_user_id_optional(
    service: QueryService, repo: MagicMock,
) -> None:
    service.get_entry(42)
    repo.get_entry.assert_called_once_with(42, user_id=None)


def test_count_entries_delegates(service: QueryService, repo: MagicMock) -> None:
    repo.count_entries.return_value = 17
    out = service.count_entries("2026-01-01", "2026-12-31", user_id=3)
    assert out == 17
    repo.count_entries.assert_called_once_with(
        "2026-01-01", "2026-12-31", user_id=3,
    )


def test_update_entry_date_delegates(
    service: QueryService, repo: MagicMock,
) -> None:
    repo.update_entry_date.return_value = sentinel.updated
    out = service.update_entry_date(5, "2026-04-01", user_id=2)
    assert out is sentinel.updated
    repo.update_entry_date.assert_called_once_with(5, "2026-04-01", user_id=2)


def test_verify_doubts_delegates(service: QueryService, repo: MagicMock) -> None:
    repo.verify_doubts.return_value = True
    out = service.verify_doubts(11, user_id=1)
    assert out is True
    repo.verify_doubts.assert_called_once_with(11, user_id=1)


# ── per-entry metadata ───────────────────────────────────────────────


def test_get_page_count_delegates(service: QueryService, repo: MagicMock) -> None:
    repo.get_page_count.return_value = 3
    assert service.get_page_count(9) == 3
    repo.get_page_count.assert_called_once_with(9)


def test_get_uncertain_span_count_delegates(
    service: QueryService, repo: MagicMock,
) -> None:
    repo.get_uncertain_span_count.return_value = 2
    assert service.get_uncertain_span_count(9) == 2
    repo.get_uncertain_span_count.assert_called_once_with(9)


def test_get_uncertain_spans_delegates(
    service: QueryService, repo: MagicMock,
) -> None:
    repo.get_uncertain_spans.return_value = [(0, 5), (10, 12)]
    assert service.get_uncertain_spans(9) == [(0, 5), (10, 12)]
    repo.get_uncertain_spans.assert_called_once_with(9)


def test_get_entity_mention_count_delegates(
    service: QueryService, repo: MagicMock,
) -> None:
    repo.get_entity_mention_count.return_value = 4
    assert service.get_entity_mention_count(9) == 4
    repo.get_entity_mention_count.assert_called_once_with(9)


def test_get_chunks_delegates(service: QueryService, repo: MagicMock) -> None:
    repo.get_chunks.return_value = sentinel.chunks
    assert service.get_chunks(9) is sentinel.chunks
    repo.get_chunks.assert_called_once_with(9)


# ── corpus aggregations ──────────────────────────────────────────────


def test_get_ingestion_stats_default_now(
    service: QueryService, repo: MagicMock,
) -> None:
    repo.get_ingestion_stats.return_value = sentinel.stats
    out = service.get_ingestion_stats()
    assert out is sentinel.stats
    args, kwargs = repo.get_ingestion_stats.call_args
    assert isinstance(args[0], datetime)
    assert kwargs == {"user_id": None}


def test_get_ingestion_stats_explicit_now(
    service: QueryService, repo: MagicMock,
) -> None:
    fixed = datetime(2026, 5, 7)
    service.get_ingestion_stats(now=fixed, user_id=5)
    repo.get_ingestion_stats.assert_called_once_with(fixed, user_id=5)


# ── dashboard aggregations ───────────────────────────────────────────


def test_get_writing_frequency_delegates(
    service: QueryService, repo: MagicMock,
) -> None:
    service.get_writing_frequency("2026-01-01", "2026-12-31", "month", user_id=4)
    repo.get_writing_frequency.assert_called_once_with(
        start_date="2026-01-01",
        end_date="2026-12-31",
        granularity="month",
        user_id=4,
    )


def test_get_mood_drilldown_delegates(
    service: QueryService, repo: MagicMock,
) -> None:
    service.get_mood_drilldown(
        "energy", "2026-01-01", "2026-01-31", user_id=4,
    )
    repo.get_mood_drilldown.assert_called_once_with(
        dimension="energy",
        period_start="2026-01-01",
        period_end="2026-01-31",
        user_id=4,
    )


def test_get_entity_distribution_delegates(
    service: QueryService, repo: MagicMock,
) -> None:
    service.get_entity_distribution(
        "person", "2026-01-01", "2026-12-31", limit=20, user_id=4,
    )
    repo.get_entity_distribution.assert_called_once_with(
        entity_type="person",
        start_date="2026-01-01",
        end_date="2026-12-31",
        limit=20,
        user_id=4,
    )


def test_get_calendar_heatmap_delegates(
    service: QueryService, repo: MagicMock,
) -> None:
    service.get_calendar_heatmap("2026-01-01", "2026-12-31", user_id=4)
    repo.get_calendar_heatmap.assert_called_once_with(
        start_date="2026-01-01", end_date="2026-12-31", user_id=4,
    )


def test_get_entity_trends_delegates(
    service: QueryService, repo: MagicMock,
) -> None:
    service.get_entity_trends(
        "2026-01-01", "2026-12-31", granularity="quarter",
        entity_type="place", limit=5, user_id=4,
    )
    repo.get_entity_trends.assert_called_once_with(
        start_date="2026-01-01",
        end_date="2026-12-31",
        granularity="quarter",
        entity_type="place",
        limit=5,
        user_id=4,
    )


def test_get_mood_entity_correlation_delegates(
    service: QueryService, repo: MagicMock,
) -> None:
    service.get_mood_entity_correlation(
        "energy", "2026-01-01", "2026-12-31",
        entity_type="person", limit=7, user_id=4,
    )
    repo.get_mood_entity_correlation.assert_called_once_with(
        dimension="energy",
        start_date="2026-01-01",
        end_date="2026-12-31",
        entity_type="person",
        limit=7,
        user_id=4,
    )


def test_get_word_count_distribution_delegates(
    service: QueryService, repo: MagicMock,
) -> None:
    service.get_word_count_distribution(
        "2026-01-01", "2026-12-31", bucket_size=50, user_id=4,
    )
    repo.get_word_count_distribution.assert_called_once_with(
        start_date="2026-01-01",
        end_date="2026-12-31",
        bucket_size=50,
        user_id=4,
    )
