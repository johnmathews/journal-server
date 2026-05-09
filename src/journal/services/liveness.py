"""Per-component liveness checks used by the `/health` endpoint.

Each check returns a `ComponentCheck` carrying:

- `name`: identifier the caller expects ("sqlite", "chromadb", etc.).
- `status`: `"ok"`, `"degraded"`, or `"error"`.
- `detail`: short human-readable string — what was checked, not
  what went wrong (error messages go in `error`).
- `error`: exception message if the check raised, else `None`.

The overall server status returned from `/health` is the worst of
the component statuses: any `"error"` → `"error"`, else any
`"degraded"` → `"degraded"`, else `"ok"`.

**Credential checks do NOT burn tokens.** Anthropic and OpenAI
checks only verify that the API key is set and has a plausible
shape. Making a real call on every `/health` hit would cost
money and rate-limit budget; anyone who wants deeper probes can
run the CLI equivalent of an ingestion or search and look at
the latencies in the query stats block.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

log = logging.getLogger(__name__)


StatusLevel = str  # "ok" | "degraded" | "error"


@dataclass
class ComponentCheck:
    name: str
    status: StatusLevel
    detail: str
    error: str | None = None


def check_sqlite(conn: sqlite3.Connection) -> ComponentCheck:
    """Ping the SQLite connection with a trivial query."""
    try:
        row = conn.execute("SELECT 1 AS ok").fetchone()
        if row is None or row[0] != 1:
            return ComponentCheck(
                name="sqlite",
                status="error",
                detail="SELECT 1 returned unexpected result",
            )
        return ComponentCheck(
            name="sqlite",
            status="ok",
            detail="SELECT 1 succeeded",
        )
    except sqlite3.Error as e:
        log.warning("SQLite liveness check failed: %s", e)
        return ComponentCheck(
            name="sqlite",
            status="error",
            detail="SELECT 1 failed",
            error=str(e),
        )


def check_chromadb(vector_store: Any) -> ComponentCheck:
    """Ping the vector store by calling its `count()` method.

    Accepts anything duck-typed to the `VectorStore` Protocol so
    tests can pass an `InMemoryVectorStore` or a mock without
    pulling in the concrete `ChromaVectorStore`.
    """
    try:
        count = int(vector_store.count())
        return ComponentCheck(
            name="chromadb",
            status="ok",
            detail=f"collection count = {count}",
        )
    except Exception as e:
        log.warning("ChromaDB liveness check failed: %s", e)
        return ComponentCheck(
            name="chromadb",
            status="error",
            detail="collection.count() failed",
            error=str(e),
        )


def check_api_key(
    name: str,
    api_key: str | None,
    min_length: int = 20,
) -> ComponentCheck:
    """Static credential check — does NOT call out.

    A missing or obviously malformed key returns `degraded` rather
    than `error` because the *service* is up; the credential
    problem is a config issue the operator can see and fix. An
    `error` status is reserved for components that are actually
    broken (SQLite unreachable, ChromaDB not responding).
    """
    if not api_key:
        return ComponentCheck(
            name=name,
            status="degraded",
            detail=f"{name} API key is not configured",
        )
    if len(api_key) < min_length:
        return ComponentCheck(
            name=name,
            status="degraded",
            detail=(
                f"{name} API key is shorter than {min_length} characters — "
                "likely malformed or a placeholder"
            ),
        )
    return ComponentCheck(
        name=name,
        status="ok",
        detail=f"{name} API key is configured ({len(api_key)} chars)",
    )


def check_fitness_freshness(
    *,
    summary: list[dict[str, Any]],
    threshold_hours: int,
    now: datetime | None = None,
) -> ComponentCheck:
    """Roll up a per-source fitness health summary into one check.

    `summary` is the list returned by
    `FitnessRepository.get_health_summary(user_id=...)`: one dict per
    configured source with `auth_status`, `auth_broken_since`,
    `last_success_at`. The check returns `degraded` if any source has
    `auth_status='broken'` with an `auth_broken_since` more than
    `threshold_hours` ago. A recently-broken source (under threshold)
    stays `ok` so the rollup doesn't flap on every transient token
    refresh.

    Status is never `error` — a broken integration is operator
    information, not a server outage. The unauthenticated `/health`
    endpoint does not call this; only the authenticated `/api/health`
    does, with the user's own fitness state.
    """
    if not summary:
        return ComponentCheck(
            name="fitness",
            status="ok",
            detail="no fitness sources configured",
        )

    current = now if now is not None else datetime.now(UTC)
    threshold = timedelta(hours=threshold_hours)
    broken_over_threshold: list[str] = []

    for row in summary:
        if row.get("auth_status") != "broken":
            continue
        broken_since_iso = row.get("auth_broken_since")
        if not broken_since_iso:
            continue
        try:
            broken_since = datetime.fromisoformat(
                broken_since_iso.replace("Z", "+00:00"),
            )
        except (TypeError, ValueError):
            continue
        if broken_since.tzinfo is None:
            broken_since = broken_since.replace(tzinfo=UTC)
        if current - broken_since > threshold:
            broken_over_threshold.append(row["source"])

    if broken_over_threshold:
        names = ", ".join(sorted(broken_over_threshold))
        return ComponentCheck(
            name="fitness",
            status="degraded",
            detail=(
                f"auth broken for >{threshold_hours}h: {names}"
            ),
        )
    return ComponentCheck(
        name="fitness",
        status="ok",
        detail=f"{len(summary)} source(s), none broken over threshold",
    )


def overall_status(checks: list[ComponentCheck]) -> StatusLevel:
    """Worst of the component statuses.

    - Any `error` → `error`
    - Else any `degraded` → `degraded`
    - Else `ok` (including the empty list, which should never
      happen in practice but is a defensible default).
    """
    if any(c.status == "error" for c in checks):
        return "error"
    if any(c.status == "degraded" for c in checks):
        return "degraded"
    return "ok"
