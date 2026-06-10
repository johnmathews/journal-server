"""Type-level registry of the shared services dict.

``ServicesDict`` enumerates the keys that ``mcp_server.bootstrap._init_services``
constructs plus the keys some routes lazily attach at runtime (the fitness
pending/cooldown stores and their test seams). It exists purely for static
typing — at runtime the services container is still a plain ``dict`` and
nothing imports this module outside ``TYPE_CHECKING`` blocks.

This module sits at the top level of the package (next to ``config.py`` /
``models.py``) so that both ``journal.mcp_server.bootstrap`` and the
``journal.api`` / ``journal.auth_api`` route modules can import it without
creating an import cycle.

``total=False`` because test harnesses routinely build partial dicts with
only the services the route under test touches, and several entries are
``None``-able or absent depending on configuration (mood scoring,
storylines, SMTP, Pushover).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypedDict

if TYPE_CHECKING:
    from journal.config import Config
    from journal.db.factory import ConnectionFactory
    from journal.db.fitness_repository import FitnessRepository
    from journal.db.jobs_repository import SQLiteJobRepository
    from journal.db.storyline_repository import SQLiteStorylineRepository
    from journal.db.user_repository import SQLiteUserRepository
    from journal.entitystore.store import SQLiteEntityStore
    from journal.services.auth import AuthService
    from journal.services.email import EmailService
    from journal.services.entity_extraction import EntityExtractionService
    from journal.services.fitness.garmin_pending import (
        GarminCooldownTracker,
        GarminPendingStore,
    )
    from journal.services.fitness.strava_pending import StravaPendingStore
    from journal.services.ingestion import IngestionService
    from journal.services.jobs import JobRunner
    from journal.services.notifications import PushoverNotificationService
    from journal.services.query import QueryService
    from journal.services.runtime_settings import RuntimeSettings
    from journal.services.stats import InMemoryStatsCollector


class ServicesDict(TypedDict, total=False):
    """Known keys of the shared services container (type-level only)."""

    # Core pipeline services
    ingestion: IngestionService
    query: QueryService
    entity_store: SQLiteEntityStore
    entity_casing_exceptions: dict[str, str]
    entity_extraction: EntityExtractionService

    # Jobs
    job_repository: SQLiteJobRepository
    job_runner: JobRunner

    # Configuration / observability
    config: Config
    runtime_settings: RuntimeSettings
    stats: InMemoryStatsCollector

    # Mood scoring (None / empty when JOURNAL_ENABLE_MOOD_SCORING is off)
    mood_dimensions: tuple[Any, ...]
    mood_dimensions_meta: Any
    mood_scoring: Any

    # Auth (auth_api routes)
    auth_service: AuthService
    email_service: EmailService | None
    user_repo: SQLiteUserRepository

    # Notifications (None when Pushover is not configured)
    notification_service: PushoverNotificationService | None

    # Fitness
    fitness_repo: FitnessRepository
    garmin_pending: GarminPendingStore
    garmin_cooldown: GarminCooldownTracker
    garmin_client_factory: Any  # test seam — defaults to garminconnect.Garmin
    strava_pending: StravaPendingStore
    strava_exchange_code: Any  # test seam — defaults to providers.strava.exchange_code

    # Storylines (None when ANTHROPIC_API_KEY is unset)
    storyline_repository: SQLiteStorylineRepository | None
    storyline_generation: Any
    storyline_extension_classifier: Any

    # Raw SQL escape hatch (pricing reads/writes, fitness integrity)
    db_factory: ConnectionFactory
