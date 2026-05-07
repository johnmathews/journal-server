"""Async batch-job tools.

These three tools drive the same JobRunner that the REST endpoints
use. The batch tools block on the MCP call until the job reaches a
terminal state — because they poll the jobs table rather than wait
on a future, they work across the shared process-wide executor the
same way the webapp's REST polling does. Failed jobs still return a
structured dict (not an exception) so Claude can read the error
message and respond to the user.
"""

import logging
import time
from typing import Any

from mcp.server.fastmcp import Context

from journal.db.jobs_repository import SQLiteJobRepository
from journal.mcp_server.app import mcp
from journal.mcp_server.tools._ctx import (
    _get_job_repository,
    _get_job_runner,
    _user_id,
)

log = logging.getLogger(__name__)


def _job_to_tool_dict(job: Any) -> dict[str, Any]:
    """Serialise a Job dataclass for MCP tool responses."""
    return {
        "id": job.id,
        "type": job.type,
        "status": job.status,
        "params": job.params,
        "progress_current": job.progress_current,
        "progress_total": job.progress_total,
        "result": job.result,
        "error_message": job.error_message,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
    }


def _poll_job_until_terminal(
    job_repository: SQLiteJobRepository,
    job_id: str,
    *,
    poll_interval: float = 0.5,
    timeout: float = 3600.0,
) -> dict[str, Any]:
    """Block until `job_id` reaches a terminal state or timeout.

    Polls `job_repository.get(job_id)` on a fixed cadence. A stuck or
    very long-running job will eventually time out — the default
    matches the webapp's tolerance for long batches.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = job_repository.get(job_id)
        if job is None:
            return {
                "status": "failed",
                "job_id": job_id,
                "result": None,
                "error_message": (
                    f"Job {job_id} disappeared from the repository"
                ),
            }
        if job.status in ("succeeded", "failed"):
            return {
                "status": job.status,
                "job_id": job.id,
                "result": job.result,
                "error_message": job.error_message,
            }
        time.sleep(poll_interval)
    return {
        "status": "timeout",
        "job_id": job_id,
        "result": None,
        "error_message": (
            f"Job did not reach a terminal state within {timeout}s"
        ),
    }


@mcp.tool()
def journal_extract_entities_batch(
    entry_id: int | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    stale_only: bool = False,
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Run entity extraction as an async batch job and wait for it to finish.

    This is the batch-job wrapper around the synchronous
    `journal_extract_entities` tool: it enqueues work onto the shared
    JobRunner, then polls the jobs table until a terminal state is
    reached. Use this when Claude wants the same progress/error
    semantics the webapp uses.

    NOTE: the tool BLOCKS until the job reaches a terminal state.
    Large batches may take minutes — expect long-running tool calls.

    Args:
        entry_id: If set, extract from this single entry only.
        start_date: Filter entries from this date (ISO 8601). Optional.
        end_date: Filter entries until this date (ISO 8601). Optional.
        stale_only: When True, only process entries flagged as stale.

    Returns:
        ``{"status", "job_id", "result", "error_message"}``. On
        success, ``result`` is the summary dict produced by the
        extraction runner. On failure, the tool returns a structured
        dict — it does NOT raise — so the caller can read the error
        message and respond to the user.
    """
    log.info(
        "Tool call: journal_extract_entities_batch("
        "entry_id=%s, start_date=%s, end_date=%s, stale_only=%s)",
        entry_id, start_date, end_date, stale_only,
    )
    runner = _get_job_runner(ctx)
    job_repository = _get_job_repository(ctx)
    user_id = _user_id(ctx)

    params: dict[str, Any] = {}
    if entry_id is not None:
        params["entry_id"] = int(entry_id)
    if start_date is not None:
        params["start_date"] = start_date
    if end_date is not None:
        params["end_date"] = end_date
    if stale_only:
        params["stale_only"] = True

    try:
        job = runner.submit_entity_extraction(params, user_id=user_id)
    except ValueError as exc:
        return {
            "status": "failed",
            "job_id": None,
            "result": None,
            "error_message": str(exc),
        }

    return _poll_job_until_terminal(job_repository, job.id)


@mcp.tool()
def journal_backfill_mood_scores_batch(
    mode: str,
    start_date: str | None = None,
    end_date: str | None = None,
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Run a mood-score backfill as an async batch job and wait for it.

    Same execution model as `journal_extract_entities_batch` — the
    call enqueues a job on the shared JobRunner and polls the jobs
    table until a terminal state is reached.

    NOTE: the tool BLOCKS until the job reaches a terminal state.
    Large backfills may take a long time — expect long-running tool
    calls for `mode="force"` over wide date ranges.

    Args:
        mode: Either ``"stale-only"`` (idempotent — score only
            entries missing a current dimension) or ``"force"``
            (rescore every entry in the date range).
        start_date: Restrict the backfill to entries from this date
            forward (ISO 8601). Optional.
        end_date: Restrict the backfill to entries up to this date
            (ISO 8601). Optional.

    Returns:
        ``{"status", "job_id", "result", "error_message"}``. On
        success, ``result`` is the summary dict produced by the
        backfill runner. On failure, the tool returns a structured
        dict — it does NOT raise.
    """
    log.info(
        "Tool call: journal_backfill_mood_scores_batch("
        "mode=%s, start_date=%s, end_date=%s)",
        mode, start_date, end_date,
    )
    runner = _get_job_runner(ctx)
    job_repository = _get_job_repository(ctx)
    user_id = _user_id(ctx)

    params: dict[str, Any] = {"mode": mode}
    if start_date is not None:
        params["start_date"] = start_date
    if end_date is not None:
        params["end_date"] = end_date

    try:
        job = runner.submit_mood_backfill(params, user_id=user_id)
    except ValueError as exc:
        return {
            "status": "failed",
            "job_id": None,
            "result": None,
            "error_message": str(exc),
        }

    return _poll_job_until_terminal(job_repository, job.id)


@mcp.tool()
def journal_get_job_status(
    job_id: str,
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Return the current state of a batch job.

    Non-blocking — returns whatever is in the jobs table right now.
    Pair with `journal_extract_entities_batch` /
    `journal_backfill_mood_scores_batch` if you need a
    fire-and-forget alternative to the blocking batch tools.

    Args:
        job_id: The UUID returned by a batch-job submission.

    Returns:
        A dict with the full serialised job shape (``id``, ``type``,
        ``status``, ``params``, progress counters, ``result``,
        ``error_message``, timestamps). If the job is not found the
        returned dict has ``{"error": "Job not found", "job_id": ...}``.
    """
    log.info("Tool call: journal_get_job_status(job_id=%s)", job_id)
    job_repository = _get_job_repository(ctx)
    user_id = _user_id(ctx)
    job = job_repository.get(job_id, user_id=user_id)
    if job is None:
        return {"error": "Job not found", "job_id": job_id}
    return _job_to_tool_dict(job)
