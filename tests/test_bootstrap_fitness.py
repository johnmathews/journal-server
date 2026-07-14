"""Bootstrap gating of the Strava fitness callables (STRAVA_ENABLED mothball).

W1 of the strava-mothball plan: ``_build_fitness_callables`` must leave
every Strava callable unwired unless ``STRAVA_ENABLED`` is true **and**
the OAuth credentials are present. Garmin wiring is unconditional and
must be unaffected by the flag.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from journal.config import Config
from journal.mcp_server.bootstrap import _build_fitness_callables

_STRAVA_KEYS = (
    "fetch_strava_callable",
    "normalize_strava_callable",
    "backfill_strava_callable",
)
_GARMIN_KEYS = (
    "fetch_garmin_callable",
    "normalize_garmin_callable",
    "backfill_garmin_callable",
)


def _build(config: Config) -> dict:
    return _build_fitness_callables(
        fitness_repo=MagicMock(),
        config=config,
        notification_service=MagicMock(),
    )


def test_strava_unwired_when_flag_off_even_with_creds() -> None:
    """Mothball: creds alone are no longer enough — the flag gates wiring."""
    out = _build(
        Config(
            strava_enabled=False,
            strava_client_id="12345",
            strava_client_secret="shh",
        ),
    )
    for key in _STRAVA_KEYS:
        assert out[key] is None, key
    for key in _GARMIN_KEYS:
        assert out[key] is not None, key


def test_strava_wired_when_flag_on_and_creds_set() -> None:
    out = _build(
        Config(
            strava_enabled=True,
            strava_client_id="12345",
            strava_client_secret="shh",
        ),
    )
    for key in (*_STRAVA_KEYS, *_GARMIN_KEYS):
        assert out[key] is not None, key


def test_strava_unwired_when_flag_on_but_no_creds() -> None:
    out = _build(
        Config(
            strava_enabled=True,
            strava_client_id="",
            strava_client_secret="",
        ),
    )
    for key in _STRAVA_KEYS:
        assert out[key] is None, key
    for key in _GARMIN_KEYS:
        assert out[key] is not None, key
