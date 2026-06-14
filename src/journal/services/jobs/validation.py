"""Parameter shape validation for queued jobs.

Each ``submit_*`` entry point on ``JobRunner`` calls ``validate_params``
with the type-keyed dict for its job type. Unknown keys or wrong-type
values raise ``ValueError`` before a row is inserted, so a malformed
request never produces a stuck-running phantom.
"""

from __future__ import annotations

from typing import Any

ENTITY_EXTRACTION_KEYS: dict[str, type | tuple[type, ...]] = {
    "entry_id": int,
    "start_date": str,
    "end_date": str,
    "stale_only": bool,
    "user_id": int,
    "parent_job_id": str,
}

MOOD_BACKFILL_KEYS: dict[str, type | tuple[type, ...]] = {
    "mode": str,
    "start_date": str,
    "end_date": str,
    "user_id": int,
}

MOOD_BACKFILL_MODES = frozenset({"stale-only", "force"})

INGEST_IMAGES_KEYS: dict[str, type | tuple[type, ...]] = {
    "entry_date": str,
    "user_id": int,
}

MOOD_SCORE_ENTRY_KEYS: dict[str, type | tuple[type, ...]] = {
    "entry_id": int,
    "user_id": int,
    "parent_job_id": str,
}

REPROCESS_EMBEDDINGS_KEYS: dict[str, type | tuple[type, ...]] = {
    "entry_id": int,
    "user_id": int,
    "parent_job_id": str,
}

SAVE_ENTRY_PIPELINE_KEYS: dict[str, type | tuple[type, ...]] = {
    "entry_id": int,
    "user_id": int,
    "notify_strategy": str,
}

INGEST_AUDIO_KEYS: dict[str, type | tuple[type, ...]] = {
    "entry_date": str,
    "source_type": str,
    "user_id": int,
}

ENTITY_REEMBED_KEYS: dict[str, type | tuple[type, ...]] = {
    "entity_id": int,
    "user_id": int,
}

# Fitness sync jobs carry the source in the job_type name (mirroring
# how mood_score_entry doesn't carry a `dimension` param), so the
# only legal param is the user_id of the row owner.
FITNESS_SYNC_KEYS: dict[str, type | tuple[type, ...]] = {
    "user_id": int,
    # Optional: scheduled (daily) syncs set this True so the worker stays
    # quiet on a success that fetched zero new rows. Manual syncs omit it.
    "quiet_success": bool,
}

# Fitness backfill (W5) wraps services/fitness/backfill.py — same
# source-in-the-type-name pattern; the params describe the date window.
# ``end`` is optional and defaults to today (UTC) inside the orchestrator.
FITNESS_BACKFILL_KEYS: dict[str, type | tuple[type, ...]] = {
    "user_id": int,
    "start": str,
    "end": str,
}

# Storylines (W5/W7). storyline_generation regenerates one storyline's
# panels end-to-end. storyline_extension_check fires after each
# ingestion to classify whether a new entry extends an active
# storyline and (optionally) queue a regeneration.
STORYLINE_GENERATION_KEYS: dict[str, type | tuple[type, ...]] = {
    "storyline_id": int,
    "user_id": int,
    "parent_job_id": str,
    # Optional: regenerate a single chapter rather than the storyline's
    # open chapter. When present, the worker calls regenerate_chapter and
    # the chapter's own date window is authoritative.
    "chapter_id": int,
    # Optional date-window overrides + mode ("replace" | "append").
    # Stored as ISO strings; the service layer parses them.
    "start_date": str,
    "end_date": str,
    "mode": str,
}

STORYLINE_GENERATION_MODES = frozenset({"replace", "append"})

STORYLINE_EXTENSION_CHECK_KEYS: dict[str, type | tuple[type, ...]] = {
    "entry_id": int,
    "user_id": int,
    "parent_job_id": str,
}


def validate_params(
    params: dict[str, Any],
    allowed: dict[str, type | tuple[type, ...]],
    *,
    job_type: str,
) -> None:
    """Reject params with unknown keys or wrong value types.

    Booleans are a subclass of int in Python, so ``stale_only=True``
    would incorrectly satisfy ``int`` typing. We handle that by
    checking bool BEFORE the generic isinstance when int is allowed
    but bool is not, and vice versa.
    """
    unknown = set(params) - set(allowed)
    if unknown:
        raise ValueError(
            f"Unknown params for {job_type}: {sorted(unknown)}"
        )
    for key, value in params.items():
        expected = allowed[key]
        # Python quirk: bool is a subclass of int. Disallow the
        # cross-type acceptance that isinstance would otherwise
        # silently grant when a caller passes True for an int field
        # or 1 for a bool field.
        if expected is int and isinstance(value, bool):
            raise ValueError(
                f"Param {key!r} for {job_type} must be int, "
                f"got bool ({value!r})"
            )
        if expected is bool and not isinstance(value, bool):
            raise ValueError(
                f"Param {key!r} for {job_type} must be bool, "
                f"got {type(value).__name__} ({value!r})"
            )
        if not isinstance(value, expected):  # type: ignore[arg-type]
            raise ValueError(
                f"Param {key!r} for {job_type} must be "
                f"{expected}, got {type(value).__name__} ({value!r})"
            )
