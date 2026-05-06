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
import re
import sqlite3
import uuid
from typing import TYPE_CHECKING, Any

from journal.models import Entity, ExtractionResult

if TYPE_CHECKING:
    from collections.abc import Callable

    from journal.db.repository import EntryRepository
    from journal.db.user_repository import UserRepository
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


# Synthetic similarity scores assigned to merge candidates surfaced via
# the relaxed string-signature heuristic. We pick values close to 1.0 so
# the candidates float to the top of the merge-review UI (which sorts by
# similarity DESC) — the heuristic is high-confidence on near-duplicate
# place names that the embedding distance happens to miss.
_SIGNATURE_EXACT_MATCH_SCORE = 1.0
_SIGNATURE_SHORT_DIFF_SCORE = 0.95


def _normalized_signature(name: str) -> str:
    """Lowercase, strip whitespace runs, drop trivial punctuation.

    Designed so OCR-driven splits like ``Zij Kanaal`` vs ``Zijkanaal``
    or ``St. Mary`` vs ``St Mary`` collapse to the same string. We keep
    this conservative — only commas, periods, and hyphens are stripped
    so names that intentionally contain other punctuation aren't merged
    by accident.
    """
    s = name.lower().strip()
    s = re.sub(r"\s+", "", s)
    s = re.sub(r"[\.,\-]", "", s)
    return s


def _is_short_difference(longer: str, shorter: str) -> bool:
    """Return True when ``shorter`` is a substring of ``longer`` and the
    leftover after removing it is small enough to suggest a near-duplicate.

    We treat "small" as either ``<= 6 characters`` or a single token
    (no whitespace). Both cases catch trailing/leading qualifiers like
    ``" Weg"``, ``" Zuid"``, or ``"St "`` that distinguish near-duplicate
    place names without producing false positives on long sentences that
    happen to contain a short common substring.
    """
    if shorter not in longer:
        return False
    leftover = longer.replace(shorter, "", 1).strip()
    return len(leftover) <= 6 or " " not in leftover


def _common_prefix_len(a: str, b: str) -> int:
    """Length of the longest common prefix of ``a`` and ``b``."""
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def _common_suffix_len(a: str, b: str) -> int:
    """Length of the longest common suffix of ``a`` and ``b``."""
    n = min(len(a), len(b))
    i = 0
    while i < n and a[-1 - i] == b[-1 - i]:
        i += 1
    return i


def _is_signature_match(name_a: str, name_b: str) -> bool:
    """True if two entity names should be flagged as merge candidates by
    the relaxed string-signature heuristic.

    Three cases trigger a match:
      1. Normalized signatures (lowercased, whitespace-stripped, trivial
         punctuation removed) are identical.
      2. One signature is a substring of the other and the leftover is
         short (≤ 6 chars or a single token).
      3. The two signatures share a long common prefix or suffix
         (≥ 60% of the shorter name and ≥ 4 chars) and each unique tail
         is short (≤ 6 chars or a single token).

    Case 3 catches near-duplicates whose trailing/leading qualifiers
    differ (e.g. ``Zij Kanaal C Weg`` vs ``Zij Kanaal C Zuid``) which
    pure substring containment misses.

    The caller is responsible for filtering out same-id pairs and
    enforcing same-``entity_type``.
    """
    # Degenerate inputs: skip to avoid false positives on empty strings
    # or single-character names that would substring-match anything.
    if not name_a.strip() or not name_b.strip():
        return False
    if min(len(name_a.strip()), len(name_b.strip())) < 2:
        return False

    sig_a = _normalized_signature(name_a)
    sig_b = _normalized_signature(name_b)
    if not sig_a or not sig_b:
        return False
    if sig_a == sig_b:
        return True

    # Case 2: substring + short-leftover. Compare the
    # whitespace-collapsed but case-preserved variants so the leftover
    # length reflects the original strings, not the case-folded
    # signatures.
    collapsed_a = re.sub(r"\s+", "", name_a.strip())
    collapsed_b = re.sub(r"\s+", "", name_b.strip())
    if sig_b in sig_a and _is_short_difference(collapsed_a, collapsed_b):
        return True
    if sig_a in sig_b and _is_short_difference(collapsed_b, collapsed_a):
        return True

    # Case 3: long common prefix or suffix with short divergent tails.
    # Operate on the signatures so case/whitespace differences don't
    # truncate the common region. We require:
    #   - the common region to be ≥ 8 chars (avoids "Amsterdam" /
    #     "Rotterdam" sharing a 6-char "terdam" suffix);
    #   - the common region to be at least twice the max tail length
    #     (so the shared portion clearly dominates the divergence);
    #   - both divergent tails to be short (≤ 6 chars).
    prefix = _common_prefix_len(sig_a, sig_b)
    if prefix >= 8:
        tail_a = sig_a[prefix:]
        tail_b = sig_b[prefix:]
        max_tail = max(len(tail_a), len(tail_b))
        if (
            _is_short_tail(tail_a)
            and _is_short_tail(tail_b)
            and prefix >= 2 * max_tail
        ):
            return True

    suffix = _common_suffix_len(sig_a, sig_b)
    if suffix >= 8:
        tail_a = sig_a[: len(sig_a) - suffix]
        tail_b = sig_b[: len(sig_b) - suffix]
        max_tail = max(len(tail_a), len(tail_b))
        if (
            _is_short_tail(tail_a)
            and _is_short_tail(tail_b)
            and suffix >= 2 * max_tail
        ):
            return True

    return False


