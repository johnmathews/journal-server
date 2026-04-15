"""Entity extraction service.

Orchestrates the on-demand batch job that reads each entry's text,
calls an LLM-backed extraction provider, resolves extracted entities
against the existing store (via exact name -> alias -> embedding
similarity fallbacks), and persists mentions and relationships.

Storage is accessed exclusively through the `EntityStore` Protocol so
a graph-DB backend can be swapped in later without touching this
file. External LLM calls go through `ExtractionProvider` for the same
reason.
"""

from __future__ import annotations

import contextlib
import logging
import math
import uuid
from typing import TYPE_CHECKING, Any

from journal.models import ExtractionResult

if TYPE_CHECKING:
    from collections.abc import Callable

    from journal.db.repository import EntryRepository
    from journal.entitystore.store import EntityStore
    from journal.providers.embeddings import EmbeddingsProvider
    from journal.providers.extraction import ExtractionProvider

log = logging.getLogger(__name__)


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Return the cosine similarity of two equal-length float vectors.

    Returns 0.0 if either vector is empty or has zero magnitude. Does
    not pull in numpy — an entry-level extraction run compares at most
    a few hundred vectors, so a pure-Python loop is plenty fast.
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b, strict=True):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))


def _report_progress(
    callback: Callable[[int, int], None] | None,
    current: int,
    total: int,
) -> None:
    """Invoke a progress callback, swallowing any exception it raises.

    A broken progress sink must never break the batch — the whole
    point of the callback is out-of-band reporting.
    """
    if callback is None:
        return
    try:
        callback(current, total)
    except Exception as exc:  # noqa: BLE001 — callback may raise anything
        log.warning("Progress callback failed: %s", exc)


