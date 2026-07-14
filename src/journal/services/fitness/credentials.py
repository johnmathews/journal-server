"""Fernet encryption helpers for saved fitness credentials (W4).

Threat model: this protects the SQLite file at rest — a copied
`journal.db` (backup, stolen disk) must not leak the user's Garmin
password. The symmetric key lives in the ``FITNESS_CREDENTIAL_KEY``
environment variable, alongside the deployment's other secrets; an
attacker with both the DB *and* the process environment is out of
scope (they already have every API key). A rotated or lost key must
degrade to "credentials unavailable" via :class:`CredentialDecryptError`
— never crash a sync.

Generate a key with::

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

from cryptography.fernet import Fernet, InvalidToken

__all__ = [
    "CredentialDecryptError",
    "CredentialKeyInvalid",
    "decrypt_credential",
    "encrypt_credential",
    "validate_credential_key",
]

_GENERATE_CMD = (
    'python -c "from cryptography.fernet import Fernet;'
    ' print(Fernet.generate_key().decode())"'
)


class CredentialKeyInvalid(Exception):  # noqa: N818  named per W4 plan; matches BackfillBlocked convention
    """The configured credential key is not a valid Fernet key.

    This is a configuration bug (fail fast at startup), distinct from
    :class:`CredentialDecryptError` which is a runtime data condition.
    """


class CredentialDecryptError(Exception):
    """A stored credential token could not be decrypted.

    Raised on wrong/rotated keys, truncated tokens, or garbage input.
    Callers must treat this as "credentials unavailable" and degrade
    gracefully — never let it crash a sync.
    """


def validate_credential_key(key: str) -> None:
    """Raise :class:`CredentialKeyInvalid` unless ``key`` is a valid Fernet key.

    A valid key is 32 random bytes encoded as urlsafe base64 (44
    characters). The error message includes the generation command so
    the operator can fix their config without reading docs.
    """
    try:
        Fernet(key.encode())
    except (ValueError, TypeError) as exc:
        raise CredentialKeyInvalid(
            "Not a valid Fernet key (expected 32 bytes, urlsafe"
            f" base64-encoded). Generate one with: {_GENERATE_CMD}"
        ) from exc


def _fernet(key: str) -> Fernet:
    validate_credential_key(key)
    return Fernet(key.encode())


def encrypt_credential(plaintext: str, *, key: str) -> str:
    """Encrypt ``plaintext`` with ``key``; return the Fernet token as str.

    Raises :class:`CredentialKeyInvalid` if the key is malformed.
    """
    return _fernet(key).encrypt(plaintext.encode()).decode()


def decrypt_credential(token: str, *, key: str) -> str:
    """Decrypt a Fernet ``token`` produced by :func:`encrypt_credential`.

    Raises :class:`CredentialKeyInvalid` if the key is malformed, and
    :class:`CredentialDecryptError` if the token cannot be decrypted
    (wrong/rotated key, truncated or garbage token).
    """
    try:
        return _fernet(key).decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise CredentialDecryptError(
            "Could not decrypt stored credential — the token is invalid"
            " or FITNESS_CREDENTIAL_KEY has changed since it was"
            " encrypted. Saved credentials are unavailable until the"
            " user reconnects."
        ) from exc
