"""Fitness pipeline error hierarchy.

The fetch service (W6) catches provider-level exceptions
(:class:`journal.providers.strava.StravaAuthError`,
:class:`journal.providers.garmin.GarminAuthError`, plus transient
``stravalib`` / ``garminconnect`` HTTP errors) and re-raises them as one
of the subclasses defined here. Downstream code — workers (W8), the
REST surface (W9), the MCP tools (W10) — depends only on this module,
not on the provider modules. Swapping a provider library never reaches
into worker code.

:class:`FitnessNormalizeDrift` is defined here for W7's convenience
(the normalize service raises it on payload-shape divergence) but is
never raised by W6 itself.
"""

from __future__ import annotations


class FitnessError(Exception):
    """Base class for all fitness-pipeline errors."""


class FitnessAuthError(FitnessError):
    """Auth state is unrecoverable until the user re-authenticates.

    Mirrors HTTP 401/403 from either source. The fetch service uses
    this to drive ``transition_auth`` to ``broken`` and fire
    ``notif_fitness_auth_broken`` (fire-once on transition).
    """


class FitnessTransientError(FitnessError):
    """Transient infrastructure failure — network, 5xx, 429.

    The fetch service classifies these as ``transient_failure`` and
    fires ``notif_fitness_sync_failure`` only after N consecutive
    failures (N = ``Config.fitness_transient_failure_threshold``).
    """


class FitnessNormalizeDrift(FitnessError):  # noqa: N818  named per W6 plan; W7 raises it
    """Raw payload no longer matches the normalize service's expected shape.

    Raised by W7. W6 defines the class here so the worker import graph
    stays single-rooted at ``services/fitness/errors``.
    """
