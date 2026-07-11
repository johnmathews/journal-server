"""The usage-collecting worker wrapper shared by the runner + save pipeline.

Every job worker is dispatched through :func:`run_job` rather than being
submitted to the executor directly. ``run_job`` opens a
:func:`journal.services.usage.usage_scope` on the worker thread, runs the
real worker inside it, and — in a ``finally`` so it fires for FAILED jobs
too — flushes the accumulated token totals onto the job row via
``ctx.jobs.record_usage``.

It lives in its own module (not ``runner.py``) purely to break the import
cycle: ``runner`` imports ``save_pipeline`` and both need this wrapper, so
a shared leaf module is the clean home for it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from journal.db.pricing import estimate_cost
from journal.services.usage import usage_scope

if TYPE_CHECKING:
    from collections.abc import Callable

    from journal.services.jobs.workers import WorkerContext


def run_job(
    worker_fn: Callable[..., Any],
    ctx: WorkerContext,
    job_id: str,
    params: dict[str, Any],
    *extra: Any,
) -> None:
    """Run ``worker_fn`` under a usage scope, then flush tokens to the row.

    The ``finally`` runs on the worker thread AFTER the worker's own
    ``mark_succeeded`` / ``mark_failed``, so ``record_usage`` is a
    follow-up UPDATE that records tokens even when the job failed. Cost is
    computed best-effort from the existing pricing table via
    ``estimate_cost`` (W3) and may be ``None`` when nothing was priceable
    (e.g. transcription-only usage). Jobs that made no LLM call leave the
    columns untouched (NULL).

    The pricing lookup reads through ``ctx.jobs.connection`` — the same
    repository the flush already writes through, and the worker thread's
    own migrated connection — so ``record_usage`` stays a pure persistence
    writer with no pricing logic leaking into the DB layer.
    """
    with usage_scope() as collector:
        try:
            worker_fn(ctx, job_id, params, *extra)
        finally:
            input_tokens, output_tokens = collector.totals
            if input_tokens or output_tokens:
                cost = estimate_cost(ctx.jobs.connection, collector.per_model)
                ctx.jobs.record_usage(job_id, input_tokens, output_tokens, cost)