class EntityExtractionService:
    """Run the LLM extraction pipeline for one or many entries."""

    def __init__(
        self,
        repository: EntryRepository,
        entity_store: EntityStore,
        extraction_provider: ExtractionProvider,
        embeddings_provider: EmbeddingsProvider,
        author_name: str = "John",
        dedup_similarity_threshold: float = 0.88,
    ) -> None:
        self._repo = repository
        self._store = entity_store
        self._extractor = extraction_provider
        self._embeddings = embeddings_provider
        self._author_name = author_name
        self._threshold = dedup_similarity_threshold

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def extract_from_entry(self, entry_id: int) -> ExtractionResult:
        entry = self._repo.get_entry(entry_id)
        if entry is None:
            raise ValueError(f"Entry {entry_id} not found")

        user_id = entry.user_id or None
        run_id = str(uuid.uuid4())
        log.info(
            "Extracting entities from entry %d (run=%s)", entry_id, run_id
        )

        raw = self._extractor.extract_entities(
            entry_text=entry.final_text or entry.raw_text,
            entry_date=entry.entry_date,
            author_name=self._author_name,
        )

        # Idempotency: clear any prior extraction results for this
        # entry before writing the new ones. A re-run must never
        # produce duplicate mentions or relationships.
        self._store.delete_mentions_for_entry(entry_id)
        self._store.delete_relationships_for_entry(entry_id)

        warnings: list[str] = []
        entities_created = 0
        entities_matched = 0
        # Map canonical_name (lowered) -> resolved entity id. Used when
        # wiring relationships so we can fall back to what the LLM
        # actually emitted.
        resolved: dict[str, int] = {}
        # Cache the author entity so we only look it up / create it once.
        author_entity_id: int | None = None

        for raw_entity in raw.entities:
            canonical = (raw_entity.get("canonical_name") or "").strip()
            entity_type = raw_entity.get("entity_type") or "other"
            description = raw_entity.get("description") or ""
            aliases: list[str] = list(raw_entity.get("aliases") or [])
            quote = raw_entity.get("quote") or ""
            confidence = float(raw_entity.get("confidence") or 0.0)

            if not canonical:
                continue

            entity_id, created, warning, near_miss = self._resolve_entity(
                canonical=canonical,
                entity_type=entity_type,
                description=description,
                aliases=aliases,
                first_seen=entry.entry_date,
                user_id=user_id,
            )
            if created:
                entities_created += 1
            else:
                entities_matched += 1
            if warning:
                warnings.append(warning)
            if near_miss is not None:
                candidate_id, score = near_miss
                with contextlib.suppress(Exception):
                    self._store.create_merge_candidate(
                        entity_id_a=candidate_id,
                        entity_id_b=entity_id,
                        similarity=score,
                        extraction_run_id=run_id,
                    )

            # Record the resolved id under every name we know about
            # so the relationships step has the best chance of
            # finding it.
            resolved[canonical.lower()] = entity_id
            for alias in aliases:
                resolved[alias.strip().lower()] = entity_id

            # If this extracted entity IS the author, cache the id so
            # the relationships step doesn't need to look them up.
            if canonical.lower() == self._author_name.lower():
                author_entity_id = entity_id

            # Add any newly-seen aliases to the store. Idempotent.
            for alias in aliases:
                self._store.add_alias(entity_id, alias)

            # Create the mention tying this entity to the current entry.
            self._store.create_mention(
                entity_id=entity_id,
                entry_id=entry_id,
                quote=quote,
                confidence=confidence,
                extraction_run_id=run_id,
            )

        mentions_created = entities_created + entities_matched

        # Wire up relationships. The subject/object must already be in
        # the resolved map — if the LLM referenced a name that wasn't
        # in its own entity list, we warn and skip rather than try to
        # invent something.
        relationships_created = 0
        for rel in raw.relationships:
            subject = (rel.get("subject") or "").strip()
            predicate = (rel.get("predicate") or "").strip()
            obj = (rel.get("object") or "").strip()
            quote = rel.get("quote") or ""
            confidence = float(rel.get("confidence") or 0.0)

            if not subject or not predicate or not obj:
                warnings.append(
                    f"skipped malformed relationship: "
                    f"{subject!r} {predicate!r} {obj!r}"
                )
                continue

            subject_id, subject_warn = self._resolve_for_relationship(
                subject,
                resolved,
                entry_date=entry.entry_date,
                author_entity_id=author_entity_id,
                user_id=user_id,
            )
            # Refresh the cached author id in case the relationship
            # step was what created the author entity.
            if subject.lower() == self._author_name.lower():
                author_entity_id = subject_id

            object_id, object_warn = self._resolve_for_relationship(
                obj,
                resolved,
                entry_date=entry.entry_date,
                author_entity_id=author_entity_id,
                user_id=user_id,
            )
            if obj.lower() == self._author_name.lower():
                author_entity_id = object_id

            if subject_warn:
                warnings.append(subject_warn)
            if object_warn:
                warnings.append(object_warn)

            if subject_id is None or object_id is None:
                continue

            self._store.create_relationship(
                subject_id=subject_id,
                predicate=predicate,
                object_id=object_id,
                quote=quote,
                entry_id=entry_id,
                confidence=confidence,
                extraction_run_id=run_id,
            )
            relationships_created += 1

        self._store.mark_entry_extracted(entry_id)

        result = ExtractionResult(
            entry_id=entry_id,
            extraction_run_id=run_id,
            entities_created=entities_created,
            entities_matched=entities_matched,
            mentions_created=mentions_created,
            relationships_created=relationships_created,
            warnings=warnings,
        )
        log.info(
            "Extraction complete for entry %d: %d new / %d matched,"
            " %d mentions, %d relationships, %d warnings",
            entry_id,
            entities_created,
            entities_matched,
            mentions_created,
            relationships_created,
            len(warnings),
        )
        return result

    def extract_batch(
        self,
        entry_ids: list[int] | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        stale_only: bool = False,
        *,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> list[ExtractionResult]:
        """Run extraction across many entries with filter support.

        Per-entry exceptions are captured and surfaced as warnings on
        a synthetic ExtractionResult so one bad entry can't halt the
        whole batch.

        Args:
            on_progress: Optional keyword-only callback invoked as
                ``on_progress(current, total)``. Called once with
                ``(0, total)`` after the target entry set has been
                resolved but before the loop begins, then with the
                1-based ``(current, total)`` after each entry is
                processed — whether it succeeded or failed. A raising
                callback is logged and swallowed: a broken progress
                sink must never break the batch.
        """
        ids = self._resolve_batch_ids(
            entry_ids=entry_ids,
            start_date=start_date,
            end_date=end_date,
            stale_only=stale_only,
        )
        log.info("Extracting entities for %d entries", len(ids))

        total = len(ids)
        _report_progress(on_progress, 0, total)

        results: list[ExtractionResult] = []
        for idx, eid in enumerate(ids, start=1):
            try:
                results.append(self.extract_from_entry(eid))
            except Exception as e:  # noqa: BLE001 — we want to keep going
                log.exception("Extraction failed for entry %d", eid)
                results.append(
                    ExtractionResult(
                        entry_id=eid,
                        extraction_run_id="",
                        entities_created=0,
                        entities_matched=0,
                        mentions_created=0,
                        relationships_created=0,
                        warnings=[f"extraction failed: {e}"],
                    )
                )
            _report_progress(on_progress, idx, total)
        return results

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _resolve_batch_ids(
        self,
        entry_ids: list[int] | None,
        start_date: str | None,
        end_date: str | None,
        stale_only: bool,
    ) -> list[int]:
        """Figure out which entry ids to extract for a batch run."""
        if entry_ids:
            return list(entry_ids)

        # Pull from the raw connection so we can honour the stale_only
        # filter without adding a whole new repo method for a narrow
        # use case. The repo is the SQLite implementation in practice,
        # and the service-level Protocol only wraps read methods we
        # already have; sneaking in a conn read here is pragmatic.
        conn = getattr(self._repo, "_conn", None)
        if conn is None:
            raise RuntimeError(
                "EntityExtractionService.extract_batch requires a"
                " repository that exposes a SQLite connection"
            )

        sql = "SELECT id FROM entries WHERE 1=1"
        params: list[object] = []
        if start_date:
            sql += " AND entry_date >= ?"
            params.append(start_date)
        if end_date:
            sql += " AND entry_date <= ?"
            params.append(end_date)
        if stale_only:
            sql += " AND entity_extraction_stale = 1"
        sql += " ORDER BY entry_date, id"
        rows = conn.execute(sql, params).fetchall()
        return [int(r["id"]) for r in rows]

    def _resolve_entity(
        self,
        canonical: str,
        entity_type: str,
        description: str,
        aliases: list[str],
        first_seen: str,
        user_id: int | None = None,
    ) -> tuple[int, bool, str | None, tuple[int, float] | None]:
        """Resolve an extracted entity against the store.

        Returns (entity_id, created, warning, near_miss). `created` is
        True when a brand-new row was inserted. `warning` is populated if
        the embedding-similarity fallback fired (stage c). `near_miss` is
        a ``(candidate_id, score)`` tuple when a new entity was created
        but a similar entity exists below the merge threshold — the caller
        should persist this as a merge candidate for user review.
        """
        # Stage a: exact canonical name match.
        existing = self._store.get_entity_by_name(
            canonical, entity_type, user_id=user_id,
        )
        if existing is not None:
            return existing.id, False, None, None

        # Stage b: alias match on the canonical name itself, then on
        # each provided alias.
        by_alias = self._store.find_by_alias(
            canonical, entity_type, user_id=user_id,
        )
        if by_alias is not None:
            return by_alias.id, False, None, None
        for alias in aliases:
            by_alias = self._store.find_by_alias(
                alias, entity_type, user_id=user_id,
            )
            if by_alias is not None:
                return by_alias.id, False, None, None

        # Stage c: embedding similarity fallback.
        new_embedding = self._embeddings.embed_query(
            f"{canonical} {description}".strip()
        )
        candidates = self._store.list_entities_of_type_with_embeddings(
            entity_type, user_id=user_id,
        )
        best_id: int | None = None
        best_score = 0.0
        best_name = ""
        for candidate, vec in candidates:
            score = _cosine_similarity(new_embedding, vec)
            if score > best_score:
                best_score = score
                best_id = candidate.id
                best_name = candidate.canonical_name
        if best_id is not None and best_score >= self._threshold:
            warning = (
                f"potential merge: {canonical!r} ~ {best_name!r},"
                f" similarity {best_score:.3f}"
            )
            return best_id, False, warning, None

        # Still no match — create a new entity and remember its
        # embedding so future runs can short-circuit via stage c.
        entity = self._store.create_entity(
            entity_type=entity_type,
            canonical_name=canonical,
            description=description,
            first_seen=first_seen,
            user_id=user_id or 1,
        )
        self._store.set_entity_embedding(entity.id, new_embedding)

        # If there was a near-miss (below threshold but close), return
        # it so the caller can persist a merge candidate for user review.
        near_miss_threshold = max(self._threshold - 0.15, 0.5)
        near_miss: tuple[int, float] | None = None
        if best_id is not None and best_score >= near_miss_threshold:
            near_miss = (best_id, best_score)

        return entity.id, True, None, near_miss

    def _resolve_for_relationship(
        self,
        name: str,
        resolved: dict[str, int],
        entry_date: str,
        author_entity_id: int | None,
        user_id: int | None = None,
    ) -> tuple[int | None, str | None]:
        """Look up a subject/object name for a relationship row.

        Resolution order:
          1. Author name -> (create on first use if needed)
          2. `resolved` map populated during the entity pass
          3. Author name fallback is handled again in case the LLM
             spelled it differently
        """
        lower = name.strip().lower()
        if not lower:
            return None, "skipped relationship with empty name"

        if lower == self._author_name.lower():
            if author_entity_id is not None:
                return author_entity_id, None
            # Author wasn't in the extracted entity list — create one.
            existing = self._store.get_entity_by_name(
                self._author_name, "person", user_id=user_id,
            )
            if existing is not None:
                return existing.id, None
            author = self._store.create_entity(
                entity_type="person",
                canonical_name=self._author_name,
                description="Journal author",
                first_seen=entry_date,
                user_id=user_id or 1,
            )
            return author.id, None

        if lower in resolved:
            return resolved[lower], None

        return (
            None,
            f"skipped relationship — {name!r} not in extracted entities",
        )

    # Unused but provided for completeness when adapter tests want to
    # prod internal state.
    def _debug(self) -> dict[str, Any]:
        return {
            "author_name": self._author_name,
            "threshold": self._threshold,
        }
