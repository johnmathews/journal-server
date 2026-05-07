"""Post-extraction sanity sweep.

After ``EntityExtractionService.extract_from_entry`` writes its mentions
and relationships, ``run_sanity_sweep`` walks every entity touched in
the run and quarantines any whose canonical name is not supported by
the actual quotes / entry text it was tied to. Catches LLM
hallucinations that survived the provider-level repair stage and
zombie-rebound entities (where a hallucinated name was rebound to a
corrected quote via embedding similarity).

The author entity is exempt: first-person prose ("I went...") legitimately
produces an "author" mention whose canonical (the user's display name)
is never written verbatim.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from journal.db.repository import EntryRepository
    from journal.entitystore.store import EntityStore
    from journal.models import Entity

log = logging.getLogger(__name__)


def is_canonical_name_supported(
    store: EntityStore, repo: EntryRepository, entity: Entity,
) -> bool:
    """True if the entity's canonical name appears in at least one of its
    mention quotes, or in the ``final_text`` of any entry the entity is
    mentioned in.

    Comparison is **case-insensitive** and **whitespace-tolerant**:
    whitespace runs on both sides are collapsed to a single space before
    substring matching. This mirrors the provider-level repair so a
    canonical that only shows up with extra/missing whitespace is still
    considered supported.
    """
    canonical = (entity.canonical_name or "").strip()
    if not canonical:
        # An empty canonical can't be 'found' anywhere meaningful;
        # don't quarantine on that signal alone.
        return True
    canonical_lower = re.sub(r"\s+", " ", canonical.lower())

    # 1. Mention quotes — there may be many across all entries. We pull
    # a generous limit; in practice an entity has a handful of mentions,
    # and even active power users top out in the low hundreds. The limit
    # is a safety belt, not an expected boundary.
    mentions = store.get_mentions_for_entity(entity.id, limit=10_000, offset=0)
    for m in mentions:
        quote = m.quote or ""
        if not quote:
            continue
        quote_lower = re.sub(r"\s+", " ", quote.lower())
        if canonical_lower in quote_lower:
            return True

    # 2. Entry final_text for any entry the entity is mentioned in.
    # final_text is already populated for OCR'd entries (it falls back
    # to raw_text in the repository hydrator).
    seen_entry_ids: set[int] = set()
    for m in mentions:
        seen_entry_ids.add(m.entry_id)
    for entry_id in seen_entry_ids:
        entry = repo.get_entry(entry_id)
        if entry is None:
            continue
        text = entry.final_text or entry.raw_text or ""
        if not text:
            continue
        text_lower = re.sub(r"\s+", " ", text.lower())
        if canonical_lower in text_lower:
            return True

    return False


def run_sanity_sweep(
    store: EntityStore,
    repo: EntryRepository,
    *,
    touched_entity_ids: set[int],
    author_name: str,
    run_id: str,
) -> None:
    """Quarantine any touched entity whose canonical name is unsupported.

    Already-quarantined entities (pre-existing or set during this run by
    other paths) and the author entity are skipped. Failures inside the
    quarantine call are logged and do not abort the sweep — the rest of
    the touched entities still get checked.
    """
    author_lower = author_name.lower()
    for touched_id in touched_entity_ids:
        entity = store.get_entity(touched_id)
        if entity is None or entity.is_quarantined:
            # Already quarantined or deleted by orphan cleanup —
            # nothing to do.
            continue
        if entity.canonical_name.lower() == author_lower:
            continue
        if not is_canonical_name_supported(store, repo, entity):
            reason = (
                f"canonical name {entity.canonical_name!r} not found "
                f"in any mention quote or entry text after extraction "
                f"run {run_id}"
            )
            try:
                store.quarantine_entity(touched_id, reason=reason)
                log.info(
                    "Quarantined entity %d (sanity sweep): %s",
                    touched_id, reason,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "Sanity-sweep quarantine failed for entity %d: %s",
                    touched_id, exc,
                )
