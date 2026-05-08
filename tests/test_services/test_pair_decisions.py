"""Tests for entity pair-decision (persistent "not a duplicate") storage.

Covers the repository methods on the merge mixin (record / lookup /
list / delete / transfer-on-merge) plus the integration with
``resolve_merge_candidate`` (dismissing a candidate writes a rejection)
and ``merge_entities`` (rejections involving the absorbed entity move
to the survivor).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from journal.entitystore.store import SQLiteEntityStore

if TYPE_CHECKING:
    import sqlite3


@pytest.fixture
def store(db_conn: sqlite3.Connection) -> SQLiteEntityStore:
    return SQLiteEntityStore(db_conn)


def _make_pair(
    store: SQLiteEntityStore,
    name_a: str = "John",
    name_b: str = "John's Mum",
    user_id: int = 1,
) -> tuple[int, int]:
    a = store.create_entity(
        "person", name_a, "", "2026-01-01", user_id=user_id,
    )
    b = store.create_entity(
        "person", name_b, "", "2026-01-01", user_id=user_id,
    )
    return a.id, b.id


class TestRecordAndLookup:
    def test_record_then_is_rejected_returns_true(
        self, store: SQLiteEntityStore,
    ) -> None:
        a_id, b_id = _make_pair(store)
        store.record_pair_rejection(1, a_id, b_id)
        assert store.is_pair_rejected(1, a_id, b_id) is True

    def test_lookup_is_order_independent(
        self, store: SQLiteEntityStore,
    ) -> None:
        a_id, b_id = _make_pair(store)
        store.record_pair_rejection(1, a_id, b_id)
        # Same pair queried in reverse order.
        assert store.is_pair_rejected(1, b_id, a_id) is True

    def test_record_idempotent(self, store: SQLiteEntityStore) -> None:
        a_id, b_id = _make_pair(store)
        store.record_pair_rejection(1, a_id, b_id)
        store.record_pair_rejection(1, a_id, b_id)
        store.record_pair_rejection(1, b_id, a_id)
        assert store.count_pair_rejections(1) == 1

    def test_unrelated_pair_not_rejected(
        self, store: SQLiteEntityStore,
    ) -> None:
        a_id, b_id = _make_pair(store)
        c = store.create_entity(
            "person", "Some Other", "", "2026-01-01", user_id=1,
        )
        store.record_pair_rejection(1, a_id, b_id)
        assert store.is_pair_rejected(1, a_id, c.id) is False
        assert store.is_pair_rejected(1, b_id, c.id) is False

    def test_rejection_scoped_to_user(
        self, store: SQLiteEntityStore,
    ) -> None:
        a_id, b_id = _make_pair(store, user_id=1)
        store.record_pair_rejection(1, a_id, b_id)
        assert store.is_pair_rejected(2, a_id, b_id) is False

    def test_self_pair_ignored(self, store: SQLiteEntityStore) -> None:
        a_id, _ = _make_pair(store)
        store.record_pair_rejection(1, a_id, a_id)
        assert store.is_pair_rejected(1, a_id, a_id) is False
        assert store.count_pair_rejections(1) == 0


class TestListAndDelete:
    def test_list_returns_normalised_pair(
        self, store: SQLiteEntityStore,
    ) -> None:
        a_id, b_id = _make_pair(store)
        # Record with reversed order — the list should still surface
        # the pair with entity_a having the lower id.
        store.record_pair_rejection(1, b_id, a_id)
        rejections = store.list_pair_rejections(1)
        assert len(rejections) == 1
        assert rejections[0].entity_a.id < rejections[0].entity_b.id
        assert rejections[0].decision == "rejected"

    def test_list_pagination(self, store: SQLiteEntityStore) -> None:
        ids = [
            store.create_entity(
                "person", f"Person {i}", "", "2026-01-01", user_id=1,
            ).id
            for i in range(6)
        ]
        for i in range(1, 6):
            store.record_pair_rejection(1, ids[0], ids[i])
        page1 = store.list_pair_rejections(1, limit=3, offset=0)
        page2 = store.list_pair_rejections(1, limit=3, offset=3)
        assert len(page1) == 3
        assert len(page2) == 2
        assert {d.id for d in page1}.isdisjoint({d.id for d in page2})
        assert store.count_pair_rejections(1) == 5

    def test_delete_pair_rejection_returns_true(
        self, store: SQLiteEntityStore,
    ) -> None:
        a_id, b_id = _make_pair(store)
        store.record_pair_rejection(1, a_id, b_id)
        decisions = store.list_pair_rejections(1)
        assert store.delete_pair_rejection(1, decisions[0].id) is True
        assert store.is_pair_rejected(1, a_id, b_id) is False

    def test_delete_returns_false_when_missing(
        self, store: SQLiteEntityStore,
    ) -> None:
        assert store.delete_pair_rejection(1, 99999) is False

    def test_delete_scoped_to_user(
        self, store: SQLiteEntityStore,
    ) -> None:
        a_id, b_id = _make_pair(store, user_id=1)
        store.record_pair_rejection(1, a_id, b_id)
        decisions = store.list_pair_rejections(1)
        # Different user trying to delete user 1's rejection
        assert store.delete_pair_rejection(2, decisions[0].id) is False
        assert store.is_pair_rejected(1, a_id, b_id) is True


class TestCascadeOnEntityDelete:
    def test_rejection_cascade_when_entity_deleted(
        self,
        store: SQLiteEntityStore,
        db_conn: sqlite3.Connection,
    ) -> None:
        """Deleting one of the entities should remove the rejection row
        via FK CASCADE — defends us if a delete path bypasses the merge
        flow and its explicit transfer step."""
        a_id, b_id = _make_pair(store)
        store.record_pair_rejection(1, a_id, b_id)
        db_conn.execute("DELETE FROM entities WHERE id = ?", (b_id,))
        db_conn.commit()
        assert store.count_pair_rejections(1) == 0


class TestDismissRecordsRejection:
    def test_dismiss_writes_rejection(
        self, store: SQLiteEntityStore,
    ) -> None:
        a_id, b_id = _make_pair(store)
        store.create_merge_candidate(a_id, b_id, 0.95, "run-1")
        candidates = store.list_merge_candidates(status="pending")
        assert len(candidates) == 1

        store.resolve_merge_candidate(candidates[0].id, "dismissed")
        assert store.is_pair_rejected(1, a_id, b_id) is True

    def test_accept_does_not_write_rejection(
        self, store: SQLiteEntityStore,
    ) -> None:
        a_id, b_id = _make_pair(store)
        store.create_merge_candidate(a_id, b_id, 0.95, "run-1")
        candidates = store.list_merge_candidates(status="pending")
        store.resolve_merge_candidate(candidates[0].id, "accepted")
        assert store.is_pair_rejected(1, a_id, b_id) is False


class TestTransferOnMerge:
    def test_rejection_transfers_to_survivor(
        self, store: SQLiteEntityStore,
    ) -> None:
        # Three entities. User has rejected (A, C). Then A is merged
        # into B. The rejection should now apply to (B, C).
        a = store.create_entity("person", "John", "", "2026-01-01", user_id=1)
        b = store.create_entity("person", "Jonathan", "", "2026-01-01", user_id=1)
        c = store.create_entity("person", "John's Mum", "", "2026-01-01", user_id=1)
        store.record_pair_rejection(1, a.id, c.id)

        store.merge_entities(survivor_id=b.id, absorbed_ids=[a.id])

        assert store.is_pair_rejected(1, b.id, c.id) is True
        # A no longer exists, so any rejection involving A is gone.
        assert store.count_pair_rejections(1) == 1

    def test_self_pair_after_merge_dropped(
        self, store: SQLiteEntityStore,
    ) -> None:
        # User rejected (A, B). Then A is merged into B. The transfer
        # would produce a self-pair (B, B) which we drop.
        a = store.create_entity("person", "John", "", "2026-01-01", user_id=1)
        b = store.create_entity("person", "Jonathan", "", "2026-01-01", user_id=1)
        store.record_pair_rejection(1, a.id, b.id)

        store.merge_entities(survivor_id=b.id, absorbed_ids=[a.id])

        assert store.count_pair_rejections(1) == 0

    def test_transfer_skips_when_survivor_already_has_decision(
        self, store: SQLiteEntityStore,
    ) -> None:
        # Both (A, C) and (B, C) are rejected. After A merges into B,
        # only one rejection survives — INSERT OR IGNORE keeps the
        # existing (B, C) row instead of duplicating it.
        a = store.create_entity("person", "John", "", "2026-01-01", user_id=1)
        b = store.create_entity("person", "Jonathan", "", "2026-01-01", user_id=1)
        c = store.create_entity("person", "John's Mum", "", "2026-01-01", user_id=1)
        store.record_pair_rejection(1, a.id, c.id)
        store.record_pair_rejection(1, b.id, c.id)

        store.merge_entities(survivor_id=b.id, absorbed_ids=[a.id])

        assert store.count_pair_rejections(1) == 1
        assert store.is_pair_rejected(1, b.id, c.id) is True