def _is_short_tail(tail: str) -> bool:
    """Whether a divergent tail string counts as 'short' for the
    common-prefix/suffix heuristic.

    A tail of ≤ 6 characters is always short. Longer tails are only
    accepted when they're empty or a single token — but since we've
    already collapsed whitespace, this collapses to the length check.
    """
    return len(tail) <= 6


def _signature_match_score(name_a: str, name_b: str) -> float | None:
    """Return a synthetic similarity score for the heuristic, or None.

    ``_SIGNATURE_EXACT_MATCH_SCORE`` for identical signatures (case /
    whitespace / trivial-punctuation differences only),
    ``_SIGNATURE_SHORT_DIFF_SCORE`` for short-substring near-duplicates.
    """
    if not _is_signature_match(name_a, name_b):
        return None
    if _normalized_signature(name_a) == _normalized_signature(name_b):
        return _SIGNATURE_EXACT_MATCH_SCORE
    return _SIGNATURE_SHORT_DIFF_SCORE


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
    ) -> None:
        self._repo = repository
        self._store = entity_store
        self._extractor = extraction_provider
        self._embeddings = embeddings_provider
        self._author_name = author_name
        self._threshold = dedup_similarity_threshold
        self._user_repo = user_repo

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

        raw = self._extractor.extract_entities(
            entry_text=entry.final_text or entry.raw_text,
            entry_date=entry.entry_date,
            author_name=author_name,
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

            if not canonical:
                continue

            (
                entity_id,
                created,
                warning,
                near_miss,
                signature_matches,
            ) = self._resolve_entity(
                canonical=canonical,
                entity_type=entity_type,
                description=description,
                aliases=aliases,
                first_seen=entry.entry_date,
                user_id=user_id,
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
            if near_miss is not None:
                candidate_id, score = near_miss
                with contextlib.suppress(Exception):
                    self._store.create_merge_candidate(
                        entity_id_a=candidate_id,
                        entity_id_b=entity_id,
                        similarity=score,
                        extraction_run_id=run_id,
                    )
            for candidate_id, score in signature_matches:
                if candidate_id == entity_id:
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

        # Post-extraction sanity sweep: any entity touched in this run
        # whose canonical name can't be found anywhere in its mention
        # quotes or any mentioned entry's final_text gets soft-
        # quarantined. Catches LLM hallucinations that survived the
        # provider-level repair stage and zombie-rebound entities (e.g.
        # a hallucinated name re-bound to a corrected quote via
        # embedding similarity).
        #
        # The author entity is exempt: first-person prose ("I went...")
        # legitimately produces an "author" mention whose canonical
        # (the user's display name) is never written verbatim. Skip it.
        author_lower = author_name.lower()
        for touched_id in touched_entity_ids:
            entity = self._store.get_entity(touched_id)
            if entity is None or entity.is_quarantined:
                # Already quarantined (either pre-existing or by the
                # pending_quarantine_reason path above) or deleted by
                # orphan cleanup — nothing to do.
                continue
            if entity.canonical_name.lower() == author_lower:
                continue
            if not self._canonical_name_supported(entity):
                reason = (
                    f"canonical name {entity.canonical_name!r} not found "
                    f"in any mention quote or entry text after extraction "
                    f"run {run_id}"
                )
                try:
                    self._store.quarantine_entity(touched_id, reason=reason)
                    log.info(
                        "Quarantined entity %d (sanity sweep): %s",
                        touched_id, reason,
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "Sanity-sweep quarantine failed for entity %d: %s",
                        touched_id, exc,
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
        conn = getattr(self._repo, "_conn", None)
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
    ) -> tuple[
        int,
        bool,
        str | None,
        tuple[int, float] | None,
        list[tuple[int, float]],
    ]:
        """Resolve an extracted entity against the store.

        Returns ``(entity_id, created, warning, near_miss, signature_matches)``.

        ``created`` is True when a brand-new row was inserted. ``warning``
        is populated if the embedding-similarity fallback fired (stage c).
        ``near_miss`` is a ``(candidate_id, score)`` tuple when a new entity
        was created but a similar entity exists below the merge threshold —
        the caller should persist this as a merge candidate.

        ``signature_matches`` is a list of ``(candidate_id, score)`` pairs
        produced by the relaxed string-signature heuristic (lowercased,
        whitespace-stripped equality, or short-substring containment).
        These are emitted in addition to ``near_miss`` so OCR-driven
        near-duplicates that the embedding distance happens to miss
        (e.g. ``Zij Kanaal C Weg`` vs ``Zij Kanaal C Zuid``) still surface
        in the merge-review UI. The list is unioned with the embedding
        path's result; the caller is responsible for persisting them.
        """
        # Run the relaxed signature heuristic against every same-type
        # entity (regardless of whether the current extraction matches
        # one of them via stage a/b or proceeds to stage c). This is the
        # source of merge candidates that the embedding distance misses.
        signature_matches = self._find_signature_matches(
            canonical, entity_type, user_id=user_id,
        )

        # Stage a: exact canonical name match.
        existing = self._store.get_entity_by_name(
            canonical, entity_type, user_id=user_id,
        )
        if existing is not None:
            return existing.id, False, None, None, signature_matches

        # Stage b: alias match on the canonical name itself, then on
        # each provided alias.
        by_alias = self._store.find_by_alias(
            canonical, entity_type, user_id=user_id,
        )
        if by_alias is not None:
            return by_alias.id, False, None, None, signature_matches
        for alias in aliases:
            by_alias = self._store.find_by_alias(
                alias, entity_type, user_id=user_id,
            )
            if by_alias is not None:
                return by_alias.id, False, None, None, signature_matches

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
            return best_id, False, warning, None, signature_matches

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

        return entity.id, True, None, near_miss, signature_matches

    def _find_signature_matches(
        self,
        canonical: str,
        entity_type: str,
        user_id: int | None = None,
    ) -> list[tuple[int, float]]:
        """Scan existing same-type entities for string-signature matches.

        Returns a list of ``(entity_id, score)`` for every existing entity
        whose canonical name pairs with ``canonical`` under
        ``_is_signature_match``. Empty when nothing matches.

        We pull the entity rows via ``list_entities`` (not the embedding
        variant) so the heuristic still works for entities that were
        somehow stored without an embedding. The list is bounded to a
        large but finite limit — same-type collections are small in
        practice.
        """
        if not canonical or not canonical.strip():
            return []
        # 5000 is well above any realistic same-type cardinality but caps
        # us in case of pathological data.
        existing = self._store.list_entities(
            entity_type=entity_type, limit=5000, user_id=user_id,
        )
        matches: list[tuple[int, float]] = []
        for candidate in existing:
            if candidate.canonical_name == canonical:
                # Same name — that's stage-a territory, not a candidate.
                continue
            score = _signature_match_score(canonical, candidate.canonical_name)
            if score is not None:
                matches.append((candidate.id, score))
        return matches

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

    def _canonical_name_supported(self, entity: Entity) -> bool:
        """True if the entity's canonical name appears in at least one
        of its mention quotes, or in the ``final_text`` of any entry
        the entity is mentioned in.

        Comparison is **case-insensitive** and **whitespace-tolerant**:
        whitespace runs on both sides are collapsed to a single space
        before substring matching. This mirrors the provider-level
        repair so a canonical that only shows up with extra/missing
        whitespace is still considered supported.
        """
        canonical = (entity.canonical_name or "").strip()
        if not canonical:
            # An empty canonical can't be 'found' anywhere meaningful;
            # don't quarantine on that signal alone.
            return True
        canonical_lower = re.sub(r"\s+", " ", canonical.lower())

        # 1. Mention quotes — there may be many across all entries.
        # We pull a generous limit; in practice an entity has a handful
        # of mentions, and even active power users top out in the low
        # hundreds. The limit is a safety belt, not an expected boundary.
        mentions = self._store.get_mentions_for_entity(
            entity.id, limit=10_000, offset=0,
        )
        for m in mentions:
            quote = m.quote or ""
            if not quote:
                continue
            quote_lower = re.sub(r"\s+", " ", quote.lower())
            if canonical_lower in quote_lower:
                return True

        # 2. Entry final_text for any entry the entity is mentioned in.
        # final_text is already populated for OCR'd entries (it falls
        # back to raw_text in the repository hydrator).
        seen_entry_ids: set[int] = set()
        for m in mentions:
            seen_entry_ids.add(m.entry_id)
        for entry_id in seen_entry_ids:
            entry = self._repo.get_entry(entry_id)
            if entry is None:
                continue
            text = entry.final_text or entry.raw_text or ""
            if not text:
                continue
            text_lower = re.sub(r"\s+", " ", text.lower())
            if canonical_lower in text_lower:
                return True

        return False

    # Unused but provided for completeness when adapter tests want to
    # prod internal state.
    def _debug(self) -> dict[str, Any]:
        return {
            "author_name": self._author_name,
            "threshold": self._threshold,
        }
