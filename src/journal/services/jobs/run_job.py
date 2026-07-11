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
    passed as ``None`` (W3 wires pricing). Jobs that made no LLM call leave
    the columns untouched (NULL).
    """
    with usage_scope() as collector:
        try:
            worker_fn(ctx, job_id, params, *extra)
        finally:
            input_tokens, output_tokens = collector.totals
            if input_tokens or output_tokens:
                ctx.jobs.record_usage(job_id, input_tokens, output_tokens, None)
