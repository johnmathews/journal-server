"""Entity merge + quarantine + merge-candidate mixin.

Holds the entity-lifecycle operations that go beyond plain CRUD:
``merge_entities``, ``delete_orphaned_entities``, the quarantine
trio (``quarantine_entity`` / ``release_quarantine`` /
``list_quarantined_entities``), the merge-candidate workflow, and
``get_merge_history``. Methods stay bound to ``self`` so they keep
using ``self._conn`` and ``self.get_entity`` from the base store.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from journal.entitystore.protocol import _normalise

if TYPE_CHECKING:
    from journal.models import (
        Entity,
        MergeCandidate,
        MergeResult,
        PairDecision,
    )

log = logging.getLogger(__name__)


class _MergeMixin:
    """Merge, delete-orphans, quarantine, and merge-candidate workflow."""

    # ---- merge --------------------------------------------------------

    def merge_entities(
        self, survivor_id: int, absorbed_ids: list[int],
    ) -> MergeResult:
        from journal.models import MergeResult

        survivor = self.get_entity(survivor_id)  # type: ignore[attr-defined]
        if survivor is None:
            raise ValueError(f"Survivor entity {survivor_id} not found")

        total_mentions = 0
        total_relationships = 0
        total_aliases = 0

        for absorbed_id in absorbed_ids:
            absorbed = self.get_entity(absorbed_id)  # type: ignore[attr-defined]
            if absorbed is None:
                raise ValueError(f"Absorbed entity {absorbed_id} not found")
            if absorbed_id == survivor_id:
                raise ValueError("Cannot merge entity into itself")

            # Snapshot the absorbed entity for merge history. Quarantine
            # state is included so the audit trail survives merges of
            # previously-quarantined entities.
            self._conn.execute(  # type: ignore[attr-defined]
                "INSERT INTO entity_merge_history"
                " (survivor_id, absorbed_id, absorbed_name,"
                "  absorbed_type, absorbed_desc, absorbed_aliases,"
                "  absorbed_is_quarantined, absorbed_quarantine_reason,"
                "  absorbed_quarantined_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    survivor_id,
                    absorbed_id,
                    absorbed.canonical_name,
                    absorbed.entity_type,
                    absorbed.description,
                    json.dumps(absorbed.aliases),
                    1 if absorbed.is_quarantined else 0,
                    absorbed.quarantine_reason,
                    absorbed.quarantined_at,
                ),
            )

            # Reassign mentions
            cursor = self._conn.execute(  # type: ignore[attr-defined]
                "UPDATE entity_mentions SET entity_id = ?"
                " WHERE entity_id = ?",
                (survivor_id, absorbed_id),
            )
            total_mentions += cursor.rowcount

            # Reassign relationships (both sides)
            cursor = self._conn.execute(  # type: ignore[attr-defined]
                "UPDATE entity_relationships SET subject_entity_id = ?"
                " WHERE subject_entity_id = ?",
                (survivor_id, absorbed_id),
            )
            total_relationships += cursor.rowcount
            cursor = self._conn.execute(  # type: ignore[attr-defined]
                "UPDATE entity_relationships SET object_entity_id = ?"
                " WHERE object_entity_id = ?",
                (survivor_id, absorbed_id),
            )
            total_relationships += cursor.rowcount

            # Copy aliases (including the absorbed entity's canonical name)
            for alias in [
                *absorbed.aliases,
                _normalise(absorbed.canonical_name),
            ]:
                if alias and alias != _normalise(survivor.canonical_name):
                    self._conn.execute(  # type: ignore[attr-defined]
                        "INSERT OR IGNORE INTO entity_aliases"
                        " (entity_id, alias_normalised) VALUES (?, ?)",
                        (survivor_id, alias),
                    )
                    total_aliases += 1

            # Transfer "not a duplicate" decisions from the absorbed
            # entity onto the survivor so they survive the merge.
            # Done before the FK CASCADE deletes the rows on entity
            # deletion: an explicit transfer preserves the semantic
            # ("survivor is not the same as B") whereas the CASCADE
            # would silently lose it.
            self._transfer_pair_rejections_for_merge(
                absorbed_id=absorbed_id, survivor_id=survivor_id,
            )

            # Delete the absorbed entity (cascades aliases + any
            # remaining pair-decision rows still referencing it)
            self._conn.execute(  # type: ignore[attr-defined]
                "DELETE FROM entities WHERE id = ?", (absorbed_id,),
            )

            # Dismiss any pending merge candidates involving the absorbed entity
            self._conn.execute(  # type: ignore[attr-defined]
                "UPDATE entity_merge_candidates SET status = 'accepted',"
                " resolved_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"
                " WHERE status = 'pending'"
                " AND (entity_id_a = ? OR entity_id_b = ?)",
                (absorbed_id, absorbed_id),
            )

            log.info(
                "Merged entity %d (%s) into %d (%s)",
                absorbed_id, absorbed.canonical_name,
                survivor_id, survivor.canonical_name,
            )

        self._conn.commit()  # type: ignore[attr-defined]
        return MergeResult(
            survivor_id=survivor_id,
            absorbed_ids=absorbed_ids,
            mentions_reassigned=total_mentions,
            relationships_reassigned=total_relationships,
            aliases_added=total_aliases,
        )

    def delete_orphaned_entities(self, entity_ids: list[int]) -> int:
        if not entity_ids:
            return 0
        placeholders = ", ".join("?" for _ in entity_ids)
        cursor = self._conn.execute(  # type: ignore[attr-defined]
            f"DELETE FROM entities WHERE id IN ({placeholders})"
            f" AND id NOT IN (SELECT DISTINCT entity_id FROM entity_mentions)",
            entity_ids,
        )
        self._conn.commit()  # type: ignore[attr-defined]
        deleted = cursor.rowcount
        if deleted:
            log.info("Deleted %d orphaned entities (zero mentions)", deleted)
        return deleted

    # ---- quarantine ---------------------------------------------------

    def quarantine_entity(self, entity_id: int, reason: str) -> None:
        """Soft-quarantine an entity.

        Sets ``is_quarantined = 1``, stamps ``quarantine_reason`` and
        ``quarantined_at`` (UTC ISO-8601), and leaves all other
        columns — including aliases, descriptions, and merge history —
        untouched.

        Raises ``ValueError`` if the entity does not exist;
        idempotent for repeat calls on an already-quarantined row
        (the reason and timestamp are refreshed so the most recent
        action wins).
        """
        existing = self.get_entity(entity_id)  # type: ignore[attr-defined]
        if existing is None:
            raise ValueError(f"Entity {entity_id} not found")
        self._conn.execute(  # type: ignore[attr-defined]
            "UPDATE entities SET is_quarantined = 1,"
            " quarantine_reason = ?,"
            " quarantined_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),"
            " updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"
            " WHERE id = ?",
            (reason, entity_id),
        )
        self._conn.commit()  # type: ignore[attr-defined]
        log.info(
            "Quarantined entity %d (%s): %s",
            entity_id, existing.canonical_name, reason,
        )

    def release_quarantine(self, entity_id: int) -> None:
        """Clear the quarantine flag, reason, and timestamp.

        Raises ``ValueError`` if the entity does not exist; safe to
        call on a non-quarantined entity (it just becomes a no-op
        write).
        """
        existing = self.get_entity(entity_id)  # type: ignore[attr-defined]
        if existing is None:
            raise ValueError(f"Entity {entity_id} not found")
        self._conn.execute(  # type: ignore[attr-defined]
            "UPDATE entities SET is_quarantined = 0,"
            " quarantine_reason = '',"
            " quarantined_at = '',"
            " updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"
            " WHERE id = ?",
            (entity_id,),
        )
        self._conn.commit()  # type: ignore[attr-defined]
        log.info(
            "Released quarantine on entity %d (%s)",
            entity_id, existing.canonical_name,
        )

    def list_quarantined_entities(self, user_id: int) -> list[Entity]:
        """Return only quarantined entities for the given user.

        Ordering matches ``list_entities`` (entity_type then
        canonical_name) so the operator UI is stable.
        """
        rows = self._conn.execute(  # type: ignore[attr-defined]
            "SELECT * FROM entities"
            " WHERE user_id = ? AND is_quarantined = 1"
            " ORDER BY entity_type, canonical_name",
            (user_id,),
        ).fetchall()
        return [self._hydrate(r) for r in rows]  # type: ignore[attr-defined]

    # ---- merge candidates --------------------------------------------

    def create_merge_candidate(
        self,
        entity_id_a: int,
        entity_id_b: int,
        similarity: float,
        extraction_run_id: str,
    ) -> None:
        """UPSERT a candidate for the given pair.

        The table has been per-pair unique since migration 0022. If the
        pair already exists in ``pending``, bump similarity to the
        higher of the two scores and refresh ``extraction_run_id`` /
        ``updated_at``. Already-resolved rows (``accepted`` or
        ``dismissed``) are left alone so historical decisions are not
        silently overwritten — the rejection check in the extraction
        service is the primary defence against re-flagging dismissed
        pairs, this clause is belt + braces.
        """
        # Normalise order so (a, b) == (b, a) — matches the table's
        # CHECK (entity_id_a < entity_id_b).
        lo, hi = sorted([entity_id_a, entity_id_b])
        self._conn.execute(  # type: ignore[attr-defined]
            "INSERT INTO entity_merge_candidates"
            " (entity_id_a, entity_id_b, similarity, extraction_run_id)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(entity_id_a, entity_id_b) DO UPDATE SET"
            "   similarity = MAX(similarity, excluded.similarity),"
            "   extraction_run_id = excluded.extraction_run_id,"
            "   updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"
            " WHERE entity_merge_candidates.status = 'pending'",
            (lo, hi, similarity, extraction_run_id),
        )
        self._conn.commit()  # type: ignore[attr-defined]

    def list_merge_candidates(
        self,
        status: str = "pending",
        limit: int = 50,
        user_id: int | None = None,
    ) -> list[MergeCandidate]:
        from journal.models import MergeCandidate

        if user_id is not None:
            # Filter at DB level: both entities must belong to the user.
            rows = self._conn.execute(  # type: ignore[attr-defined]
                "SELECT c.* FROM entity_merge_candidates c"
                " JOIN entities ea ON ea.id = c.entity_id_a"
                " JOIN entities eb ON eb.id = c.entity_id_b"
                " WHERE c.status = ? AND ea.user_id = ? AND eb.user_id = ?"
                " ORDER BY c.similarity DESC LIMIT ?",
                (status, user_id, user_id, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(  # type: ignore[attr-defined]
                "SELECT * FROM entity_merge_candidates"
                " WHERE status = ? ORDER BY similarity DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        candidates: list[MergeCandidate] = []
        for row in rows:
            entity_a = self.get_entity(row["entity_id_a"])  # type: ignore[attr-defined]
            entity_b = self.get_entity(row["entity_id_b"])  # type: ignore[attr-defined]
            if entity_a is None or entity_b is None:
                continue
            candidates.append(MergeCandidate(
                id=row["id"],
                entity_a=entity_a,
                entity_b=entity_b,
                similarity=row["similarity"],
                status=row["status"],
                extraction_run_id=row["extraction_run_id"],
                created_at=row["created_at"],
            ))
        return candidates

    def resolve_merge_candidate(
        self, candidate_id: int, status: str,
    ) -> None:
        """Update a candidate's status; on dismiss, persist the rejection.

        Dismissing a candidate writes a row to ``entity_pair_decisions``
        in the same transaction. Future extractions consult that table
        and skip re-creating the pair as a candidate, so a rejected pair
        never resurfaces unless the user explicitly undoes the decision.
        """
        if status not in ("accepted", "dismissed"):
            raise ValueError(f"Invalid status: {status}")
        row = self._conn.execute(  # type: ignore[attr-defined]
            "SELECT entity_id_a, entity_id_b FROM entity_merge_candidates"
            " WHERE id = ?",
            (candidate_id,),
        ).fetchone()
        self._conn.execute(  # type: ignore[attr-defined]
            "UPDATE entity_merge_candidates SET status = ?,"
            " resolved_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"
            " WHERE id = ?",
            (status, candidate_id),
        )
        if status == "dismissed" and row is not None:
            entity_id_a = int(row["entity_id_a"])
            entity_id_b = int(row["entity_id_b"])
            user_row = self._conn.execute(  # type: ignore[attr-defined]
                "SELECT user_id FROM entities WHERE id = ?",
                (entity_id_a,),
            ).fetchone()
            if user_row is not None:
                self._record_pair_rejection_no_commit(
                    user_id=int(user_row["user_id"]),
                    entity_id_a=entity_id_a,
                    entity_id_b=entity_id_b,
                )
        self._conn.commit()  # type: ignore[attr-defined]

    # ---- pair decisions (persistent "not a duplicate") ---------------

    def _record_pair_rejection_no_commit(
        self, user_id: int, entity_id_a: int, entity_id_b: int,
    ) -> None:
        """Insert a rejection row without committing.

        Internal helper so callers that already hold a transaction
        (``resolve_merge_candidate``, ``merge_entities``) batch the
        rejection write into the same commit.
        """
        if entity_id_a == entity_id_b:
            return
        lo, hi = sorted([entity_id_a, entity_id_b])
        self._conn.execute(  # type: ignore[attr-defined]
            "INSERT OR IGNORE INTO entity_pair_decisions"
            " (user_id, entity_id_lo, entity_id_hi, decision)"
            " VALUES (?, ?, ?, 'rejected')",
            (user_id, lo, hi),
        )

    def record_pair_rejection(
        self, user_id: int, entity_id_a: int, entity_id_b: int,
    ) -> None:
        """Record a "not a duplicate" decision for the given pair.

        Idempotent: if the pair is already rejected, this is a no-op.
        Order of ids does not matter — they are normalised internally.
        """
        self._record_pair_rejection_no_commit(
            user_id=user_id,
            entity_id_a=entity_id_a,
            entity_id_b=entity_id_b,
        )
        self._conn.commit()  # type: ignore[attr-defined]

    def is_pair_rejected(
        self, user_id: int, entity_id_a: int, entity_id_b: int,
    ) -> bool:
        """Return True if the user has rejected the given pair."""
        if entity_id_a == entity_id_b:
            return False
        lo, hi = sorted([entity_id_a, entity_id_b])
        row = self._conn.execute(  # type: ignore[attr-defined]
            "SELECT 1 FROM entity_pair_decisions"
            " WHERE user_id = ? AND entity_id_lo = ? AND entity_id_hi = ?",
            (user_id, lo, hi),
        ).fetchone()
        return row is not None

    def list_pair_rejections(
        self, user_id: int, limit: int = 50, offset: int = 0,
    ) -> list[PairDecision]:
        """Return the user's rejected pairs, newest first."""
        from journal.models import PairDecision

        rows = self._conn.execute(  # type: ignore[attr-defined]
            "SELECT * FROM entity_pair_decisions"
            " WHERE user_id = ?"
            " ORDER BY decided_at DESC, id DESC"
            " LIMIT ? OFFSET ?",
            (user_id, limit, offset),
        ).fetchall()
        decisions: list[PairDecision] = []
        for row in rows:
            entity_a = self.get_entity(row["entity_id_lo"])  # type: ignore[attr-defined]
            entity_b = self.get_entity(row["entity_id_hi"])  # type: ignore[attr-defined]
            if entity_a is None or entity_b is None:
                # FK CASCADE should have removed these, but guard anyway.
                continue
            decisions.append(
                PairDecision(
                    id=row["id"],
                    user_id=row["user_id"],
                    entity_a=entity_a,
                    entity_b=entity_b,
                    decision=row["decision"],
                    decided_at=row["decided_at"],
                )
            )
        return decisions

    def count_pair_rejections(self, user_id: int) -> int:
        """Total rejections for the user (for paginated list metadata)."""
        row = self._conn.execute(  # type: ignore[attr-defined]
            "SELECT COUNT(*) AS n FROM entity_pair_decisions"
            " WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return int(row["n"]) if row is not None else 0

    def delete_pair_rejection(
        self, user_id: int, decision_id: int,
    ) -> bool:
        """Remove a rejection. Returns True if a row was deleted."""
        cursor = self._conn.execute(  # type: ignore[attr-defined]
            "DELETE FROM entity_pair_decisions"
            " WHERE id = ? AND user_id = ?",
            (decision_id, user_id),
        )
        self._conn.commit()  # type: ignore[attr-defined]
        return cursor.rowcount > 0

    def _transfer_pair_rejections_for_merge(
        self, absorbed_id: int, survivor_id: int,
    ) -> None:
        """Re-target rejections involving the absorbed entity onto the
        survivor.

        Called inside ``merge_entities``' transaction. For each
        rejection ``(absorbed, X)``:

        - If ``X == survivor``: drop it (a self-pair can't be "not the
          same"); will also be removed by FK CASCADE.
        - Otherwise: insert ``(survivor, X)`` if not already present,
          then delete the original. The survivor inherits the user's
          decision that "this entity is not the same as X".

        The FK CASCADE on the absorbed entity's deletion is a safety
        net for any rows the loop missed (it shouldn't miss any).
        """
        rows = self._conn.execute(  # type: ignore[attr-defined]
            "SELECT id, user_id, entity_id_lo, entity_id_hi"
            " FROM entity_pair_decisions"
            " WHERE entity_id_lo = ? OR entity_id_hi = ?",
            (absorbed_id, absorbed_id),
        ).fetchall()
        for row in rows:
            other = (
                row["entity_id_hi"]
                if row["entity_id_lo"] == absorbed_id
                else row["entity_id_lo"]
            )
            if other == survivor_id:
                # Self-pair after the merge — drop it.
                self._conn.execute(  # type: ignore[attr-defined]
                    "DELETE FROM entity_pair_decisions WHERE id = ?",
                    (row["id"],),
                )
                continue
            new_lo, new_hi = sorted([survivor_id, other])
            self._conn.execute(  # type: ignore[attr-defined]
                "INSERT OR IGNORE INTO entity_pair_decisions"
                " (user_id, entity_id_lo, entity_id_hi, decision)"
                " VALUES (?, ?, ?, 'rejected')",
                (row["user_id"], new_lo, new_hi),
            )
            self._conn.execute(  # type: ignore[attr-defined]
                "DELETE FROM entity_pair_decisions WHERE id = ?",
                (row["id"],),
            )

    # ---- merge history ------------------------------------------------

    def get_merge_history(
        self, entity_id: int,
    ) -> list[dict[str, object]]:
        rows = self._conn.execute(  # type: ignore[attr-defined]
            "SELECT * FROM entity_merge_history"
            " WHERE survivor_id = ? ORDER BY merged_at DESC",
            (entity_id,),
        ).fetchall()
        return [
            {
                "id": r["id"],
                "survivor_id": r["survivor_id"],
                "absorbed_id": r["absorbed_id"],
                "absorbed_name": r["absorbed_name"],
                "absorbed_type": r["absorbed_type"],
                "absorbed_desc": r["absorbed_desc"],
                "absorbed_aliases": json.loads(r["absorbed_aliases"]),
                "absorbed_is_quarantined": bool(
                    r["absorbed_is_quarantined"]
                ),
                "absorbed_quarantine_reason": r[
                    "absorbed_quarantine_reason"
                ],
                "absorbed_quarantined_at": r["absorbed_quarantined_at"],
                "merged_at": r["merged_at"],
                "merged_by": r["merged_by"],
            }
            for r in rows
        ]
