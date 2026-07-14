"""Garmin per-user auth routes (W2 of the fitness multi-user plan).

Owns the four Garmin connect-flow endpoints under ``/api/fitness/garmin/``:

- ``POST /api/fitness/garmin/connect`` — start a Garmin login (sync;
  may return ``mfa_required`` and a pending session token).
- ``POST /api/fitness/garmin/connect/mfa`` — complete an MFA-required
  Garmin login.
- ``POST /api/fitness/garmin/reconnect`` — re-login with saved
  (encrypted) credentials, no body (W5 of the strava-mothball /
  garmin-credentials plan). Same login path as connect, including the
  cooldown gates and the ``mfa_required`` pending-session shape.
- ``POST /api/fitness/garmin/disconnect`` — drop the user's Garmin tokens
  (including any saved credentials — the whole auth row goes).

W5 saved credentials: when ``config.fitness_credential_key`` is set, the
connect handler encrypts the password at first touch (Fernet, see
``services/fitness/credentials.py``) and on success persists
``extra_state_json.garmin_username`` + ``.enc_password`` alongside
``tokens_blob``. Only ciphertext ever reaches SQLite or the pending
store. When the key is unset, behavior is byte-for-byte the pre-W5 flow
— no credential keys are written.

These routes are direct upstream writes against Garmin's login API —
auth flow, not job creation — so they place by URL-resource root and
were carved out of ``api/fitness.py`` when that file outgrew the
~800-line size rule. Reads live in ``api/fitness.py``; job creation
(sync/backfill) lives in ``api/fitness_jobs.py``; the Strava
counterpart lives in ``api/fitness_strava.py``.

Auth is enforced by ``RequireAuthMiddleware``: every route below assumes
``request.user`` is an :class:`AuthenticatedUser`. The per-route
``get_authenticated_user`` call extracts the user_id for query scoping.
"""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING, Any

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectTooManyRequestsError,
)
from starlette.responses import JSONResponse

