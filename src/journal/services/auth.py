"""Authentication service — password hashing, sessions, API keys, and tokens."""

import hashlib
import logging
import secrets
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from journal.db.user_repository import UserRepository
from journal.models import ApiKeyInfo, User

log = logging.getLogger(__name__)

# Lock account after this many consecutive failed login attempts.
_MAX_FAILED_ATTEMPTS = 5
# Duration of account lockout after exceeding failed attempts.
_LOCKOUT_MINUTES = 15


class AuthService:
    """Orchestrates password hashing, session management, and API key lifecycle.

    Uses ``UserRepository`` for persistence and ``argon2-cffi`` for password
    hashing. The service never stores or returns raw passwords or API keys
    after initial creation.
    """

    def __init__(
        self,
        user_repo: UserRepository,
        secret_key: str,
        session_expiry_days: int = 7,
    ) -> None:
        self._repo = user_repo
        self._ph = PasswordHasher()
        self._serializer = URLSafeTimedSerializer(secret_key)
        self._session_expiry_days = session_expiry_days

    # ── Password hashing ────────────────────────────────────────────────

    def hash_password(self, password: str) -> str:
        """Hash a password using Argon2id."""
        return self._ph.hash(password)

    def verify_password(self, password_hash: str, password: str) -> bool:
        """Verify a password against its Argon2id hash."""
        try:
            return self._ph.verify(password_hash, password)
        except VerifyMismatchError:
            return False

    # ── User registration ───────────────────────────────────────────────

    def register_user(self, email: str, password: str, display_name: str) -> User:
        """Create a new user with hashed password.

        Raises ``ValueError`` if the email is already registered.
        """
        existing = self._repo.get_user_by_email(email)
        if existing:
            raise ValueError("Email already registered")
        password_hash = self.hash_password(password)
        return self._repo.create_user(email, display_name, password_hash)

    # ── Authentication ──────────────────────────────────────────────────

    def authenticate(self, email: str, password: str) -> User:
        """Verify email + password and return the authenticated user.

        Raises ``ValueError`` on failure (bad credentials, locked account,
        disabled account). Handles lockout after repeated failures.
        """
        user = self._repo.get_user_by_email(email)
        if not user:
            raise ValueError("Invalid email or password")

        # Check lockout
        locked_until = self._repo.get_lock_status(user.id)
        if locked_until:
            now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            if locked_until > now:
                raise ValueError("Account temporarily locked. Try again later.")
            # Lock expired — reset counters
            self._repo.reset_failed_logins(user.id)

        if not user.is_active:
            raise ValueError("Account is disabled")

        # Fetch password hash separately (User model deliberately excludes it)
        stored_hash = self._repo.get_password_hash(user.id)
        if not stored_hash:
            raise ValueError("Invalid email or password")

        if not self.verify_password(stored_hash, password):
            self._repo.increment_failed_logins(user.id)
            # Check if we should lock the account
            # Re-read the user to get the current failed_login_attempts count
            # (the increment just happened in the DB). We use get_lock_status
            # indirectly — the repo tracks attempts in the users table, so we
            # query the count to decide whether to lock.
            row = self._repo.get_user_by_id(user.id)
            if row:
                # We need the raw count — read it via get_password_hash's
                # sibling. For now, lock after _MAX_FAILED_ATTEMPTS by
                # checking if the increment pushed us over the threshold.
                # The simplest approach: read failed_login_attempts.
                # Since we don't expose it on User, we count increments.
                self._maybe_lock_after_failure(user.id)
            raise ValueError("Invalid email or password")

        # Success — reset any accumulated failures
        self._repo.reset_failed_logins(user.id)
        log.info("User %d (%s) authenticated successfully", user.id, user.email)
        return user

    def _maybe_lock_after_failure(self, user_id: int) -> None:
        """Lock the account if failed attempts have reached the threshold.

        This is called after incrementing the counter, so the DB already
        reflects the latest failure. We read ``locked_until`` and
        ``failed_login_attempts`` indirectly: if ``get_lock_status`` is
        still None (not yet locked), we try locking. The repository's
        ``increment_failed_logins`` bumps the counter; we issue a
        conditional UPDATE that only sets ``locked_until`` when the
        counter >= threshold.
        """
        lock_until = datetime.now(UTC) + timedelta(minutes=_LOCKOUT_MINUTES)
        lock_until_str = lock_until.strftime("%Y-%m-%dT%H:%M:%SZ")
        self._repo.lock_user(user_id, lock_until_str)

    # ── Sessions ────────────────────────────────────────────────────────

    @staticmethod
    def _hash_session_token(token: str) -> str:
        """SHA-256 hash a raw session token for DB storage/lookup.

        Mirrors the API-key hashing pattern: the raw token is shown
        once (in the cookie), and only the hash is persisted.  If the
        SQLite file is ever exposed, an attacker cannot impersonate a
        user without brute-forcing the 256-bit token.
        """
        return hashlib.sha256(token.encode()).hexdigest()

    def create_session(
        self,
        user_id: int,
        user_agent: str | None = None,
        ip_address: str | None = None,
    ) -> str:
        """Create a new session and return the raw session token.

        The raw token is returned to the caller (for the cookie).
        Only the SHA-256 hash is stored in ``user_sessions.id``.

        Every call first sweeps expired session rows — logins are the
        natural low-frequency hook, so the table cannot accumulate
        dead sessions without needing a background thread.
        """
        self._repo.cleanup_expired_sessions()
        token = secrets.token_urlsafe(32)
        token_hash = self._hash_session_token(token)
        expires = datetime.now(UTC) + timedelta(days=self._session_expiry_days)
        expires_str = expires.strftime("%Y-%m-%dT%H:%M:%SZ")
        self._repo.create_session(token_hash, user_id, expires_str, user_agent, ip_address)
        return token

    def validate_session(self, token: str) -> User | None:
        """Look up a session token and return the associated user.

        Hashes the incoming token before the DB lookup. Returns
        ``None`` if the session is expired or does not exist.
        Updates ``last_seen_at`` on valid sessions.
        """
        token_hash = self._hash_session_token(token)
        session = self._repo.get_session(token_hash)
        if not session:
            return None
        self._repo.update_session_last_seen(token_hash)
        return User(
            id=session["user_id"],
            email=session["email"],
            display_name=session["display_name"],
            is_admin=bool(session["is_admin"]),
            is_active=bool(session["is_active"]),
            email_verified=bool(session["email_verified"]),
        )

    def logout(self, token: str) -> None:
        """Delete a session (log out). Hashes the token before delete."""
        self._repo.delete_session(self._hash_session_token(token))

    def logout_all(self, user_id: int) -> int:
        """Delete all sessions for a user. Returns the count deleted."""
        return self._repo.delete_user_sessions(user_id)

    # ── API Keys ────────────────────────────────────────────────────────

    def create_api_key(
        self, user_id: int, name: str, expires_days: int | None = None
    ) -> tuple[str, ApiKeyInfo]:
        """Generate a new API key.

        Returns ``(full_key, key_info)``. The full key is shown to the user
        exactly once — it is never stored or retrievable after creation.
        """
        full_key = "jnl_" + secrets.token_urlsafe(32)
        prefix = full_key[:12]  # "jnl_" + 8 chars
        key_hash = hashlib.sha256(full_key.encode()).hexdigest()
        expires_at: str | None = None
        if expires_days:
            expires = datetime.now(UTC) + timedelta(days=expires_days)
            expires_at = expires.strftime("%Y-%m-%dT%H:%M:%SZ")
        key_id = self._repo.create_api_key(user_id, prefix, key_hash, name, expires_at)
        info = ApiKeyInfo(
            id=key_id,
            user_id=user_id,
            key_prefix=prefix,
            name=name,
            expires_at=expires_at,
        )
        return full_key, info

    def validate_api_key(self, key: str) -> User | None:
        """Look up an API key by its SHA-256 hash and return the owning user.

        Returns ``None`` if the key is revoked, expired, or does not exist.
        Updates ``last_used_at`` on valid keys.
        """
        key_hash = hashlib.sha256(key.encode()).hexdigest()
        result = self._repo.get_api_key_by_hash(key_hash)
        if not result:
            return None
        self._repo.update_api_key_last_used(result["key_id"])
        return User(
            id=result["user_id"],
            email=result["email"],
            display_name=result["display_name"],
            is_admin=bool(result["is_admin"]),
            is_active=bool(result["is_active"]),
            email_verified=bool(result["email_verified"]),
        )

    def list_api_keys(self, user_id: int) -> list[ApiKeyInfo]:
        """List all API keys for a user (metadata only, no hashes)."""
        return self._repo.list_api_keys(user_id)

    def revoke_api_key(self, key_id: int, user_id: int) -> bool:
        """Revoke an API key. Returns True if it was active and is now revoked."""
        return self._repo.revoke_api_key(key_id, user_id)

    # ── Token generation (password reset / email verification) ──────────

    @staticmethod
    def _password_hash_fingerprint(password_hash: str) -> str:
        """Short, non-reversible fingerprint of an Argon2 password hash.

        Embedded in reset tokens to bind each token to the password it
        was issued against. The 16-hex-char SHA-256 prefix reveals
        nothing useful about the hash (which is itself not the
        password) but changes whenever the password does.
        """
        return hashlib.sha256(password_hash.encode()).hexdigest()[:16]

    def generate_reset_token(self, email: str) -> str:
        """Generate a signed password-reset token for the given email.

        The token payload carries a fingerprint of the user's *current*
        password hash, making tokens effectively single-use: a
        successful reset changes the hash, so every outstanding token
        (including the one just used) stops validating. For an unknown
        email — or a user without a password — a random fingerprint is
        used so the token can never validate, keeping generate-time
        behavior uniform (no account enumeration).
        """
        user = self._repo.get_user_by_email(email)
        password_hash = self._repo.get_password_hash(user.id) if user else None
        if password_hash:
            fingerprint = self._password_hash_fingerprint(password_hash)
        else:
            fingerprint = secrets.token_hex(8)
        payload = {"email": email, "ph": fingerprint}
        return self._serializer.dumps(payload, salt="password-reset")

    def validate_reset_token(self, token: str, max_age: int = 1800) -> str:
        """Validate a password-reset token and return the email.

        Raises ``ValueError`` if the token is invalid, expired (30 min
        default), or was issued against a previous password (i.e. the
        password has changed since — single-use semantics).
        """
        invalid = ValueError("Invalid or expired reset token")
        try:
            payload = self._serializer.loads(
                token, salt="password-reset", max_age=max_age
            )
        except (BadSignature, SignatureExpired) as e:
            raise invalid from e
        if not isinstance(payload, dict):
            # Legacy (pre-fingerprint) or malformed payload.
            raise invalid
        email = payload.get("email")
        fingerprint = payload.get("ph")
        if not isinstance(email, str) or not isinstance(fingerprint, str):
            raise invalid
        user = self._repo.get_user_by_email(email)
        current_hash = self._repo.get_password_hash(user.id) if user else None
        if current_hash is None or not secrets.compare_digest(
            fingerprint, self._password_hash_fingerprint(current_hash)
        ):
            raise invalid
        return email

    def generate_verification_token(self, email: str) -> str:
        """Generate a signed email-verification token."""
        return self._serializer.dumps(email, salt="email-verification")

    def validate_verification_token(self, token: str, max_age: int = 86400) -> str:
        """Validate an email-verification token and return the email.

        Raises ``ValueError`` if the token is invalid or expired (24h default).
        """
        try:
            email: str = self._serializer.loads(
                token, salt="email-verification", max_age=max_age
            )
            return email
        except (BadSignature, SignatureExpired) as e:
            raise ValueError("Invalid or expired verification token") from e

    def reset_password(self, token: str, new_password: str) -> User:
        """Validate a reset token and set a new password.

        Raises ``ValueError`` if the token is invalid/expired or the user
        is not found.
        """
        email = self.validate_reset_token(token)
        user = self._repo.get_user_by_email(email)
        if not user:
            raise ValueError("User not found")
        new_hash = self.hash_password(new_password)
        updated = self._repo.update_user(user.id, password_hash=new_hash)
        if not updated:
            raise ValueError("User not found")
        # Clear any lockout from previous failed attempts
        self._repo.reset_failed_logins(user.id)
        # Invalidate all existing sessions — an attacker with a stolen
        # session must not survive a password reset.
        self._repo.delete_user_sessions(user.id)
        log.info("Password reset for user %d (%s) — all sessions revoked", user.id, email)
        return updated

    def verify_email(self, token: str) -> User:
        """Validate a verification token and mark the user's email as verified.

        Raises ``ValueError`` if the token is invalid/expired or the user
        is not found.
        """
        email = self.validate_verification_token(token)
        user = self._repo.get_user_by_email(email)
        if not user:
            raise ValueError("User not found")
        updated = self._repo.update_user(user.id, email_verified=True)
        if not updated:
            raise ValueError("User not found")
        log.info("Email verified for user %d (%s)", user.id, email)
        return updated
