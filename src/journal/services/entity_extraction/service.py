"""Entity extraction service — LLM extraction + dedup + sanity sweep.

The orchestrator class lives in this module. Self-contained helpers live
in sibling modules under ``services/entity_extraction/``:

- ``signature.py`` — string-signature heuristic for merge candidates
  that pure embedding distance misses.
- ``matching.py`` — stage-0 LLM-asserted match (WU4-D four-guard sanity
  check) and ``cosine_similarity``.
- ``sanity.py`` — post-extraction sanity sweep that quarantines entities
  whose canonical name is unsupported by their quotes / entry text.

``extract_from_entry`` is the orchestrator that ties them together. It
remains relatively large by design — it is the integration point.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
import uuid
from typing import TYPE_CHECKING, Any

from journal.models import ExtractionResult
from journal.services.entity_extraction.matching import (
    cosine_similarity,
    try_llm_asserted_match,
)
from journal.services.entity_extraction.sanity import run_sanity_sweep
from journal.services.entity_extraction.signature import find_signature_matches

if TYPE_CHECKING:
    from collections.abc import Callable

    from journal.db.repository import EntryRepository
    from journal.db.user_repository import UserRepository
    from journal.entitystore.store import EntityStore
    from journal.providers.embeddings import EmbeddingsProvider
    from journal.providers.extraction import ExtractionProvider

log = logging.getLogger(__name__)



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
        user_repo: UserRepository | None = None,
        # WU4: known-entity prompt injection knobs.
        llm_candidate_top_k: int = 30,
        llm_candidate_threshold: float = 0.4,
        llm_match_min_cosine: float = 0.3,
    ) -> None:
        self._repo = repository
        self._store = entity_store
        self._extractor = extraction_provider
        self._embeddings = embeddings_provider
        self._author_name = author_name
        self._threshold = dedup_similarity_threshold
        self._user_repo = user_repo
        self._llm_candidate_top_k = llm_candidate_top_k
        self._llm_candidate_threshold = llm_candidate_threshold
        self._llm_match_min_cosine = llm_match_min_cosine

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def _get_author_name(self, user_id: int | None) -> str:
        """Look up the display name for a user, falling back to the
        global default when the user repo is unavailable or the user
        is not found."""
        if user_id and self._user_repo is not None:
            user = self._user_repo.get_user_by_id(user_id)
            if user is not None:
                return user.display_name
        return self._author_name

    def reembed_entity_for_description(
        self, entity_id: int, *, user_id: int,
    ) -> dict[str, object]:
        """Recompute and persist an entity's embedding from its current
        canonical name + description.

        Used by the ``entity_reembed`` background job, which fires when
        a user edits an entity's description. The stored embedding feeds
        stage-c similarity matching during extraction (see
        ``_resolve_entity``); without this, an edit only changes
        cosmetic display text and never influences future recognition.

        Returns a small summary dict suitable for the job result.
        Empty / whitespace-only descriptions short-circuit with
        ``embedded=False`` so the job records why nothing happened
        instead of writing a meaningless zero-vector.
        """
        entity = self._store.get_entity(entity_id, user_id=user_id)
        if entity is None:
            raise ValueError(f"Entity {entity_id} not found for user {user_id}")

        text = f"{entity.canonical_name} {entity.description}".strip()
        if not text or not entity.description.strip():
            log.info(
                "Reembed skipped for entity %d: description empty",
                entity_id,
            )
            return {
                "entity_id": entity_id,
                "embedded": False,
                "reason": "empty description",
            }

        embedding = self._embeddings.embed_query(text)
        self._store.set_entity_embedding(entity_id, embedding)
        log.info(
            "Reembedded entity %d (%s, %d dims)",
            entity_id, entity.canonical_name, len(embedding),
        )
        return {
            "entity_id": entity_id,
            "embedded": True,
            "dimensions": len(embedding),
        }

    def build_known_entity_candidates(
        self,
        entry_text: str,
        *,
        user_id: int | None,
    ) -> tuple[list[dict[str, Any]], dict[int, list[float]]]:
        """Vector pre-filter for the known-entities block.

        Embeds ``entry_text``, retrieves all of the user's entities
        with stored embeddings (across all types), computes cosine
        similarity, drops anything below
        ``llm_candidate_threshold``, and returns the top
        ``llm_candidate_top_k``.

        Returns ``(candidates, candidate_embeddings)`` where
        ``candidates`` is the list of dicts shaped for the LLM tool
        (``id``, ``canonical_name``, ``entity_type``, ``aliases``,
        ``description``) and ``candidate_embeddings`` is a parallel
        ``id -> embedding`` map kept by the caller for guard D in the
        stage-0 sanity check (saves another store round-trip).

        When ``user_id`` is None or the user has no entities with
        embeddings yet, both return values are empty.
        """
        if user_id is None:
            return [], {}

        # No fast path for "all types" in the store today; iterate
        # the six-element ENTITY_TYPES tuple and union. The cost is
        # six small SELECTs against an indexed table — cheap.
        from journal.providers.extraction import ENTITY_TYPES

        all_with_embeddings: list[tuple[Any, list[float]]] = []
        for etype in ENTITY_TYPES:
            all_with_embeddings.extend(
                self._store.list_entities_of_type_with_embeddings(
                    etype, user_id=user_id,
                )
            )

        if not all_with_embeddings:
            return [], {}

        entry_vec = self._embeddings.embed_query(entry_text)

        scored: list[tuple[float, Any, list[float]]] = []
        for entity, vec in all_with_embeddings:
            score = cosine_similarity(entry_vec, vec)
            if score < self._llm_candidate_threshold:
                continue
            scored.append((score, entity, vec))

        scored.sort(key=lambda t: t[0], reverse=True)
        scored = scored[: self._llm_candidate_top_k]

        candidates: list[dict[str, Any]] = []
        embeddings_by_id: dict[int, list[float]] = {}
        for _score, entity, vec in scored:
            candidates.append(
                {
                    "id": entity.id,
                    "canonical_name": entity.canonical_name,
                    "entity_type": entity.entity_type,
                    "aliases": list(entity.aliases),
                    "description": entity.description or "",
                }
            )
            embeddings_by_id[entity.id] = vec

        return candidates, embeddings_by_id

    def extract_from_entry(self, entry_id: int) -> ExtractionResult:
        entry = self._repo.get_entry(entry_id)
        if entry is None:
            raise ValueError(f"Entry {entry_id} not found")

        user_id = entry.user_id or None
        author_name = self._get_author_name(user_id)
        run_id = str(uuid.uuid4())
        log.info(
            "Extracting entities from entry %d (run=%s, author=%s)",
            entry_id, run_id, author_name,
        )

        entry_text = entry.final_text or entry.raw_text or ""
        known_entities, candidate_embeddings = self.build_known_entity_candidates(
            entry_text, user_id=user_id,
        )
        log.info(
            "Extraction candidate set for entry %d: %d known entities",
            entry_id, len(known_entities),
        )
        # Local scratch for this extraction call — threaded explicitly
        # through ``_resolve_entity`` and ``_try_llm_asserted_match`` so
        # guard B (membership) and guard D (cosine) on stage-0
        # LLM-asserted matches can read them without reaching for state
        # on ``self``. Empty in unit tests that drive ``_resolve_entity``
        # directly with no candidates.
        candidate_ids: set[int] = {int(c["id"]) for c in known_entities}
        candidate_embeddings_by_id: dict[int, list[float]] = candidate_embeddings

        raw = self._extractor.extract_entities(
            entry_text=entry_text,
            entry_date=entry.entry_date,
            author_name=author_name,
            known_entities=known_entities or None,
        )

        # Idempotency: clear any prior extraction results for this
        # entry before writing the new ones. A re-run must never
        # produce duplicate mentions or relationships.
        # Snapshot the entity ids that currently have mentions for this
        # entry so we can prune any that become orphans after re-extraction.
        prior_entity_ids = [
            m.entity_id for m in self._store.get_mentions_for_entry(entry_id)
        ]
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
        # Every entity touched by this extraction run — used by the
        # post-extraction sanity sweep below.
        touched_entity_ids: set[int] = set()

        for raw_entity in raw.entities:
            canonical = (raw_entity.get("canonical_name") or "").strip()
            entity_type = raw_entity.get("entity_type") or "other"
            description = raw_entity.get("description") or ""
            aliases: list[str] = list(raw_entity.get("aliases") or [])
            quote = raw_entity.get("quote") or ""
            confidence = float(raw_entity.get("confidence") or 0.0)
            pending_quarantine_reason = (
                raw_entity.get("pending_quarantine_reason") or ""
            )
            raw_matches_known_id = raw_entity.get("matches_known_id")
            matches_known_id = (
                int(raw_matches_known_id)
                if isinstance(raw_matches_known_id, int)
                and not isinstance(raw_matches_known_id, bool)
                else None
            )
            match_justification = raw_entity.get("match_justification")

            if not canonical:
                continue

            (
                entity_id,
                created,
                warning,
                near_miss,
                signature_matches,
                match_source,
            ) = self._resolve_entity(
                canonical=canonical,
                entity_type=entity_type,
                description=description,
                aliases=aliases,
                first_seen=entry.entry_date,
                user_id=user_id,
                matches_known_id=matches_known_id,
                match_justification=match_justification,
                candidate_ids=candidate_ids,
                candidate_embeddings=candidate_embeddings_by_id,
            )
            touched_entity_ids.add(entity_id)
            if created:
                entities_created += 1
                # If the LLM result was flagged at the provider layer
                # because the canonical can't be found in its source
                # quote, soft-quarantine the freshly-created entity so
                # the operator sees it in the quarantine UI before it
                # pollutes any chart.
                if pending_quarantine_reason:
                    try:
                        self._store.quarantine_entity(
                            entity_id, reason=pending_quarantine_reason,
                        )
                        log.info(
                            "Quarantined new entity %d on creation: %s",
                            entity_id, pending_quarantine_reason,
                        )
                    except Exception as exc:  # noqa: BLE001
                        log.warning(
                            "Failed to quarantine new entity %d: %s",
                            entity_id, exc,
                        )
            else:
                entities_matched += 1
            if warning:
                warnings.append(warning)
            # Skip candidate creation for any pair the user has already
            # rejected. A user_id of None means single-tenant / unset
            # context; default to user 1 to match create_entity below.
            rejection_user_id = user_id if user_id is not None else 1
            # ``near_miss`` is always None — the embedding near-miss
            # candidate path was removed in WU4 (it produced 0 useful
            # suggestions vs ~21 false positives in prod). The signature
            # heuristic is the sole remaining candidate source.
            assert near_miss is None
            for candidate_id, score in signature_matches:
                if candidate_id == entity_id:
                    continue
                if self._store.is_pair_rejected(
                    rejection_user_id, candidate_id, entity_id,
                ):
                    continue
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
            if canonical.lower() == author_name.lower():
                author_entity_id = entity_id

            # Add any newly-seen aliases to the store. Idempotent.
            for alias in aliases:
                self._store.add_alias(entity_id, alias)

            # Create the mention tying this entity to the current entry.
            try:
                self._store.create_mention(
                    entity_id=entity_id,
                    entry_id=entry_id,
                    quote=quote,
                    confidence=confidence,
                    extraction_run_id=run_id,
                    match_source=match_source,
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(
                    f"Entry {entry_id} was deleted during extraction"
                ) from exc

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
                author_name=author_name,
                user_id=user_id,
            )
            # Refresh the cached author id in case the relationship
            # step was what created the author entity.
            if subject.lower() == author_name.lower():
                author_entity_id = subject_id

            object_id, object_warn = self._resolve_for_relationship(
                obj,
                resolved,
                entry_date=entry.entry_date,
                author_entity_id=author_entity_id,
                author_name=author_name,
                user_id=user_id,
            )
            if obj.lower() == author_name.lower():
                author_entity_id = object_id

            if subject_warn:
                warnings.append(subject_warn)
            if object_warn:
                warnings.append(object_warn)

            if subject_id is None or object_id is None:
                continue

            try:
                self._store.create_relationship(
                    subject_id=subject_id,
                    predicate=predicate,
                    object_id=object_id,
                    quote=quote,
                    entry_id=entry_id,
                    confidence=confidence,
                    extraction_run_id=run_id,
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(
                    f"Entry {entry_id} was deleted during extraction"
                ) from exc
            relationships_created += 1

        # Prune entities that lost all their mentions after re-extraction.
        if prior_entity_ids:
            orphans_deleted = self._store.delete_orphaned_entities(
                list(set(prior_entity_ids))
            )
        else:
            orphans_deleted = 0

        # Post-extraction sanity sweep — see services/entity_extraction/sanity.py
        # for the rationale (quarantines hallucinated / zombie-rebound entities
        # whose canonical name isn't supported by any mention quote or entry
        # text). The author entity is exempt: first-person prose ("I went...")
        # legitimately produces an "author" mention whose canonical (the user's
        # display name) is never written verbatim.
        run_sanity_sweep(
            self._store,
            self._repo,
            touched_entity_ids=touched_entity_ids,
            author_name=author_name,
            run_id=run_id,
        )

        self._store.mark_entry_extracted(entry_id)

        result = ExtractionResult(
            entry_id=entry_id,
            extraction_run_id=run_id,
            entities_created=entities_created,
            entities_matched=entities_matched,
            mentions_created=mentions_created,
            relationships_created=relationships_created,
            warnings=warnings,
            entities_deleted=orphans_deleted,
        )
        log.info(
            "Extraction complete for entry %d: %d new / %d matched,"
            " %d mentions, %d relationships, %d orphans pruned, %d warnings",
            entry_id,
            entities_created,
            entities_matched,
            mentions_created,
            relationships_created,
            orphans_deleted,
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
        user_id: int | None = None,
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
            user_id: When set, only entries belonging to this user are
                included in the batch.
        """
        ids = self._resolve_batch_ids(
            entry_ids=entry_ids,
            start_date=start_date,
            end_date=end_date,
            stale_only=stale_only,
            user_id=user_id,
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
        user_id: int | None = None,
    ) -> list[int]:
        """Figure out which entry ids to extract for a batch run."""
        if entry_ids:
            return list(entry_ids)

        # Pull from the raw connection so we can honour the stale_only
        # filter without adding a whole new repo method for a narrow
        # use case. The repo is the SQLite implementation in practice,
        # and the service-level Protocol only wraps read methods we
        # already have; sneaking in a conn read here is pragmatic.
        conn = getattr(self._repo, "connection", None)
        if conn is None:
            raise RuntimeError(
                "EntityExtractionService.extract_batch requires a"
                " repository that exposes a SQLite connection"
            )

        sql = "SELECT id FROM entries WHERE 1=1"
        params: list[object] = []
        if user_id is not None:
            sql += " AND user_id = ?"
            params.append(user_id)
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
        matches_known_id: int | None = None,
        match_justification: str | None = None,
        candidate_ids: set[int] | None = None,
        candidate_embeddings: dict[int, list[float]] | None = None,
    ) -> tuple[
        int,
        bool,
        str | None,
        tuple[int, float] | None,
        list[tuple[int, float]],
        str | None,
    ]:
        """Resolve an extracted entity against the store.

        Returns ``(entity_id, created, warning, near_miss,
        signature_matches, match_source)``.

        ``match_source`` is one of ``"stage_a"`` (exact canonical
        match), ``"stage_b"`` (alias match), ``"stage_c"`` (embedding
        similarity ≥ threshold), ``"llm_asserted"`` (matches_known_id
        accepted by all four WU4-D guards), or ``None`` (a brand-new
        row was created).

        ``created`` is True when a brand-new row was inserted. ``warning``
        is populated if the embedding-similarity fallback fired (stage c).
        ``near_miss`` is a ``(candidate_id, score)`` tuple when a new entity
        was created but a similar entity exists below the merge threshold —
        the caller should persist this as a merge candidate.

        ``signature_matches`` is a list of ``(candidate_id, score)`` pairs
        produced by the relaxed string-signature heuristic.
        """
        # Run the relaxed signature heuristic against every same-type
        # entity (regardless of whether the current extraction matches
        # one of them via stage a/b or proceeds to stage c). This is the
        # source of merge candidates that the embedding distance misses.
        signature_matches = find_signature_matches(
            self._store, canonical, entity_type, user_id=user_id,
        )

        # Stage 0: LLM asserted a match against a known entity.
        # Accept only if all four guards (A: user-scope, B: candidate
        # membership, C: type match, D: cosine ≥ floor) pass.
        if matches_known_id is not None:
            asserted_id = try_llm_asserted_match(
                self._store,
                self._embeddings,
                asserted_id=int(matches_known_id),
                canonical=canonical,
                entity_type=entity_type,
                description=description,
                user_id=user_id,
                justification=match_justification,
                candidate_ids=candidate_ids or set(),
                candidate_embeddings=candidate_embeddings or {},
                min_cosine=self._llm_match_min_cosine,
            )
            if asserted_id is not None:
                return asserted_id, False, None, None, signature_matches, "llm_asserted"
            # Fall through to stages a/b/c.

        # Stage a: exact canonical name match.
        existing = self._store.get_entity_by_name(
            canonical, entity_type, user_id=user_id,
        )
        if existing is not None:
            return existing.id, False, None, None, signature_matches, "stage_a"

        # Stage b: alias match on the canonical name itself, then on
        # each provided alias.
        by_alias = self._store.find_by_alias(
            canonical, entity_type, user_id=user_id,
        )
        if by_alias is not None:
            return by_alias.id, False, None, None, signature_matches, "stage_b"
        for alias in aliases:
            by_alias = self._store.find_by_alias(
                alias, entity_type, user_id=user_id,
            )
            if by_alias is not None:
                return by_alias.id, False, None, None, signature_matches, "stage_b"

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
            score = cosine_similarity(new_embedding, vec)
            if score > best_score:
                best_score = score
                best_id = candidate.id
                best_name = candidate.canonical_name
        if best_id is not None and best_score >= self._threshold:
            warning = (
                f"potential merge: {canonical!r} ~ {best_name!r},"
                f" similarity {best_score:.3f}"
            )
            return best_id, False, warning, None, signature_matches, "stage_c"

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

        # WU4: the embedding "near-miss" candidate path was removed —
        # in prod it produced 0 acceptances over ~21 false positives.
        # Auto-merge at >= self._threshold still applies above. The
        # signature heuristic is now the sole candidate source.
        return entity.id, True, None, None, signature_matches, None

    def _resolve_for_relationship(
        self,
        name: str,
        resolved: dict[str, int],
        entry_date: str,
        author_entity_id: int | None,
        author_name: str | None = None,
        user_id: int | None = None,
    ) -> tuple[int | None, str | None]:
        """Look up a subject/object name for a relationship row.

        Resolution order:
          1. Author name -> (create on first use if needed)
          2. `resolved` map populated during the entity pass
          3. Author name fallback is handled again in case the LLM
             spelled it differently
        """
        effective_author = author_name or self._author_name
        lower = name.strip().lower()
        if not lower:
            return None, "skipped relationship with empty name"

        if lower == effective_author.lower():
            if author_entity_id is not None:
                return author_entity_id, None
            # Author wasn't in the extracted entity list — create one.
            existing = self._store.get_entity_by_name(
                effective_author, "person", user_id=user_id,
            )
            if existing is not None:
                return existing.id, None
            author = self._store.create_entity(
                entity_type="person",
                canonical_name=effective_author,
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

    # prod internal state.
    def _debug(self) -> dict[str, Any]:
        return {
            "author_name": self._author_name,
            "threshold": self._threshold,
        }