from journal.api._handler import JsonBody, handler
from journal.api._shared import _now_iso
from journal.auth import get_authenticated_user
from journal.models import FitnessAuthState
from journal.services.fitness.credentials import (
    CredentialDecryptError,
    CredentialKeyInvalid,
    decrypt_credential,
    encrypt_credential,
)
from journal.services.fitness.garmin_pending import (
    GarminCooldownTracker,
    GarminPendingStore,
    GarminUpstreamCooldown,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from mcp.server.fastmcp import FastMCP
    from starlette.requests import Request

    from journal.db.fitness_repository import FitnessRepository
    from journal.service_registry import ServicesDict

log = logging.getLogger(__name__)


# Substrings that mark a Garmin login failure as a rate-limit / Cloudflare
# bot-challenge rather than genuinely wrong credentials. Lower-cased match
# against both the exception text and the diagnostics garminconnect logs
# mid-login (see ``_capture_garmin_logs``). Kept deliberately broad: a false
# positive only changes a 401 into a "try again later" 429, never the reverse.
_RATE_LIMIT_SIGNALS = (
    "429",
    "rate limit",
    "rate-limit",
    "rate limiting",
    "too many",
    "cloudflare",
    "bot challenge",
    "captcha",
    "unexpected title",
    "strategies exhausted",
    "ip rate limited",
    "blocking this request",
)


def _looks_rate_limited(*texts: str) -> bool:
    """True when any text carries a rate-limit / bot-challenge signal."""
    blob = " ".join(t for t in texts if t).lower()
    return any(signal in blob for signal in _RATE_LIMIT_SIGNALS)


class _GarminLogCapture(logging.Handler):
    """Collect the WARNING+ lines garminconnect emits during a login attempt.

    garminconnect's login runs a 5-strategy chain; each strategy logs its 429 /
    Cloudflare / bot-challenge outcome as a warning, then the chain can still
    surface the *terminal* failure as a generic
    :class:`GarminConnectAuthenticationError` (e.g. when the portal strategy
    misreads a Cloudflare interstitial as ``INVALID_USERNAME_PASSWORD``). The
    terminal message alone is therefore indistinguishable from a real bad
    password — but the captured warnings are not. This handler lets the connect
    endpoint tell "wrong password" apart from "Garmin blocked this IP".
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        # Logging capture must never break the login it observes.
        with contextlib.suppress(Exception):
            self.messages.append(record.getMessage())

    @property
    def text(self) -> str:
        return " ".join(self.messages)


@contextlib.contextmanager
def _capture_garmin_logs() -> Iterator[_GarminLogCapture]:
    """Temporarily tee ``garminconnect``'s WARNING+ records into a buffer."""
    handler = _GarminLogCapture()
    gc_logger = logging.getLogger("garminconnect")
    gc_logger.addHandler(handler)
    try:
        yield handler
    finally:
        gc_logger.removeHandler(handler)


def _garmin_rate_limited_response(
    detail: str, *, retry_after_seconds: int = 300,
) -> JSONResponse:
    """Uniform 429 for an upstream rate-limit / Cloudflare bot challenge.

    Distinct from the *local* cooldown 429 (``reason="local_cooldown"``): this
    one means Garmin/Cloudflare refused the upstream login, typically because
    the IP has been hammered. The remedy is to stop retrying and wait — every
    further attempt re-arms the block — so the message says so.

    Returned both when an attempt is freshly blocked upstream and, pre-flight,
    when the global :class:`GarminUpstreamCooldown` is still hot from a recent
    block (``retry_after_seconds`` then carries the real time remaining).
    """
    return JSONResponse(
        {
            "error": (
                "Garmin is rate-limiting or bot-challenging login attempts "
                "from this server. Stop retrying and wait — each attempt "
                "re-arms the block. Try again in a few minutes (longer if it "
                "persists), ideally from an unflagged network."
            ),
            "reason": "upstream_rate_limited",
            "retry_after_seconds": retry_after_seconds,
            "detail": detail,
        },
        status_code=429,
    )


def _extract_upstream_user_id(client: Any, fallback: str) -> str | None:
    """Pull the stable upstream account identifier from a logged-in client.

    Garmin's ``get_user_profile`` returns a dict with ``displayName``
    (typically the username, stable across sessions). We use that as the
    upstream id (D8) to detect silent reconnects with a *different* Garmin
    account. ``fallback`` is the username the user typed — only used if
    the profile call returns an empty or malformed response, which the
    W2 spec treats as a post-MFA failure.
    """
    try:
        profile = client.get_user_profile()
    except Exception:  # noqa: BLE001  caller decides whether to surface
        raise
    if not isinstance(profile, dict):
        return None
    upstream = profile.get("displayName") or profile.get("userName")
    if isinstance(upstream, str) and upstream:
        return upstream
    return fallback or None


def _persist_garmin_auth(
    repo: FitnessRepository,
    *,
    user_id: int,
    tokens_blob: str,
    upstream_user_id: str,
    garmin_username: str | None = None,
    enc_password: str | None = None,
) -> None:
    """Upsert the user's Garmin auth row after a successful connect or MFA.

    Mirrors the operator-driven semantics of
    ``cli/fitness.cmd_fitness_reauth_garmin``: forces ``auth_status="ok"``,
    clears ``auth_broken_since``, stamps ``last_successful_login_at``, and
    preserves any unrelated ``extra_state`` keys (e.g. fields the fetch
    service writes during a sync).

    W5: when ``enc_password`` (Fernet ciphertext, never plaintext) is
    provided, it is stored together with ``garmin_username`` so unattended
    re-login / one-click reconnect can reuse them. When absent (credential
    key unset) no credential keys are written — and any previously saved
    ones are left untouched, matching the preserve-unrelated-keys contract.
    """
    existing = repo.get_auth_state(user_id=user_id, source="garmin")
    extra = dict(existing.extra_state) if existing else {}
    extra["tokens_blob"] = tokens_blob
    extra["upstream_user_id"] = upstream_user_id
    if enc_password:
        extra["enc_password"] = enc_password
        if garmin_username:
            extra["garmin_username"] = garmin_username
    repo.upsert_auth_state(
        FitnessAuthState(
            user_id=user_id,
            source="garmin",
            access_token=existing.access_token if existing else None,
            refresh_token=existing.refresh_token if existing else None,
            token_expires_at=existing.token_expires_at if existing else None,
            extra_state=extra,
            last_successful_login_at=_now_iso(),
            last_refresh_at=existing.last_refresh_at if existing else None,
            auth_status="ok",
            auth_broken_since=None,
            created_at=existing.created_at if existing else "",
        ),
    )


def _credential_key(services: ServicesDict) -> str:
    """The configured Fernet key, or ``""`` when the feature is off.

    Tolerates a missing/partial config service (test harnesses build
    partial dicts) — absent config means the feature is dark.
    """
    config = services.get("config")
    return getattr(config, "fitness_credential_key", "") or ""


def _cooldown_preflight(
    *,
    label: str,
    user_id: int,
    username: str,
    cooldown: GarminCooldownTracker,
    upstream_cooldown: GarminUpstreamCooldown,
) -> JSONResponse | None:
    """The two pre-flight refusal gates shared by connect and reconnect.

    Returns the refusal :class:`JSONResponse`, or ``None`` when the login
    attempt may proceed. Must run *before* any upstream contact — the
    whole point of both gates is to refuse without touching Garmin.
    """
    # Global upstream gate before anything else. A Cloudflare/IP block is
    # account-agnostic — it lives on the server's egress IP — so once any
    # recent attempt was blocked we refuse *every* login (any email)
    # until it ages out. Attempting again would only re-arm the block.
    upstream_remaining = upstream_cooldown.check()
    if upstream_remaining is not None:
        log.info(
            "%s — refused pre-flight, upstream cooldown hot (%ds left) "
            "for user_id=%d", label, int(upstream_remaining), user_id,
        )
        return _garmin_rate_limited_response(
            "A recent login attempt was blocked by Garmin's rate-limiter; "
            "the server is waiting before trying again.",
            retry_after_seconds=int(upstream_remaining),
        )

    # Per-email cool-down before any upstream call. Garmin's rate-limiter
    # keys on clientId+email; if we let the user keep retrying after a few
    # wrong passwords we deepen the upstream lockout. The local tracker
    # protects them by refusing inside the same window.
    retry_after = cooldown.check(username)
    if retry_after is not None:
        return JSONResponse(
            {
                "error": (
                    "Too many failed Garmin login attempts for that "
                    "account. Try again in a few minutes."
                ),
                "reason": "local_cooldown",
                "retry_after_seconds": int(retry_after),
            },
            status_code=429,
        )
    return None


def _login_and_persist(
    *,
    label: str,
    user_id: int,
    repo: FitnessRepository,
    pending: GarminPendingStore,
    cooldown: GarminCooldownTracker,
    upstream_cooldown: GarminUpstreamCooldown,
    client: Any,
    username: str,
    enc_password: str | None,
) -> JSONResponse:
    """Run a constructed Garmin client's login and persist the outcome.

    The single login path shared by ``connect`` and ``reconnect`` (W5):
    log-captured login, rate-limit/bot-challenge disambiguation, MFA
    pending-session issue, post-login profile fetch, D8 account-mismatch
    guard, token-blob capture, and the final auth-row upsert (including
    ``enc_password``/``garmin_username`` when credential capture is on).

    ``client`` is constructed by the caller — that keeps the plaintext
    password's lifetime confined to the caller's construction line, so
    each handler can ``del`` its reference immediately after.
    """
    # Capture garminconnect's mid-login diagnostics so we can tell a
    # genuine bad password apart from a Cloudflare/rate-limit block that
    # the strategy chain misreports as an auth error (the prod failure
    # mode that looked like "invalid credentials" but was really a 429).
    try:
        with _capture_garmin_logs() as gc_logs:
            result = client.login()
    except GarminConnectAuthenticationError as exc:
        cooldown.record_failure(username)
        if _looks_rate_limited(str(exc), gc_logs.text):
            upstream_cooldown.record_block()
            log.warning(
                "%s — login blocked by rate-limit/bot-challenge (surfaced "
                "as an auth error) for user_id=%d", label, user_id,
            )
            return _garmin_rate_limited_response(str(exc))
        log.info("%s — invalid credentials for user_id=%d", label, user_id)
        return JSONResponse(
            {
                "error": "Garmin rejected those credentials.",
                "reason": "invalid_credentials",
                "detail": str(exc),
            },
            status_code=401,
        )
    except GarminConnectTooManyRequestsError as exc:
        cooldown.record_failure(username)
        upstream_cooldown.record_block()
        log.warning("%s — Garmin returned 429 for user_id=%d", label, user_id)
        return _garmin_rate_limited_response(str(exc))
    except Exception as exc:  # noqa: BLE001
        # The terminal "all strategies exhausted" failure (Cloudflare 403
        # challenges, CAPTCHA, TLS-fingerprint blocks) lands here as a
        # GarminConnectConnectionError. Classify it as a rate-limit too.
        if _looks_rate_limited(str(exc), gc_logs.text):
            cooldown.record_failure(username)
            upstream_cooldown.record_block()
            log.warning(
                "%s — login blocked by rate-limit/bot-challenge "
                "for user_id=%d", label, user_id,
            )
            return _garmin_rate_limited_response(str(exc))
        log.exception("%s — unexpected error for user_id=%d", label, user_id)
        return JSONResponse(
            {
                "error": f"Garmin login failed: {exc}",
                "reason": "upstream_error",
            },
            status_code=502,
        )

    # Upstream contact succeeded (MFA challenge or straight login both
    # mean Garmin let us through, not a block), so clear the global gate.
    upstream_cooldown.reset()

    # ``Garmin.login()`` returns ``("needs_mfa", legacy)`` when MFA is
    # required (``return_on_mfa=True``) and ``(None, legacy)`` on
    # successful no-MFA login.
    mfa_status: Any = None
    legacy_token: Any = None
    if isinstance(result, tuple) and len(result) >= 1:
        mfa_status = result[0]
        if len(result) >= 2:
            legacy_token = result[1]

    if mfa_status == "needs_mfa":
        token, expires_at_iso = pending.issue(
            user_id=user_id, client=client, state_token=legacy_token,
            username=username, enc_password=enc_password,
        )
        log.info(
            "%s — MFA required for user_id=%d (pending session minted)",
            label, user_id,
        )
        return JSONResponse(
            {
                "mfa_required": True,
                "pending_session": token,
                "expires_at": expires_at_iso,
            },
            status_code=200,
        )

    # No-MFA success: capture upstream id + token blob, persist.
    try:
        upstream = _extract_upstream_user_id(client, username)
    except Exception as exc:  # noqa: BLE001
        log.exception(
            "%s — profile fetch failed for user_id=%d after no-MFA login",
            label, user_id,
        )
        return JSONResponse(
            {
                "error": (
                    "Garmin login succeeded but the post-login profile "
                    "fetch failed. Please retry."
                ),
                "reason": "post_login_profile_fetch_failed",
                "detail": str(exc),
            },
            status_code=502,
        )
    if not upstream:
        return JSONResponse(
            {
                "error": (
                    "Garmin login succeeded but no upstream identity "
                    "could be determined."
                ),
                "reason": "post_login_profile_fetch_failed",
            },
            status_code=502,
        )

    existing = repo.get_auth_state(user_id=user_id, source="garmin")
    if existing is not None:
        stored = (existing.extra_state or {}).get("upstream_user_id")
        if stored and stored != upstream:
            return JSONResponse(
                {
                    "error": (
                        "This Garmin account differs from the one "
                        "previously connected. Disconnect Garmin first, "
                        "then reconnect."
                    ),
                    "reason": "upstream_account_mismatch",
                    "stored_upstream_user_id": stored,
                    "incoming_upstream_user_id": upstream,
                },
                status_code=409,
            )

    try:
        blob = client.client.dumps()
    except Exception as exc:  # noqa: BLE001
        log.exception(
            "%s — token blob dump failed for user_id=%d", label, user_id,
        )
        return JSONResponse(
            {
                "error": f"Garmin login succeeded but token capture failed: {exc}",
                "reason": "token_capture_failed",
            },
            status_code=502,
        )

    _persist_garmin_auth(
        repo, user_id=user_id, tokens_blob=blob, upstream_user_id=upstream,
        garmin_username=username, enc_password=enc_password,
    )
    cooldown.reset(username)
    log.info("%s — connected user_id=%d (upstream=%s)", label, user_id, upstream)
    return JSONResponse(
        {"connected": True, "upstream_user_id": upstream}, status_code=200,
    )


def register_fitness_garmin_routes(
    mcp: FastMCP,
    services_getter: Callable[[], ServicesDict | None],
) -> None:
    """Register the Garmin connect / MFA / reconnect / disconnect routes."""

    def _garmin_pending(services: ServicesDict) -> GarminPendingStore:
        store = services.get("garmin_pending")
        if store is None:
            store = GarminPendingStore()
            services["garmin_pending"] = store
        return store

    def _garmin_cooldown(services: ServicesDict) -> GarminCooldownTracker:
        tracker = services.get("garmin_cooldown")
        if tracker is None:
            tracker = GarminCooldownTracker()
            services["garmin_cooldown"] = tracker
        return tracker

    def _garmin_upstream_cooldown(services: ServicesDict) -> GarminUpstreamCooldown:
        gate = services.get("garmin_upstream_cooldown")
        if gate is None:
            gate = GarminUpstreamCooldown()
            services["garmin_upstream_cooldown"] = gate
        return gate

    @mcp.custom_route(
        "/api/fitness/garmin/connect",
        methods=["POST"],
        name="api_fitness_garmin_connect",
    )
    @handler(services_getter, parse_json=JsonBody(invalid_error="Invalid JSON", require_dict=False))
    def garmin_connect(
        request: Request, services: ServicesDict, body: dict | object
    ) -> JSONResponse:
        user = get_authenticated_user(request)
        repo: FitnessRepository = services["fitness_repo"]
        pending = _garmin_pending(services)
        cooldown = _garmin_cooldown(services)
        upstream_cooldown = _garmin_upstream_cooldown(services)
        client_factory = services.get("garmin_client_factory") or Garmin

        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        if not username or not password:
            return JSONResponse(
                {"error": "username and password are required"},
                status_code=400,
            )

        refusal = _cooldown_preflight(
            label="POST /api/fitness/garmin/connect",
            user_id=user.user_id,
            username=username,
            cooldown=cooldown,
            upstream_cooldown=upstream_cooldown,
        )
        if refusal is not None:
            return refusal

        # W5: encrypt at first touch, while the plaintext is still in
        # scope. Key unset → enc_password stays None and no credential
        # material is ever captured (pre-W5 behavior, byte-for-byte).
        credential_key = _credential_key(services)
        enc_password = (
            encrypt_credential(password, key=credential_key)
            if credential_key else None
        )

        # Instantiation is cheap; we want the password living in handler
        # scope only as long as absolutely needed. (The whole body already
        # runs on a worker thread via the handler decorator.)
        client = client_factory(
            email=username, password=password, return_on_mfa=True,
        )
        # Drop the local password reference. The Garmin client holds its
        # own copy briefly; we'll let GC reclaim that once the call returns.
        del password

        return _login_and_persist(
            label="POST /api/fitness/garmin/connect",
            user_id=user.user_id,
            repo=repo,
            pending=pending,
            cooldown=cooldown,
            upstream_cooldown=upstream_cooldown,
            client=client,
            username=username,
            enc_password=enc_password,
        )

    @mcp.custom_route(
        "/api/fitness/garmin/reconnect",
        methods=["POST"],
        name="api_fitness_garmin_reconnect",
    )
    @handler(services_getter)
    def garmin_reconnect(
        request: Request, services: ServicesDict, body: None
    ) -> JSONResponse:
        """Re-login with the saved (encrypted) credentials — no body (W5).

        404 when no credentials are saved; 409 when they exist but cannot
        be decrypted (key rotated or unset). Otherwise runs the exact
        connect login path — cooldown gates included — and may return the
        same ``mfa_required`` + pending-session shape as connect.
        """
        user = get_authenticated_user(request)
        repo: FitnessRepository = services["fitness_repo"]
        pending = _garmin_pending(services)
        cooldown = _garmin_cooldown(services)
        upstream_cooldown = _garmin_upstream_cooldown(services)
        client_factory = services.get("garmin_client_factory") or Garmin

        auth = repo.get_auth_state(user_id=user.user_id, source="garmin")
        extra = (auth.extra_state or {}) if auth is not None else {}
        username = extra.get("garmin_username") or ""
        enc_password = extra.get("enc_password") or ""
        if not username or not enc_password:
            log.info(
                "POST /api/fitness/garmin/reconnect — no saved credentials "
                "for user_id=%d", user.user_id,
            )
            return JSONResponse(
                {
                    "error": (
                        "No saved Garmin credentials for this account. "
                        "Connect with username and password first."
                    ),
                    "reason": "no_saved_credentials",
                },
                status_code=404,
            )

        credential_key = _credential_key(services)
        try:
            if not credential_key:
                raise CredentialDecryptError(
                    "FITNESS_CREDENTIAL_KEY is not configured",
                )
            password = decrypt_credential(enc_password, key=credential_key)
        except (CredentialDecryptError, CredentialKeyInvalid) as exc:
            log.warning(
                "POST /api/fitness/garmin/reconnect — saved credentials "
                "undecryptable for user_id=%d (%s)", user.user_id, exc,
            )
            return JSONResponse(
                {
                    "error": (
                        "Saved Garmin credentials cannot be decrypted — "
                        "the credential key has changed or is unset. "
                        "Reconnect with username and password."
                    ),
                    "reason": "credentials_unavailable",
                },
                status_code=409,
            )

        refusal = _cooldown_preflight(
            label="POST /api/fitness/garmin/reconnect",
            user_id=user.user_id,
            username=username,
            cooldown=cooldown,
            upstream_cooldown=upstream_cooldown,
        )
        if refusal is not None:
            return refusal

        client = client_factory(
            email=username, password=password, return_on_mfa=True,
        )
        # Same plaintext hygiene as connect: drop the local reference the
        # moment the client owns it. The stored ciphertext is re-persisted
        # as-is on success — no re-encryption needed under the same key.
        del password

        return _login_and_persist(
            label="POST /api/fitness/garmin/reconnect",
            user_id=user.user_id,
            repo=repo,
            pending=pending,
            cooldown=cooldown,
            upstream_cooldown=upstream_cooldown,
            client=client,
            username=username,
            enc_password=enc_password,
        )

    @mcp.custom_route(
        "/api/fitness/garmin/connect/mfa",
        methods=["POST"],
        name="api_fitness_garmin_connect_mfa",
    )
    @handler(services_getter, parse_json=JsonBody(invalid_error="Invalid JSON", require_dict=False))
    def garmin_connect_mfa(
        request: Request, services: ServicesDict, body: dict | object
    ) -> JSONResponse:
        user = get_authenticated_user(request)
        repo: FitnessRepository = services["fitness_repo"]
        pending = _garmin_pending(services)
        cooldown = _garmin_cooldown(services)

        token = body.get("pending_session") or ""
        code = (body.get("code") or "").strip()
        if not token or not code:
            return JSONResponse(
                {"error": "pending_session and code are required"},
                status_code=400,
            )

        # Peek first so we can enforce the user-binding check without
        # consuming the entry on a 403 — the legitimate user can still
        # complete their flow.
        entry = pending.peek(token)
        if entry is None:
            return JSONResponse(
                {
                    "error": (
                        "Pending session expired or unknown. Repeat the "
                        "connect step."
                    ),
                    "reason": "expired_pending_session",
                },
                status_code=410,
            )
        if entry.user_id != user.user_id:
            log.warning(
                "POST /api/fitness/garmin/connect/mfa — cross-user "
                "pending-session attempt: token issued to user_id=%d, "
                "consumed by user_id=%d (rejected)",
                entry.user_id, user.user_id,
            )
            return JSONResponse(
                {
                    "error": "Pending session does not belong to this user.",
                    "reason": "cross_user_pending_session",
                },
                status_code=403,
            )

        client = entry.client
        try:
            client.resume_login(entry.state_token, code)
        except GarminConnectAuthenticationError as exc:
            log.info(
                "POST /api/fitness/garmin/connect/mfa — bad MFA code "
                "for user_id=%d", user.user_id,
            )
            return JSONResponse(
                {
                    "error": "Garmin rejected the MFA code.",
                    "reason": "invalid_mfa_code",
                    "detail": str(exc),
                },
                status_code=401,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception(
                "POST /api/fitness/garmin/connect/mfa — unexpected error "
                "for user_id=%d", user.user_id,
            )
            return JSONResponse(
                {
                    "error": f"Garmin MFA resume failed: {exc}",
                    "reason": "upstream_error",
                },
                status_code=502,
            )

        # Resume_login succeeded. Now fetch the upstream profile —
        # python-garminconnect issues #312/#337 record this call as
        # intermittently flaky right after a fresh MFA, distinct from
        # "wrong code" so the UI can ask for a retry rather than blame
        # the user's credentials.
        try:
            upstream = _extract_upstream_user_id(client, "")
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "POST /api/fitness/garmin/connect/mfa — post-MFA profile "
                "fetch failed for user_id=%d (%s)", user.user_id, exc,
            )
            return JSONResponse(
                {
                    "error": (
                        "Garmin accepted the MFA code but the post-login "
                        "profile fetch failed. Please retry."
                    ),
                    "reason": "post_mfa_profile_fetch_failed",
                    "detail": str(exc),
                },
                status_code=502,
            )
        if not upstream:
            return JSONResponse(
                {
                    "error": (
                        "Garmin accepted the MFA code but no upstream "
                        "identity could be determined. Please retry."
                    ),
                    "reason": "post_mfa_profile_fetch_failed",
                },
                status_code=502,
            )

        # D8 mismatch check on the MFA path too.
        existing = repo.get_auth_state(user_id=user.user_id, source="garmin")
        if existing is not None:
            stored = (existing.extra_state or {}).get("upstream_user_id")
            if stored and stored != upstream:
                # Consume the pending entry — the MFA was valid; the user
                # just authenticated as a different upstream account, so
                # the in-flight challenge has served its purpose.
                pending.consume(token)
                return JSONResponse(
                    {
                        "error": (
                            "This Garmin account differs from the one "
                            "previously connected. Disconnect Garmin first, "
                            "then reconnect."
                        ),
                        "reason": "upstream_account_mismatch",
                        "stored_upstream_user_id": stored,
                        "incoming_upstream_user_id": upstream,
                    },
                    status_code=409,
                )

        try:
            blob = client.client.dumps()
        except Exception as exc:  # noqa: BLE001
            log.exception(
                "POST /api/fitness/garmin/connect/mfa — token blob dump "
                "failed for user_id=%d", user.user_id,
            )
            return JSONResponse(
                {
                    "error": f"Garmin login succeeded but token capture failed: {exc}",
                    "reason": "token_capture_failed",
                },
                status_code=502,
            )

        _persist_garmin_auth(
            repo, user_id=user.user_id, tokens_blob=blob, upstream_user_id=upstream,
            # W5: the pending session carried the (ciphertext) credentials
            # from the connect/reconnect attempt; persist them only now
            # that the MFA actually completed. Both are absent in
            # key-unset mode, making this a no-op there.
            garmin_username=entry.username or None,
            enc_password=entry.enc_password,
        )
        # Now consume the pending entry — login is committed.
        pending.consume(token)
        # Reset the per-email cool-down — the user just succeeded.
        # We don't have the original email here, but the cooldown is keyed
        # by what the user typed; if a stale failure-counter hangs around
        # it ages out within the window. Acceptable trade for not having
        # to plumb the email through the pending entry.
        log.info(
            "POST /api/fitness/garmin/connect/mfa — connected user_id=%d "
            "(upstream=%s)", user.user_id, upstream,
        )
        # silence unused-variable complaints from optional cooldown reset
        del cooldown
        return JSONResponse(
            {"connected": True, "upstream_user_id": upstream}, status_code=200,
        )

    @mcp.custom_route(
        "/api/fitness/garmin/disconnect",
        methods=["POST"],
        name="api_fitness_garmin_disconnect",
    )
    @handler(services_getter)
    def garmin_disconnect(
        request: Request, services: ServicesDict, body: None
    ) -> JSONResponse:
        user = get_authenticated_user(request)
        repo: FitnessRepository = services["fitness_repo"]
        deleted = repo.delete_auth_state(user_id=user.user_id, source="garmin")
        log.info(
            "POST /api/fitness/garmin/disconnect — user_id=%d deleted=%s",
            user.user_id, deleted,
        )
        return JSONResponse({"disconnected": bool(deleted)}, status_code=200)
