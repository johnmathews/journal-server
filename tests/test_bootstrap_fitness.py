"""Bootstrap gating of the Strava fitness callables (STRAVA_ENABLED mothball).

W1 of the strava-mothball plan: ``_build_fitness_callables`` must leave
every Strava callable unwired unless ``STRAVA_ENABLED`` is true **and**
the OAuth credentials are present. Garmin wiring is unconditional and
must be unaffected by the flag.

W6 of the same plan: the Garmin provider factory decrypts saved
credentials (``garmin_username`` + ``enc_password`` on the auth row)
when ``FITNESS_CREDENTIAL_KEY`` is set, enabling unattended re-login;
decrypt failures degrade to the empty-credential provider.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from cryptography.fernet import Fernet

from journal.config import Config
from journal.mcp_server.bootstrap import _build_fitness_callables
from journal.models import FitnessAuthState
from journal.services.fitness.credentials import encrypt_credential
from journal.services.fitness.garmin_pending import GarminUpstreamCooldown

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


# ── W6: Garmin provider factory credential injection ─────────────────


def _garmin_auth(extra_state: dict) -> FitnessAuthState:
    return FitnessAuthState(
        user_id=1, source="garmin",
        access_token=None, refresh_token=None, token_expires_at=None,
        extra_state=extra_state,
    )


def _garmin_provider_from(out: dict, auth: FitnessAuthState):  # noqa: ANN202
    """Reach the provider factory through the fetch service the callable
    is bound to — the only public seam ``_build_fitness_callables`` exposes."""
    fetch_service = out["fetch_garmin_callable"].__self__
    return fetch_service._provider_factory(auth)  # noqa: SLF001


def test_garmin_factory_injects_decrypted_saved_credentials() -> None:
    key = Fernet.generate_key().decode()
    enc = encrypt_credential("s3cret-pw", key=key)
    out = _build(Config(fitness_credential_key=key))

    provider = _garmin_provider_from(out, _garmin_auth({
        "tokens_blob": "dead-blob",
        "garmin_username": "user@example.com",
        "enc_password": enc,
    }))

    assert provider.can_relogin_with_password()
    assert provider._username == "user@example.com"  # noqa: SLF001
    assert provider._password == "s3cret-pw"  # noqa: SLF001


def test_garmin_factory_falls_back_to_empty_on_decrypt_error() -> None:
    key = Fernet.generate_key().decode()
    other_key = Fernet.generate_key().decode()
    enc_with_other_key = encrypt_credential("s3cret-pw", key=other_key)
    out = _build(Config(fitness_credential_key=key))

    provider = _garmin_provider_from(out, _garmin_auth({
        "tokens_blob": "dead-blob",
        "garmin_username": "user@example.com",
        "enc_password": enc_with_other_key,
    }))

    assert not provider.can_relogin_with_password()
    assert provider._username == ""  # noqa: SLF001
    assert provider._password == ""  # noqa: SLF001


def test_garmin_factory_ignores_saved_credentials_when_key_unset() -> None:
    key = Fernet.generate_key().decode()
    enc = encrypt_credential("s3cret-pw", key=key)
    out = _build(Config(fitness_credential_key=""))

    provider = _garmin_provider_from(out, _garmin_auth({
        "tokens_blob": "dead-blob",
        "garmin_username": "user@example.com",
        "enc_password": enc,
    }))

    assert not provider.can_relogin_with_password()


def test_garmin_factory_empty_credentials_without_saved_material() -> None:
    out = _build(Config(fitness_credential_key=Fernet.generate_key().decode()))

    provider = _garmin_provider_from(out, _garmin_auth({
        "tokens_blob": "dead-blob",
    }))

    assert not provider.can_relogin_with_password()


def test_garmin_fetch_service_shares_injected_upstream_cooldown() -> None:
    """The gate handed to ``_build_fitness_callables`` must be the very
    instance the Garmin fetch service consults — sharing it with the
    connect/reconnect API handlers via the services dict."""
    gate = GarminUpstreamCooldown()
    out = _build_fitness_callables(
        fitness_repo=MagicMock(),
        config=Config(),
        notification_service=MagicMock(),
        garmin_upstream_cooldown=gate,
    )
    fetch_service = out["fetch_garmin_callable"].__self__
    assert fetch_service._upstream_cooldown is gate  # noqa: SLF001
