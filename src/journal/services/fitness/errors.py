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

    ``recovery_attempted`` (W6 of the strava-mothball / garmin-credentials
    plan) is True when the Garmin fetch service actually ran an unattended
    re-login with saved credentials before giving up — the notification
    mentions the failed automatic recovery only in that case.
    """

    def __init__(self, message: str, *, recovery_attempted: bool = False) -> None:
        super().__init__(message)
        self.recovery_attempted = recovery_attempted


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


class MidRunAuthLost(FitnessError):  # noqa: N818  matches FitnessNormalizeDrift convention
    """Auth state vanished or flipped to ``broken`` *during* a fetch run.

    Raised by the W5 retroactive hardening: workers re-read
    ``fitness_auth_state`` between provider calls so a mid-run disconnect
    (row deleted) or a parallel auth-broken transition aborts the
    in-flight run cleanly. Distinct from :class:`FitnessAuthError`
    because the recovery is different — when the row is gone, we must
    *not* recreate it via ``transition_auth``, and when it's already
    broken there's nothing to transition.

    ``reason`` is ``"removed"`` (auth row deleted) or ``"broken"``
    (row still present, ``auth_status='broken'``). The fetch service
    uses it to populate ``fitness_sync_runs.error_message``.
    """

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason
