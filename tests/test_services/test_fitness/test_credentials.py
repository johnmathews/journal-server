"""Tests for the Fernet credential-encryption helpers (W4).

Foundation for saved Garmin credentials: round-trip integrity, typed
failure on wrong/rotated keys, and key-format validation. Nothing in
the app consumes this module yet — these tests pin the contract that
W5/W6 will build on.
"""

import pytest
from cryptography.fernet import Fernet

from journal.services.fitness.credentials import (
    CredentialDecryptError,
    CredentialKeyInvalid,
    decrypt_credential,
    encrypt_credential,
    validate_credential_key,
)


def _new_key() -> str:
    return Fernet.generate_key().decode()


class TestRoundTrip:
    def test_encrypt_then_decrypt_returns_plaintext(self) -> None:
        key = _new_key()
        token = encrypt_credential("s3cret-garmin-pw", key=key)
        assert decrypt_credential(token, key=key) == "s3cret-garmin-pw"

    def test_ciphertext_differs_from_plaintext(self) -> None:
        key = _new_key()
        token = encrypt_credential("s3cret-garmin-pw", key=key)
        assert token != "s3cret-garmin-pw"
        assert "s3cret-garmin-pw" not in token

    def test_round_trip_preserves_unicode(self) -> None:
        key = _new_key()
        plaintext = "pässwörd-日本語-🔑"
        assert decrypt_credential(
            encrypt_credential(plaintext, key=key), key=key,
        ) == plaintext

    def test_encrypt_returns_str(self) -> None:
        token = encrypt_credential("pw", key=_new_key())
        assert isinstance(token, str)


class TestDecryptFailures:
    def test_wrong_key_raises_decrypt_error(self) -> None:
        """A rotated/lost key must degrade to a catchable error —
        never crash a sync."""
        token = encrypt_credential("pw", key=_new_key())
        with pytest.raises(CredentialDecryptError):
            decrypt_credential(token, key=_new_key())

    def test_garbage_token_raises_decrypt_error(self) -> None:
        with pytest.raises(CredentialDecryptError):
            decrypt_credential("not-a-fernet-token", key=_new_key())

    def test_truncated_token_raises_decrypt_error(self) -> None:
        key = _new_key()
        token = encrypt_credential("pw", key=key)
        with pytest.raises(CredentialDecryptError):
            decrypt_credential(token[: len(token) // 2], key=key)

    def test_empty_token_raises_decrypt_error(self) -> None:
        with pytest.raises(CredentialDecryptError):
            decrypt_credential("", key=_new_key())

    def test_decrypt_error_is_catchable_exception(self) -> None:
        assert issubclass(CredentialDecryptError, Exception)


class TestValidateCredentialKey:
    def test_generated_key_passes(self) -> None:
        validate_credential_key(_new_key())  # must not raise

    @pytest.mark.parametrize(
        "bad_key",
        [
            "",
            "short",
            "not base64 at all!!!",
            "x" * 43,  # right-ish length, not valid urlsafe-b64 of 32 bytes
            "dG9vLXNob3J0",  # valid base64 but decodes to < 32 bytes
        ],
    )
    def test_malformed_keys_raise(self, bad_key: str) -> None:
        with pytest.raises(CredentialKeyInvalid):
            validate_credential_key(bad_key)

    def test_error_message_is_actionable(self) -> None:
        with pytest.raises(CredentialKeyInvalid, match="Fernet"):
            validate_credential_key("nope")

    def test_encrypt_with_invalid_key_raises_key_invalid(self) -> None:
        with pytest.raises(CredentialKeyInvalid):
            encrypt_credential("pw", key="nope")

    def test_decrypt_with_invalid_key_raises_key_invalid(self) -> None:
        """A malformed key is a config bug, not a data problem —
        distinct exception from CredentialDecryptError."""
        with pytest.raises(CredentialKeyInvalid):
            decrypt_credential("whatever", key="nope")
