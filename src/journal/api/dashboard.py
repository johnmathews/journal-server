"""Dashboard aggregation routes.

All eight ``/api/dashboard/*`` endpoints are read-only aggregations over
the SQLite store. Each returns a typed envelope (``{from, to, ...}``)
shaped for direct consumption by the webapp's chart components.

- ``writing-stats`` — entry count + word count per time bucket
- ``mood-dimensions`` — server-side mood facet definitions
- ``mood-trends`` — average mood scores per bucket, by dimension
- ``mood-drilldown`` — per-entry scores for a single dimension/window
- ``entity-distribution`` — mention counts grouped by entity name
- ``calendar-heatmap`` — daily entry counts for the heatmap
- ``entity-trends`` — entity mentions over time (top-N entities)
- ``mood-entity-correlation`` — average mood for entries mentioning each entity
- ``word-count-distribution`` — histogram of entry word counts + summary stats
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import TYPE_CHECKING

from starlette.responses import JSONResponse

from journal.auth import get_authenticated_user

if TYPE_CHECKING:
    from collections.abc import Callable

    from mcp.server.fastmcp import FastMCP
    from starlette.requests import Request

    from journal.services.query import QueryService

log = logging.getLogger(__name__)


def register_dashboard_routes(
    mcp: FastMCP,
    services_getter: Callable[[], dict | None],
) -> None:
    """Register the ``/api/dashboard/*`` routes."""

    @mcp.custom_route(
        "/api/dashboard/writing-stats",
        methods=["GET"],
        name="api_dashboard_writing_stats",
    )
    async def dashboard_writing_stats(request: Request) -> JSONResponse:
        """Aggregate writing frequency + word count per time bucket.

        Query params:

        - `from` — ISO-8601 start date (optional)
        - `to` — ISO-8601 end date (optional)
        - `bin` — `week` (default), `month`, `quarter`, or `year`

        Returns a `{from, to, bin, bins: [...]}` envelope where
        each `bin` entry carries `bin_start`, `entry_count`, and
        `total_words`. Empty buckets are NOT emitted — a month with
        zero entries will simply not appear in the series, and the
        frontend is expected to fill gaps client-side if it wants
        a dense line chart.
        """
        services = services_getter()
        if services is None:
            return JSONResponse({"error": "Server not initialized"}, status_code=503)

        query_svc: QueryService = services["query"]
        user = get_authenticated_user(request)
        user_id = user.user_id

        bin_param = request.query_params.get("bin", "week")
        start_date = request.query_params.get("from")
        end_date = request.query_params.get("to")

        try:
            bins = query_svc._repo.get_writing_frequency(
                start_date=start_date,
                end_date=end_date,
                granularity=bin_param,
                user_id=user_id,
            )
        except ValueError as e:
            log.info(
                "GET /api/dashboard/writing-stats — invalid bin %r: %s",
                bin_param,
                e,
            )
            return JSONResponse(
                {
                    "error": "invalid_bin",
                    "message": str(e),
                },
                status_code=400,
            )

        log.info(
            "GET /api/dashboard/writing-stats — bin=%s from=%s to=%s returned %d non-empty buckets",
            bin_param,
            start_date,
            end_date,
            len(bins),
        )
        return JSONResponse(
            {
                "from": start_date,
                "to": end_date,
                "bin": bin_param,
                "bins": [asdict(b) for b in bins],
            }
        )

    @mcp.custom_route(
        "/api/dashboard/mood-dimensions",
        methods=["GET"],
        name="api_dashboard_mood_dimensions",
    )
    async def dashboard_mood_dimensions(
        request: Request,
    ) -> JSONResponse:
        """Return the currently-loaded mood dimensions.

        Serves as the source of truth for the webapp's mood chart:
        the dimension definitions live in a server-side TOML file,
        and the frontend fetches them via this endpoint on page
        load so adding/removing a facet in the file (plus a
        server restart) flows through to the UI without a webapp
        rebuild.

        When mood scoring is disabled (`JOURNAL_ENABLE_MOOD_SCORING=false`)
        or no dimensions are loaded, returns an empty list with
        200 — callers should treat that as "no mood data to
        display" rather than an error.
        """
        services = services_getter()
        if services is None:
            return JSONResponse({"error": "Server not initialized"}, status_code=503)

        dimensions = services.get("mood_dimensions") or ()
        payload = [
            {
                "name": d.name,
                "positive_pole": d.positive_pole,
                "negative_pole": d.negative_pole,
                "scale_type": d.scale_type,
                "score_min": d.score_min,
                "score_max": d.score_max,
                "notes": d.notes,
            }
            for d in dimensions
        ]
        meta = services.get("mood_dimensions_meta")
        meta_payload = {
            "version": meta.version if meta is not None else "",
            "description": meta.description if meta is not None else "",
        }
        log.info(
            "GET /api/dashboard/mood-dimensions — %d dimensions, version=%r",
            len(payload),
            meta_payload["version"],
        )
        return JSONResponse({"dimensions": payload, "meta": meta_payload})

    @mcp.custom_route(
        "/api/dashboard/mood-trends",
        methods=["GET"],
        name="api_dashboard_mood_trends",
    )
    async def dashboard_mood_trends(request: Request) -> JSONResponse:
        """Aggregate mood scores per bucket, grouped by dimension.

        Query params:

        - `from` — ISO-8601 start date (optional)
        - `to` — ISO-8601 end date (optional)
        - `bin` — `week` (default), `month`, `quarter`, `year`
          (matches the writing-stats endpoint)
        - `dimension` — optional filter. When present, only the
          named dimension is returned. When absent, all
          dimensions are returned.

        Returns a `{from, to, bin, bins}` envelope. Each `bin`
        entry is `{period, dimension, avg_score, entry_count}`.
        Empty buckets are omitted — a bucket with zero scored
        entries does not appear in the series.
        """
        services = services_getter()
        if services is None:
            return JSONResponse({"error": "Server not initialized"}, status_code=503)

        query_svc: QueryService = services["query"]
        user = get_authenticated_user(request)
        user_id = user.user_id

        bin_param = request.query_params.get("bin", "week")
        start_date = request.query_params.get("from")
        end_date = request.query_params.get("to")
        dimension_filter = request.query_params.get("dimension")

        try:
            trends = query_svc.get_mood_trends(
                start_date=start_date,
                end_date=end_date,
                granularity=bin_param,
                user_id=user_id,
            )
        except ValueError as e:
            log.info(
                "GET /api/dashboard/mood-trends — invalid bin %r: %s",
                bin_param,
                e,
            )
            return JSONResponse(
                {
                    "error": "invalid_bin",
                    "message": str(e),
                },
                status_code=400,
            )

        if dimension_filter:
            trends = [t for t in trends if t.dimension == dimension_filter]

        log.info(
            "GET /api/dashboard/mood-trends — bin=%s from=%s to=%s dim=%s returned %d buckets",
            bin_param,
            start_date,
            end_date,
            dimension_filter,
            len(trends),
        )
        return JSONResponse(
            {
                "from": start_date,
                "to": end_date,
                "bin": bin_param,
                "bins": [asdict(t) for t in trends],
            }
        )

    @mcp.custom_route(
        "/api/dashboard/mood-drilldown",
        methods=["GET"],
        name="api_dashboard_mood_drilldown",
    )
    async def dashboard_mood_drilldown(request: Request) -> JSONResponse:
        """Return per-entry scores for one dimension within a date window.

        Query params:
        - `dimension` (required) — the mood dimension name
        - `from` (required) — ISO-8601 start date, inclusive
        - `to` (required) — ISO-8601 end date, inclusive
        """
        services = services_getter()
        if services is None:
            return JSONResponse({"error": "Server not initialized"}, status_code=503)

        query_svc: QueryService = services["query"]
        user = get_authenticated_user(request)
        user_id = user.user_id

        dimension = request.query_params.get("dimension")
        if not dimension:
            return JSONResponse(
                {"error": "missing_dimension", "message": "'dimension' is required"},
                status_code=400,
            )
        period_start = request.query_params.get("from")
        period_end = request.query_params.get("to")
        if not period_start or not period_end:
            return JSONResponse(
                {"error": "missing_dates", "message": "'from' and 'to' are required"},
                status_code=400,
            )

        entries = query_svc._repo.get_mood_drilldown(
            dimension=dimension,
            period_start=period_start,
            period_end=period_end,
            user_id=user_id,
        )
        log.info(
            "GET /api/dashboard/mood-drilldown — dim=%s from=%s to=%s returned %d entries",
            dimension,
            period_start,
            period_end,
            len(entries),
        )
        return JSONResponse(
            {
                "dimension": dimension,
                "from": period_start,
                "to": period_end,
                "entries": [
                    {
                        "entry_id": e.entry_id,
                        "entry_date": e.entry_date,
                        "score": e.score,
                        "confidence": e.confidence,
                        "rationale": e.rationale,
                    }
                    for e in entries
                ],
            }
        )

    @mcp.custom_route(
        "/api/dashboard/entity-distribution",
        methods=["GET"],
        name="api_dashboard_entity_distribution",
    )
    async def dashboard_entity_distribution(request: Request) -> JSONResponse:
        """Return entity mention counts grouped by entity name.

        Query params:
        - `type` (optional) — entity_type filter
        - `from` (optional) — ISO-8601 start date
        - `to` (optional) — ISO-8601 end date
        - `limit` (optional) — max items, default 50, max 200
        """
        services = services_getter()
        if services is None:
            return JSONResponse({"error": "Server not initialized"}, status_code=503)

        query_svc: QueryService = services["query"]
        user = get_authenticated_user(request)
        user_id = user.user_id

        entity_type = request.query_params.get("type")
        start_date = request.query_params.get("from")
        end_date = request.query_params.get("to")
        valid_types = {"person", "place", "activity", "organization", "topic", "other"}
        if entity_type is not None and entity_type not in valid_types:
            return JSONResponse(
                {
                    "error": "invalid_type",
                    "message": f"'type' must be one of {sorted(valid_types)}",
                },
                status_code=400,
            )
        try:
            limit = min(int(request.query_params.get("limit", "50")), 200)
        except ValueError:
            limit = 50

        bins = query_svc._repo.get_entity_distribution(
            entity_type=entity_type,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
            user_id=user_id,
        )
        log.info(
            "GET /api/dashboard/entity-distribution — type=%s from=%s to=%s returned %d items",
            entity_type,
            start_date,
            end_date,
            len(bins),
        )
        return JSONResponse(
            {
                "type": entity_type,
                "from": start_date,
                "to": end_date,
                "total": len(bins),
                "items": [asdict(b) for b in bins],
            }
        )

    @mcp.custom_route(
        "/api/dashboard/calendar-heatmap",
        methods=["GET"],
        name="api_dashboard_calendar_heatmap",
    )
    async def dashboard_calendar_heatmap(request: Request) -> JSONResponse:
        """Daily entry counts for a calendar heatmap visualization.

        Query params:
        - `from` (optional) — ISO-8601 start date
        - `to` (optional) — ISO-8601 end date

        Returns ``{from, to, days: [{date, entry_count, total_words}, ...]}``
        with one item per day that has at least one entry.
        """
        services = services_getter()
        if services is None:
            return JSONResponse({"error": "Server not initialized"}, status_code=503)

        query_svc: QueryService = services["query"]
        user = get_authenticated_user(request)
        user_id = user.user_id

        start_date = request.query_params.get("from")
        end_date = request.query_params.get("to")

        days = query_svc._repo.get_calendar_heatmap(
            start_date=start_date,
            end_date=end_date,
            user_id=user_id,
        )
        log.info(
            "GET /api/dashboard/calendar-heatmap — from=%s to=%s returned %d days",
            start_date,
            end_date,
            len(days),
        )
        return JSONResponse(
            {
                "from": start_date,
                "to": end_date,
                "days": [asdict(d) for d in days],
            }
        )

    @mcp.custom_route(
        "/api/dashboard/entity-trends",
        methods=["GET"],
        name="api_dashboard_entity_trends",
    )
    async def dashboard_entity_trends(request: Request) -> JSONResponse:
        """Entity mention counts over time, showing how topics wax and wane.

        Query params:
        - `bin` — `week`, `month` (default), `quarter`, or `year`
        - `from` (optional) — ISO-8601 start date
        - `to` (optional) — ISO-8601 end date
        - `type` (optional) — entity_type filter
        - `limit` (optional) — top N entities, default 8, max 50
        """
        services = services_getter()
        if services is None:
            return JSONResponse({"error": "Server not initialized"}, status_code=503)

        query_svc: QueryService = services["query"]
        user = get_authenticated_user(request)
        user_id = user.user_id

        bin_param = request.query_params.get("bin", "month")
        start_date = request.query_params.get("from")
        end_date = request.query_params.get("to")
        entity_type = request.query_params.get("type")
        try:
            limit = min(int(request.query_params.get("limit", "8")), 50)
        except ValueError:
            limit = 8

        try:
            entity_names, bins = query_svc._repo.get_entity_trends(
                start_date=start_date,
                end_date=end_date,
                granularity=bin_param,
                entity_type=entity_type,
                limit=limit,
                user_id=user_id,
            )
        except ValueError as e:
            log.info(
                "GET /api/dashboard/entity-trends — invalid bin %r: %s",
                bin_param,
                e,
            )
            return JSONResponse(
                {
                    "error": "invalid_bin",
                    "message": str(e),
                },
                status_code=400,
            )

        log.info(
            "GET /api/dashboard/entity-trends — bin=%s from=%s to=%s "
            "type=%s returned %d bins for %d entities",
            bin_param,
            start_date,
            end_date,
            entity_type,
            len(bins),
            len(entity_names),
        )
        return JSONResponse(
            {
                "from": start_date,
                "to": end_date,
                "bin": bin_param,
                "entity_type": entity_type,
                "entities": entity_names,
                "bins": [asdict(b) for b in bins],
            }
        )

    @mcp.custom_route(
        "/api/dashboard/mood-entity-correlation",
        methods=["GET"],
        name="api_dashboard_mood_entity_correlation",
    )
    async def dashboard_mood_entity_correlation(request: Request) -> JSONResponse:
        """Average mood score when a specific entity is mentioned vs overall.

        Query params:
        - `dimension` (required) — the mood dimension name
        - `from` (optional) — ISO-8601 start date
        - `to` (optional) — ISO-8601 end date
        - `type` (optional) — entity_type filter
        - `limit` (optional) — top N entities, default 10, max 50
        """
        services = services_getter()
        if services is None:
            return JSONResponse({"error": "Server not initialized"}, status_code=503)

        query_svc: QueryService = services["query"]
        user = get_authenticated_user(request)
        user_id = user.user_id

        dimension = request.query_params.get("dimension")
        if not dimension:
            return JSONResponse(
                {"error": "missing_dimension", "message": "'dimension' is required"},
                status_code=400,
            )

        start_date = request.query_params.get("from")
        end_date = request.query_params.get("to")
        entity_type = request.query_params.get("type")
        try:
            limit = min(int(request.query_params.get("limit", "10")), 50)
        except ValueError:
            limit = 10

        overall_avg, items = query_svc._repo.get_mood_entity_correlation(
            dimension=dimension,
            start_date=start_date,
            end_date=end_date,
            entity_type=entity_type,
            limit=limit,
            user_id=user_id,
        )
        log.info(
            "GET /api/dashboard/mood-entity-correlation — "
            "dim=%s from=%s to=%s type=%s returned %d items",
            dimension,
            start_date,
            end_date,
            entity_type,
            len(items),
        )
        return JSONResponse(
            {
                "dimension": dimension,
                "from": start_date,
                "to": end_date,
                "entity_type": entity_type,
                "overall_avg": overall_avg,
                "items": [asdict(i) for i in items],
            }
        )

    @mcp.custom_route(
        "/api/dashboard/word-count-distribution",
        methods=["GET"],
        name="api_dashboard_word_count_distribution",
    )
    async def dashboard_word_count_distribution(request: Request) -> JSONResponse:
        """Histogram of entry word counts with summary statistics.

        Query params:
        - `from` (optional) — ISO-8601 start date
        - `to` (optional) — ISO-8601 end date
        - `bucket_size` (optional) — bucket width, default 100, min 10
        """
        services = services_getter()
        if services is None:
            return JSONResponse({"error": "Server not initialized"}, status_code=503)

        query_svc: QueryService = services["query"]
        user = get_authenticated_user(request)
        user_id = user.user_id

        start_date = request.query_params.get("from")
        end_date = request.query_params.get("to")
        try:
            bucket_size = max(int(request.query_params.get("bucket_size", "100")), 10)
        except ValueError:
            bucket_size = 100

        buckets, stats = query_svc._repo.get_word_count_distribution(
            start_date=start_date,
            end_date=end_date,
            bucket_size=bucket_size,
            user_id=user_id,
        )
        log.info(
            "GET /api/dashboard/word-count-distribution — "
            "from=%s to=%s bucket_size=%d returned %d buckets",
            start_date,
            end_date,
            bucket_size,
            len(buckets),
        )
        return JSONResponse(
            {
                "from": start_date,
                "to": end_date,
                "bucket_size": bucket_size,
                "buckets": [asdict(b) for b in buckets],
                "stats": asdict(stats),
            }
        )
