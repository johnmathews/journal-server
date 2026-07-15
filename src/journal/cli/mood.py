"""Mood-backfill CLI command.

One command lifted out of ``cli/__init__.py``: ``journal
backfill-mood``, which scores entries against the currently-loaded
mood-dimension set and reports a rough cost estimate so the
operator can decide whether to proceed on a large corpus.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from journal.db.factory import ConnectionFactory
from journal.db.migrations import run_migrations
from journal.db.repository import SQLiteEntryRepository

if TYPE_CHECKING:
    import argparse

    from journal.config import Config


def cmd_backfill_mood(args: argparse.Namespace, config: Config) -> None:
    """Run the mood-score backfill against the currently-loaded
    dimension set.

    Modes:

    - ``--stale-only`` (default): score entries missing at least one
      currently-configured dimension. Idempotent.
    - ``--force``: rescore every entry in the selected date range,
      regardless of existing state. Use after editing a dimension's
      labels or notes.

    Flags:

    - ``--prune-retired``: delete ``mood_scores`` rows whose
      dimension is not in the current tuple. Off by default.
      Combined with ``--dry-run`` it reports what would be deleted.
    - ``--dry-run``: count what would change without making any
      network or DB writes.
    - ``--start-date`` / ``--end-date``: ISO-8601 window (inclusive).

    The CLI prints an estimated cost using public Sonnet-4.5 pricing
    so the user can decide whether to proceed on a large corpus.
    """
    from journal.providers.mood_scorer import AnthropicMoodScorer
    from journal.services.backfill import backfill_mood_scores
    from journal.services.mood_dimensions import load_mood_dimensions
    from journal.services.mood_scoring import MoodScoringService

    try:
        dimensions = load_mood_dimensions(config.mood_dimensions_path)
    except Exception as e:
        print(
            f"Error: failed to load mood dimensions: {e}",
            file=sys.stderr,
        )
        sys.exit(1)

    db_factory = ConnectionFactory(config.db_path)
    run_migrations(db_factory.get())
    repo = SQLiteEntryRepository(db_factory)

    scorer = AnthropicMoodScorer(
        api_key=config.anthropic_api_key,
        model=config.mood_scorer_model,
        max_tokens=config.mood_scorer_max_tokens,
    )
    service = MoodScoringService(scorer, repo, dimensions)

    mode = "force" if args.force else "stale-only"
    print(f"Mood backfill — mode={mode}, dimensions={len(dimensions)}")
    for d in dimensions:
        print(f"  - {d.name} ({d.scale_type})")
    if args.dry_run:
        print("Dry run: no scoring or writes will occur.")

    result = backfill_mood_scores(
        repository=repo,
        mood_scoring=service,
        mode=mode,
        start_date=args.start_date,
        end_date=args.end_date,
        prune_retired=args.prune_retired,
        dry_run=args.dry_run,
    )

    prefix = "[dry-run] " if result.dry_run else ""
    print(f"{prefix}Scored:          {result.scored}")
    print(f"{prefix}Skipped:         {result.skipped}")
    if args.prune_retired:
        print(f"{prefix}Pruned retired:  {result.pruned}")
    if result.errors:
        print(f"\nErrors ({len(result.errors)}):")
        for err in result.errors:
            print(f"  {err}")

    # Rough cost estimate using public Sonnet 4.5 pricing: $3/M
    # input tokens, $15/M output. Per-entry call is ~1750 input
    # tokens (prompt ~1000 for the 10-facet schema + ~750 for a
    # 500-word entry) + ~210 output tokens. Adjust if you change
    # the model or the facet set.
    if result.scored and not result.dry_run:
        input_cost = result.scored * 1750 * 3.0 / 1_000_000
        output_cost = result.scored * 210 * 15.0 / 1_000_000
        total = input_cost + output_cost
        print(f"\nEstimated cost for this run: ${total:.4f}")
